"""Integration test: kb_resolvable predicate → KB verifier → verdict.

v0.16.1 WS5b: the standalone Layer-2 Router/Validator were deleted; routing is
predicate-driven off the oracle's `routing_hint`. This suite asserts the
kb_resolvable routing_hint on the oracle metadata and exercises the KB verifier
verdicts directly.

Fix-up note (M4): claims carry natural-language objects resolved through the
entity resolver; CONTRADICTED requires a functional (single_valued) predicate.
"""

from __future__ import annotations

import pytest

from aedos.database import open_memory_db
from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer3_substrate.predicate_translation import PredicateTranslation
from aedos.layer3_substrate.resolver import EntityResolver
from aedos.layer4_sources.kb_protocol import LocalContext, ResolutionCandidate, Statement, SubsumptionResult
from aedos.layer4_sources.kb_verifier import KBVerdictType, KBVerifier
from aedos.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RESOLUTIONS = {
    "Obama": "Q76",
    "President of the United States": "Q11696",
    "New York City": "Q60",
    "Honolulu": "Q18094",
}


class MockTransport:
    def __init__(self, kb_property="P39", single_valued=0):
        self._prop = kb_property
        self._single_valued = single_valued

    def extract_with_tool(self, *a, **kw):
        return {
            "object_type": "entity",
            "user_subject_required": 0,
            "distinct_slots": None,
            "routing_hint": "kb_resolvable",
            "kb_namespace": "wikidata",
            "kb_property": self._prop,
            "slot_to_qualifier": None,
            "single_valued": self._single_valued,
            "reason": "test",
        }

    def chat(self, *a, **kw):
        return ""


class MockKB:
    def __init__(self, statements):
        self._stmts = statements

    def resolve_entity(self, reference, local_context):
        qid = _RESOLUTIONS.get(reference)
        return [ResolutionCandidate(kb_identifier=qid, score=0.9)] if qid else []

    def lookup_statements(self, entity, predicate):
        return list(self._stmts)

    def subsumption(self, entity_a, entity_b, relation_type):
        return SubsumptionResult(verdict="unrelated")


def _make_system(statements, kb_property="P39", single_valued=0):
    db = open_memory_db()
    client = LLMClient(_transport=MockTransport(kb_property, single_valued))
    pt = PredicateTranslation(db=db, llm_client=client)
    kb = MockKB(statements)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    return pt, verifier


def _claim(subject="Obama", predicate="holds_role",
           object_val="President of the United States", polarity=1):
    return Claim(
        claim_id="c1",
        subject=subject,
        predicate=predicate,
        object=object_val,
        polarity=polarity,
        source_text="test",
        asserting_party="user_test",
        triage_decision=TriageDecision.VERIFY,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestKBPathRoundtrip:
    def test_predicate_routes_kb_resolvable(self):
        pt, _ = _make_system([Statement(value="Q11696", value_type="entity")])
        assert pt.consult("holds_role").routing_hint == "kb_resolvable"

    def test_kb_resolvable_claim_verified(self):
        _, verifier = _make_system([Statement(value="Q11696", value_type="entity")])
        result = verifier.verify(_claim())
        assert result.verdict == KBVerdictType.VERIFIED

    def test_kb_resolvable_no_statement_no_match(self):
        _, verifier = _make_system([])
        result = verifier.verify(_claim())
        assert result.verdict == KBVerdictType.NO_MATCH

    def test_multivalued_wrong_value_is_no_match(self):
        # holds_role (P39) is multi-valued — a non-matching position is not a
        # contradiction (M4): the subject may hold the claimed position too.
        _, verifier = _make_system([Statement(value="Q99999", value_type="entity")])
        result = verifier.verify(_claim())
        assert result.verdict == KBVerdictType.NO_MATCH

    def test_single_valued_wrong_value_contradicted(self):
        # born_in (P19) is functional — a different birthplace contradicts.
        _, verifier = _make_system(
            [Statement(value="Q18094", value_type="entity")],  # Honolulu
            kb_property="P19", single_valued=1,
        )
        result = verifier.verify(_claim(predicate="born_in", object_val="New York City"))
        assert result.verdict == KBVerdictType.CONTRADICTED

    def test_verified_result_has_subject_kb_id(self):
        _, verifier = _make_system([Statement(value="Q11696", value_type="entity")])
        result = verifier.verify(_claim())
        assert result.subject_kb_id is not None

    def test_verified_result_has_matched_statement(self):
        stmt = Statement(value="Q11696", value_type="entity")
        _, verifier = _make_system([stmt])
        result = verifier.verify(_claim())
        assert result.matched_statement is not None
