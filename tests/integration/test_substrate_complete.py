"""Integration: Substrate facade + cross-oracle consistency."""

from __future__ import annotations

import pytest

from aedos.database import open_memory_db
from aedos.layer3_substrate import Substrate
from aedos.layer3_substrate.predicate_distribution import PredicateDistributionOracle
from aedos.layer3_substrate.predicate_translation import PredicateTranslation
from aedos.layer3_substrate.resolver import EntityResolver
from aedos.layer3_substrate.subsumption import EntityRef, SubsumptionOracle, SubsumptionVerdictType
from aedos.layer4_sources.kb_protocol import SubsumptionResult
from aedos.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockTransport:
    def extract_with_tool(self, *a, purpose=None, **kw):
        if purpose == "substrate:predicate_distribution":
            return {"verdict": "distributes_up", "reason": "test distribution"}
        if purpose == "substrate:subsumption":
            return {"verdict": "a_subsumed_by_b", "reason": "test subsumption"}
        return {
            "object_type": "entity",
            "user_subject_required": 0,
            "distinct_slots": None,
            "routing_hint": "kb_resolvable",
            "kb_namespace": "wikidata",
            "kb_property": "P39",
            "slot_to_qualifier": {"valid_from": "P580", "valid_until": "P582"},
            "reason": "test",
        }
    def chat(self, *a, **kw): return ""


class MockKB:
    def resolve_entity(self, reference, local_context):
        return []
    def lookup_statements(self, entity, predicate):
        return []
    def subsumption(self, entity_a, entity_b, relation_type):
        return SubsumptionResult(verdict="a_subsumed_by_b", establishing_property="P31", traversal_chain=["Q5"])


def _make_substrate():
    db = open_memory_db()
    client = LLMClient(_transport=MockTransport())
    kb = MockKB()
    resolver = EntityResolver(kb_protocol=kb, db=db)
    pt = PredicateTranslation(db=db, llm_client=client)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    return Substrate(resolver=resolver, predicate_translation=pt, subsumption=sub, predicate_distribution=pd), db


# ---------------------------------------------------------------------------
# TestSubstrateFacade
# ---------------------------------------------------------------------------

class TestSubstrateFacade:
    def test_substrate_has_resolver(self):
        substrate, _ = _make_substrate()
        assert isinstance(substrate.resolver, EntityResolver)

    def test_substrate_has_predicate_translation(self):
        substrate, _ = _make_substrate()
        assert isinstance(substrate.predicate_translation, PredicateTranslation)

    def test_substrate_has_subsumption(self):
        substrate, _ = _make_substrate()
        assert isinstance(substrate.subsumption, SubsumptionOracle)

    def test_substrate_has_predicate_distribution(self):
        substrate, _ = _make_substrate()
        assert isinstance(substrate.predicate_distribution, PredicateDistributionOracle)

    def test_predicate_translation_accessible_via_facade(self):
        substrate, _ = _make_substrate()
        meta = substrate.predicate_translation.consult("holds_role")
        assert meta.aedos_predicate == "holds_role"

    def test_subsumption_accessible_via_facade(self):
        substrate, _ = _make_substrate()
        result = substrate.subsumption.consult(
            EntityRef(namespace="wikidata", identifier="Q76"),
            EntityRef(namespace="wikidata", identifier="Q5"),
            "is_a",
        )
        assert result.verdict == SubsumptionVerdictType.A_SUBSUMED_BY_B

    def test_predicate_distribution_accessible_via_facade(self):
        substrate, _ = _make_substrate()
        result = substrate.predicate_distribution.consult("lives_in", 1, "part_of")
        assert result.verdict is not None


# ---------------------------------------------------------------------------
# TestCrossOracleConsistency
# ---------------------------------------------------------------------------

class TestCrossOracleConsistency:
    def test_kb_resolvable_predicate_has_kb_property(self):
        substrate, _ = _make_substrate()
        meta = substrate.predicate_translation.consult("holds_role")
        assert meta.routing_hint == "kb_resolvable"
        assert meta.kb_property is not None
        # v0.16 WS1: the scalar kb_property mirrors bindings[0]. Read through
        # the authoritative binding list to confirm the single-binding synthesis.
        assert meta.bindings
        assert meta.bindings[0].kb_property == meta.kb_property

    def test_kb_property_used_in_subsumption_alignment(self):
        # If predicate maps to P39, and subsumption returns a_subsumed_by_b,
        # those are independently callable from the same substrate
        substrate, _ = _make_substrate()
        meta = substrate.predicate_translation.consult("holds_role")
        sub_result = substrate.subsumption.consult(
            EntityRef("wikidata", "Q76"), EntityRef("wikidata", "Q5"), "is_a"
        )
        assert meta.kb_property == "P39"
        # v0.16 WS1: the binding-level view agrees with the scalar accessor.
        assert meta.bindings[0].kb_property == "P39"
        assert sub_result.source == "kb"
