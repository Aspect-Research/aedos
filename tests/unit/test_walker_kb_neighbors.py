"""Phase H D5: tests for Walker._expand_via_kb_neighbors.

Constructed walker scenarios — mocked Substrate + mocked KB adapter —
exercise the D5 fallback behavior: it fires when subsumption substrate
is empty, it doesn't fire when substrate has neighbors, it doesn't fire
when the distribution gate is closed, it doesn't fire when the
walker's `kb` parameter is None, and the emitted trace edges have the
expected shape.
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
)
from aedos.layer4_sources.kb_verifier import KBVerdictType, KBVerdict
from aedos.layer4_sources.tier_u import LookupResult
from aedos.layer4_sources.walker import (
    VerificationContext,
    Walker,
    _D5_NEIGHBOR_PROPS_BY_RELATION,
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
    def lookup(self, claim, current_time=None):
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
    resolved_qid: str | None = "Q49112",
):
    """Build a mocked Substrate that:
      - returns `distribution_verdict` from predicate_distribution.consult
      - returns `sub_neighbors` from subsumption.find_neighbors
      - resolves any reference to `resolved_qid` (or empty if None)
    """
    pd = MagicMock()
    pd.consult.return_value = _distribution_verdict(distribution_verdict)
    sub = MagicMock()
    sub.find_neighbors.return_value = list(sub_neighbors)
    resolver = MagicMock()
    if resolved_qid is None:
        resolver.resolve.return_value = []
    else:
        resolver.resolve.return_value = [
            ResolutionCandidate(kb_identifier=resolved_qid, score=1.0)
        ]
    pt = MagicMock()
    return Substrate(
        resolver=resolver,
        predicate_translation=pt,
        subsumption=sub,
        predicate_distribution=pd,
    )


def _make_kb(
    neighbors_by_prop: dict | None = None,
    incoming_by_prop: dict | None = None,
):
    """A mock KB adapter exposing `enumerate_neighbors`. `neighbors_by_prop`
    is what the outgoing direction returns; `incoming_by_prop` is the
    incoming (D51 reverse). Either defaults to empty per requested
    property."""
    kb = MagicMock()

    def fake(entity, properties, direction="outgoing"):
        if direction == "incoming":
            if incoming_by_prop is None:
                return {p: [] for p in properties}
            return {p: list(incoming_by_prop.get(p, [])) for p in properties}
        if neighbors_by_prop is None:
            return {p: [] for p in properties}
        return {p: list(neighbors_by_prop.get(p, [])) for p in properties}

    kb.enumerate_neighbors.side_effect = fake
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
        """The headline case: subsumption oracle has nothing, distribution
        gate is open in the 'parent' direction → D5 enumerates KB
        neighbors and emits expansions."""
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

    def test_does_not_fire_when_distribution_gate_closed(self):
        """When predicate_distribution returns 'neither' (gate closed),
        no expansion happens at all — D5 doesn't get a chance to fire."""
        substrate = _make_substrate(
            distribution_verdict="neither",
            sub_neighbors=[],
        )
        kb = _make_kb({"P131": ["Q771397"]})
        walker = _make_walker(substrate, kb)

        walker.walk(_claim(), _ctx())

        kb.enumerate_neighbors.assert_not_called()

    def test_fires_reverse_for_distributes_up_after_d51(self):
        """Phase H D51 (2026-05-24): `distributes_up` → directions={'child'}.
        D5's outgoing-only used to skip; D51 adds reverse enumeration so
        the walker fires with `direction="incoming"` instead."""
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

    def test_correct_properties_passed_per_relation(self):
        """is_a uses (P31, P279); part_of uses (P131, P361, P17). Verify
        the right property tuple reaches enumerate_neighbors."""
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

        called_properties = [
            call.args[1] for call in kb.enumerate_neighbors.call_args_list
        ]
        # We see calls for both is_a and part_of relation types.
        assert ["P31", "P279"] in called_properties
        assert ["P131", "P361", "P17"] in called_properties
