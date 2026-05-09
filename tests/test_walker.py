"""Walker tests (v0.14 Phase 7d).

Pins the tier composition contracts:

  * Tier order: U → W → derivation → fresh.
  * Routing anomaly short-circuits.
  * Tier U MATCH terminates with verification_status='user_asserted'.
  * Tier U CONTRADICTION terminates with verification_status='user_asserted'.
  * Tier W MATCH with terminal status terminates.
  * Tier W MATCH with fall-through status (retrieval_inconclusive,
    retrieval_failed, unverifiable_pending_implementation) advances
    to derivation; notes record the cached row's status.
  * Tier W CONTRADICTION terminates (regardless of inner verdict).
  * Derivation MATCH terminates with verification_status='verified'.
  * Fresh dispatcher invoked when all earlier tiers miss.
  * routing_method propagates unchanged from Layer 2 to WalkerDecision.
  * walker_decision pipeline event fires once per claim.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.fact_store import Fact, FactStore
from src.layer1_extraction.pattern_registry import (
    PatternRegistry,
)
from src.layer2_routing.types import (
    Decision,
    RoutingOutcome,
    ValidationResult,
)
from src.layer3_substrate.entity_equivalence import (
    EntityEquivalence,
)
from src.layer3_substrate.entity_taxonomy import (
    EntityTaxonomy,
)
from src.layer3_substrate.predicate_distribution import (
    PredicateDistribution,
)
from src.layer3_substrate.predicate_equivalence import (
    PredicateEquivalence,
)
from src.layer4_lookup import tier_w
from src.layer4_lookup.types import (
    LookupOutcome,
    WalkerDecision,
)
from src.layer4_lookup.walker import walk_claim


_PATTERNS_PATH = (
    Path(__file__).parent.parent
    / "src" / "layer1_extraction" / "patterns.yaml"
)


@pytest.fixture(scope="module")
def registry() -> PatternRegistry:
    return PatternRegistry.from_yaml(_PATTERNS_PATH)


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "walker.db")
    yield s
    s.close()


@pytest.fixture
def oracles(store):
    return {
        "predicate_oracle": PredicateEquivalence(store),
        "entity_oracle": EntityEquivalence(store),
        "taxonomy_oracle": EntityTaxonomy(store),
        "distribution_oracle": PredicateDistribution(store),
    }


def _seed_tier_w_row_directly(
    store, registry, claim, verification_status, *,
    stability_class="decade_stable",
):
    """v0.14.1: write_verifier_result no longer persists non-actionable
    statuses (retrieval_inconclusive, retrieval_failed,
    unverifiable_pending_implementation, unverifiable_in_principle).
    Tests that need such a row to exist in Tier W (to exercise the
    walker's defense-in-depth filter for legacy / edge-case data) seed
    via direct SQL, bypassing the policy guard. Returns the canonical
    key for the inserted row."""
    canonical_key = tier_w.canonicalize_claim_key(claim, registry)
    pattern = claim.get("pattern", "")
    predicate = (claim.get("predicate") or "").strip().lower()
    store._conn.execute(
        "INSERT INTO verification_cache (canonical_key, pattern, predicate, "
        "verdict, evidence, stability_class, cached_at, created_at, "
        "expires_at, hit_count, refresh_count, contradiction_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (canonical_key, pattern, predicate, verification_status,
         "{}", stability_class,
         "2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00",
         None, 0, 0, 0),
    )
    store._conn.commit()
    return canonical_key


def _classified_decision(claim, method="retrieval"):
    """Build a Layer-2 Decision for a CLASSIFIED claim."""
    return Decision(
        claim=claim,
        outcome=RoutingOutcome.CLASSIFIED,
        method=method,
        reason="test",
        memo_hit=False,
        validation=ValidationResult.passed(),
        routing_decision={"method": method, "reason": "test"},
    )


def _anomaly_decision(claim):
    """Build a Layer-2 Decision for a ROUTING_ANOMALY claim."""
    return Decision(
        claim=claim,
        outcome=RoutingOutcome.ROUTING_ANOMALY,
        method=None,
        reason=None,
        memo_hit=False,
        validation=ValidationResult.anomaly(
            invariant="user_subject_pattern",
            slot="agent",
            expected="user",
            actual="Donald Trump",
        ),
    )


def _store_user_fact(store, *, pattern, predicate, slots, polarity=1):
    fid = store.insert_fact(Fact(
        pattern=pattern, predicate=predicate, slots=dict(slots),
        polarity=polarity, asserted_by="user",
        verification_status="user_asserted",
    ))
    return store.get_fact(fid)


# ============================================================================
# Routing-anomaly short-circuit
# ============================================================================


def test_routing_anomaly_short_circuit(store, registry, oracles):
    claim = {
        "pattern": "preference",
        "predicate": "likes",
        "polarity": 1,
        "slots": {"agent": "Donald Trump", "object": "peanut butter"},
        "source_text": "Donald Trump likes peanut butter",
    }
    decision = walk_claim(
        claim, _anomaly_decision(claim), store, registry=registry,
        **oracles,
    )
    assert decision.served_from_tier == "routing_anomaly"
    assert decision.verification_status == "routing_anomaly"
    assert decision.outcome is LookupOutcome.MISS
    assert decision.routing_method is None
    # No tier lookup ran — no tier_u/tier_w events fired here.


# ============================================================================
# Tier U resolves
# ============================================================================


def test_tier_u_match(store, registry, oracles):
    _store_user_fact(
        store, pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"},
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "polarity": 1,
        "slots": {"agent": "user", "object": "olives"},
        "source_text": "you like olives",
    }
    decision = walk_claim(
        claim, _classified_decision(claim, method="user_authoritative"),
        store, registry=registry, **oracles,
    )
    assert decision.served_from_tier == "u"
    assert decision.outcome is LookupOutcome.MATCH
    assert decision.verification_status == "user_asserted"
    assert decision.routing_method == "user_authoritative"
    assert decision.matching_fact_id is not None


def test_tier_u_contradiction(store, registry, oracles):
    _store_user_fact(
        store, pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"},
        polarity=1,
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "polarity": 0,  # opposite of stored
        "slots": {"agent": "user", "object": "olives"},
        "source_text": "you don't like olives",
    }
    decision = walk_claim(
        claim, _classified_decision(claim), store,
        registry=registry, **oracles,
    )
    assert decision.served_from_tier == "u"
    assert decision.outcome is LookupOutcome.CONTRADICTION
    assert decision.verification_status == "user_asserted"
    assert decision.contradicting_fact_id is not None


def test_tier_u_cheetahs_polarity_flip(store, registry, oracles):
    """Phase 3's cheetahs case via the walker."""
    _store_user_fact(
        store, pattern="preference", predicate="dislikes",
        slots={"agent": "user", "object": "cheetahs"},
        polarity=1,
    )
    oracles["predicate_oracle"].record(
        "preference", "likes", "dislikes",
        label="contradictory", slot_reversal="none",
        reason="antonym preference predicates",
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "polarity": 0,
        "slots": {"agent": "user", "object": "cheetahs"},
        "source_text": "you don't like cheetahs",
    }
    decision = walk_claim(
        claim, _classified_decision(claim), store,
        registry=registry, **oracles,
    )
    assert decision.served_from_tier == "u"
    assert decision.outcome is LookupOutcome.MATCH
    assert decision.polarity_flipped is True
    assert decision.via == ["predicate_equivalence"]


# ============================================================================
# Tier W resolves
# ============================================================================


def test_tier_w_match_verified_terminates(store, registry, oracles):
    cached_claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "polarity": 1,
        "slots": {"entity": "Tokyo", "location": "Japan"},
        "source_text": "Tokyo is in Japan",
    }
    tier_w.write_verifier_result(
        cached_claim, store,
        verification_status="verified",
        registry=registry,
    )
    decision = walk_claim(
        cached_claim, _classified_decision(cached_claim, method="retrieval"),
        store, registry=registry, **oracles,
    )
    assert decision.served_from_tier == "w"
    assert decision.outcome is LookupOutcome.MATCH
    assert decision.verification_status == "verified"
    assert decision.routing_method == "retrieval"


