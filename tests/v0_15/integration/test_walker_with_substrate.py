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
