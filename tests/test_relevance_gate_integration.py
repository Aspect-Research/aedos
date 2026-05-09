"""Integration tests for the relevance gate (v0.14.4).

Verifies the gate actually skips the entity_equivalence oracle when
candidates have no token overlap with the active context, AND that
genuine cross-turn aliases still reach the oracle.

Two scenarios:
  1. Cairo↔lizard — different turns, different patterns/topics, zero
     token overlap → skipped, no LLM call, no substrate row.
  2. NYC↔New York City — same topical context (residence, location),
     non-empty intersection → oracle consulted, alias resolves.

Tests use Tier W's broadening path because that's the most common
LLM-call-write-on-novel-pair entry. Tier U's path has identical
gate behavior (same _gather_alias_identity_candidates pattern); a
single Tier W test pins the contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.fact_store import FactStore
from src.layer1_extraction.pattern_registry import PatternRegistry
from src.layer3_substrate.entity_equivalence import EntityEquivalence
from src.layer3_substrate.predicate_equivalence import PredicateEquivalence
from src.layer4_lookup import tier_w
from src.layer4_lookup.relevance import compute_active_context


_PATTERNS_PATH = (
    Path(__file__).parent.parent
    / "src" / "layer1_extraction" / "patterns.yaml"
)


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "relevance_gate.db")
    yield s
    s.close()


@pytest.fixture
def registry():
    return PatternRegistry.from_yaml(_PATTERNS_PATH)


class _CountingLLM:
    """Records every extract_with_tool call. Returns a 'different'
    verdict by default so any unexpected oracle call still produces
    a valid response (we count the calls, we don't depend on what
    the verdict is)."""

    def __init__(self):
        self.calls: list[str] = []

    def extract_with_tool(self, *, system, user_message, tool, purpose):
        self.calls.append(purpose)
        return {"label": "different", "reason": "test stub"}


# ---- Scenario 1: Cairo turn-1 cached, lizard turn-2 should not consult


def test_cairo_w_row_does_not_trigger_oracle_for_lizard_claim(store, registry):
    """Pre-seed Tier W with a Cairo fact under spatial_temporal.
    Then verify a lizard claim under spatial_temporal — the active
    context for the lizard claim shares no tokens with the Cairo
    row. The relevance gate should skip the candidate; the
    entity_equivalence oracle should NOT be consulted."""

    # Seed Tier W with the Cairo row.
    cairo_claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "polarity": 1,
        "slots": {"entity": "Cairo", "location": "Egypt"},
        "source_text": "Cairo is in Egypt",
    }
    tier_w.write_verifier_result(
        cairo_claim, store,
        verification_status="verified",
        registry=registry,
    )

    # Now try to look up a lizard claim. The lizard claim's active
    # context (slots + source_text) shares NO tokens with Cairo/Egypt.
    lizard_claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "polarity": 1,
        "slots": {"entity": "lizard", "location": "desert"},
        "source_text": "lizards live in deserts",
    }
    active_tokens = compute_active_context(
        lizard_claim, current_user_message="Where do lizards live?",
    )

    # Sanity check: tokens have ZERO overlap.
    cairo_tokens = compute_active_context(cairo_claim)
    assert not (active_tokens & cairo_tokens), (
        f"test setup error: tokens shouldn't overlap; got "
        f"intersection {active_tokens & cairo_tokens}"
    )

    counting_llm = _CountingLLM()
    predicate_oracle = PredicateEquivalence(store)
    entity_oracle = EntityEquivalence(store)

    result = tier_w.lookup(
        lizard_claim, store, predicate_oracle,
        key_slot_names=["entity", "location"],
        registry=registry,
        entity_oracle=entity_oracle,
        llm=counting_llm,
        active_context_tokens=active_tokens,
    )

    # No oracle calls fired (gate skipped the Cairo candidate).
    entity_eq_calls = [c for c in counting_llm.calls
                       if "entity_equivalence" in c]
    assert entity_eq_calls == [], (
        f"expected no entity_equivalence LLM calls; got {entity_eq_calls}"
    )

    # No new entity_equivalence rows written.
    n_rows = store._conn.execute(
        "SELECT COUNT(*) AS n FROM entity_equivalence"
    ).fetchone()["n"]
    assert n_rows == 0, (
        f"expected 0 entity_equivalence rows; got {n_rows} "
        "(gate failed to suppress the LLM-write path)"
    )


# ---- Scenario 2: cross-turn alias should still reach the oracle


def test_relevant_candidate_does_reach_the_oracle(store, registry):
    """When the active context DOES overlap with a candidate (via
    shared topic tokens or shared predicate concept), the gate
    passes and the oracle is consulted. Demonstrates that the
    cross-turn alias case isn't broken."""

    # Seed Tier W with a fact about NYC.
    nyc_claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "polarity": 1,
        "slots": {"entity": "user", "location": "NYC"},
        "source_text": "the user lives in NYC",
    }
    tier_w.write_verifier_result(
        nyc_claim, store,
        verification_status="verified",
        registry=registry,
    )

    # New claim: same predicate (lives_in), different location label
    # ("New York City"). Shared tokens: "user", "lives", "in" (in is
    # a stopword). The non-stopword overlap survives — at minimum
    # "user" and "lives".
    new_claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "polarity": 1,
        "slots": {"entity": "user", "location": "New York City"},
        "source_text": "the user lives in New York City",
    }
    active_tokens = compute_active_context(
        new_claim,
        current_user_message="Where did you say the user lives?",
    )

    # Sanity check: there IS overlap.
    nyc_tokens = compute_active_context(nyc_claim)
    overlap = active_tokens & nyc_tokens
    assert overlap, (
        f"test setup error: expected overlap between active context "
        f"and NYC fact tokens; got empty intersection"
    )

    counting_llm = _CountingLLM()
    predicate_oracle = PredicateEquivalence(store)
    entity_oracle = EntityEquivalence(store)

    tier_w.lookup(
        new_claim, store, predicate_oracle,
        key_slot_names=["entity", "location"],
        registry=registry,
        entity_oracle=entity_oracle,
        llm=counting_llm,
        active_context_tokens=active_tokens,
    )

    # The oracle WAS consulted (gate passed the candidate through).
    # Specifically, ("NYC", "New York City") got handed to
    # entity_equivalence — exactly the cross-turn alias pattern the
    # broadening was designed for.
    entity_eq_calls = [c for c in counting_llm.calls
                       if "entity_equivalence" in c]
    assert entity_eq_calls, (
        "expected at least one entity_equivalence LLM call when the "
        "candidate has token overlap with the active context"
    )


