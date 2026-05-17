"""Tests for v0.14.8 taxonomy-aware alias broadening.

The walker's stage-3 alias-identity broadening (in tier_u and tier_w)
used to consult ``entity_equivalence`` on every slot-value mismatch,
including pairs that are in CONTAINMENT relations rather than alias
relations (e.g. "Massachusetts" vs "United States" — these are
different entities, not aliases). The user's complaint: the
substrate inspector showed entity_equivalence("different") rows for
geographic containment pairs when the architecturally-correct
oracle is entity_taxonomy(part_of) which would record
"child_subsumed_by_parent" — a more useful artifact for downstream
derivation.

Fix: for slots declared as ``taxonomy_relevant_slots`` on the
pattern's schema (spatial_temporal.location, mereological.part /
.whole), consult entity_taxonomy(part_of) FIRST. On a containment
verdict, skip entity_equivalence entirely; let derivation handle the
match via predicate_distribution. On "neither" (or cold), fall back
to entity_equivalence as before.

These tests pin both behaviors:
  * Containment pairs short-circuit on taxonomy and don't pollute
    entity_equivalence.
  * Alias pairs still resolve via entity_equivalence (taxonomy says
    "neither" → fallback fires).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.fact_store import FactStore
from src.layer1_extraction.pattern_registry import PatternRegistry
from src.layer3_substrate.entity_equivalence import EntityEquivalence
from src.layer3_substrate.entity_taxonomy import EntityTaxonomy
from src.layer3_substrate.predicate_equivalence import PredicateEquivalence
from src.layer4_lookup import tier_u, tier_w
from src.layer4_lookup.types import LookupOutcome


_PATTERNS_PATH = (
    Path(__file__).parent.parent
    / "src" / "layer1_extraction" / "patterns.yaml"
)


@pytest.fixture(scope="module")
def registry() -> PatternRegistry:
    return PatternRegistry.from_yaml(_PATTERNS_PATH)


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "tax_aware.db")
    yield s
    s.close()


@pytest.fixture
def predicate_oracle(store):
    return PredicateEquivalence(store)


@pytest.fixture
def entity_oracle(store):
    return EntityEquivalence(store)


@pytest.fixture
def taxonomy_oracle(store):
    return EntityTaxonomy(store)


# ============================================================================
# Schema gate — patterns.yaml declares which slots are taxonomy-relevant
# ============================================================================


def test_spatial_temporal_location_is_taxonomy_relevant(registry):
    """v0.14.8 — spatial_temporal.location is the canonical
    containment-relevant slot. The user's complaint case
    (Massachusetts ↔ United States) flows through this slot."""
    p = registry.get("spatial_temporal")
    assert "location" in p.taxonomy_relevant_slots


def test_mereological_part_and_whole_are_taxonomy_relevant(registry):
    """v0.14.8 — mereological's whole point is part_of relations.
    Both slots get the taxonomy-first treatment."""
    p = registry.get("mereological")
    assert "part" in p.taxonomy_relevant_slots
    assert "whole" in p.taxonomy_relevant_slots


def test_preference_has_no_taxonomy_relevant_slots(registry):
    """Negative case — preference's slots (agent, object) aren't
    structurally containment-relevant. The taxonomy short-circuit
    must NOT fire for preference claims."""
    p = registry.get("preference")
    assert p.taxonomy_relevant_slots == ()


# ============================================================================
# Tier W — containment short-circuits, alias still works
# ============================================================================


def _seed_w_row(store, registry, *, claim, verdict="verified"):
    tier_w.write_verifier_result(
        claim, store,
        verification_status=verdict,
        registry=registry,
        evidence={"trace": "seeded"},
        stability_class="decade_stable",
        ttl_seconds=365 * 24 * 3600,
    )


def test_w_containment_pair_uses_taxonomy_skips_equivalence(
    store, registry, predicate_oracle, entity_oracle, taxonomy_oracle,
):
    """The user's bug case end-to-end: a Tier W row exists for
    'Williams College located in Massachusetts'. A new claim asserts
    'Williams College located in United States'. Pre-warm the
    taxonomy with Massachusetts part_of United States →
    child_subsumed_by_parent. The alias broadening MUST consult
    taxonomy (find the containment), skip entity_equivalence (no
    'different' row written), and return MISS so derivation handles
    the chain."""
    seeded = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "polarity": 1,
        "slots": {"entity": "Williams College",
                  "location": "Massachusetts",
                  "relation_kind": "containment"},
        "source_text": "Williams College is in Massachusetts",
    }
    _seed_w_row(store, registry, claim=seeded)

    # Pre-warm taxonomy with the containment relation.
    taxonomy_oracle.record(
        "Massachusetts", "United States", "part_of",
        "child_subsumed_by_parent",
        reason="setup: state contained in country",
    )

    claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "polarity": 1,
        "slots": {"entity": "Williams College",
                  "location": "United States",
                  "relation_kind": "containment"},
        "source_text": "Williams College is in the United States",
    }
    result = tier_w.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["entity", "location"],
        registry=registry,
        llm=None,
        entity_oracle=entity_oracle,
        taxonomy_oracle=taxonomy_oracle,
    )
    # Containment relation, not alias — Tier W returns MISS.
    # Derivation walker downstream composes the chain via
    # predicate_distribution(spatial_temporal, located_in,
    # distributes_up, part_of).
    assert result.outcome is LookupOutcome.MISS
    # And critically: NO entity_equivalence row was written.
    ee_existing = entity_oracle.lookup("Massachusetts", "United States")
    assert ee_existing is None, (
        f"taxonomy short-circuit failed; entity_equivalence row "
        f"unexpectedly written: {ee_existing}"
    )


def test_w_alias_pair_falls_back_to_equivalence_when_taxonomy_says_neither(
    store, registry, predicate_oracle, entity_oracle, taxonomy_oracle,
):
    """The classic alias case must still work after the v0.14.8
    change. NYC ↔ New York City is alias-equivalent, NOT containment.
    Pre-warm taxonomy with 'neither' (the correct verdict for
    aliases) and entity_equivalence with 'same'. The walker must
    consult taxonomy, see 'neither', fall back to entity_equivalence,
    see 'same', and MATCH the alias candidate."""
    seeded = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "polarity": 1,
        "slots": {"entity": "user", "location": "NYC",
                  "relation_kind": "residence"},
        "source_text": "the user lives in NYC",
    }
    _seed_w_row(store, registry, claim=seeded)

    taxonomy_oracle.record(
        "New York City", "NYC", "part_of",
        "neither",
        reason="setup: NYC and New York City are aliases, not part-of pair",
    )
    entity_oracle.record(
        "NYC", "New York City", "same", reason="setup: alias",
    )

    claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "polarity": 1,
        "slots": {"entity": "user", "location": "New York City",
                  "relation_kind": "residence"},
        "source_text": "user lives in New York City",
    }
    result = tier_w.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["entity", "location"],
        registry=registry,
        llm=None,
        entity_oracle=entity_oracle,
        taxonomy_oracle=taxonomy_oracle,
    )
    assert result.outcome is LookupOutcome.MATCH
    assert "entity_equivalence" in result.via


def test_w_taxonomy_oracle_omitted_falls_back_to_old_behavior(
    store, registry, predicate_oracle, entity_oracle,
):
    """Back-compat: callers that don't pass taxonomy_oracle get the
    pre-v0.14.8 behavior (entity_equivalence consulted on every
    slot-value mismatch, regardless of containment)."""
    seeded = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "polarity": 1,
        "slots": {"entity": "user", "location": "NYC",
                  "relation_kind": "residence"},
        "source_text": "user lives in NYC",
    }
    _seed_w_row(store, registry, claim=seeded)
    entity_oracle.record(
        "NYC", "New York City", "same", reason="setup: alias",
    )

    claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "polarity": 1,
        "slots": {"entity": "user", "location": "New York City",
                  "relation_kind": "residence"},
        "source_text": "user lives in New York City",
    }
    result = tier_w.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["entity", "location"],
        registry=registry,
        llm=None,
        entity_oracle=entity_oracle,
        # taxonomy_oracle omitted — pre-v0.14.8 path.
    )
    assert result.outcome is LookupOutcome.MATCH


# ============================================================================
# Tier U — same containment short-circuit, same alias fallback
# ============================================================================


def _insert_user_fact(store, fact_pattern, predicate, slots, source_text):
    """Insert a Tier U user-asserted fact directly via the store."""
    from src.fact_store import Fact
    return store.insert_fact(Fact(
        pattern=fact_pattern, predicate=predicate,
        slots=slots, polarity=1,
        asserted_by="user",
        verification_status="user_asserted",
        confidence=0.5, affirmed_count=1,
        is_session_local=0, session_ids=[],
        source_text=source_text,
    ))


def test_u_containment_pair_uses_taxonomy_skips_equivalence(
    store, registry, predicate_oracle, entity_oracle, taxonomy_oracle,
):
    """Same as the Tier W test but on Tier U: a stored user fact
    'I live in Williamstown' + a new claim 'user lives in
    Massachusetts'. Williamstown part_of Massachusetts is the
    containment chain. With taxonomy pre-warmed, the alias
    broadening must consult taxonomy and skip entity_equivalence."""
    _insert_user_fact(
        store, "spatial_temporal", "lives_in",
        slots={"entity": "user", "location": "Williamstown",
               "relation_kind": "residence"},
        source_text="I live in Williamstown",
    )
    taxonomy_oracle.record(
        "Williamstown", "Massachusetts", "part_of",
        "child_subsumed_by_parent",
        reason="setup: town in state",
    )

    claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "polarity": 1,
        "slots": {"entity": "user", "location": "Massachusetts",
                  "relation_kind": "residence"},
        "source_text": "user lives in Massachusetts",
    }
    result = tier_u.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["entity", "location"],
        llm=None,
        entity_oracle=entity_oracle,
        taxonomy_oracle=taxonomy_oracle,
        registry=registry,
    )
    # Tier U returns MISS — derivation will compose the containment
    # chain via predicate_distribution(spatial_temporal, lives_in,
    # distributes_up, part_of).
    assert result.outcome is tier_u.TierUOutcome.MISS
    # And no entity_equivalence row was polluted with "different".
    ee_existing = entity_oracle.lookup("Williamstown", "Massachusetts")
    assert ee_existing is None


def test_u_alias_pair_falls_back_to_equivalence_when_taxonomy_says_neither(
    store, registry, predicate_oracle, entity_oracle, taxonomy_oracle,
):
    """Alias path on Tier U: I live in NYC + user lives in New York
    City. Taxonomy says 'neither', equivalence says 'same' →
    MATCH via entity_equivalence."""
    _insert_user_fact(
        store, "spatial_temporal", "lives_in",
        slots={"entity": "user", "location": "NYC",
               "relation_kind": "residence"},
        source_text="I live in NYC",
    )
    taxonomy_oracle.record(
        "New York City", "NYC", "part_of",
        "neither",
        reason="setup: aliases, not part-of",
    )
    entity_oracle.record(
        "NYC", "New York City", "same",
        reason="setup: alias",
    )

    claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "polarity": 1,
        "slots": {"entity": "user", "location": "New York City",
                  "relation_kind": "residence"},
        "source_text": "user lives in New York City",
    }
    result = tier_u.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["entity", "location"],
        llm=None,
        entity_oracle=entity_oracle,
        taxonomy_oracle=taxonomy_oracle,
        registry=registry,
    )
    assert result.outcome is tier_u.TierUOutcome.MATCH
    assert "entity_equivalence" in result.via


def test_u_non_taxonomy_relevant_slot_skips_taxonomy_consultation(
    store, registry, predicate_oracle, entity_oracle, taxonomy_oracle,
):
    """preference.object isn't taxonomy_relevant. A claim with a
    different object value goes straight to entity_equivalence as
    today — no taxonomy consultation, even with taxonomy_oracle
    available."""
    _insert_user_fact(
        store, "preference", "loves",
        slots={"agent": "user", "object": "ramen"},
        source_text="I love ramen",
    )
    entity_oracle.record(
        "ramen", "noodles", "different",
        reason="setup: distinct foods",
    )

    claim = {
        "pattern": "preference", "predicate": "loves",
        "polarity": 1,
        "slots": {"agent": "user", "object": "noodles"},
        "source_text": "user loves noodles",
    }
    result = tier_u.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        llm=None,
        entity_oracle=entity_oracle,
        taxonomy_oracle=taxonomy_oracle,
        registry=registry,
    )
    # preference.object isn't in taxonomy_relevant_slots, so
    # entity_equivalence is consulted directly. It returned 'different'
    # → MISS.
    assert result.outcome is tier_u.TierUOutcome.MISS
    # Crucially: no taxonomy row was written (slot wasn't relevant).
    tax_existing = taxonomy_oracle.lookup("ramen", "noodles", "part_of")
    assert tax_existing is None
