"""Phase H D5 + v0.16 WS2: tests for the walker's KB-neighbor discovery.

Constructed walker scenarios — mocked Substrate + mocked KB adapter —
exercise the KB-neighbor enumeration that fires (as the DISCOVERY
enumerator) when the substrate's subsumption oracle has no neighbors
(cheapest-path-first), is skipped when substrate has neighbors, is
skipped when the walker has no `kb`, and emits trace edges of the
expected shape.

v0.16 WS2 §3/§5 rewrite (gate -> ranker, depth==0 cap removed):
  - `predicate_distribution` is demoted from a GATE to a RANKER.
    A `neither` verdict no longer forecloses the relation; discovery
    is LIBERAL — KB enumeration MAY fire for `neither`. Soundness moved
    entirely to `_verify_chain`: a discovered substitution survives to a
    verdict only if the taxonomy/transitive edge is confirmed in a source
    (and, for intensional is_a, only if the kind-entailment verdict is
    non-`neither`). So a `neither` predicate still ends at
    `no_grounding_found` — but via verify-time rejection / no grounding
    premise, not via a discovery-time skip.
  - The depth==0 cap on KB enumeration is removed; both directions
    (parent via outgoing, child via incoming) now enumerate regardless of
    the distribution verdict — `preferred` only ORDERS the calls. Cost is
    bounded by the per-walk fanout budget (see test_walker_ws2_budget.py),
    not the depth cap.

The MagicMock KB gains a `verify_transitive_path` return (KBProtocol grew
the method in WS2 §1); the substrate-`find_neighbors` admission path routes
through it inside `_verify_chain`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer3_substrate import Substrate
from aedos.layer4_sources.kb_protocol import (
    ResolutionCandidate,
    SubsumptionResult,
    TransitivePathResult,
)
from aedos.layer4_sources.kb_verifier import KBVerdictType, KBVerdict
from aedos.layer4_sources.tier_u import LookupResult
from aedos.layer4_sources.walker import (
    VerificationContext,
    Walker,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _claim(subject="Asa", predicate="lives_in", object_val="Williamstown", polarity=1):
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


class _MockTierU:
    def lookup(self, claim, current_time=None, exclude_row_ids=None):
        return LookupResult(found=False)

    def lookup_object_conflict(self, claim, current_time=None):
        return LookupResult(found=False)


class _NoMatchKBVerifier:
    def verify(self, claim, current_time=None, source_text=None):
        return KBVerdict(verdict=KBVerdictType.NO_MATCH)


def _distribution_verdict(value: str):
    """A minimal mock matching `PredicateDistributionResult` shape — the
    walker reads `verdict.value` and `was_cached` to decide expansion."""
    obj = MagicMock()
    obj.verdict.value = value
    obj.was_cached = True
    return obj


def _make_substrate(
    *,
    distribution_verdict: str = "distributes_down",
    sub_neighbors: list = (),
    sub_neighbors_by_relation: dict | None = None,
    consult_verdict: str = "unrelated",
    resolved_qid: str | None = "Q49112",
):
    """Build a mocked Substrate that:
      - returns `distribution_verdict` from predicate_distribution.consult
      - returns `sub_neighbors` from subsumption.find_neighbors (or, when
        `sub_neighbors_by_relation` is given, keys neighbors per relation_type)
      - returns `consult_verdict` from subsumption.consult (the WS2 §6
        verify-time authority _verify_chain falls back to when the KB
        transitive path can't confirm)
      - resolves any reference to `resolved_qid` (or empty if None)
    """
    pd = MagicMock()
    pd.consult.return_value = _distribution_verdict(distribution_verdict)
    sub = MagicMock()
    if sub_neighbors_by_relation is not None:
        def _find(entity_ref, relation_type):
            return list(sub_neighbors_by_relation.get(relation_type, ()))
        sub.find_neighbors.side_effect = _find
    else:
        sub.find_neighbors.return_value = list(sub_neighbors)
    sub.consult.return_value = SubsumptionResult(verdict=consult_verdict)
    pt = MagicMock()
    # Realistic predicate metadata for the walker's oracle reads
    # (routing_hint / user_subject_required). A bare MagicMock would expose a
    # truthy `user_subject_required`, spuriously tripping the walker's
    # user_subject_required anomaly guard before KB enumeration runs.
    pt_meta = MagicMock()
    pt_meta.routing_hint = "kb_resolvable"
    pt_meta.user_subject_required = False
    pt.consult.return_value = pt_meta
    resolver = MagicMock()
    if resolved_qid is None:
        resolver.resolve.return_value = []
    else:
        resolver.resolve.return_value = [
            ResolutionCandidate(kb_identifier=resolved_qid, score=1.0)
        ]
    return Substrate(
        resolver=resolver,
        predicate_translation=pt,
        subsumption=sub,
        predicate_distribution=pd,
    )


def _make_kb(
    neighbors_by_prop: dict | None = None,
    incoming_by_prop: dict | None = None,
    *,
    path_holds: bool = True,
):
    """A mock KB adapter exposing `enumerate_neighbors`. `neighbors_by_prop`
    is what the outgoing direction returns; `incoming_by_prop` is the
    incoming (D51 reverse). Either defaults to empty per requested
    property.

    v0.16 WS2 §1: the KB also exposes `verify_transitive_path` (KBProtocol
    grew the method) — `_verify_chain` consults it for the SLOT-SUBSTITUTION
    case when both endpoints resolve to Q-ids. `path_holds` controls whether
    the transitive-path ASK is reported as holding (default True).
    """
    kb = MagicMock()

    # v0.16.1 WS5c: the walker now passes the OPAQUE relation_type and the
    # adapter resolves the P-id neighbor set internally. Mirror that mapping
    # here so the mock returns per-property neighbor lists for the requested
    # relation (matching kb_wikidata._NEIGHBOR_PROPERTIES_BY_RELATION).
    _NEIGHBOR_PROPS = {
        "is_a": ("P31", "P279"),
        "part_of": ("P131", "P361", "P17"),
    }

    def fake(entity, properties=None, direction="outgoing", relation_type=None):
        props = tuple(properties) if properties else _NEIGHBOR_PROPS.get(relation_type, ())
        if direction == "incoming":
            if incoming_by_prop is None:
                return {p: [] for p in props}
            return {p: list(incoming_by_prop.get(p, [])) for p in props}
        if neighbors_by_prop is None:
            return {p: [] for p in props}
        return {p: list(neighbors_by_prop.get(p, [])) for p in props}

    kb.enumerate_neighbors.side_effect = fake
    kb.verify_transitive_path.return_value = TransitivePathResult(holds=path_holds)
    return kb


def _make_walker(substrate, kb):
    return Walker(
        tier_u=_MockTierU(),
        kb_verifier=_NoMatchKBVerifier(),
        python_verifier=None,
        substrate=substrate,
        kb=kb,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestD5Fallback:
    def test_fires_when_substrate_empty_and_distribution_down(self):
        """The headline case: subsumption oracle has nothing, so the KB-neighbor
        enumerator fires (cheapest-path-first fallback) and emits expansions.
        v0.16 WS2: with the gate demoted to a ranker, `distributes_down`
        deprioritizes nothing here — it ORDERS the parent direction first; the
        parent (outgoing) enumeration still produces the P131 edge under test."""
        substrate = _make_substrate(
            distribution_verdict="distributes_down",
            sub_neighbors=[],
            resolved_qid="Q49112",
        )
        # Williams College's P131 neighbor is Williamstown (Q771397).
        kb = _make_kb({"P131": ["Q771397"], "P361": [], "P17": []})
        walker = _make_walker(substrate, kb)

        # Run a walk so the KB neighbor enumeration fires during expansion.
        result = walker.walk(_claim(), _ctx())

        # Walker abstains (no premise verifies), but the trace records the
        # KB neighbor enumeration that was attempted.
        kb_edges = [
            e for e in result.trace.edges
            if e.edge_type == "kb_neighbor_enumeration"
        ]
        assert len(kb_edges) >= 1, (
            f"expected at least one kb_neighbor_enumeration edge; "
            f"got {[e.edge_type for e in result.trace.edges]}"
        )
        # The P131 enumeration belongs to the part_of relation. Walker
        # iterates relation_types in order (is_a first), so we look for
        # the part_of edge specifically.
        # Walker recurses across depth iterations, so the same P131
        # enumeration may fire multiple times (once per (slot × depth)
        # combination that produces an expansion). At least one
        # part_of/P131 edge must exist with the expected metadata.
        part_of_p131_edges = [
            e for e in kb_edges
            if e.metadata.get("relation_type") == "part_of"
            and e.metadata.get("kb_property") == "P131"
        ]
        assert part_of_p131_edges, (
            f"expected at least one part_of/P131 edge; "
            f"got relation_types={[e.metadata.get('relation_type') for e in kb_edges]}"
        )
        edge = part_of_p131_edges[0]
        assert edge.metadata["subject_qid"] == "Q49112"
        assert edge.metadata["direction"] == "parent"

    def test_does_not_fire_when_substrate_has_neighbors(self):
        """Cheapest-path-first: if subsumption oracle returns neighbors,
        the D5 fallback should NOT fire — substrate is the cheap path."""
        # Substrate returns one neighbor.
        sub_neighbor = MagicMock()
        sub_neighbor.direction = "parent"
        sub_neighbor.entity.identifier = "Massachusetts"
        sub_neighbor.row_id = 1
        substrate = _make_substrate(
            distribution_verdict="distributes_down",
            sub_neighbors=[sub_neighbor],
        )
        kb = _make_kb({"P131": ["Q771397"]})
        walker = _make_walker(substrate, kb)

        walker.walk(_claim(), _ctx())

        # KB enumerate_neighbors should not have been called.
        kb.enumerate_neighbors.assert_not_called()

    def test_neither_distribution_skips_kb_enumeration(self):
        """Direct-binding-first (Phase E #1): a `neither` distribution
        forecloses every substitution through the relation, so the UNBOUNDED
        KB-neighbor enumeration fallback is SKIPPED — the walker no longer fans
        out over irrelevant neighbors (e.g. P17 country edges off a role claim)
        only to reject them all. The claim's own direct binding stays the
        grounding path; the walk ends at a FAST `no_grounding_found` instead of
        burning the wall-clock to `budget_wall_clock`.

        Verdict-preserving: an is_a `neither` candidate is rejected by
        _verify_chain's kind-entailment gate anyway, and a part_of substitution
        is unsound unless the predicate distributes — so skipping enumeration
        removes only never-grounding work (and a latent false-verify surface),
        never a sound grounding."""
        substrate = _make_substrate(
            distribution_verdict="neither",
            sub_neighbors=[],
        )
        kb = _make_kb({"P131": ["Q771397"], "P17": ["Q30"]})
        walker = _make_walker(substrate, kb)

        result = walker.walk(_claim(), _ctx())

        # The KB-neighbor enumeration fallback must NOT fire for a `neither`
        # predicate — that is the whole point of the gate.
        kb.enumerate_neighbors.assert_not_called()
        assert result.verdict == "no_grounding_found"

    def test_neither_distribution_admits_no_subsumption_substitution(self):
        """v0.16 WS2 §3.2 soundness: with a substrate `is_a` neighbor present
        and a `neither` distribution verdict, `_verify_chain` REJECTS the
        substitution — the kind-entailment authority (distribution=neither)
        says the predicate does not transfer across is_a, even if the
        structural edge holds. No `subsumption_traversal` edge survives, and
        the verdict is `no_grounding_found`. This is the verify-time analog of
        the old closed-gate behavior, proven at the substrate-neighbor path."""
        sub_neighbor = MagicMock()
        sub_neighbor.direction = "parent"
        sub_neighbor.entity.identifier = "dog"
        sub_neighbor.row_id = 7
        substrate = _make_substrate(
            distribution_verdict="neither",
            # Only an is_a neighbor exists, so the is_a kind-entailment gate is
            # the sole admission path under test (no part_of edge to confound).
            sub_neighbors_by_relation={"is_a": [sub_neighbor], "part_of": []},
        )
        # KB transitive path WOULD report the edge as holding, but the is_a
        # kind-entailment gate forecloses it before any structural check.
        kb = _make_kb(path_holds=True)
        walker = _make_walker(substrate, kb)

        result = walker.walk(_claim(predicate="prefers", object_val="dog"), _ctx())

        assert result.verdict == "no_grounding_found"
        assert "subsumption_traversal" not in [
            e.edge_type for e in result.trace.edges
        ]

    def test_fires_reverse_for_distributes_up_after_d51(self):
        """Phase H D51 (2026-05-24): `distributes_up` prefers the 'child'
        direction. D5's outgoing-only used to skip; D51 added reverse
        enumeration so the walker also fires with `direction="incoming"`.
        v0.16 WS2 §3/§5: both directions now enumerate regardless of the
        verdict (the depth cap and direction gate are gone) — `distributes_up`
        merely ORDERS the incoming/child direction first. This test asserts the
        incoming enumeration fires and records `direction='child'`, which holds
        under the ranker semantics (incoming is among the now-unconditional
        directions)."""
        substrate = _make_substrate(
            distribution_verdict="distributes_up",
            sub_neighbors=[],
        )
        # Outgoing has nothing for these properties; incoming returns
        # children of the entity.
        kb = _make_kb(
            neighbors_by_prop=None,
            incoming_by_prop={"P131": ["Q5165"]},  # Williamstown is in this region
        )
        walker = _make_walker(substrate, kb)

        result = walker.walk(_claim(), _ctx())

        # KB enumerate_neighbors should now be called WITH direction="incoming".
        assert kb.enumerate_neighbors.called
        call_directions = [
            (c.kwargs.get("direction") or (c.args[2] if len(c.args) > 2 else "outgoing"))
            for c in kb.enumerate_neighbors.call_args_list
        ]
        assert "incoming" in call_directions, (
            f"expected 'incoming' direction call; got {call_directions!r}"
        )
        # Trace edges should record direction='child'
        child_edges = [
            e for e in result.trace.edges
            if e.edge_type == "kb_neighbor_enumeration"
            and e.metadata.get("direction") == "child"
        ]
        assert child_edges, (
            "expected at least one kb_neighbor_enumeration edge with "
            f"direction='child'; got {[e.metadata for e in result.trace.edges]}"
        )

    def test_does_not_fire_when_walker_has_no_kb(self):
        """Back-compat: Walker constructed without `kb` parameter
        skips D5 entirely. Existing tests that pre-date D5 keep working."""
        substrate = _make_substrate(
            distribution_verdict="distributes_down",
            sub_neighbors=[],
        )
        walker = Walker(
            tier_u=_MockTierU(),
            kb_verifier=_NoMatchKBVerifier(),
            python_verifier=None,
            substrate=substrate,
            kb=None,  # explicit: no D5 fallback
        )

        result = walker.walk(_claim(), _ctx())

        kb_edges = [
            e for e in result.trace.edges
            if e.edge_type == "kb_neighbor_enumeration"
        ]
        assert kb_edges == []

    def test_fires_for_both_distribution_emitting_both_directions(self):
        """`both` direction means parent AND child. D5+D51 should fire
        both an outgoing (parent) and an incoming (child) enumeration."""
        substrate = _make_substrate(
            distribution_verdict="both",
            sub_neighbors=[],
        )
        kb = _make_kb(
            neighbors_by_prop={"P131": ["Q771397"], "P361": [], "P17": []},
            incoming_by_prop={"P131": ["Q5165"]},
        )
        walker = _make_walker(substrate, kb)

        result = walker.walk(_claim(), _ctx())

        # Both direction calls must have happened (across slots × call types).
        call_directions = set(
            c.kwargs.get("direction") or (c.args[2] if len(c.args) > 2 else "outgoing")
            for c in kb.enumerate_neighbors.call_args_list
        )
        assert "outgoing" in call_directions
        assert "incoming" in call_directions

        # Trace should record both direction tags.
        kb_edges = [
            e for e in result.trace.edges
            if e.edge_type == "kb_neighbor_enumeration"
        ]
        emitted_directions = {e.metadata.get("direction") for e in kb_edges}
        assert "parent" in emitted_directions
        assert "child" in emitted_directions

    def test_skips_slot_with_no_resolution(self):
        """If the resolver returns no candidates for a slot, D5 skips
        that slot quietly (no exception, no edge)."""
        substrate = _make_substrate(
            distribution_verdict="distributes_down",
            sub_neighbors=[],
            resolved_qid=None,  # resolver returns []
        )
        kb = _make_kb({"P131": ["Q771397"]})
        walker = _make_walker(substrate, kb)

        result = walker.walk(_claim(), _ctx())

        kb.enumerate_neighbors.assert_not_called()
        kb_edges = [
            e for e in result.trace.edges
            if e.edge_type == "kb_neighbor_enumeration"
        ]
        assert kb_edges == []

    def test_correct_relation_passed_per_relation(self):
        """v0.16.1 WS5c: CORE no longer names P-ids — the walker passes the
        OPAQUE relation_type ("is_a"/"part_of") to enumerate_neighbors and the
        adapter resolves the property set internally. Verify both relations are
        requested through the relation_type keyword."""
        substrate = _make_substrate(
            distribution_verdict="distributes_down",
            sub_neighbors=[],
        )
        kb = _make_kb({})
        walker = _make_walker(substrate, kb)

        # Trigger predicate_distribution to return distributes_down for
        # both relation types — the mock substrate returns it for any
        # consult call. So enumerate_neighbors is called for both is_a
        # and part_of (subject + object × 2 relation_types = up to 4 calls).
        walker.walk(_claim(), _ctx())

        called_relations = {
            call.kwargs.get("relation_type")
            for call in kb.enumerate_neighbors.call_args_list
        }
        # We see calls for both is_a and part_of relation types, and CORE
        # never passes an explicit P-id properties list.
        assert "is_a" in called_relations
        assert "part_of" in called_relations
        for call in kb.enumerate_neighbors.call_args_list:
            # properties is never passed positionally or as a kwarg by CORE.
            assert len(call.args) <= 1
            assert not call.kwargs.get("properties")


# ---------------------------------------------------------------------------
# PATCH-A soundness pins for the KB-neighbor enumeration arm.
# ---------------------------------------------------------------------------

class TestKBEnumCandidateGated:
    """PATCH-A fix (1) / SS3 symmetry: every KB-ENUMERATED candidate is routed
    through the SAME _verify_chain entailment gate the substrate find_neighbors
    arm uses. The single enumeration hop proves the neighbor EXISTS, not that the
    SUBSTITUTION is entailed: an is_a `neither` predicate (kind-entailment says
    the predicate does not transfer across is_a) must REJECT the candidate — it
    must not appear as a surviving kb_neighbor_enumeration edge. An entailed one
    (non-`neither` verdict + holding KB path) still passes."""

    def _is_a_edges(self, result):
        return [
            e for e in result.trace.edges
            if e.edge_type == "kb_neighbor_enumeration"
            and e.metadata.get("relation_type") == "is_a"
        ]

    def test_unentailed_is_a_candidate_rejected(self):
        # `neither` distribution forecloses the is_a substitution. Two layers now
        # ensure no unentailed is_a candidate survives: (1) direct-binding-first
        # skips the KB-neighbor enumeration entirely for a `neither` predicate
        # (so no candidate is even produced), and (2) were it produced,
        # _verify_chain's is_a kind-entailment gate would still REJECT it. Either
        # way no is_a kb_neighbor_enumeration edge survives and the walk abstains.
        substrate = _make_substrate(
            distribution_verdict="neither",
            sub_neighbors=[],
        )
        kb = _make_kb({"P31": ["Q12345"], "P279": [], "P131": [], "P361": [], "P17": []},
                      path_holds=True)
        walker = _make_walker(substrate, kb)

        result = walker.walk(_claim(predicate="prefers"), _ctx())

        assert result.verdict == "no_grounding_found"
        assert self._is_a_edges(result) == [], (
            "an is_a `neither` KB-enum candidate must be rejected by the gate; "
            f"got {[e.metadata for e in self._is_a_edges(result)]}"
        )

    def test_entailed_is_a_candidate_admitted(self):
        # Same shape, but a non-`neither` verdict (distributes_down) passes the
        # is_a kind-entailment gate and the KB transitive path HOLDS — the
        # candidate is admitted and the is_a kb_neighbor_enumeration edge survives.
        # This is the positive control proving the gate is selective, not a
        # blanket suppression of the is_a arm.
        substrate = _make_substrate(
            distribution_verdict="distributes_down",
            sub_neighbors=[],
        )
        kb = _make_kb({"P31": ["Q12345"], "P279": [], "P131": [], "P361": [], "P17": []},
                      path_holds=True)
        walker = _make_walker(substrate, kb)

        result = walker.walk(_claim(predicate="prefers"), _ctx())

        assert self._is_a_edges(result), (
            "an entailed (non-`neither` + holding-path) is_a KB-enum candidate "
            "must be admitted; got no surviving is_a edge"
        )


class TestVerifyChainKBNegativeAuthoritative:
    """PATCH-C r2pa-01 / §3.2 never-false-verify, REFINED. A DEFINITE KB
    transitive NON-HOLD (TransitivePathResult.holds=False, error=None) is an
    authoritative negative against a *cold LLM positive*, but it must NOT
    discard a sound Priority-2 substrate row (operator-seeded / discovered —
    trust ordering: substrate > KB > LLM). So on a definite KB negative
    _verify_chain now FALLS THROUGH to the substrate consult in LLM-EXCLUDED
    mode (`allow_llm=False`):

      * a substrate row that confirms the step STILL admits it (a seeded
        `Williamstown part_of Massachusetts` survives Wikidata's incomplete
        part_of closure);
      * with NO substrate row the LLM Priority-3 is suppressed, so a cold LLM
        guess can never fabricate a positive over the KB negative — the step
        is rejected (§3.2).

    A holding KB path still returns True without any consult. A KB-UNAVAILABLE
    answer (None / fail-open error) still does the FULL consult (LLM included)."""

    def test_definite_kb_negative_admits_when_substrate_row_confirms(self):
        # Definite KB non-hold (holds=False, error=None) + a substrate row that
        # CONFIRMS the step (consult_verdict="a_subsumed_by_b"). Under the
        # round-2 fix this is exactly the scenario that SHOULD verify: the KB
        # negative excludes only the LLM tier; the sound substrate row (trust:
        # substrate > KB) still admits the substitution.
        substrate = _make_substrate(
            distribution_verdict="distributes_down",  # passes the is_a gate
            sub_neighbors=[],
            consult_verdict="a_subsumed_by_b",  # a real substrate row confirms
        )
        kb = _make_kb({"P131": ["Q771397"], "P361": [], "P17": [],
                       "P31": ["Q5"], "P279": []},
                      path_holds=False)  # DEFINITE negative, no error
        walker = _make_walker(substrate, kb)

        result = walker.walk(_claim(), _ctx())

        # The substrate row admits the KB-enum substitution despite the KB
        # transitive-negative.
        kb_edges = [
            e for e in result.trace.edges
            if e.edge_type == "kb_neighbor_enumeration"
        ]
        assert kb_edges, (
            "a definite KB transitive-negative must NOT discard a confirming "
            "substrate row; expected a surviving KB-enum substitution"
        )
        # The fix's heart: the consult IS reached, but in LLM-EXCLUDED mode so
        # only a sound substrate/KB row (never a cold LLM positive) can confirm.
        assert substrate.subsumption.consult.called, (
            "a definite KB negative must fall through to the substrate consult"
        )
        for call in substrate.subsumption.consult.call_args_list:
            assert call.kwargs.get("allow_llm") is False, (
                "the consult on a KB negative must exclude the LLM tier "
                f"(allow_llm=False); got {call.kwargs}"
            )

    def test_definite_kb_negative_rejects_when_only_llm_would_confirm(self):
        # Definite KB non-hold AND no confirming substrate row
        # (consult_verdict="unrelated" — the row tier yields nothing, and the
        # LLM tier is suppressed by allow_llm=False). A cold LLM positive is
        # NEVER admitted over the KB negative (§3.2). Every substitution is
        # rejected → no grounding.
        substrate = _make_substrate(
            distribution_verdict="distributes_down",
            sub_neighbors=[],
            consult_verdict="unrelated",  # no substrate row admits; LLM excluded
        )
        kb = _make_kb({"P131": ["Q771397"], "P361": [], "P17": [],
                       "P31": ["Q5"], "P279": []},
                      path_holds=False)  # DEFINITE negative, no error
        walker = _make_walker(substrate, kb)

        result = walker.walk(_claim(), _ctx())

        assert result.verdict == "no_grounding_found"
        kb_edges = [
            e for e in result.trace.edges
            if e.edge_type == "kb_neighbor_enumeration"
        ]
        assert kb_edges == [], (
            "with no confirming substrate row and the LLM tier suppressed, a "
            f"definite KB negative must reject every substitution; got "
            f"{[e.metadata for e in kb_edges]}"
        )
        # The consult IS reached (substrate tier may confirm) but the LLM tier
        # is excluded — proving a cold LLM positive cannot be admitted.
        assert substrate.subsumption.consult.called
        for call in substrate.subsumption.consult.call_args_list:
            assert call.kwargs.get("allow_llm") is False

    def test_kb_unavailable_does_full_consult_including_llm(self):
        # Positive control for the UNAVAILABLE branch: when the KB transitive
        # ASK fails open (error set → tp treated as no authoritative answer),
        # allow_llm stays True and the consult runs the FULL KB->substrate->LLM
        # resolution. Here the substrate row confirms (consult_verdict admits)
        # and the consult is invoked WITHOUT allow_llm=False.
        substrate = _make_substrate(
            distribution_verdict="distributes_down",
            sub_neighbors=[],
            consult_verdict="a_subsumed_by_b",
        )
        kb = _make_kb({"P131": ["Q771397"], "P361": [], "P17": [],
                       "P31": ["Q5"], "P279": []})
        # Fail-open: the transitive-path ASK reports an ERROR, not a verdict —
        # an UNAVAILABLE answer, so _verify_chain keeps allow_llm=True.
        kb.verify_transitive_path.return_value = TransitivePathResult(
            holds=False, error="simulated SPARQL timeout"
        )
        walker = _make_walker(substrate, kb)

        result = walker.walk(_claim(), _ctx())

        assert substrate.subsumption.consult.called, (
            "a KB-unavailable answer must fall through to the consult"
        )
        # Full consult: the LLM tier is NOT excluded (default True / not False).
        for call in substrate.subsumption.consult.call_args_list:
            assert call.kwargs.get("allow_llm", True) is not False, (
                "a KB-UNAVAILABLE consult must keep the LLM tier available "
                f"(allow_llm must not be False); got {call.kwargs}"
            )

    def test_holding_kb_path_still_admits(self):
        # Positive control: a holding KB transitive path (holds=True) confirms
        # the edge — _verify_chain returns True and the candidate is admitted,
        # again WITHOUT needing the substrate consult.
        substrate = _make_substrate(
            distribution_verdict="distributes_down",
            sub_neighbors=[],
            consult_verdict="unrelated",
        )
        kb = _make_kb({"P131": ["Q771397"], "P361": [], "P17": []},
                      path_holds=True)
        walker = _make_walker(substrate, kb)

        result = walker.walk(_claim(), _ctx())

        part_of_edges = [
            e for e in result.trace.edges
            if e.edge_type == "kb_neighbor_enumeration"
            and e.metadata.get("relation_type") == "part_of"
        ]
        assert part_of_edges, "a holding KB path must admit the candidate"
        substrate.subsumption.consult.assert_not_called()
