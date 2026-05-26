"""Tests for the derivation walker."""

from __future__ import annotations

import time
import pytest

from aedos.database import open_memory_db
from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer3_substrate import Substrate
from aedos.layer3_substrate.predicate_distribution import PredicateDistributionOracle
from aedos.layer3_substrate.predicate_translation import PredicateTranslation
from aedos.layer3_substrate.resolver import EntityResolver
from aedos.layer3_substrate.subsumption import SubsumptionOracle
from aedos.layer4_sources.kb_protocol import SubsumptionResult, LocalContext, ResolutionCandidate, Statement
from aedos.layer4_sources.kb_verifier import KBVerdict, KBVerdictType, KBVerifier
from aedos.layer4_sources.python_verifier import PythonVerifier
from aedos.layer4_sources.tier_u import LookupResult, TierU
from aedos.layer4_sources.walker import (
    BudgetExceeded,
    VerificationContext,
    Walker,
    WalkerBudget,
    WalkResult,
)
from aedos.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockTransport:
    def __init__(self, routing_hint="kb_resolvable", distribution_verdict="neither"):
        self._hint = routing_hint
        self._dist = distribution_verdict
        self.call_count = 0

    def extract_with_tool(self, *a, purpose=None, **kw):
        self.call_count += 1
        if purpose == "substrate:predicate_distribution":
            return {"verdict": self._dist, "reason": "test"}
        if purpose == "substrate:subsumption":
            return {"verdict": "a_subsumed_by_b", "reason": "test"}
        return {
            "object_type": "entity",
            "user_subject_required": 0,
            "distinct_slots": None,
            "routing_hint": self._hint,
            "kb_namespace": "wikidata" if self._hint == "kb_resolvable" else None,
            "kb_property": "P39" if self._hint == "kb_resolvable" else None,
            "slot_to_qualifier": None,
            "reason": "test",
        }

    def chat(self, *a, **kw):
        return ""


class MockTierU:
    def __init__(self, found=False, historical_only=False):
        self._found = found
        self._historical = historical_only

    def lookup(self, claim, current_time=None, exclude_row_ids=None):
        return LookupResult(found=self._found, historical_only=self._historical)

    def lookup_object_conflict(self, claim, current_time=None):
        # No object conflict in these unit fixtures; the walker's
        # object-conflict path (B2/D16) falls through to KB/Python.
        return LookupResult(found=False)

    def write(self, *a, **kw):
        pass


class MockKBVerifier:
    def __init__(self, verdict=KBVerdictType.NO_MATCH):
        self._verdict = verdict

    def verify(self, claim, current_time=None, source_text=None):
        return KBVerdict(verdict=self._verdict, subject_kb_id="Q76")


def _make_walker(
    tier_u_found=False,
    kb_verdict=KBVerdictType.NO_MATCH,
    distribution_verdict="neither",
    # Phase H Cluster 2 step 3: default is `kb_resolvable` (was
    # `user_authoritative`). The Q-UserAuth flag-set-at-walk-start
    # made every walker unit test produce *_given_assertion verdicts,
    # entangling routine flow assertions with the user_authoritative
    # semantic. The Q-UserAuth path is tested explicitly in
    # TestF042RoutingGate and TestQUserAuth (the latter added in step
    # 3); other tests assert flow behavior with a neutral route.
    routing_hint="kb_resolvable",
):
    db = open_memory_db()
    transport = MockTransport(routing_hint=routing_hint, distribution_verdict=distribution_verdict)
    client = LLMClient(_transport=transport)
    pt = PredicateTranslation(db=db, llm_client=client)

    class StubKB:
        def resolve_entity(self, r, lc): return [ResolutionCandidate("Q76", score=0.9)]
        def lookup_statements(self, e, p): return []
        def subsumption(self, a, b, rt): return SubsumptionResult(verdict="unrelated")

    resolver = EntityResolver(kb_protocol=StubKB(), db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=StubKB())
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(resolver=resolver, predicate_translation=pt, subsumption=sub, predicate_distribution=pd)

    tier_u = MockTierU(found=tier_u_found)
    kb_verifier = MockKBVerifier(verdict=kb_verdict)
    py_verifier = PythonVerifier()

    return Walker(
        tier_u=tier_u,
        kb_verifier=kb_verifier,
        python_verifier=py_verifier,
        substrate=substrate,
    )


