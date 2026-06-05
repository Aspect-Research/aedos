"""Tests for the Layer 1 extraction module."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from aedos.layer1_extraction.extractor import (
    EXTRACTION_TOOL,
    Claim,
    ExtractionContext,
    Extractor,
)
from aedos.layer1_extraction.triage import AbstentionReason, TriageDecision
from aedos.layer1_extraction.temporal import BEFORE_PRESENT
from aedos.llm.client import LLMClient


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
        "valid_from_ref": None,
        "valid_until_ref": None,
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
        # v0.16.1 WS8 Stage 1: the event-relative bound refs default None.
        assert c.valid_from_ref is None
        assert c.valid_until_ref is None
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

class TestVerbShapePromptRules:
    """Phase H Cluster 3 (2026-05-26) step 3: the v5 prompt carries rules
    12-14 for verb-shape variants on seeded predicates:
      - Rule 12: 'joined / was hired by / started at' → employed_by with valid_from
      - Rule 13: 'left / quit / departed / resigned from' → employed_by with valid_until
      - Rule 14: 'ended / began' on state-bearing subjects → status with valid_*

    These tests pin the rule wording (so a future prompt edit cannot silently
    drop them) and verify the post-LLM pipeline produces the expected Claim
    when the LLM returns the rule-prescribed shape. End-to-end LLM-following
    behavior is validated against the live derivation_corpus in Step 5.
    """

    def test_prompt_carries_rule_12_employment_start(self):
        from aedos.layer1_extraction.extractor import _SYSTEM_PROMPT
        assert "12. EMPLOYMENT EVENTS" in _SYSTEM_PROMPT
        assert "joined Google" in _SYSTEM_PROMPT
        assert "employed_by" in _SYSTEM_PROMPT

    def test_prompt_carries_rule_13_employment_termination(self):
        from aedos.layer1_extraction.extractor import _SYSTEM_PROMPT
        assert "EMPLOYMENT TERMINATION" in _SYSTEM_PROMPT
        assert "left Google" in _SYSTEM_PROMPT
        assert "valid_until" in _SYSTEM_PROMPT

    def test_prompt_carries_rule_14_state_changes(self):
        from aedos.layer1_extraction.extractor import _SYSTEM_PROMPT
        assert "STATE CHANGES" in _SYSTEM_PROMPT
        assert "STATE-BEARING" in _SYSTEM_PROMPT
        assert "status" in _SYSTEM_PROMPT
        assert "The project ended in 2024" in _SYSTEM_PROMPT

    def test_prompt_carries_rule_26_string_property_count(self):
        # v0.16.4: shape "X has N vowels/letters/…" as a <measure>_count predicate
        # (bare word subject, bare integer object) so it routes to the python tier.
        from aedos.layer1_extraction.extractor import _SYSTEM_PROMPT
        assert "STRING-PROPERTY COUNT" in _SYSTEM_PROMPT
        assert "vowel_count" in _SYSTEM_PROMPT
        assert "superstrawberry" in _SYSTEM_PROMPT
        # It must steer the subject to the bare word and the object to the integer.
        assert "NOT 'the word superstrawberry'" in _SYSTEM_PROMPT
        assert "NOT '4 vowels'" in _SYSTEM_PROMPT

    def test_prompt_carries_rule_8_event_non_trigger(self):
        # Rule 14 must not over-apply to historical events — its non-trigger
        # explicitly cedes 'the war ended in 1945' to Rule 8.
        from aedos.layer1_extraction.extractor import _SYSTEM_PROMPT
        assert "the war ended in 1945" in _SYSTEM_PROMPT
        assert "Rule 8 applies" in _SYSTEM_PROMPT

    def test_pipeline_handles_employment_start_shape(self):
        # When the LLM follows Rule 12, the post-LLM pipeline produces a
        # Claim with predicate=employed_by and the year in valid_from.
        raw = _raw_claim(
            subject="Asa", predicate="employed_by", object="Google",
            valid_from="2020", source_text="Asa joined Google in 2020",
        )
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Asa joined Google in 2020", _default_context())
        assert len(claims) == 1
        c = claims[0]
        assert c.predicate == "employed_by"
        assert c.object == "Google"
        assert c.valid_from == "2020"
        assert c.valid_until is None

    def test_pipeline_handles_employment_termination_shape(self):
        raw = _raw_claim(
            subject="Asa", predicate="employed_by", object="Microsoft",
            valid_until="2019", source_text="Asa quit Microsoft in 2019",
        )
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Asa quit Microsoft in 2019", _default_context())
        assert len(claims) == 1
        c = claims[0]
        assert c.predicate == "employed_by"
        assert c.object == "Microsoft"
        assert c.valid_until == "2019"
        assert c.valid_from is None

    def test_pipeline_handles_status_change_shape(self):
        raw = _raw_claim(
            subject="The project", predicate="status", object="ended",
            valid_until="2024", source_text="The project ended in 2024",
        )
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("The project ended in 2024", _default_context())
        assert len(claims) == 1
        c = claims[0]
        assert c.predicate == "status"
        assert c.object == "ended"
        assert c.valid_until == "2024"


class TestRule25IntervalEndpointPromptRules:
    """v0.16 WS6 T1: the prompt carries Rule 25 (interval endpoints emit a
    SEPARATE date-in-object *_started/_ended claim) and Rules 12/13/14 are
    amended to cross-reference it. These pins prevent a future prompt edit
    from silently dropping the endpoint-emission instruction."""

    def test_prompt_carries_rule_25_interval_endpoints(self):
        from aedos.layer1_extraction.extractor import _SYSTEM_PROMPT
        assert "25. INTERVAL ENDPOINTS" in _SYSTEM_PROMPT
        # The endpoint predicate naming convention.
        assert "_started" in _SYSTEM_PROMPT
        assert "_ended" in _SYSTEM_PROMPT
        assert "employment_started" in _SYSTEM_PROMPT
        assert "employment_ended" in _SYSTEM_PROMPT

    def test_rule_25_carries_do_not_non_trigger(self):
        # D45 discipline: Rule 25 must carry explicit non-triggering conditions.
        # The load-bearing one: a bare relation with no date emits NO endpoint
        # claim, and the endpoint claim's date is its object (not duplicated
        # into valid_from/valid_until).
        from aedos.layer1_extraction.extractor import _SYSTEM_PROMPT
        assert "DO NOT apply Rule 25 when" in _SYSTEM_PROMPT
        assert "No date/year is present" in _SYSTEM_PROMPT

    def test_rules_12_13_cross_reference_rule_25(self):
        # The interval-bearing employment rules (12/13) each instruct the LLM to
        # ALSO emit the Rule 25 endpoint claim. (v0.16.1 WS4 dropped the dead
        # status_started/status_ended seed rows, so Rule 14 no longer carries a
        # Rule 25 status endpoint cross-reference and the prompt no longer
        # elicits status_started/status_ended.)
        from aedos.layer1_extraction.extractor import _SYSTEM_PROMPT
        assert _SYSTEM_PROMPT.count("per Rule 25") >= 2
        assert "status_started" not in _SYSTEM_PROMPT
        assert "status_ended" not in _SYSTEM_PROMPT


class TestRule25EndpointClaimPipeline:
    """v0.16 WS6 T1: a two-claim LLM output (employed_by + employment_started)
    round-trips through _build_claim with BOTH claims fully shaped (non-empty
    subject/predicate/object) and abstention_reason None — so the endpoint
    claim survives the WS4 verify-every-claim drops and reaches the walker. No
    live API: the MockTransport returns the rule-prescribed two-claim shape."""

    def test_two_claim_output_both_shaped_and_survive(self):
        base = _raw_claim(
            subject="Asa", predicate="employed_by", object="Google",
            valid_from="2020", source_text="Asa joined Google in 2020",
        )
        endpoint = _raw_claim(
            subject="Asa", predicate="employment_started", object="2020",
            source_text="Asa joined Google in 2020",
        )
        extractor, _ = _make_extractor([base, endpoint])
        claims = extractor.extract("Asa joined Google in 2020", _default_context())
        assert len(claims) == 2
        by_pred = {c.predicate: c for c in claims}
        assert set(by_pred) == {"employed_by", "employment_started"}

        # The base relation keeps its scope.
        rel = by_pred["employed_by"]
        assert rel.object == "Google"
        assert rel.valid_from == "2020"
        assert rel.abstention_reason is None

        # The endpoint claim is fully shaped: the year is the OBJECT (Rule 23
        # date-in-object pattern), subject/predicate/object all non-empty, and
        # it carries NO abstention_reason — so the walker routes it, not a drop.
        ep = by_pred["employment_started"]
        assert ep.subject == "Asa"
        assert ep.predicate == "employment_started"
        assert ep.object == "2020"
        assert ep.abstention_reason is None
        # Rule 25: the endpoint claim does not repeat the scope on itself.
        assert ep.valid_from is None
        assert ep.valid_until is None

    def test_employment_ended_endpoint_claim_round_trips(self):
        base = _raw_claim(
            subject="Asa", predicate="employed_by", object="Microsoft",
            valid_until="2019", source_text="Asa left Microsoft in 2019",
        )
        endpoint = _raw_claim(
            subject="Asa", predicate="employment_ended", object="2019",
            source_text="Asa left Microsoft in 2019",
        )
        extractor, _ = _make_extractor([base, endpoint])
        claims = extractor.extract("Asa left Microsoft in 2019", _default_context())
        by_pred = {c.predicate: c for c in claims}
        assert "employment_ended" in by_pred
        ep = by_pred["employment_ended"]
        assert ep.object == "2019"
        assert ep.abstention_reason is None


class TestEventRelativeBoundRefs:
    """v0.16.1 WS8 Stage 1: valid_from_ref / valid_until_ref are event-relative
    bound references mirroring valid_during_ref. WRITE-ONLY metadata — no
    grounding/verdict path reads them (Stage 2 resolver deferred).

    These pin (1) the round-trip through _build_claim's
    extract_temporal_scope call and single Claim construction, and (2) the split
    Rule 16 prompt wording (before→valid_until_ref, after/since→valid_from_ref,
    during→valid_during_ref) so a future prompt edit cannot silently collapse the
    three directions back onto one field."""

    def test_valid_until_ref_round_trips_through_build_claim(self):
        # "before X" upper bound → valid_until_ref. The LLM emits the ref on the
        # raw claim; _build_claim must thread it onto the produced Claim.
        raw = _raw_claim(
            subject="The team", predicate="had", object="five members",
            verb_tense="past", valid_until_ref="claim_acquisition",
            source_text="The team had five members before the acquisition",
        )
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract(
            "The team had five members before the acquisition", _default_context()
        )
        assert len(claims) == 1
        c = claims[0]
        assert c.valid_until_ref == "claim_acquisition"
        assert c.valid_from_ref is None
        assert c.valid_during_ref is None
        # The ref suppresses the implicit-past-tense before_present default.
        assert c.valid_until is None
        assert c.valid_until != BEFORE_PRESENT

    def test_valid_from_ref_round_trips_through_build_claim(self):
        # "after/since X" lower bound → valid_from_ref.
        raw = _raw_claim(
            subject="she", predicate="was", object="President",
            verb_tense="past", valid_from_ref="claim_election",
            source_text="After the election, she was President",
        )
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract(
            "After the election, she was President", _default_context()
        )
        assert len(claims) == 1
        c = claims[0]
        assert c.valid_from_ref == "claim_election"
        assert c.valid_until_ref is None
        assert c.valid_during_ref is None
        assert c.valid_until is None  # ref suppresses before_present

    def test_refs_default_none_when_unset(self):
        # A normal claim carries no event-relative refs.
        raw = _raw_claim()
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Asa graduated from Williams College", _default_context())
        c = claims[0]
        assert c.valid_from_ref is None
        assert c.valid_until_ref is None

    def test_prompt_rule_16_splits_before_to_valid_until_ref(self):
        from aedos.layer1_extraction.extractor import _SYSTEM_PROMPT
        assert "16. EVENT-RELATIVE BOUNDS" in _SYSTEM_PROMPT
        # Upper bound: "before X" / "until X" → valid_until_ref.
        assert '"before X" or "until X"' in _SYSTEM_PROMPT
        assert "set valid_until_ref" in _SYSTEM_PROMPT

    def test_prompt_rule_16_splits_after_since_to_valid_from_ref(self):
        from aedos.layer1_extraction.extractor import _SYSTEM_PROMPT
        # Lower bound: "after X" / "since X" → valid_from_ref.
        assert '"after X" or "since X"' in _SYSTEM_PROMPT
        assert "set valid_from_ref" in _SYSTEM_PROMPT

    def test_prompt_rule_16_redirects_during_to_rule_15_valid_during_ref(self):
        from aedos.layer1_extraction.extractor import _SYSTEM_PROMPT
        # The co-temporal "during X" bound stays Rule 15 / valid_during_ref —
        # Rule 16 explicitly cedes it, not collapsing all three onto one field.
        assert 'A CO-TEMPORAL "during X" bound is Rule 15, not Rule 16' in _SYSTEM_PROMPT

    def test_prompt_rule_16_carries_d45_non_triggers(self):
        # D45 discipline: Rule 16 must carry explicit non-triggering conditions
        # (date/year, plain past-tense, Rule-9 subordinate clause).
        from aedos.layer1_extraction.extractor import _SYSTEM_PROMPT
        assert "DO NOT apply Rule 16 when" in _SYSTEM_PROMPT
        assert "X is a date or year" in _SYSTEM_PROMPT


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
    def test_entity_not_in_text_emits_with_subject_absent_reason(self):
        # v0.16 WS4 (4a): Bob's subject AND object are absent from the text.
        # Previously the claim was SILENTLY DROPPED; it is now EMITTED carrying
        # abstention_reason='subject_absent_from_source' (the walker
        # short-circuits it pre-lookup to no_grounding_found — abstention, the
        # conservative outcome). This inverts the v0.15 drop assertion.
        raws = [
            _raw_claim(subject="Asa", object="Google", source_text="Asa works at Google"),
            _raw_claim(subject="Bob", predicate="employed_by", object="Microsoft",
                       source_text="Bob works at Microsoft"),
        ]
        extractor, _ = _make_extractor(raws)
        claims = extractor.extract("Asa works at Google", _default_context())
        by_subject = {c.subject: c for c in claims}
        assert "Asa" in by_subject
        assert "Bob" in by_subject  # no longer dropped
        assert by_subject["Asa"].abstention_reason is None
        assert (
            by_subject["Bob"].abstention_reason
            == AbstentionReason.SUBJECT_ABSENT_FROM_SOURCE.value
        )

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
# TestAbstentionReasonStamping — v0.16 WS4 (4a): _build_claim NEVER drops a
# shaped claim silently. The four former early `return None` drops now stamp
# an abstention_reason and the claim is emitted; the walker short-circuits it
# pre-lookup. The ONE remaining `return None` is the future-tense filter
# (TestFutureTenseRejection, unchanged).
# ---------------------------------------------------------------------------

class TestAbstentionReasonStamping:
    def test_abstention_reason_defaults_none(self):
        # A well-formed, checkworthy claim carries no abstention_reason.
        extractor, _ = _make_extractor([_raw_claim()])
        claims = extractor.extract("Asa graduated from Williams College", _default_context())
        assert len(claims) == 1
        assert claims[0].abstention_reason is None

    def test_self_referential_sets_reason(self):
        # subject == object (after trim/casefold) → emitted with
        # abstention_reason='self_referential' (was a silent drop in v0.15).
        # Subject is present in the text so the hard-claim check passes and
        # subject_absent does NOT pre-empt self_referential.
        raw = _raw_claim(
            subject="Einstein", predicate="born_in", object="Einstein",
            source_text="Einstein was born in 1879",
        )
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Einstein was born in 1879", _default_context())
        assert len(claims) == 1
        assert claims[0].abstention_reason == AbstentionReason.SELF_REFERENTIAL.value

    def test_predicate_eq_object_sets_reason(self):
        # predicate == object (verb repeated into the object slot) → emitted
        # with abstention_reason='predicate_eq_object'. Subject is in the text
        # so the hard-claim check passes.
        raw = _raw_claim(
            subject="Berlin Wall", predicate="fell", object="fell",
            source_text="The Berlin Wall fell in 1989",
        )
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("The Berlin Wall fell in 1989", _default_context())
        assert len(claims) == 1
        assert claims[0].abstention_reason == AbstentionReason.PREDICATE_EQ_OBJECT.value

    def test_subject_absent_sets_reason(self):
        # Both subject AND object are absent from the source text → emitted
        # with abstention_reason='subject_absent_from_source'.
        raw = _raw_claim(
            subject="Zorblax", predicate="employed_by", object="Acme",
            source_text="Zorblax works at Acme",
        )
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Something entirely unrelated here", _default_context())
        assert len(claims) == 1
        assert (
            claims[0].abstention_reason
            == AbstentionReason.SUBJECT_ABSENT_FROM_SOURCE.value
        )

    def test_inert_prose_sets_not_checkworthy(self):
        # A claim triaging to INERT_PROSE (no named entity, no number, unknown
        # predicate) → emitted with abstention_reason='not_checkworthy'. The
        # subject is present in the text so the hard-claim check passes.
        raw = _raw_claim(
            subject="weather", predicate="is_nice", object="pleasant",
            source_text="the weather is nice",
        )
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("the weather is nice", _default_context())
        assert len(claims) == 1
        c = claims[0]
        assert c.triage_decision == TriageDecision.INERT_PROSE
        assert c.abstention_reason == AbstentionReason.NOT_CHECKWORTHY.value

    def test_reason_precedence(self):
        # When multiple reasons apply simultaneously, the FIRST in precedence
        # order wins: subject_absent_from_source > self_referential. Here the
        # subject == object AND both are absent from the source text — the
        # hard-claim check (checked first) must win.
        raw = _raw_claim(
            subject="Zorblax", predicate="is", object="Zorblax",
            source_text="Zorblax is Zorblax",
        )
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Completely different text", _default_context())
        assert len(claims) == 1
        assert (
            claims[0].abstention_reason
            == AbstentionReason.SUBJECT_ABSENT_FROM_SOURCE.value
        )

    @pytest.mark.parametrize(
        "subject,predicate,obj",
        [
            (None, "x", None),       # the reported crash shape
            (None, None, None),      # all-null
            ("Asa", None, "Google"), # null predicate
            ("Asa", "works_at", None),  # null object
            (None, "works_at", "Google"),  # null subject
        ],
    )
    def test_null_slot_does_not_crash(self, subject, predicate, obj):
        # Regression: an LLM-emitted explicit null in any of the three slots
        # previously crashed _build_claim ('NoneType' has no attribute 'strip')
        # at the self-referential check, propagating out of extract() and
        # losing the whole statement (benchmark verdict='error'). The slots are
        # coerced to "" at the point of use; a malformed claim must abstain,
        # never raise.
        raw = _raw_claim(subject=subject, predicate=predicate, object=obj,
                         source_text="some source")
        extractor, _ = _make_extractor([raw])
        # Must not raise.
        claims = extractor.extract("some source text", _default_context())
        # The claim is emitted (WS4: never silently dropped) and any empty
        # subject is routed to abstention rather than flowing as well-formed.
        for c in claims:
            if not (c.subject or "").strip():
                assert (
                    c.abstention_reason
                    == AbstentionReason.SUBJECT_ABSENT_FROM_SOURCE.value
                )

    def test_null_source_text_and_polarity_do_not_crash(self):
        # Defense-in-depth: explicit-null source_text (feeds a regex) and
        # explicit-null polarity (int()) must not crash either; polarity
        # defaults to the affirm value 1, not flipping a legitimate 0.
        raw = _raw_claim(source_text=None, polarity=None)
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Asa graduated from Williams College", _default_context())
        assert len(claims) == 1
        assert claims[0].polarity == 1

    def test_null_slot_one_bad_claim_does_not_abort_batch(self):
        # Defense-in-depth backstop: even if a raw claim somehow still raises
        # in _build_claim, extract() must skip it and keep the good claims —
        # one malformed claim cannot turn the whole statement into a lost case.
        raws = [
            _raw_claim(),  # well-formed, must survive
            _raw_claim(subject=None, predicate="x", object=None,
                       source_text="bad"),  # null-slot, must not abort batch
        ]
        extractor, _ = _make_extractor(raws)
        claims = extractor.extract("Asa graduated from Williams College", _default_context())
        # The good claim survives.
        assert any(c.subject == "Asa" and c.abstention_reason is None for c in claims)

    def test_build_claim_null_object_does_not_raise(self):
        # _build_claim-level pin (the exact crash site): a raw claim with an
        # explicit-null object reaches the self-referential check
        # (raw_subject.strip().casefold() == raw_object.strip().casefold()) and
        # previously raised "'NoneType' object has no attribute 'strip'". Call
        # _build_claim directly so the regression is pinned at the unit it
        # crashed in, not only through extract().
        extractor, _ = _make_extractor([])
        raw = _raw_claim(subject="Asa", predicate="works_at", object=None,
                         source_text="Asa works at Google")
        # Must not raise.
        claim = extractor._build_claim(raw, "Asa works at Google", _default_context())
        # A null object grounds to no_grounding_found (Deletion #2): it may be
        # emitted as a shaped claim or carry an abstention_reason, but never
        # crashes and never silently becomes well-formed-and-verifiable with a
        # None object slot.
        if claim is not None:
            assert claim.object != None  # noqa: E711 — pin: coerced to "", not None

    @pytest.mark.parametrize(
        "subject,predicate,obj",
        [
            (None, "x", None),       # the reported crash shape
            (None, None, None),      # all-null
            (None, "works_at", "Google"),  # null subject (object present)
        ],
    )
    def test_null_subject_routes_to_abstention(self, subject, predicate, obj):
        # §3.2 soundness pin (task (b), empty-SUBJECT half — the extractor-side
        # guarantee). A null/empty subject vacuously passes the hard-claim
        # check ("" is a substring of any text), so without a guard it would
        # flow as a well-formed triple. The extractor stamps
        # subject_absent_from_source; the walker short-circuits any claim with
        # abstention_reason to no_grounding_found PRE-lookup (walker.walk entry
        # guard), so it can never reach a KB/Tier U/Python lookup and never
        # becomes a spurious verified/contradicted. Pinned at the extractor.
        raw = _raw_claim(subject=subject, predicate=predicate, object=obj,
                         source_text="works at Google")
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("works at Google", _default_context())
        for c in claims:
            if not (c.subject or "").strip():
                assert (
                    c.abstention_reason
                    == AbstentionReason.SUBJECT_ABSENT_FROM_SOURCE.value
                ), "empty-subject claim must abstain, never flow as a triple"

    def test_null_object_coerced_not_none(self):
        # §3.2 soundness pin (task (b), empty-OBJECT half — the extractor-side
        # invariant). Per the documented Deletion #2 design the empty-object
        # path is intentionally NOT stamped at the extractor; instead the object
        # is coerced from None to "" and the shaped claim flows on to ground to
        # no_grounding_found in the walker (an abstention). The extractor's
        # contract here is narrower but load-bearing: the object slot is NEVER
        # left as None (which would crash the downstream .strip()/grounding),
        # always the empty string. The walker-side no_grounding_found outcome is
        # pinned in test_walker.py::TestNullSlotClaimGrounding.
        raw = _raw_claim(subject="Asa", predicate="works_at", object=None,
                         source_text="Asa works at Google")
        extractor, _ = _make_extractor([raw])
        claims = extractor.extract("Asa works at Google", _default_context())
        assert len(claims) == 1
        assert claims[0].object == ""  # coerced, never None


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

    def test_event_relative_ref_properties_in_schema(self):
        # v0.16.1 WS8 Stage 1: the extract tool advertises the event-relative
        # bound refs as optional (nullable) string properties, mirroring
        # valid_during_ref, so the LLM can populate them.
        props = EXTRACTION_TOOL["input_schema"]["properties"]["claims"]["items"]["properties"]
        for field in ("valid_during_ref", "valid_from_ref", "valid_until_ref"):
            assert field in props
            assert props[field]["type"] == ["string", "null"]