# ---- Back-compat: callers that don't supply active context get the
# pre-gate behavior (unfiltered broadening). Pins the contract.


def test_no_active_context_means_no_gating(store, registry):
    """Without ``active_context_tokens`` (None), the gate is
    inactive — every same-pattern candidate flows through to the
    oracle as before. Critical for tests/callers that haven't been
    updated."""
    cairo_claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "polarity": 1,
        "slots": {"entity": "Cairo", "location": "Egypt"},
        "source_text": "Cairo is in Egypt",
    }
    tier_w.write_verifier_result(
        cairo_claim, store,
        verification_status="verified",
        registry=registry,
    )

    lizard_claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "polarity": 1,
        "slots": {"entity": "lizard", "location": "desert"},
        "source_text": "lizards live in deserts",
    }
    counting_llm = _CountingLLM()
    predicate_oracle = PredicateEquivalence(store)
    entity_oracle = EntityEquivalence(store)

    # No active_context_tokens → no gating. Cairo↔lizard pair gets
    # consulted (this is the OLD behavior; the gate is what the new
    # callers opt into).
    tier_w.lookup(
        lizard_claim, store, predicate_oracle,
        key_slot_names=["entity", "location"],
        registry=registry,
        entity_oracle=entity_oracle,
        llm=counting_llm,
        # active_context_tokens omitted (defaults to None)
    )

    entity_eq_calls = [c for c in counting_llm.calls
                       if "entity_equivalence" in c]
    assert entity_eq_calls, (
        "back-compat broken: without active context, the oracle "
        "should still be consulted on every same-pattern candidate"
    )
