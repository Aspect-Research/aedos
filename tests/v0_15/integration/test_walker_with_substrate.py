"""Integration: Walker with real substrate (mocked oracles), multi-source chains."""

from __future__ import annotations

import pytest

from src.aedos_v0_15.database import open_memory_db
from src.aedos_v0_15.layer1_extraction.extractor import Claim
from src.aedos_v0_15.layer1_extraction.triage import TriageDecision
from src.aedos_v0_15.layer3_substrate import Substrate
from src.aedos_v0_15.layer3_substrate.predicate_distribution import PredicateDistributionOracle
from src.aedos_v0_15.layer3_substrate.predicate_translation import PredicateTranslation
from src.aedos_v0_15.layer3_substrate.resolver import EntityResolver
from src.aedos_v0_15.layer3_substrate.subsumption import SubsumptionOracle
from src.aedos_v0_15.layer4_sources.kb_protocol import ResolutionCandidate, Statement, SubsumptionResult
from src.aedos_v0_15.layer4_sources.kb_verifier import KBVerdictType, KBVerifier
from src.aedos_v0_15.layer4_sources.python_verifier import PythonVerifier
from src.aedos_v0_15.layer4_sources.tier_u import TierU
from src.aedos_v0_15.layer4_sources.walker import VerificationContext, Walker, WalkerBudget
from src.aedos_v0_15.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockTransport:
    def extract_with_tool(self, *a, purpose=None, **kw):
        if purpose == "distribution_generation":
            return {"verdict": "neither", "reason": "test"}
        if purpose == "subsumption_generation":
            return {"verdict": "unrelated", "reason": "test"}
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
    def chat(self, *a, **kw): return ""


class MockKB:
    def __init__(self, stmts=None):
        self._stmts = stmts or []
    def resolve_entity(self, r, lc): return [ResolutionCandidate("Q76", score=0.9)]
    def lookup_statements(self, e, p): return list(self._stmts)
    def subsumption(self, a, b, rt): return SubsumptionResult(verdict="unrelated")


def _make_full_system(kb_stmts=None):
    db = open_memory_db()
    client = LLMClient(_transport=MockTransport())
    kb = MockKB(kb_stmts)
    pt = PredicateTranslation(db=db, llm_client=client)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(resolver=resolver, predicate_translation=pt, subsumption=sub, predicate_distribution=pd)
    tier_u = TierU(db=db, predicate_translation=pt)
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    py_verifier = PythonVerifier()
    walker = Walker(tier_u=tier_u, kb_verifier=kb_verifier, python_verifier=py_verifier, substrate=substrate)
    return walker, tier_u, db


def _claim(subject="Obama", predicate="holds_role", object_val="Q11696", polarity=1):
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


def _ctx():
    from datetime import datetime, timezone
    return VerificationContext(
        current_time=datetime.now(timezone.utc).isoformat(),
        asserting_party="user_test",
    )


# ---------------------------------------------------------------------------
# TestWalkerTierUPath
# ---------------------------------------------------------------------------

class TestWalkerTierUPath:
    def test_tier_u_verified_claim(self):
        walker, tier_u, _ = _make_full_system()
        claim = _claim()
        tier_u.write(claim)
        result = walker.walk(claim, _ctx())
        assert result.verdict == "verified"

    def test_tier_u_absent_no_grounding(self):
        walker, _, _ = _make_full_system()
        result = walker.walk(_claim(), _ctx())
        assert result.verdict == "no_grounding_found"


# ---------------------------------------------------------------------------
# TestWalkerKBPath
# ---------------------------------------------------------------------------

class TestWalkerKBPath:
    def test_kb_match_verified(self):
        stmts = [Statement(value="Q11696", value_type="entity")]
        walker, _, _ = _make_full_system(kb_stmts=stmts)
        result = walker.walk(_claim(), _ctx())
        assert result.verdict == "verified"

    def test_kb_contradiction_returns_contradicted(self):
        stmts = [Statement(value="Q99999", value_type="entity")]
        walker, _, _ = _make_full_system(kb_stmts=stmts)
        result = walker.walk(_claim(), _ctx())
        assert result.verdict == "contradicted"


# ---------------------------------------------------------------------------
# TestWalkerBudgetIntegration
# ---------------------------------------------------------------------------

class TestWalkerBudgetIntegration:
    def test_wall_clock_budget_in_integration(self):
        walker, _, _ = _make_full_system()
        budget = WalkerBudget(wall_clock_seconds=-1.0)
        result = walker.walk(_claim(), _ctx(), budget=budget)
        assert result.verdict == "no_grounding_found"
        assert "budget" in result.abstention_reason

    def test_normal_budget_allows_walk(self):
        walker, tier_u, _ = _make_full_system()
        claim = _claim()
        tier_u.write(claim)
        budget = WalkerBudget(wall_clock_seconds=30.0, max_llm_calls=20)
        result = walker.walk(claim, _ctx(), budget=budget)
        assert result.verdict == "verified"


