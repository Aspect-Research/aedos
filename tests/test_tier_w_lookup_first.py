"""Tier W lookup-first refactor tests (v0.14 Phase 8d).

Mirror of test_tier_u_lookup_first.py for Tier W. Stages 2 and 3 in
tier_w now honor ``llm=None`` by returning lookup-first results
(graceful MISS on cold cells) instead of crashing.

Tests:
  * Stage 2 cold ``predicate_equivalence`` + ``llm=None`` → MISS, no crash.
  * Stage 2 warm ``predicate_equivalence`` + ``llm=None`` → MATCH per cached.
  * Stage 3 cold ``entity_equivalence`` + ``llm=None`` → candidate fails.
  * Stage 3 warm ``entity_equivalence`` + ``llm=None`` → candidate qualifies.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.fact_store import FactStore
from src.layer1_extraction.pattern_registry import (
    PatternRegistry,
)
from src.layer3_substrate.entity_equivalence import (
    EntityEquivalence,
)
from src.layer3_substrate.predicate_equivalence import (
    PredicateEquivalence,
)
from src.layer4_lookup import tier_w
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
    s = FactStore(tmp_path / "tier_w.db")
    yield s
    s.close()


@pytest.fixture
def predicate_oracle(store):
    return PredicateEquivalence(store)


@pytest.fixture
def entity_oracle(store):
    return EntityEquivalence(store)


def _seed_w_row(
    store: FactStore, registry: PatternRegistry,
    *, claim: dict, verdict: str = "verified",
    stability_class: str = "decade_stable",
):
    """Use tier_w.write_verifier_result to seed a cache row."""
    tier_w.write_verifier_result(
        claim, store,
        verification_status=verdict,
        registry=registry,
        evidence={"trace": "seeded"},
        stability_class=stability_class,
        ttl_seconds=365 * 24 * 3600,
    )


# ============================================================================
# Stage 2: predicate_equivalence (Tier W)
# ============================================================================


def test_w_stage2_cold_pe_with_llm_none_returns_miss_no_crash(
    store, registry, predicate_oracle,
):
    """Pre-Phase-8 bug: stage 2 would crash on cold pe cell. Phase 8:
    gracefully MISS."""
    seeded = {
        "pattern": "preference", "predicate": "loves", "polarity": 1,
        "slots": {"agent": "user", "object": "olives"},
        "source_text": "the user loves olives",
    }
    _seed_w_row(store, registry, claim=seeded)

    claim = {
        "pattern": "preference", "predicate": "adores", "polarity": 1,
        "slots": {"agent": "user", "object": "olives"},
        "source_text": "user adores olives",
    }
    result = tier_w.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        registry=registry,
        llm=None,
    )
    assert result.outcome is LookupOutcome.MISS


def test_w_stage2_warm_pe_with_llm_none_matches(
    store, registry, predicate_oracle,
):
    """Pre-warm (loves, adores) as equivalent — warm cache match
    works under llm=None."""
    seeded = {
        "pattern": "preference", "predicate": "loves", "polarity": 1,
        "slots": {"agent": "user", "object": "olives"},
        "source_text": "the user loves olives",
    }
    _seed_w_row(store, registry, claim=seeded)
    predicate_oracle.record(
        "preference", "adores", "loves", "equivalent",
        slot_reversal="none", reason="setup",
    )
    claim = {
        "pattern": "preference", "predicate": "adores", "polarity": 1,
        "slots": {"agent": "user", "object": "olives"},
        "source_text": "user adores olives",
    }
    result = tier_w.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        registry=registry,
        llm=None,
    )
    assert result.outcome is LookupOutcome.MATCH
    assert result.via == ["predicate_equivalence"]


# ============================================================================
# Stage 3: entity_equivalence (Tier W)
# ============================================================================


def test_w_stage3_cold_ee_with_llm_none_skips_candidate_no_crash(
    store, registry, predicate_oracle, entity_oracle,
):
    """Pre-Phase-8 bug: stage 3 crashed on cold ee. Phase 8: graceful
    MISS."""
    seeded = {
        "pattern": "preference", "predicate": "loves", "polarity": 1,
        "slots": {"agent": "user", "object": "NYC"},
        "source_text": "the user loves NYC",
    }
    _seed_w_row(store, registry, claim=seeded)
    claim = {
        "pattern": "preference", "predicate": "loves", "polarity": 1,
        "slots": {"agent": "user", "object": "New York City"},
        "source_text": "user loves New York City",
    }
    result = tier_w.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        registry=registry,
        llm=None,
        entity_oracle=entity_oracle,
    )
    assert result.outcome is LookupOutcome.MISS


def test_w_stage3_warm_ee_with_llm_none_matches_via_alias(
    store, registry, predicate_oracle, entity_oracle,
):
    """Pre-warm (NYC, New York City) — warm match resolves under llm=None."""
    seeded = {
        "pattern": "preference", "predicate": "loves", "polarity": 1,
        "slots": {"agent": "user", "object": "NYC"},
        "source_text": "the user loves NYC",
    }
    _seed_w_row(store, registry, claim=seeded)
    entity_oracle.record(
        "NYC", "New York City", "same", reason="setup",
    )
    claim = {
        "pattern": "preference", "predicate": "loves", "polarity": 1,
        "slots": {"agent": "user", "object": "New York City"},
        "source_text": "user loves New York City",
    }
    result = tier_w.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        registry=registry,
        llm=None,
        entity_oracle=entity_oracle,
    )
    assert result.outcome is LookupOutcome.MATCH
    assert "entity_equivalence" in result.via


# ============================================================================
# llm=None doesn't break the literal-match path
# ============================================================================


def test_w_literal_match_with_llm_none(
    store, registry, predicate_oracle,
):
    """Stage 1 (literal canonical_key) never consulted oracles, so
    llm=None has always worked. Pin behavior."""
    seeded = {
        "pattern": "preference", "predicate": "loves", "polarity": 1,
        "slots": {"agent": "user", "object": "olives"},
        "source_text": "the user loves olives",
    }
    _seed_w_row(store, registry, claim=seeded)
    result = tier_w.lookup(
        seeded, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        registry=registry,
        llm=None,
    )
    assert result.outcome is LookupOutcome.MATCH
    assert result.via == []