def _claim(subject="Obama", predicate="holds_role", object_val="President", polarity=1):
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
# TestWalkResultDataclass
# ---------------------------------------------------------------------------

class TestWalkResultDataclass:
    def test_fields_present(self):
        from aedos.layer5_result.trace import JustificationTrace, TraceNode
        trace = JustificationTrace(root=TraceNode("claim"))
        wr = WalkResult(verdict="no_grounding_found", trace=trace)
        assert wr.verdict == "no_grounding_found"
        assert wr.abstention_reason is None
        assert wr.budget_consumption.llm_calls == 0


# ---------------------------------------------------------------------------
# TestWalkerDirectLookup
# ---------------------------------------------------------------------------

class TestWalkerDirectLookup:
    def test_tier_u_found_returns_verified(self):
        # Phase H Cluster 2 step 3: MockTierU's `found=True, rows=[]`
        # shape exercises the walker's defensive defaults (row id
        # unknown, status defaults to asserted_unverified). KB mock
        # returns NO_MATCH, so the Q-Lookup-α external-grounding
        # attempt fails and the verdict is verified_given_assertion.
        # See TestUpgradePath for the upgrade case where KB succeeds.
        walker = _make_walker(tier_u_found=True)
        result = walker.walk(_claim(), _ctx())
        assert result.verdict == "verified_given_assertion"

    def test_tier_u_not_found_no_grounding(self):
        walker = _make_walker(tier_u_found=False, kb_verdict=KBVerdictType.NO_MATCH)
        result = walker.walk(_claim(), _ctx())
        assert result.verdict == "no_grounding_found"

    def test_kb_verified_returns_verified(self):
        walker = _make_walker(kb_verdict=KBVerdictType.VERIFIED)
        result = walker.walk(_claim(), _ctx())
        assert result.verdict == "verified"

    def test_kb_contradicted_returns_contradicted(self):
        walker = _make_walker(kb_verdict=KBVerdictType.CONTRADICTED)
        result = walker.walk(_claim(), _ctx())
        assert result.verdict == "contradicted"

    def test_no_match_returns_no_grounding_found(self):
        walker = _make_walker(tier_u_found=False, kb_verdict=KBVerdictType.NO_MATCH)
        result = walker.walk(_claim(), _ctx())
        assert result.verdict == "no_grounding_found"
        assert result.abstention_reason == "depth_exhausted"


# ---------------------------------------------------------------------------
# TestF042RoutingGate — Python verifier is gated on routing_hint=="python"
# ---------------------------------------------------------------------------

class _AdversarialPythonVerifier:
    """A Python verifier that mimics the live LLM-driven verifier's failure
    mode for subjective / preference / opinion claims: when invoked, it
    returns CONTRADICTED rather than the architecturally-correct
    no_terminal_result.

    Used to drive the walker against the kind of behavior the live verifier
    actually exhibits — see docs/v0.16_planning.md D41 on adversarial mock
    fixtures. If the walker properly gates on routing_hint=="python", this
    verifier is never invoked for non-python claims and the walker abstains
    cleanly. If the gate is missing (the F-042 bug), this verifier produces
    contradicted for every claim the walker delegates to it.
    """

    def __init__(self):
        self.call_count = 0

    def verify(self, claim):
        self.call_count += 1
        from aedos.layer4_sources.python_verifier import PythonVerdict
        return PythonVerdict(
            verdict="contradicted",
            generated_code="def verify(s, p, o): return False",
            inputs={"subject": claim.subject, "predicate": claim.predicate, "object": claim.object},
            output="FALSE",
        )