def test_tier_w_match_contradicted_terminates(store, registry, oracles):
    """A W row with verification_status='contradicted' is terminal —
    the walker doesn't fall through, even though the cached verdict
    means the claim was contradicted."""
    cached_claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "polarity": 1,
        "slots": {"entity": "X", "location": "Y"},
        "source_text": "X in Y",
    }
    tier_w.write_verifier_result(
        cached_claim, store,
        verification_status="contradicted",
        registry=registry,
    )
    decision = walk_claim(
        cached_claim, _classified_decision(cached_claim),
        store, registry=registry, **oracles,
    )
    assert decision.served_from_tier == "w"
    assert decision.outcome is LookupOutcome.MATCH
    assert decision.verification_status == "contradicted"


def test_tier_w_inconclusive_falls_through_to_derivation(
    store, registry, oracles,
):
    """A cached retrieval_inconclusive row should NOT terminate;
    the walker falls through to derivation, and notes record the
    cached status."""
    cached_claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "polarity": 1,
        "slots": {"entity": "user", "location": "Massachusetts"},
        "source_text": "user lives in Massachusetts",
    }
    # v0.14.1 — direct SQL insert; the public write path skips
    # non-actionable statuses now. The walker still needs to handle
    # such rows defensively (legacy data / edge cases).
    _seed_tier_w_row_directly(
        store, registry, cached_claim,
        verification_status="retrieval_inconclusive",
    )
    # Set up a derivation chain that DOES match: pre-warm
    # entity_taxonomy + predicate_distribution + a Tier U fact.
    _store_user_fact(
        store, pattern="spatial_temporal", predicate="lives_in",
        slots={"entity": "user", "location": "Williamstown"},
        polarity=1,
    )
    oracles["taxonomy_oracle"].record(
        "Williamstown", "Massachusetts", "part_of",
        label="child_subsumed_by_parent", reason="town in state",
    )
    oracles["distribution_oracle"].record(
        "spatial_temporal", "lives_in", 1, "part_of",
        label="distributes_up", reason="residence aggregates",
    )
    # Pre-warm entity_equivalence so Tier U stage-3 alias broadening
    # doesn't trigger an LLM call (no LLM in this test). The cached
    # 'different' verdict prevents a false alias-match between
    # Massachusetts and Williamstown — they are distinct entities
    # under entity_equivalence's contract; their part_of relationship
    # is what the entity_taxonomy oracle handles.
    oracles["entity_oracle"].record(
        "Massachusetts", "Williamstown", "different",
        reason="containment, not equivalence — handled by entity_taxonomy",
    )
    decision = walk_claim(
        cached_claim, _classified_decision(cached_claim),
        store, registry=registry, **oracles,
    )
    assert decision.served_from_tier == "derivation"
    assert decision.outcome is LookupOutcome.MATCH
    assert decision.verification_status == "verified"
    # The fall-through note should be present.
    assert any(
        "tier_w cached" in note and "retrieval_inconclusive" in note
        for note in decision.notes
    ), f"expected fall-through note; got notes: {decision.notes}"


