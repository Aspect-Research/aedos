"""Integration: Walker with real substrate (mocked oracles), multi-source chains."""

from __future__ import annotations

import pytest

from aedos.database import open_memory_db
from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer3_substrate import Substrate
from aedos.layer3_substrate.predicate_distribution import PredicateDistributionOracle
from aedos.layer3_substrate.predicate_translation import PredicateTranslation
from aedos.layer3_substrate.resolver import EntityResolver
from aedos.layer3_substrate.subsumption import SubsumptionOracle
from aedos.layer4_sources.kb_protocol import (
    ResolutionCandidate,
    Statement,
    SubsumptionResult,
    TransitivePathResult,
)
from aedos.layer4_sources.kb_verifier import KBVerdictType, KBVerifier
from aedos.layer4_sources.python_verifier import PythonVerifier
from aedos.layer4_sources.tier_u import TierU
from aedos.layer4_sources.walker import VerificationContext, Walker, WalkerBudget
from aedos.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RESOLUTIONS = {"Obama": "Q76", "President": "Q11696", "Honolulu": "Q18094",
                "Berlin": "Q64", "Germany": "Q183",
                "Williamstown": "Q771397", "Massachusetts": "Q771",
                "United States": "Q30"}


class MockTransport:
    def __init__(self, kb_property="P39", single_valued=0, slot_to_qualifier=None):
        self._prop = kb_property
        self._single_valued = single_valued
        self._sq = slot_to_qualifier

    def extract_with_tool(self, *a, purpose=None, **kw):
        if purpose == "substrate:predicate_distribution":
            return {"verdict": "neither", "reason": "test"}
        if purpose == "substrate:subsumption":
            return {"verdict": "unrelated", "reason": "test"}
        return {
            "object_type": "entity",
            "user_subject_required": 0,
            "distinct_slots": None,
            "routing_hint": "kb_resolvable",
            "kb_namespace": "wikidata",
            "kb_property": self._prop,
            "slot_to_qualifier": self._sq,
            "single_valued": self._single_valued,
            "reason": "test",
        }
    def chat(self, *a, **kw): return ""


class MockKB:
    def __init__(self, stmts=None, transitive_paths=None):
        self._stmts = stmts or []
        # v0.16 WS2 §1: (source_qid, target_qid) pairs whose part_of/is_a
        # transitive path HOLDS. Hand-rolled stub must implement the new
        # KBProtocol method (MagicMock-based stubs get it free).
        self._transitive_paths = set(transitive_paths or ())
    def resolve_entity(self, r, lc):
        qid = _RESOLUTIONS.get(r)
        return [ResolutionCandidate(qid, score=0.9)] if qid else []
    def lookup_statements(self, e, p): return list(self._stmts)
    def subsumption(self, a, b, rt): return SubsumptionResult(verdict="unrelated")
    def verify_transitive_path(self, source, target, kb_property, relation_type=None):
        return TransitivePathResult(holds=(source, target) in self._transitive_paths)


def _make_full_system(kb_stmts=None, kb_property="P39", single_valued=0,
                      slot_to_qualifier=None, kb=None, wire_kb=False):
    db = open_memory_db()
    client = LLMClient(_transport=MockTransport(kb_property, single_valued, slot_to_qualifier))
    kb = kb or MockKB(kb_stmts)
    pt = PredicateTranslation(db=db, llm_client=client)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(resolver=resolver, predicate_translation=pt, subsumption=sub, predicate_distribution=pd)
    tier_u = TierU(db=db, predicate_translation=pt)
    # Phase H Cluster 2 step 3: tests in this file are about walker
    # mechanics (Tier U match, KB grounding, subsumption derivation,
    # belief revision), not about user-assertion-source semantics. The
    # rows they write represent "established prior Tier U state",
    # which is `externally_verified` under Cluster 2. Override write's
    # default so each tier_u.write(claim) keeps its pre-Cluster-2
    # intent. Tests that specifically need asserted_unverified
    # semantics pass `status='asserted_unverified'` explicitly.
    _orig_write = tier_u.write
    def _write_external(claim, source_context=None, status="externally_verified"):
        return _orig_write(claim, source_context=source_context, status=status)
    tier_u.write = _write_external  # type: ignore[method-assign]
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    py_verifier = PythonVerifier()
    # v0.16 WS2 §4: premise-forward + KB-neighbor discovery require the walker's
    # `kb` to be wired. Most mechanics tests construct the walker without it
    # (kb=None disables KB-side discovery); premise-forward tests pass
    # wire_kb=True to drive the new bidirectional-meet grounding.
    walker = Walker(
        tier_u=tier_u, kb_verifier=kb_verifier, python_verifier=py_verifier,
        substrate=substrate, kb=(kb if wire_kb else None),
    )
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
        result = walker.walk(_claim(object_val="President"), _ctx())
        assert result.verdict == "verified"

    def test_kb_contradiction_returns_contradicted(self):
        # A functional predicate (born_in) with a non-matching KB statement
        # genuinely contradicts the claim and the verdict flows to the walker.
        stmts = [Statement(value="Q11696", value_type="entity")]
        walker, _, _ = _make_full_system(kb_stmts=stmts, kb_property="P19", single_valued=1)
        result = walker.walk(_claim(predicate="born_in", object_val="Honolulu"), _ctx())
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
        from aedos.layer5_result.trace import trace_to_json
        walker, tier_u, _ = _make_full_system()
        claim = _claim()
        tier_u.write(claim)
        result = walker.walk(claim, _ctx())
        d = trace_to_json(result.trace)
        json.dumps(d)