def _make_walker_with_py_verifier(py_verifier, routing_hint, kb_verdict=KBVerdictType.NO_MATCH):
    """Walker fixture that takes a specific Python verifier — for F-042
    routing-gate tests."""
    db = open_memory_db()
    transport = MockTransport(routing_hint=routing_hint)
    client = LLMClient(_transport=transport)
    pt = PredicateTranslation(db=db, llm_client=client)

    class StubKB:
        def resolve_entity(self, r, lc): return [ResolutionCandidate("Q76", score=0.9)]
        def lookup_statements(self, e, p): return []
        def subsumption(self, a, b, rt): return SubsumptionResult(verdict="unrelated")

    resolver = EntityResolver(kb_protocol=StubKB(), db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=StubKB())
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(resolver=resolver, predicate_translation=pt, subsumption=sub, predicate_distribution=pd)

    return Walker(
        tier_u=MockTierU(found=False),
        kb_verifier=MockKBVerifier(verdict=kb_verdict),
        python_verifier=py_verifier,
        substrate=substrate,
    )


class TestF042RoutingGate:
    """F-042: the walker invokes the Python verifier only when the predicate's
    routing_hint is 'python' (architecture §6.5 step 3). Before this gate,
    the walker invoked Python for every claim that didn't get a Tier U or
    KB verdict — producing false `contradicted` for subjective/preference/
    opinion claims when the LLM-driven verifier wrote `return False`."""

    def test_python_not_invoked_for_user_authoritative_route(self):
        py = _AdversarialPythonVerifier()
        walker = _make_walker_with_py_verifier(py, routing_hint="user_authoritative")
        result = walker.walk(_claim(predicate="prefers"), _ctx())
        # Walker must abstain, not propagate the adversarial verifier's
        # contradiction. Phase H Cluster 2 step 3 (Q-UserAuth):
        # user_authoritative claims always produce *_given_assertion
        # verdicts because external grounding is structurally
        # unreachable. Abstention here is correct.
        assert result.verdict == "abstained_given_assertion", (
            f"User-authoritative route should abstain (no Tier U match, "
            f"Python skipped); got verdict={result.verdict}"
        )
        assert py.call_count == 0, (
            f"Adversarial Python verifier should not have been called; "
            f"called {py.call_count} times"
        )

    def test_python_not_invoked_for_abstain_route(self):
        py = _AdversarialPythonVerifier()
        walker = _make_walker_with_py_verifier(py, routing_hint="abstain")
        result = walker.walk(_claim(predicate="is_best"), _ctx())
        assert result.verdict == "no_grounding_found"
        assert py.call_count == 0

    def test_python_not_invoked_for_kb_resolvable_route(self):
        py = _AdversarialPythonVerifier()
        walker = _make_walker_with_py_verifier(
            py, routing_hint="kb_resolvable", kb_verdict=KBVerdictType.NO_MATCH
        )
        result = walker.walk(_claim(predicate="located_in"), _ctx())
        # KB returns NO_MATCH; walker must abstain instead of falling
        # through to Python.
        assert result.verdict == "no_grounding_found"
        assert py.call_count == 0

    def test_python_IS_invoked_for_python_route(self):
        py = _AdversarialPythonVerifier()
        walker = _make_walker_with_py_verifier(py, routing_hint="python")
        result = walker.walk(_claim(predicate="greater_than"), _ctx())
        # Python route is authorized; the verifier fires. Its (adversarial)
        # output propagates — that's correct for a python-routed claim,
        # because Python is the only premise source for that route.
        assert py.call_count == 1
        assert result.verdict == "contradicted"


# ---------------------------------------------------------------------------
# TestWalkerTrace
# ---------------------------------------------------------------------------

