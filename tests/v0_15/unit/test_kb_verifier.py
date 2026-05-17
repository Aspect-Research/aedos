"""Tests for KBVerifier — verify, contradict, no_match, scope."""

from __future__ import annotations

import pytest

from src.aedos_v0_15.database import open_memory_db
from src.aedos_v0_15.layer1_extraction.extractor import Claim
from src.aedos_v0_15.layer1_extraction.triage import TriageDecision
from src.aedos_v0_15.layer3_substrate.predicate_translation import PredicateTranslation
from src.aedos_v0_15.layer3_substrate.resolver import EntityResolver
from src.aedos_v0_15.layer4_sources.kb_protocol import (
    LocalContext, ResolutionCandidate, Statement, SubsumptionResult
)
from src.aedos_v0_15.layer4_sources.kb_verifier import KBVerdict, KBVerdictType, KBVerifier
from src.aedos_v0_15.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockTransport:
    def __init__(self, routing_hint="kb_resolvable", kb_property="P39"):
        self._hint = routing_hint
        self._prop = kb_property

    def extract_with_tool(self, *a, **kw):
        return {
            "object_type": "entity",
            "user_subject_required": 0,
            "distinct_slots": None,
            "routing_hint": self._hint,
            "kb_namespace": "wikidata" if self._hint == "kb_resolvable" else None,
            "kb_property": self._prop if self._hint == "kb_resolvable" else None,
            "slot_to_qualifier": None,
            "reason": "test",
        }

    def chat(self, *a, **kw):
        return ""


class MockKB:
    def __init__(self, statements: list[Statement]):
        self._statements = statements

    def resolve_entity(self, reference, local_context):
        # Always resolve to Q76 for any reference
        return [ResolutionCandidate(kb_identifier="Q76", score=0.9)]

    def lookup_statements(self, entity, predicate):
        return list(self._statements)

    def subsumption(self, entity_a, entity_b, relation_type):
        return SubsumptionResult(verdict="unrelated")


def _make_verifier(statements: list[Statement], routing_hint="kb_resolvable", kb_property="P39"):
    db = open_memory_db()
    transport = MockTransport(routing_hint, kb_property)
    client = LLMClient(_transport=transport)
    pt = PredicateTranslation(db=db, llm_client=client)
    kb = MockKB(statements)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    return KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)


def _claim(subject="Obama", predicate="holds_role", object_val="Q11696",
           valid_from=None, valid_until=None):
    return Claim(
        claim_id="c1",
        subject=subject,
        predicate=predicate,
        object=object_val,
        polarity=1,
        source_text="test",
        asserting_party="user_test",
        triage_decision=TriageDecision.VERIFY,
        valid_from=valid_from,
        valid_until=valid_until,
    )


# ---------------------------------------------------------------------------
# TestKBVerdictDataclass
# ---------------------------------------------------------------------------

class TestKBVerdictDataclass:
    def test_fields_present(self):
        v = KBVerdict(verdict=KBVerdictType.VERIFIED)
        assert v.verdict == KBVerdictType.VERIFIED
        assert v.matched_statement is None
        assert v.subject_kb_id is None
        assert v.trace == {}


# ---------------------------------------------------------------------------
# TestKBVerifierVerify
# ---------------------------------------------------------------------------

class TestKBVerifierVerify:
    def test_verified_when_value_matches(self):
        stmts = [Statement(value="Q11696", value_type="entity")]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim())
        assert result.verdict == KBVerdictType.VERIFIED

    def test_verified_returns_matched_statement(self):
        stmt = Statement(value="Q11696", value_type="entity")
        verifier = _make_verifier([stmt])
        result = verifier.verify(_claim())
        assert result.matched_statement is not None
        assert result.matched_statement.value == "Q11696"

    def test_verified_sets_subject_kb_id(self):
        stmts = [Statement(value="Q11696", value_type="entity")]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim())
        assert result.subject_kb_id == "Q76"

    def test_no_match_when_value_differs(self):
        stmts = [Statement(value="Q12345_other", value_type="entity")]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim())
        # Different value but same property → contradicted
        assert result.verdict == KBVerdictType.CONTRADICTED

    def test_no_match_when_no_statements(self):
        verifier = _make_verifier([])
        result = verifier.verify(_claim())
        assert result.verdict == KBVerdictType.NO_MATCH

    def test_no_kb_path_when_not_kb_resolvable(self):
        stmts = [Statement(value="Q11696", value_type="entity")]
        verifier = _make_verifier(stmts, routing_hint="user_authoritative")
        result = verifier.verify(_claim())
        assert result.verdict == KBVerdictType.NO_KB_PATH

    def test_no_kb_path_when_predicate_translation_fails(self):
        db = open_memory_db()
        class FailingTransport:
            def extract_with_tool(self, *a, **kw): raise RuntimeError("fail")
            def chat(self, *a, **kw): return ""
        pt = PredicateTranslation(db=db, llm_client=LLMClient(_transport=FailingTransport()))
        kb = MockKB([])
        resolver = EntityResolver(kb_protocol=kb, db=db)
        verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
        result = verifier.verify(_claim())
        assert result.verdict == KBVerdictType.NO_KB_PATH


# ---------------------------------------------------------------------------
# TestKBVerifierTemporalScope
# ---------------------------------------------------------------------------

class TestKBVerifierTemporalScope:
    def test_scope_compatible_no_qualifiers(self):
        stmts = [Statement(value="Q11696", value_type="entity")]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim(valid_from="2010-01-01", valid_until="2016-01-01"))
        assert result.verdict == KBVerdictType.VERIFIED

    def test_scope_compatible_with_matching_qualifiers(self):
        stmts = [Statement(
            value="Q11696", value_type="entity",
            qualifiers={"P580": "2009-01-20", "P582": "2017-01-20"}
        )]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim(valid_from="2010-01-01", valid_until="2016-01-01"))
        assert result.verdict == KBVerdictType.VERIFIED

    def test_scope_mismatch_returns_no_match(self):
        # Claim says 2005 but statement only starts 2009 → scope incompatible
        stmts = [Statement(
            value="Q11696", value_type="entity",
            qualifiers={"P580": "2009-01-20"}
        )]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim(valid_from="2005-01-01"))
        assert result.verdict == KBVerdictType.NO_MATCH


# ---------------------------------------------------------------------------
# TestKBVerifierCaseInsensitive
# ---------------------------------------------------------------------------

class TestKBVerifierCaseInsensitive:
    def test_case_insensitive_literal_match(self):
        stmts = [Statement(value="president", value_type="literal")]
        verifier = _make_verifier(stmts, kb_property="P39")
        result = verifier.verify(_claim(object_val="President"))
        assert result.verdict == KBVerdictType.VERIFIED