# ---------------------------------------------------------------------------
# Fix-up (C2, M3): derivation, polarity, and conflict-detection integration
# tests. Cases are sourced from tests/calibration/derivation_corpus.jsonl.
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

    def test_distribution_neither_rejected_at_verify_time(self):
        # der_multihop_009: `prefers` does NOT distribute over is_a. Even with a
        # subsumption row golden_retriever is_a dog (the structural edge HOLDS),
        # the substitution must not ground the claim.
        #
        # v0.16 WS2 §3.2 (gate -> ranker): pre-v0.16 the distribution `neither`
        # verdict was a GATE that skipped the relation outright. Now the relation
        # is EXPLORED (distribution is a ranker), but `_verify_chain` REJECTS the
        # is_a substitution because the kind-entailment authority
        # (distribution=neither) says `prefers` does not transfer across is_a.
        # OUTCOME is identical to the old gate (no_grounding_found, no surviving
        # subsumption_traversal edge) — reached soundly, at verify time.
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


class TestWalkerPremiseForward:
    """v0.16 WS2 §4: premise-forward / bidirectional-meet search.

    The walker seeds a forward frontier from a Tier U premise about the goal's
    subject (surfaced by `lookup_object_conflict` as a DIFFERENT object O′),
    resolves O′ and the goal object O to Q-ids, and confirms O is reachable from
    O′ via the part_of transitive path. When it grounds, it emits a
    `premise_forward` trace edge and substitutes O→O′ so the walk re-looks-up
    the grounded premise. This grounds WITHOUT any seeded subsumption /
    distribution substrate rows — the KB transitive primitive is the evidence."""

    def test_premise_forward_meets_goal(self):
        # Tier U: lives_in(Asa, Williamstown). Goal: lives_in(Asa, Massachusetts).
        # Williamstown part_of Massachusetts (KB transitive path holds). No
        # subsumption/distribution rows are seeded — premise-forward is the
        # only path that can ground this.
        kb = MockKB(transitive_paths={("Q771397", "Q771")})  # Williamstown ⊂ MA
        walker, tier_u, db = _make_full_system(kb=kb, wire_kb=True)
        tier_u.write(_claim(subject="Asa", predicate="lives_in", object_val="Williamstown"))
        goal = _claim(subject="Asa", predicate="lives_in", object_val="Massachusetts")
        result = walker.walk(goal, _ctx())
        assert result.verdict == "verified"
        edge_types = [e.edge_type for e in result.trace.edges]
        assert "premise_forward" in edge_types

    def test_premise_forward_abstains_when_path_does_not_hold(self):
        # Same shape, but the KB transitive path does NOT hold (Williamstown is
        # NOT part_of Germany). Premise-forward must not ground it — abstain,
        # never false-verify (§3.2).
        kb = MockKB(transitive_paths=set())  # no path holds
        walker, tier_u, db = _make_full_system(kb=kb, wire_kb=True)
        tier_u.write(_claim(subject="Asa", predicate="lives_in", object_val="Williamstown"))
        goal = _claim(subject="Asa", predicate="lives_in", object_val="Germany")
        result = walker.walk(goal, _ctx())
        assert result.verdict == "no_grounding_found"
        assert "premise_forward" not in [e.edge_type for e in result.trace.edges]


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