class TestWalkerTrace:
    def test_trace_root_is_claim(self):
        walker = _make_walker(tier_u_found=True)
        result = walker.walk(_claim(), _ctx())
        assert result.trace.root.node_type == "claim"

    def test_trace_source_breakdown_tier_u(self):
        walker = _make_walker(tier_u_found=True)
        result = walker.walk(_claim(), _ctx())
        assert result.trace.source_breakdown.get("tier_u", 0) >= 1

    def test_trace_source_breakdown_kb(self):
        walker = _make_walker(kb_verdict=KBVerdictType.VERIFIED)
        result = walker.walk(_claim(), _ctx())
        assert result.trace.source_breakdown.get("kb", 0) >= 1

    def test_trace_has_premise_lookup_edge(self):
        walker = _make_walker(tier_u_found=True)
        result = walker.walk(_claim(), _ctx())
        edge_types = [e.edge_type for e in result.trace.edges]
        assert "premise_lookup" in edge_types

    def test_trace_walk_metadata_has_depth(self):
        walker = _make_walker()
        result = walker.walk(_claim(), _ctx())
        assert "depth_reached" in result.trace.walk_metadata

    def test_trace_serializable(self):
        import json
        from aedos.layer5_result.trace import trace_to_json
        walker = _make_walker(tier_u_found=True)
        result = walker.walk(_claim(), _ctx())
        d = trace_to_json(result.trace)
        json.dumps(d)  # must not raise


# ---------------------------------------------------------------------------
# TestWalkerCycleDetection
# ---------------------------------------------------------------------------

class TestWalkerCycleDetection:
    def test_same_claim_not_revisited(self):
        # If walker expands to identical claim, it should not loop
        walker = _make_walker()
        result = walker.walk(_claim(), _ctx())
        # Walker terminates without error
        assert result.verdict in ("verified", "contradicted", "no_grounding_found")

    def test_walk_terminates_within_depth(self):
        walker = _make_walker()
        walker._max_depth = 2
        result = walker.walk(_claim(), _ctx())
        assert result.trace.walk_metadata["depth_reached"] <= 2


# ---------------------------------------------------------------------------
# TestWalkerBudgetEnforcement
# ---------------------------------------------------------------------------

class TestWalkerBudgetEnforcement:
    def test_wall_clock_budget_triggers_abstention(self):
        walker = _make_walker()
        # Negative threshold ensures elapsed is always > threshold on first check
        budget = WalkerBudget(wall_clock_seconds=-1.0, max_llm_calls=100)
        result = walker.walk(_claim(), _ctx(), budget=budget)
        assert result.verdict == "no_grounding_found"
        assert result.abstention_reason == "budget_wall_clock"

    def test_llm_call_budget_triggers_abstention(self):
        # Use a routing hint that will trigger LLM calls on cold cache
        walker = _make_walker(kb_verdict=KBVerdictType.NO_MATCH, routing_hint="kb_resolvable")
        budget = WalkerBudget(wall_clock_seconds=30.0, max_llm_calls=0)
        result = walker.walk(_claim(), _ctx(), budget=budget)
        # With 0 llm_calls budget and any cold-cache oracle calls, should abstain
        assert result.verdict == "no_grounding_found"
        assert result.abstention_reason == "budget_llm_calls"

    def test_budget_consumption_in_result(self):
        walker = _make_walker()
        result = walker.walk(_claim(), _ctx())
        assert result.budget_consumption.wall_clock_ms >= 0
        assert result.budget_consumption.llm_calls >= 0


# ---------------------------------------------------------------------------
# TestWalkerPolarityTracking
# ---------------------------------------------------------------------------

class TestWalkerPolarityTracking:
    def test_polarity_in_trace(self):
        walker = _make_walker(tier_u_found=True)
        c = _claim(polarity=1)
        result = walker.walk(c, _ctx())
        assert 1 in result.trace.polarity_trace

    def test_negated_polarity_tracked(self):
        walker = _make_walker(tier_u_found=True)
        c = _claim(polarity=0)
        result = walker.walk(c, _ctx())
        assert 0 in result.trace.polarity_trace