def test_tier_w_failed_falls_through_to_derivation(
    store, registry, oracles,
):
    """retrieval_failed also falls through (verifier broke; substrate
    might still derive a verdict)."""
    cached_claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "polarity": 1,
        "slots": {"entity": "user", "location": "Massachusetts"},
        "source_text": "user lives in Massachusetts",
    }
    _seed_tier_w_row_directly(
        store, registry, cached_claim,
        verification_status="retrieval_failed",
    )
    # No supporting derivation chain → walker falls through to fresh
    # (which has no dispatcher; produces unverifiable_pending_implementation).
    decision = walk_claim(
        cached_claim, _classified_decision(cached_claim),
        store, registry=registry, **oracles,
    )
    # served_from_tier should NOT be "w" because of fall-through;
    # derivation finds nothing; fresh stub fires.
    assert decision.served_from_tier == "fresh"
    assert any(
        "retrieval_failed" in note for note in decision.notes
    )


def test_tier_w_unverifiable_in_principle_terminal(
    store, registry, oracles,
):
    cached_claim = {
        "pattern": "preference", "predicate": "loves",
        "polarity": 1,
        "slots": {"agent": "user", "object": "olives"},
        "source_text": "user loves olives",
    }
    _seed_tier_w_row_directly(
        store, registry, cached_claim,
        verification_status="unverifiable_in_principle",
    )
    decision = walk_claim(
        cached_claim, _classified_decision(cached_claim,
                                           method="unverifiable"),
        store, registry=registry, **oracles,
    )
    assert decision.served_from_tier == "w"
    assert decision.outcome is LookupOutcome.MATCH
    assert decision.verification_status == "unverifiable_in_principle"