# ---------------------------------------------------------------------------
# TestWalkerTraceIntegration
# ---------------------------------------------------------------------------

class TestWalkerTraceIntegration:
    def test_trace_emitted_on_verified(self):
        stmts = [Statement(value="Q11696", value_type="entity")]
        walker, _, _ = _make_full_system(kb_stmts=stmts)
        result = walker.walk(_claim(), _ctx())
        assert result.trace is not None

    def test_trace_root_has_subject(self):
        walker, tier_u, _ = _make_full_system()
        claim = _claim(subject="Obama")
        tier_u.write(claim)
        result = walker.walk(claim, _ctx())
        assert result.trace.root.content.get("subject") == "Obama"

    def test_trace_serializable_in_integration(self):
        import json
        from src.aedos_v0_15.layer5_result.trace import trace_to_json
        walker, tier_u, _ = _make_full_system()
        claim = _claim()
        tier_u.write(claim)
        result = walker.walk(claim, _ctx())
        d = trace_to_json(result.trace)
        json.dumps(d)


# ---------------------------------------------------------------------------
# Fix-up (C2, M3): derivation, polarity, and conflict-detection integration
# tests. Cases are sourced from tests/v0_15/calibration/derivation_corpus.jsonl.
# ---------------------------------------------------------------------------

def _seed_subsumption(db, a, b, relation_type, verdict="a_subsumed_by_b"):
    """Seed a substrate subsumption row (verdict a_subsumed_by_b means a R b)."""
    db.execute(
        """INSERT INTO subsumption
           (entity_a_namespace, entity_a_identifier, entity_b_namespace, entity_b_identifier,
            relation_type, verdict, source, reason, created_at)
           VALUES ('aedos', ?, 'aedos', ?, ?, ?, 'substrate', 'seeded test row', '2026-01-01T00:00:00')""",
        (a, b, relation_type, verdict),
    )
    db.commit()


def _seed_distribution(db, predicate, relation_type, verdict, polarity=1):
    """Seed a substrate predicate_distribution row."""
    db.execute(
        """INSERT INTO predicate_distribution
           (aedos_predicate, polarity, relation_type, verdict, reason, created_at)
           VALUES (?, ?, ?, ?, 'seeded test row', '2026-01-01T00:00:00')""",
        (predicate, polarity, relation_type, verdict),
    )
    db.commit()


class TestWalkerSubsumptionDerivation:
    """C2: distribution-gated subsumption traversal. Pre-fix the traversal block
    was a `pass` stub, so every case here returned no_grounding_found."""

    def test_single_hop_distribution_derivation(self):
        # der_multihop_001 paraphrase: Tier U grounds the town-level claim;
        # the goal is the state-level claim derived via part_of + distributes_up.
        walker, tier_u, db = _make_full_system()
        tier_u.write(_claim(subject="Asa", predicate="lives_in", object_val="Williamstown"))
        _seed_subsumption(db, "Williamstown", "Massachusetts", "part_of")
        _seed_distribution(db, "lives_in", "part_of", "distributes_up")
        goal = _claim(subject="Asa", predicate="lives_in", object_val="Massachusetts")
        result = walker.walk(goal, _ctx())
        assert result.verdict == "verified"

    def test_multi_hop_distribution_derivation(self):
        # Architectural sanity check: derive "Asa lives in the United States"
        # from Tier U "Asa lives_in Williamstown" + a two-hop part_of chain.
        walker, tier_u, db = _make_full_system()
        tier_u.write(_claim(subject="Asa", predicate="lives_in", object_val="Williamstown"))
        _seed_subsumption(db, "Williamstown", "Massachusetts", "part_of")
        _seed_subsumption(db, "Massachusetts", "United States", "part_of")
        _seed_distribution(db, "lives_in", "part_of", "distributes_up")
        goal = _claim(subject="Asa", predicate="lives_in", object_val="United States")
        result = walker.walk(goal, _ctx())
        assert result.verdict == "verified"

    def test_subsumption_traversal_emits_trace_edge(self):
        walker, tier_u, db = _make_full_system()
        tier_u.write(_claim(subject="Asa", predicate="lives_in", object_val="Williamstown"))
        _seed_subsumption(db, "Williamstown", "Massachusetts", "part_of")
        _seed_distribution(db, "lives_in", "part_of", "distributes_up")
        goal = _claim(subject="Asa", predicate="lives_in", object_val="Massachusetts")
        result = walker.walk(goal, _ctx())
        edge_types = [e.edge_type for e in result.trace.edges]
        assert "subsumption_traversal" in edge_types

    def test_distribution_gate_blocks_invalid_traversal(self):
        # der_multihop_009: `prefers` does NOT distribute over is_a. Even with a
        # subsumption row golden_retriever is_a dog, the gate must stay closed.
        walker, tier_u, db = _make_full_system()
        tier_u.write(_claim(subject="Asa", predicate="prefers", object_val="golden_retriever"))
        _seed_subsumption(db, "golden_retriever", "dog", "is_a")
        _seed_distribution(db, "prefers", "is_a", "neither")
        goal = _claim(subject="Asa", predicate="prefers", object_val="dog")
        result = walker.walk(goal, _ctx())
        assert result.verdict == "no_grounding_found"
        assert "subsumption_traversal" not in [e.edge_type for e in result.trace.edges]

    def test_distributes_down_ascends_to_parent(self):
        # der_multihop_006: "Obama is mortal" from "Obama is_a human" — mortal
        # distributes_down over is_a, so the walker ascends to the parent.
        walker, tier_u, db = _make_full_system()
        tier_u.write(_claim(subject="human", predicate="has_property", object_val="mortal"))
        _seed_subsumption(db, "Obama", "human", "is_a")
        _seed_distribution(db, "has_property", "is_a", "distributes_down")
        goal = _claim(subject="Obama", predicate="has_property", object_val="mortal")
        result = walker.walk(goal, _ctx())
        assert result.verdict == "verified"


