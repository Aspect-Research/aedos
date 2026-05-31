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
    def __init__(self, found=False, historical_only=False, match_polarity=1):
        self._found = found
        self._historical = historical_only
        # Phase H Cluster 3 step 7 fixup: the walker now checks the
        # polarity-flipped lookup FIRST (to detect belief-revision
        # priors before falling through to Stage 1). The mock needs to
        # match only the claim's intended polarity — otherwise an
        # always-found stub fires the polarity_conflict path on every
        # walk and shadows the Stage 1 verification intent.
        self._match_polarity = match_polarity

    def lookup(self, claim, current_time=None, exclude_row_ids=None):
        if self._found and claim.polarity == self._match_polarity:
            return LookupResult(found=True, historical_only=self._historical)
        if self._historical and claim.polarity == self._match_polarity:
            return LookupResult(found=False, historical_only=True)
        return LookupResult(found=False)

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
# TestWalkerContradictingValue — WS5(a): the CONTRADICTED KB premise_lookup
# edge carries the contradicting value (the source's statement value).
# ---------------------------------------------------------------------------

def _contradicted_edge(trace):
    """Return the first contradicted premise_lookup edge metadata, or None."""
    for e in trace.edges:
        if e.metadata.get("verdict") == "contradicted":
            return e.metadata
    return None


class _ContradictingKBVerifier:
    """Mock KBVerifier returning a CONTRADICTED verdict whose
    matched_statement carries the value the source holds — the value WS5
    must surface onto the trace edge."""

    def __init__(self, statement):
        self._statement = statement

    def verify(self, claim, current_time=None, source_text=None):
        return KBVerdict(
            verdict=KBVerdictType.CONTRADICTED,
            matched_statement=self._statement,
            subject_kb_id="Q76",
            trace={"kb_property": "P19"},
        )


def _make_walker_with_kb_verifier(kb_verifier, routing_hint="kb_resolvable"):
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
        kb_verifier=kb_verifier,
        python_verifier=PythonVerifier(),
        substrate=substrate,
    )


class TestWalkerContradictingValue:
    def test_contradicted_edge_carries_matched_statement_value(self):
        # WS5(a.1): the contradicting value on the CONTRADICTED kb edge is
        # exactly matched_statement.value, with its value_type.
        stmt = Statement(value="Q18094", value_type="entity")
        walker = _make_walker_with_kb_verifier(_ContradictingKBVerifier(stmt))
        result = walker.walk(_claim(), _ctx())
        assert result.verdict == "contradicted"
        md = _contradicted_edge(result.trace)
        assert md is not None
        assert md["contradicting_value"] == "Q18094"
        assert md["contradicting_value_type"] == "entity"

    def test_contradicting_value_flows_to_claim_verdict(self):
        # End-to-end: the value carried on the edge is plumbed by the
        # aggregator into ClaimVerdict.contradicting_value.
        from aedos.layer5_result.aggregator import Aggregator
        stmt = Statement(value="Paris", value_type="literal")
        walker = _make_walker_with_kb_verifier(_ContradictingKBVerifier(stmt))
        claim = _claim()
        result = walker.walk(claim, _ctx())
        vr = Aggregator().aggregate([claim], [result])
        cv = vr.claim_verdicts[0]
        assert cv.verdict == "contradicted"
        assert cv.contradicting_value == "Paris"
        assert cv.contradicting_value_type == "literal"

    def test_subsumption_fallback_contradiction_has_no_value(self):
        # matched_statement is None on the subsumption-fallback CONTRADICTED
        # path (and the verifier trace carries no contradicting_value) → the
        # edge carries None, never an invented value (§3.2-safe).
        class _NoStatementContradictingKB:
            def verify(self, claim, current_time=None, source_text=None):
                return KBVerdict(
                    verdict=KBVerdictType.CONTRADICTED,
                    matched_statement=None,
                    subject_kb_id="Q76",
                    trace={"kb_property": "P19"},
                )

        walker = _make_walker_with_kb_verifier(_NoStatementContradictingKB())
        result = walker.walk(_claim(), _ctx())
        assert result.verdict == "contradicted"
        md = _contradicted_edge(result.trace)
        assert md is not None
        assert md.get("contradicting_value") is None