# ============================================================================
# Derivation resolves
# ============================================================================


def test_derivation_match_williamstown(store, registry, oracles):
    _store_user_fact(
        store, pattern="spatial_temporal", predicate="lives_in",
        slots={"entity": "user", "location": "Williamstown"},
        polarity=1,
    )
    oracles["taxonomy_oracle"].record(
        "Williamstown", "Massachusetts", "part_of",
        label="child_subsumed_by_parent", reason="town in state",
    )
    oracles["distribution_oracle"].record(
        "spatial_temporal", "lives_in", 1, "part_of",
        label="distributes_up", reason="residence aggregates",
    )
    # Pre-warm entity_equivalence so Tier U stage-3 doesn't trigger LLM.
    oracles["entity_oracle"].record(
        "Massachusetts", "Williamstown", "different",
        reason="containment relationship; not entity equivalence",
    )
    claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "polarity": 1,
        "slots": {"entity": "user", "location": "Massachusetts"},
        "source_text": "you live in Massachusetts",
    }
    decision = walk_claim(
        claim, _classified_decision(claim), store,
        registry=registry, **oracles,
    )
    assert decision.served_from_tier == "derivation"
    assert decision.outcome is LookupOutcome.MATCH
    assert decision.verification_status == "verified"
    assert decision.derivation_path != []
    assert "entity_taxonomy" in decision.via
    assert "predicate_distribution" in decision.via


# ============================================================================
# Fresh dispatcher integration
# ============================================================================


def test_fresh_dispatcher_called_when_all_earlier_tiers_miss(
    store, registry, oracles,
):
    """The walker invokes the fresh_dispatch callable when U, W, and
    derivation all miss. The dispatcher returns whatever WalkerDecision
    it builds; the walker propagates routing_method into it."""
    calls: list[dict] = []

    def fresh_stub(claim, *, routing_method, store, registry,
                   llm, source_turn_id, user_id, current_session,
                   prior_notes):
        calls.append({"claim": claim, "method": routing_method})
        return WalkerDecision(
            claim=claim,
            served_from_tier="fresh",
            outcome=LookupOutcome.MATCH,
            verification_status="verified",
            routing_method=None,  # walker should fill in
            chain_reliability=0.85,
            evidence={"snippets": []},
            notes=list(prior_notes) + ["dispatcher fired"],
        )

    claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "polarity": 1,
        "slots": {"entity": "user", "location": "Mars"},
        "source_text": "user lives on Mars",
    }
    decision = walk_claim(
        claim, _classified_decision(claim, method="retrieval"),
        store, registry=registry, fresh_dispatch=fresh_stub,
        **oracles,
    )
    assert len(calls) == 1
    assert calls[0]["method"] == "retrieval"
    assert decision.served_from_tier == "fresh"
    assert decision.outcome is LookupOutcome.MATCH
    assert decision.verification_status == "verified"
    # routing_method propagated even though the dispatcher set None.
    assert decision.routing_method == "retrieval"


def test_no_fresh_dispatcher_emits_pending_placeholder(
    store, registry, oracles,
):
    claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "polarity": 1,
        "slots": {"entity": "user", "location": "Mars"},
        "source_text": "user lives on Mars",
    }
    decision = walk_claim(
        claim, _classified_decision(claim), store,
        registry=registry, **oracles,
    )
    assert decision.served_from_tier == "fresh"
    assert decision.outcome is LookupOutcome.MISS
    assert decision.verification_status == "unverifiable_pending_implementation"