class TestWalkerObjectConflictVerdicts:
    """B2 / D16: object-conflict belief revision. A functional (single_valued)
    predicate admits at most one object value per subject, so a Tier U row
    asserting a different value contradicts a positive claim. Multi-valued
    predicates do not — a different value is a parallel assertion."""

    def test_functional_object_conflict_is_contradicted(self):
        # Tier U holds (Asa, lives_in, NYC); single_valued=1 makes lives_in
        # functional, so a claimed different residence is contradicted.
        walker, tier_u, _ = _make_full_system(single_valued=1)
        tier_u.write(_claim(subject="Asa", predicate="lives_in", object_val="NYC", polarity=1))
        goal = _claim(subject="Asa", predicate="lives_in", object_val="Boston", polarity=1)
        result = walker.walk(goal, _ctx())
        assert result.verdict == "contradicted"
        markers = [e.metadata.get("belief_revision") for e in result.trace.edges]
        assert "object_conflict" in markers

    def test_multi_valued_object_difference_is_not_contradicted(self):
        # occupation is multi-valued (single_valued=0): a person may hold
        # several, so a different occupation does not fire the object-conflict
        # path — it abstains rather than contradicting.
        walker, tier_u, _ = _make_full_system(single_valued=0)
        tier_u.write(_claim(subject="Asa", predicate="occupation", object_val="teacher", polarity=1))
        goal = _claim(subject="Asa", predicate="occupation", object_val="lawyer", polarity=1)
        result = walker.walk(goal, _ctx())
        assert result.verdict == "no_grounding_found"
        markers = [e.metadata.get("belief_revision") for e in result.trace.edges]
        assert "object_conflict" not in markers

    def test_polarity_conflict_records_trace_marker(self):
        # Regression: the polarity-flip path still contradicts, and now tags its
        # trace edge so Phase 10.5 can tell polarity revision from object
        # conflict.
        walker, tier_u, _ = _make_full_system()
        tier_u.write(_claim(subject="Asa", predicate="holds_role", object_val="student", polarity=1))
        goal = _claim(subject="Asa", predicate="holds_role", object_val="student", polarity=0)
        result = walker.walk(goal, _ctx())
        assert result.verdict == "contradicted"
        markers = [e.metadata.get("belief_revision") for e in result.trace.edges]
        assert "polarity_conflict" in markers

    def test_negated_claim_against_functional_prior_abstains(self):
        # Decision 1 (conservative): Tier U holds (Asa, lives_in, NYC); a
        # negated claim about a DIFFERENT residence is logically implied by the
        # functional prior, but Phase B does not verify it — the negated-claim
        # direction falls through to abstain rather than firing belief revision.
        walker, tier_u, _ = _make_full_system(single_valued=1)
        tier_u.write(_claim(subject="Asa", predicate="lives_in", object_val="NYC", polarity=1))
        goal = _claim(subject="Asa", predicate="lives_in", object_val="Boston", polarity=0)
        result = walker.walk(goal, _ctx())
        assert result.verdict == "no_grounding_found"
        markers = [e.metadata.get("belief_revision") for e in result.trace.edges]
        assert "object_conflict" not in markers

    def test_both_negative_object_difference_is_not_contradicted(self):
        # Polarity guard: two negative assertions about different objects of a
        # functional predicate are consistent ("not in NYC" and "not in Boston"
        # can both hold). The object-conflict path must not fire — it is
        # guarded to positive claims.
        walker, tier_u, _ = _make_full_system(single_valued=1)
        tier_u.write(_claim(subject="Asa", predicate="lives_in", object_val="NYC", polarity=0))
        goal = _claim(subject="Asa", predicate="lives_in", object_val="Boston", polarity=0)
        result = walker.walk(goal, _ctx())
        assert result.verdict != "contradicted"
        markers = [e.metadata.get("belief_revision") for e in result.trace.edges]
        assert "object_conflict" not in markers


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


class TestWalkerKBLookupInverted:
    """R1: the walker copies the D19 ``lookup_inverted`` flag from
    ``KBVerdict.trace`` onto the KB ``premise_lookup`` trace edge, so the
    result-level trace records which KB lookups took the inverted path.
    Pre-R1 the KB edge metadata had no ``lookup_inverted`` key."""

    @staticmethod
    def _kb_edge(result):
        for e in result.trace.edges:
            if e.edge_type == "premise_lookup" and e.metadata.get("source") == "kb":
                return e
        return None

    def test_inverse_predicate_edge_records_lookup_inverted_true(self):
        # capital_of is inverse-mapped (subject -> statement_value): the KB
        # statement `Germany P36 Berlin` verifies capital_of(Berlin, Germany).
        stmts = [Statement(value="Q64", value_type="entity")]  # Berlin
        walker, _, _ = _make_full_system(
            kb_stmts=stmts, kb_property="P36",
            slot_to_qualifier={"subject": "statement_value", "object": "statement_subject"},
        )
        result = walker.walk(
            _claim(subject="Berlin", predicate="capital_of", object_val="Germany"), _ctx()
        )
        assert result.verdict == "verified"
        edge = self._kb_edge(result)
        assert edge is not None
        assert edge.metadata.get("lookup_inverted") is True

    def test_standard_predicate_edge_records_lookup_inverted_false(self):
        # holds_role is standard-mapped: lookup_inverted is False on the edge.
        stmts = [Statement(value="Q11696", value_type="entity")]
        walker, _, _ = _make_full_system(kb_stmts=stmts)
        result = walker.walk(_claim(object_val="President"), _ctx())
        assert result.verdict == "verified"
        edge = self._kb_edge(result)
        assert edge is not None
        assert edge.metadata.get("lookup_inverted") is False
