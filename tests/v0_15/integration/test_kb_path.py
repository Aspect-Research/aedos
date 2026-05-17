"""Integration test: claim → router (kb_resolvable) → KB verifier → verdict."""

from __future__ import annotations

import pytest

from src.aedos_v0_15.database import open_memory_db
from src.aedos_v0_15.layer1_extraction.extractor import Claim
from src.aedos_v0_15.layer1_extraction.triage import TriageDecision
from src.aedos_v0_15.layer2_routing.router import Router
from src.aedos_v0_15.layer2_routing.validator import Validator
from src.aedos_v0_15.layer3_substrate.predicate_translation import PredicateTranslation
from src.aedos_v0_15.layer3_substrate.resolver import EntityResolver
from src.aedos_v0_15.layer4_sources.kb_protocol import LocalContext, ResolutionCandidate, Statement, SubsumptionResult
from src.aedos_v0_15.layer4_sources.kb_verifier import KBVerdictType, KBVerifier
from src.aedos_v0_15.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockTransport:
    def extract_with_tool(self, *a, **kw):
        return {
            "object_type": "entity",
            "user_subject_required": 0,
            "distinct_slots": None,
            "routing_hint": "kb_resolvable",
            "kb_namespace": "wikidata",
            "kb_property": "P39",
            "slot_to_qualifier": None,
            "reason": "test",
        }
    def chat(self, *a, **kw):
        return ""


class MockKB:
    def __init__(self, statements):
        self._stmts = statements

    def resolve_entity(self, reference, local_context):
        return [ResolutionCandidate(kb_identifier="Q76", score=0.9)]

    def lookup_statements(self, entity, predicate):
        return list(self._stmts)

    def subsumption(self, entity_a, entity_b, relation_type):
        return SubsumptionResult(verdict="unrelated")


def _make_system(statements):
    db = open_memory_db()
    client = LLMClient(_transport=MockTransport())
    pt = PredicateTranslation(db=db, llm_client=client)
    validator = Validator()
    router = Router(predicate_translation=pt, validator=validator)
    kb = MockKB(statements)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    return router, verifier


def _claim(subject="Obama", predicate="holds_role", object_val="Q11696"):
    return Claim(
        claim_id="c1",
        subject=subject,
        predicate=predicate,
        object=object_val,
        polarity=1,
        source_text="test",
        asserting_party="user_test",
        triage_decision=TriageDecision.VERIFY,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestKBPathRoundtrip:
    def test_claim_routes_kb_resolvable(self):
        router, _ = _make_system([Statement(value="Q11696", value_type="entity")])
        decision = router.route(_claim())
        assert decision.route == "kb_resolvable"

    def test_kb_resolvable_claim_verified(self):
        _, verifier = _make_system([Statement(value="Q11696", value_type="entity")])
        result = verifier.verify(_claim())
        assert result.verdict == KBVerdictType.VERIFIED

    def test_kb_resolvable_no_statement_no_match(self):
        _, verifier = _make_system([])
        result = verifier.verify(_claim())
        assert result.verdict == KBVerdictType.NO_MATCH

    def test_kb_resolvable_wrong_value_contradicted(self):
        _, verifier = _make_system([Statement(value="Q99999", value_type="entity")])
        result = verifier.verify(_claim())
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