class TestWalkerPolarityVerdicts:
    """M3a / M3 belief-revision: negated-claim verdict handling."""

    def test_negated_claim_grounded_in_negated_tier_u_is_verified(self):
        # der_cross_009: "Asa dislikes tea" == (Asa, prefers, tea, polarity=0),
        # grounded in a Tier U row of the same negated polarity -> verified.
        walker, tier_u, _ = _make_full_system()
        tier_u.write(_claim(subject="Asa", predicate="prefers", object_val="tea", polarity=0))
        goal = _claim(subject="Asa", predicate="prefers", object_val="tea", polarity=0)
        result = walker.walk(goal, _ctx())
        assert result.verdict == "verified"

    def test_negated_claim_contradicting_positive_prior(self):
        # der_revision_003: "Asa is not a student" vs Tier U "Asa is a student".
        walker, tier_u, _ = _make_full_system()
        tier_u.write(_claim(subject="Asa", predicate="holds_role", object_val="student", polarity=1))
        goal = _claim(subject="Asa", predicate="holds_role", object_val="student", polarity=0)
        result = walker.walk(goal, _ctx())
        assert result.verdict == "contradicted"

    def test_positive_claim_contradicting_negated_prior(self):
        # Symmetric: positive claim vs an asserted negation in Tier U.
        walker, tier_u, _ = _make_full_system()
        tier_u.write(_claim(subject="Asa", predicate="holds_role", object_val="student", polarity=0))
        goal = _claim(subject="Asa", predicate="holds_role", object_val="student", polarity=1)
        result = walker.walk(goal, _ctx())
        assert result.verdict == "contradicted"

    def test_polarity_trace_records_every_visited_node(self):
        # M3c: polarity_trace was a static one-element list. A multi-hop walk
        # visits >1 node, so the trace must have >1 entry.
        walker, tier_u, db = _make_full_system()
        tier_u.write(_claim(subject="Asa", predicate="lives_in", object_val="Williamstown"))
        _seed_subsumption(db, "Williamstown", "Massachusetts", "part_of")
        _seed_subsumption(db, "Massachusetts", "United States", "part_of")
        _seed_distribution(db, "lives_in", "part_of", "distributes_up")
        goal = _claim(subject="Asa", predicate="lives_in", object_val="United States")
        result = walker.walk(goal, _ctx())
        assert len(result.trace.polarity_trace) > 1
        assert all(p == 1 for p in result.trace.polarity_trace)


class TestWalkerMultiChainConflict:
    """M3b: the multi-chain conflict-detection branch was unreachable because
    the walker broke on the first `verified`."""

    def test_conflicting_chains_resolve_to_contradicted(self):
        # Mechanical conflict test. The goal subject has two is_a children;
        # expanding the subject slot yields two sibling nodes in one frontier:
        # one grounds the claim verified, the other grounds its negation.
        # Architecture 6.4: conflicting chains -> contradicted. The children
        # have distinct subjects, so the two Tier U rows do not trigger
        # contradiction-closure of one another.
        walker, tier_u, db = _make_full_system()
        tier_u.write(_claim(subject="MemberA", predicate="holds_role", object_val="winner", polarity=1))
        tier_u.write(_claim(subject="MemberB", predicate="holds_role", object_val="winner", polarity=0))
        _seed_subsumption(db, "MemberA", "Team", "is_a")
        _seed_subsumption(db, "MemberB", "Team", "is_a")
        _seed_distribution(db, "holds_role", "is_a", "distributes_up")
        goal = _claim(subject="Team", predicate="holds_role", object_val="winner")
        result = walker.walk(goal, _ctx())
        assert result.verdict == "contradicted"
        assert result.trace.walk_metadata.get("conflict") is True