# ---------------------------------------------------------------------------
# TestWalkerKBQuantitativeValue — WS5(a.2): the kb_quantitative path now
# appends an observability premise_lookup edge, and on contradiction the
# KB value is the contradicting value.
# ---------------------------------------------------------------------------

class _QuantTransport:
    """Transport whose predicate metadata routes to kb_quantitative with a
    kb_property (P1082 = population), so the walker's quantitative branch
    fires."""

    def extract_with_tool(self, *a, purpose=None, **kw):
        if purpose == "substrate:predicate_distribution":
            return {"verdict": "neither", "reason": "test"}
        if purpose == "substrate:subsumption":
            return {"verdict": "unrelated", "reason": "test"}
        return {
            "object_type": "quantity",
            "user_subject_required": 0,
            "distinct_slots": None,
            "routing_hint": "kb_quantitative",
            "kb_namespace": "wikidata",
            "kb_property": "P1082",
            "slot_to_qualifier": None,
            "reason": "test",
        }

    def chat(self, *a, **kw):
        return ""


def _make_quant_walker(kb_value_str):
    db = open_memory_db()
    client = LLMClient(_transport=_QuantTransport())
    pt = PredicateTranslation(db=db, llm_client=client)

    class StubQuantKB:
        def resolve_entity(self, r, lc): return [ResolutionCandidate("Q142", score=0.95)]
        def lookup_statements(self, e, p):
            return [Statement(value=kb_value_str, value_type="quantity")]
        def subsumption(self, a, b, rt): return SubsumptionResult(verdict="unrelated")

    kb = StubQuantKB()
    resolver = EntityResolver(kb_protocol=kb, db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(resolver=resolver, predicate_translation=pt, subsumption=sub, predicate_distribution=pd)
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    return Walker(
        tier_u=MockTierU(found=False),
        kb_verifier=kb_verifier,
        python_verifier=PythonVerifier(),
        substrate=substrate,
        kb=kb,
    )


class TestWalkerKBQuantitativeValue:
    def _quant_claim(self):
        return _claim(
            subject="France",
            predicate="population_greater_than",
            object_val="60 million",
        )

    def test_quantitative_contradiction_carries_kb_value(self):
        # KB value (10M) < threshold (60M) → contradicted; the contradicting
        # value is the KB value, typed 'quantity'.
        walker = _make_quant_walker(kb_value_str="10000000")
        result = walker.walk(self._quant_claim(), _ctx())
        assert result.verdict == "contradicted"
        md = _contradicted_edge(result.trace)
        assert md is not None
        assert md["source"] == "kb_quantitative"
        assert md["contradicting_value"] == 10000000
        assert md["contradicting_value_type"] == "quantity"
        assert md["kb_value"] == 10000000
        assert md["threshold"] == 60000000

    def test_quantitative_verified_appends_observability_edge_without_value(self):
        # KB value (67M) > threshold (60M) → verified. The observability edge
        # is still appended (kb_value/threshold), but carries NO
        # contradicting_value (it is not a contradiction).
        walker = _make_quant_walker(kb_value_str="67000000")
        result = walker.walk(self._quant_claim(), _ctx())
        assert result.verdict == "verified"
        edge = next(
            (e.metadata for e in result.trace.edges
             if e.metadata.get("source") == "kb_quantitative"),
            None,
        )
        assert edge is not None
        assert edge["kb_value"] == 67000000
        assert edge["verdict"] == "verified"
        assert "contradicting_value" not in edge

    def test_quantitative_single_kb_source_count(self):
        # WS5 risk note: the added edge must NOT double-count source_breakdown.
        walker = _make_quant_walker(kb_value_str="10000000")
        result = walker.walk(self._quant_claim(), _ctx())
        assert result.trace.source_breakdown.get("kb") == 1


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
# TestPersonaSubjectRouting — v0.16.1 WS5b: a claim whose subject is a
# stipulated user persona (a `user identity X` Tier U row) routes
# user_authoritative — KB is structurally unreachable, so the entity resolver
# can never misresolve the persona name and false-contradict. This is the
# re-proof of the persona-abstain soundness pin formerly held by the deleted
# _is_persona_subject KB-skip guard.
# ---------------------------------------------------------------------------

class _MisresolvingKBVerifier:
    """Mock KBVerifier modelling the §3.2 failure mode the persona route
    prevents: the entity resolver resolves the persona name "Asa" to an
    unrelated Wikidata entity (e.g. Asa, King of Judah) and the polarity-aware
    branch CONTRADICTS a negation claim because that wrong entity IS in France.
    If the walker ever delegates a persona-subject claim to KB, this verifier
    fires and the walk false-contradicts."""

    def __init__(self):
        self.call_count = 0

    def verify(self, claim, current_time=None, source_text=None):
        self.call_count += 1
        return KBVerdict(verdict=KBVerdictType.CONTRADICTED, subject_kb_id="Q1online")


def _make_persona_walker(kb_verifier, routing_hint="kb_resolvable",
                         identity_object="Asa", asserting_party="user_test"):
    """Walker with a REAL TierU (so has_identity resolves) seeded with a
    `(party, user, identity, <identity_object>, polarity=1)` persona row."""
    db = open_memory_db()
    transport = MockTransport(routing_hint=routing_hint)
    client = LLMClient(_transport=transport)
    pt = PredicateTranslation(db=db, llm_client=client)

    class StubKB:
        def resolve_entity(self, r, lc): return [ResolutionCandidate("Q1online", score=0.9)]
        def lookup_statements(self, e, p): return []
        def subsumption(self, a, b, rt): return SubsumptionResult(verdict="unrelated")

    resolver = EntityResolver(kb_protocol=StubKB(), db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=StubKB())
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(resolver=resolver, predicate_translation=pt, subsumption=sub, predicate_distribution=pd)
    tier_u = TierU(db=db, predicate_translation=pt)
    # Seed the persona identity row directly (bypass_normalizer keeps the
    # literal "user"/"identity"/object form has_identity matches on).
    identity_claim = Claim(
        claim_id="id1", subject="user", predicate="identity",
        object=identity_object, polarity=1, source_text="seed",
        asserting_party=asserting_party, triage_decision=TriageDecision.VERIFY,
    )
    tier_u.write(identity_claim, bypass_normalizer=True)
    return Walker(
        tier_u=tier_u,
        kb_verifier=kb_verifier,
        python_verifier=PythonVerifier(),
        substrate=substrate,
    )


class TestPersonaSubjectRouting:
    def test_persona_negation_does_not_false_contradict(self):
        # "Asa is not in France" where Asa is a stipulated persona. The KB
        # verifier WOULD contradict (misresolved entity is in France). The
        # walker must route user_authoritative and abstain — never contradict.
        kb = _MisresolvingKBVerifier()
        walker = _make_persona_walker(kb, identity_object="Asa")
        claim = _claim(subject="Asa", predicate="located_in",
                       object_val="France", polarity=0)
        result = walker.walk(claim, _ctx())
        assert result.verdict == "abstained_given_assertion", (
            f"Persona-subject negation must abstain (given-assertion), not "
            f"false-contradict; got {result.verdict}"
        )
        assert kb.call_count == 0, (
            "KB must be structurally unreachable for a persona subject; "
            f"verifier was called {kb.call_count} times"
        )

    def test_persona_positive_claim_routes_user_authoritative(self):
        # A positive persona-subject claim with no Tier U premise abstains as a
        # given-assertion verdict (KB unreachable), never a bare verdict.
        kb = _MisresolvingKBVerifier()
        walker = _make_persona_walker(kb, identity_object="Asa")
        claim = _claim(subject="Asa", predicate="located_in",
                       object_val="France", polarity=1)
        result = walker.walk(claim, _ctx())
        assert result.verdict == "abstained_given_assertion"
        assert kb.call_count == 0

    def test_non_persona_subject_still_reaches_kb(self):
        # A non-persona subject ("Obama") is NOT a stipulated identity, so the
        # persona route does NOT fire and KB grounding remains reachable.
        kb = _MisresolvingKBVerifier()
        walker = _make_persona_walker(kb, identity_object="Asa")
        claim = _claim(subject="Obama", predicate="located_in",
                       object_val="France", polarity=0)
        result = walker.walk(claim, _ctx())
        # KB is reachable for a real (non-persona) subject — verifier fires.
        assert kb.call_count == 1

    def test_persona_scoped_to_asserting_party(self):
        # Persona stipulated for user_test; a DIFFERENT party's claim about
        # "Asa" is not a persona for them → KB reachable.
        kb = _MisresolvingKBVerifier()
        walker = _make_persona_walker(kb, identity_object="Asa",
                                      asserting_party="user_test")
        from datetime import datetime, timezone
        other_ctx = VerificationContext(
            current_time=datetime.now(timezone.utc).isoformat(),
            asserting_party="user_other",
        )
        claim = Claim(
            claim_id="c2", subject="Asa", predicate="located_in",
            object="France", polarity=0, source_text="test",
            asserting_party="user_other", triage_decision=TriageDecision.VERIFY,
        )
        result = walker.walk(claim, other_ctx)
        assert kb.call_count == 1, (
            "Persona is scoped to the stipulating party; a different party's "
            "claim about the same name must still reach KB"
        )


# ---------------------------------------------------------------------------
# TestTierUHasIdentity — v0.16.1 WS5b: the parameterized tier_u.has_identity
# method that replaced the deleted walker._is_persona_subject + its raw SQL.
# Assert the True/False contract directly through the public method (no raw
# SQL, no _db access) — this is the predicate the persona route is built on.
# ---------------------------------------------------------------------------

class TestTierUHasIdentity:
    def _tier_u(self):
        db = open_memory_db()
        transport = MockTransport(routing_hint="user_authoritative")
        client = LLMClient(_transport=transport)
        pt = PredicateTranslation(db=db, llm_client=client)
        tier_u = TierU(db=db, predicate_translation=pt)
        return tier_u

    def _seed_identity(self, tier_u, object_val="Asa", asserting_party="user_test"):
        tier_u.write(
            Claim(
                claim_id="id_seed", subject="user", predicate="identity",
                object=object_val, polarity=1, source_text="seed",
                asserting_party=asserting_party,
                triage_decision=TriageDecision.VERIFY,
            ),
            bypass_normalizer=True,
        )

    def test_returns_true_for_stipulated_persona(self):
        tier_u = self._tier_u()
        self._seed_identity(tier_u, object_val="Asa", asserting_party="user_test")
        assert tier_u.has_identity("user_test", "Asa") is True

    def test_returns_false_for_non_persona_subject(self):
        tier_u = self._tier_u()
        self._seed_identity(tier_u, object_val="Asa", asserting_party="user_test")
        # A different subject is not a stipulated identity for this party.
        assert tier_u.has_identity("user_test", "Obama") is False

    def test_returns_false_for_different_asserting_party(self):
        # Identity is scoped to the stipulating party.
        tier_u = self._tier_u()
        self._seed_identity(tier_u, object_val="Asa", asserting_party="user_test")
        assert tier_u.has_identity("user_other", "Asa") is False

    def test_returns_false_for_empty_subject(self):
        tier_u = self._tier_u()
        self._seed_identity(tier_u, object_val="Asa", asserting_party="user_test")
        assert tier_u.has_identity("user_test", "") is False

    def test_returns_false_when_no_identity_seeded(self):
        tier_u = self._tier_u()
        assert tier_u.has_identity("user_test", "Asa") is False


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


# ---------------------------------------------------------------------------
# TestWalkerAbstentionReasonShortCircuit — v0.16 WS4 (4b): a claim carrying an
# extraction-layer abstention_reason is short-circuited to no_grounding_found
# at walk() entry, BEFORE any Tier U / KB / Python / LLM lookup. This is the
# §3.2-soundness guard: malformed triples (self_referential /
# predicate_eq_object) must never reach a KB lookup that could false-contradict.
# ---------------------------------------------------------------------------

class _CallCountingTierU:
    """Tier U whose lookup methods record whether they were called — to pin
    the pre-lookup guarantee (the short-circuit must fire before any lookup)."""

    def __init__(self):
        self.lookup_calls = 0
        self.object_conflict_calls = 0

    def lookup(self, claim, current_time=None, exclude_row_ids=None):
        self.lookup_calls += 1
        return LookupResult(found=False)

    def lookup_object_conflict(self, claim, current_time=None):
        self.object_conflict_calls += 1
        return LookupResult(found=False)

    def write(self, *a, **kw):
        pass


class _CallCountingKBVerifier:
    def __init__(self):
        self.verify_calls = 0

    def verify(self, claim, current_time=None, source_text=None):
        self.verify_calls += 1
        return KBVerdict(verdict=KBVerdictType.NO_MATCH, subject_kb_id="Q76")


def _make_counting_walker():
    db = open_memory_db()
    transport = MockTransport(routing_hint="kb_resolvable")
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

    tier_u = _CallCountingTierU()
    kb_verifier = _CallCountingKBVerifier()
    walker = Walker(
        tier_u=tier_u,
        kb_verifier=kb_verifier,
        python_verifier=PythonVerifier(),
        substrate=substrate,
    )
    return walker, tier_u, kb_verifier


def _reasoned_claim(abstention_reason, subject="Einstein", predicate="born_in",
                    object_val="Einstein", polarity=1):
    return Claim(
        claim_id="rc1",
        subject=subject,
        predicate=predicate,
        object=object_val,
        polarity=polarity,
        source_text="test",
        asserting_party="user_test",
        triage_decision=TriageDecision.VERIFY,
        abstention_reason=abstention_reason,
    )


class TestWalkerAbstentionReasonShortCircuit:
    def test_abstention_reason_short_circuits_pre_lookup(self):
        # A claim carrying abstention_reason='self_referential' walks to
        # no_grounding_found with the reason echoed, AND neither the Tier U
        # lookup nor the KB verify was EVER called (the pre-lookup guarantee).
        walker, tier_u, kb_verifier = _make_counting_walker()
        claim = _reasoned_claim("self_referential")
        result = walker.walk(claim, _ctx())
        assert result.verdict == "no_grounding_found"
        assert result.abstention_reason == "self_referential"
        assert tier_u.lookup_calls == 0
        assert tier_u.object_conflict_calls == 0
        assert kb_verifier.verify_calls == 0
        # Zero budget consumed — the guard returns before the budget loop.
        assert result.budget_consumption.llm_calls == 0

    def test_predicate_eq_object_short_circuits_pre_lookup(self):
        walker, tier_u, kb_verifier = _make_counting_walker()
        claim = _reasoned_claim("predicate_eq_object", predicate="fell", object_val="fell")
        result = walker.walk(claim, _ctx())
        assert result.verdict == "no_grounding_found"
        assert result.abstention_reason == "predicate_eq_object"
        assert tier_u.lookup_calls == 0
        assert kb_verifier.verify_calls == 0

    def test_not_checkworthy_short_circuits_pre_lookup(self):
        walker, tier_u, kb_verifier = _make_counting_walker()
        claim = _reasoned_claim("not_checkworthy", predicate="is_nice", object_val="pleasant")
        result = walker.walk(claim, _ctx())
        assert result.verdict == "no_grounding_found"
        assert result.abstention_reason == "not_checkworthy"
        assert tier_u.lookup_calls == 0
        assert kb_verifier.verify_calls == 0

    def test_content_less_event_walks_to_no_grounding_never_contradicted(self):
        # v0.16 WS4 Deletion #2 regression: the content-less-event extractor
        # filter is removed; a (World War II, occurred, '') shape now reaches
        # the walker with abstention_reason=None. With an empty object and no
        # KB grounding it must abstain (no_grounding_found) — and crucially
        # must NEVER yield 'contradicted' (the conservative-outcome invariant).
        walker, tier_u, kb_verifier = _make_counting_walker()
        claim = Claim(
            claim_id="cle1",
            subject="World War II",
            predicate="occurred",
            object="",
            polarity=1,
            source_text="World War II occurred",
            asserting_party="user_test",
            triage_decision=TriageDecision.VERIFY,
            abstention_reason=None,
        )
        result = walker.walk(claim, _ctx())
        assert result.verdict != "contradicted"
        assert result.verdict == "no_grounding_found"
