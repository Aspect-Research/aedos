"""Tests for the Layer 1 extraction module."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from src.aedos_v0_15.layer1_extraction.extractor import (
    EXTRACTION_TOOL,
    Claim,
    ExtractionContext,
    Extractor,
)
from src.aedos_v0_15.layer1_extraction.triage import TriageDecision
from src.aedos_v0_15.layer1_extraction.temporal import BEFORE_PRESENT
from src.aedos_v0_15.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockTransport:
    """Local mock transport — returns pre-configured extract_with_tool responses."""

    def __init__(self, claims: list[dict]):
        self._claims = claims
        self.calls: list[dict] = []

    def extract_with_tool(self, system, user_message, tool, model="", purpose=None):
        self.calls.append({"tool": tool["name"], "user_message": user_message})
        return {"claims": self._claims}

    def chat(self, system, messages, model="", purpose=None):
        return ""


def _raw_claim(**kwargs) -> dict[str, Any]:
    defaults = {
        "subject": "Asa",
        "predicate": "graduated_from",
        "object": "Williams College",
        "polarity": 1,
        "source_text": "Asa graduated from Williams College",
        "verb_tense": "present",
        "participants": [],
        "event_type": None,
        "reified_event_id": None,
        "valid_from": None,
        "valid_until": None,
        "valid_during_ref": None,
    }
    return {**defaults, **kwargs}


def _make_extractor(claims: list[dict]) -> tuple[Extractor, MockTransport]:
    transport = MockTransport(claims)
    client = LLMClient(_transport=transport)
    return Extractor(client), transport


def _default_context(**kwargs) -> ExtractionContext:
    defaults = dict(asserting_party="user_test", context_type="chat_user")
    return ExtractionContext(**{**defaults, **kwargs})


# ---------------------------------------------------------------------------
# TestClaimDataclass
# ---------------------------------------------------------------------------

class TestClaimDataclass:
    def test_required_fields_exist(self):
        c = Claim(
            claim_id="id1",
            subject="Asa",
            predicate="graduated_from",
            object="Williams",
            polarity=1,
            source_text="Asa graduated from Williams",
            asserting_party="user_test",
            triage_decision=TriageDecision.VERIFY,
        )
        assert c.claim_id == "id1"
        assert c.subject == "Asa"
        assert c.predicate == "graduated_from"
        assert c.object == "Williams"
        assert c.polarity == 1

    def test_optional_fields_default_to_none(self):
        c = Claim(
            claim_id="id1",
            subject="Asa",
            predicate="p",
            object="o",
            polarity=1,
            source_text="src",
            asserting_party="user_test",
            triage_decision=TriageDecision.VERIFY,
        )
        assert c.valid_from is None
        assert c.valid_until is None
        assert c.valid_during_ref is None
        assert c.reified_event_id is None

    def test_polarity_is_integer(self):
        c = Claim(
            claim_id="id",
            subject="s",
            predicate="p",
            object="o",
            polarity=0,
            source_text="t",
            asserting_party="user_test",
            triage_decision=TriageDecision.INERT_PROSE,
        )
        assert isinstance(c.polarity, int)
        assert c.polarity == 0

    def test_triage_decision_field(self):
        c = Claim(
            claim_id="id",
            subject="s",
            predicate="p",
            object="o",
            polarity=1,
            source_text="t",
            asserting_party="user_test",
            triage_decision=TriageDecision.INERT_PROSE,
        )
        assert c.triage_decision == TriageDecision.INERT_PROSE


class TestExtractionContextDataclass:
    def test_required_fields(self):
        ctx = ExtractionContext(asserting_party="user_abc", context_type="chat_user")
        assert ctx.asserting_party == "user_abc"
        assert ctx.context_type == "chat_user"

    def test_optional_fields_default(self):
        ctx = ExtractionContext(asserting_party="user_abc", context_type="chat_user")
        assert ctx.turn_id is None
        assert ctx.prior_conversation is None
        assert ctx.document_id is None

    def test_document_context(self):
        ctx = ExtractionContext(
            asserting_party="doc:paper1",
            context_type="document",
            document_id="paper1",
        )
        assert ctx.document_id == "paper1"


# ---------------------------------------------------------------------------
# TestExtractorRoundtrip
# ---------------------------------------------------------------------------

class TestExtractorRoundtrip:
    def test_basic_extraction_returns_claim(self):
        raw = _raw_claim()
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Asa graduated from Williams College", _default_context())
        assert len(claims) == 1
        assert claims[0].subject == "Asa"

    def test_claim_id_is_uuid(self):
        extractor, _ = _make_extractor([_raw_claim()])
        claims = extractor.extract("Asa graduated from Williams College", _default_context())
        uuid.UUID(claims[0].claim_id)  # raises if not valid UUID

    def test_predicate_is_normalized(self):
        raw = _raw_claim(predicate="was employed by")
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Asa was employed by Google", _default_context())
        assert claims[0].predicate == "employed_by"

    def test_multiple_claims_returned(self):
        raws = [
            _raw_claim(subject="Asa", predicate="graduated_from", object="Williams College",
                       source_text="Asa graduated from Williams College"),
            _raw_claim(subject="Asa", predicate="employed_by", object="Google",
                       source_text="Asa works at Google"),
        ]
        extractor, _ = _make_extractor(raws)
        claims = extractor.extract("Asa graduated from Williams College. Asa works at Google.",
                                   _default_context())
        assert len(claims) == 2

    def test_asserting_party_set_from_context(self):
        extractor, _ = _make_extractor([_raw_claim()])
        ctx = _default_context(asserting_party="user_alice")
        claims = extractor.extract("Asa graduated from Williams College", ctx)
        assert claims[0].asserting_party == "user_alice"

    def test_triage_decision_set(self):
        extractor, _ = _make_extractor([_raw_claim()])
        claims = extractor.extract("Asa graduated from Williams College", _default_context())
        assert isinstance(claims[0].triage_decision, TriageDecision)


# ---------------------------------------------------------------------------
# TestFirstPersonCanonicalization
# ---------------------------------------------------------------------------

class TestFirstPersonCanonicalization:
    def test_I_replaced_with_asserting_party(self):
        raw = _raw_claim(subject="I", source_text="I graduated from Williams College")
        extractor, _ = _make_extractor([raw])
        ctx = _default_context(asserting_party="user_test")
        claims = extractor.extract("I graduated from Williams College", ctx)
        assert claims[0].subject == "user_test"

    def test_me_replaced(self):
        raw = _raw_claim(subject="me", source_text="Williams College admitted me")
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Williams College admitted me", _default_context())
        assert claims[0].subject == "user_test"

    def test_my_replaced(self):
        raw = _raw_claim(subject="my", source_text="my employer is Google")
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("my employer is Google", _default_context())
        assert claims[0].subject == "user_test"

    def test_I_in_quoted_sentence_canonicalized(self):
        # "I" in any extracted text resolves to asserting party (see ambiguities doc)
        raw = _raw_claim(
            subject="I",
            source_text='He said "I am the president"',
        )
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract('He said "I am the president"', _default_context())
        assert claims[0].subject == "user_test"

    def test_named_subject_not_replaced(self):
        raw = _raw_claim(subject="Asa", source_text="Asa graduated from Williams College")
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Asa graduated from Williams College", _default_context())
        assert claims[0].subject == "Asa"


# ---------------------------------------------------------------------------
# TestFutureTenseRejection
# ---------------------------------------------------------------------------

class TestFutureTenseRejection:
    def test_future_tense_claim_filtered(self):
        raw = _raw_claim(
            verb_tense="future",
            subject="Asa",
            object="President",
            source_text="Asa will be President",
        )
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Asa will be President", _default_context())
        assert len(claims) == 0

    def test_present_tense_not_filtered(self):
        raw = _raw_claim(verb_tense="present")
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Asa graduated from Williams College", _default_context())
        assert len(claims) == 1

    def test_past_tense_not_filtered(self):
        raw = _raw_claim(verb_tense="past")
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Asa graduated from Williams College", _default_context())
        assert len(claims) == 1

    def test_future_claim_among_valid_claims_only_future_dropped(self):
        raws = [
            _raw_claim(verb_tense="present"),
            _raw_claim(verb_tense="future", subject="Asa", object="Mayor",
                       source_text="Asa will be Mayor"),
        ]
        extractor, _ = _make_extractor(raws)
        claims = extractor.extract("Asa graduated from Williams College. Asa will be Mayor.",
                                   _default_context())
        assert len(claims) == 1
        assert claims[0].predicate == "graduated_from"


# ---------------------------------------------------------------------------
# TestSourceTextDiscipline
# ---------------------------------------------------------------------------

class TestSourceTextDiscipline:
    def test_source_text_preserved_verbatim(self):
        text = "Asa graduated from Williams College in 2020"
        raw = _raw_claim(source_text="Asa graduated from Williams College in 2020")
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract(text, _default_context())
        assert claims[0].source_text == "Asa graduated from Williams College in 2020"

    def test_source_text_is_substring_of_input(self):
        text = "Asa graduated from Williams College in 2020"
        span = "Asa graduated from Williams College"
        raw = _raw_claim(source_text=span)
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract(text, _default_context())
        assert claims[0].source_text in text

    def test_source_text_not_paraphrase(self):
        # The source_text is what the mock returns — extractor should not alter it
        raw = _raw_claim(source_text="Asa graduated from Williams College")
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Asa graduated from Williams College", _default_context())
        # Not a rewording
        assert "Williams College" in claims[0].source_text


# ---------------------------------------------------------------------------
# TestHardClaimDiscipline
# ---------------------------------------------------------------------------

class TestHardClaimDiscipline:
    def test_entity_not_in_text_is_dropped(self):
        # Bob is not in text — extractor should drop Bob's claim
        raws = [
            _raw_claim(subject="Asa", object="Google", source_text="Asa works at Google"),
            _raw_claim(subject="Bob", predicate="employed_by", object="Microsoft",
                       source_text="Bob works at Microsoft"),
        ]
        extractor, _ = _make_extractor(raws)
        claims = extractor.extract("Asa works at Google", _default_context())
        subjects = {c.subject for c in claims}
        assert "Asa" in subjects
        assert "Bob" not in subjects

    def test_entity_in_text_is_kept(self):
        raw = _raw_claim(subject="Williams College", object="Massachusetts",
                         source_text="Williams College is in Massachusetts")
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Williams College is in Massachusetts", _default_context())
        assert len(claims) == 1

    def test_object_in_text_keeps_claim(self):
        # Subject not in text, but object is — heuristic keeps it
        raw = _raw_claim(subject="The college", object="Williams College",
                         source_text="Williams College is great")
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Williams College is great", _default_context())
        assert len(claims) == 1


# ---------------------------------------------------------------------------
# TestContrastiveCorrections
# ---------------------------------------------------------------------------

class TestContrastiveCorrections:
    def test_contrastive_extracts_both_polarities(self):
        # "Actually Paris, not London" → Paris polarity=1, London polarity=0
        raws = [
            _raw_claim(subject="capital", object="Paris", polarity=1,
                       source_text="Actually Paris, not London"),
            _raw_claim(subject="capital", object="London", polarity=0,
                       source_text="Actually Paris, not London"),
        ]
        extractor, _ = _make_extractor(raws)
        claims = extractor.extract("Actually Paris, not London", _default_context())
        assert len(claims) == 2
        polarities = {c.object: c.polarity for c in claims}
        assert polarities.get("Paris") == 1
        assert polarities.get("London") == 0

    def test_negated_claim_has_polarity_zero(self):
        raw = _raw_claim(polarity=0, object="London")
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Actually Paris, not London", _default_context())
        assert claims[0].polarity == 0


# ---------------------------------------------------------------------------
# TestExtractionToolSchema
# ---------------------------------------------------------------------------

class TestExtractionToolSchema:
    def test_tool_name(self):
        assert EXTRACTION_TOOL["name"] == "extract_claims"

    def test_tool_has_input_schema(self):
        assert "input_schema" in EXTRACTION_TOOL

    def test_claims_array_in_schema(self):
        schema = EXTRACTION_TOOL["input_schema"]
        assert schema["properties"]["claims"]["type"] == "array"

    def test_required_fields_in_item_schema(self):
        item_schema = EXTRACTION_TOOL["input_schema"]["properties"]["claims"]["items"]
        required = item_schema["required"]
        for field in ("subject", "predicate", "object", "polarity", "source_text", "verb_tense"):
            assert field in required