# ============================================================================
# routing_method propagation
# ============================================================================


@pytest.mark.parametrize("method,where_resolved", [
    ("user_authoritative", "u"),
    ("retrieval", "w"),
])
def test_routing_method_propagates_unchanged(
    store, registry, oracles, method, where_resolved,
):
    """Layer 2's method is preserved on WalkerDecision regardless of
    which tier resolved."""
    if where_resolved == "u":
        _store_user_fact(
            store, pattern="preference", predicate="likes",
            slots={"agent": "user", "object": "olives"},
        )
        claim = {
            "pattern": "preference", "predicate": "likes",
            "polarity": 1,
            "slots": {"agent": "user", "object": "olives"},
            "source_text": "you like olives",
        }
    else:  # 'w'
        claim = {
            "pattern": "spatial_temporal", "predicate": "located_in",
            "polarity": 1,
            "slots": {"entity": "Tokyo", "location": "Japan"},
            "source_text": "Tokyo is in Japan",
        }
        tier_w.write_verifier_result(
            claim, store,
            verification_status="verified",
            registry=registry,
        )
    decision = walk_claim(
        claim, _classified_decision(claim, method=method),
        store, registry=registry, **oracles,
    )
    assert decision.routing_method == method
    assert decision.served_from_tier == where_resolved


# ============================================================================
# walker_decision pipeline event
# ============================================================================


def test_walker_decision_event_fires(store, registry, oracles):
    store.insert_turn("user", "anything")
    turn_id = 1
    _store_user_fact(
        store, pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"},
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "polarity": 1,
        "slots": {"agent": "user", "object": "olives"},
        "source_text": "you like olives",
    }
    walk_claim(
        claim, _classified_decision(claim), store,
        registry=registry, source_turn_id=turn_id,
        **oracles,
    )
    events = store.get_pipeline_events(turn_id)
    walker_events = [e for e in events if e["stage"] == "walker_decision"]
    assert len(walker_events) == 1
    payload = walker_events[0]["data"]
    assert payload["served_from_tier"] == "u"
    assert payload["outcome"] == "match"
    assert payload["verification_status"] == "user_asserted"


def test_walker_decision_event_carries_derivation_path(
    store, registry, oracles,
):
    """Phase 8.5: the walker_decision event payload carries the full
    derivation_path (list of ChainEdge dicts), not just the length.
    The trace UI reconstructs the chain from this field; without it
    the chain visualization can't render historical turns."""
    turn_id = store.insert_turn("user", "anything")
    _store_user_fact(
        store, pattern="spatial_temporal", predicate="lives_in",
        slots={"entity": "user", "location": "Williamstown"},
        polarity=1,
    )
    oracles["taxonomy_oracle"].record(
        "Williamstown", "Massachusetts", "part_of",
        label="child_subsumed_by_parent", reason="town in state",
    )
    oracles["distribution_oracle"].record(
        "spatial_temporal", "lives_in", 1, "part_of",
        label="distributes_up", reason="residence aggregates",
    )
    oracles["entity_oracle"].record(
        "Massachusetts", "Williamstown", "different",
        reason="containment relationship; not entity equivalence",
    )
    claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "polarity": 1,
        "slots": {"entity": "user", "location": "Massachusetts"},
        "source_text": "you live in Massachusetts",
    }
    walk_claim(
        claim, _classified_decision(claim), store,
        registry=registry, source_turn_id=turn_id,
        **oracles,
    )
    events = store.get_pipeline_events(turn_id)
    walker_events = [e for e in events if e["stage"] == "walker_decision"]
    assert len(walker_events) == 1
    payload = walker_events[0]["data"]
    assert payload["served_from_tier"] == "derivation"
    # The new field — the chain itself, not just the length.
    assert "derivation_path" in payload
    assert isinstance(payload["derivation_path"], list)
    assert payload["derivation_path_length"] == len(payload["derivation_path"])
    assert payload["derivation_path_length"] >= 1
    # Each edge has the trace-UI contract fields.
    for edge in payload["derivation_path"]:
        assert "oracle" in edge
        assert "label" in edge
        assert "confidence" in edge
