"""Tier U lookup-first refactor tests (v0.14 Phase 8d).

Pins the post-Phase-7 contract: tier_u stages 2 and 3 honor
``llm=None`` by returning lookup-first results without crashing on
cold substrate cells. Phase 7 surfaced the crash; Phase 8d fixes
it via ``_resolve_predicate_equivalence`` and
``_resolve_entity_equivalence`` helpers.

Tests:
  * Step 2 cold ``predicate_equivalence`` + ``llm=None`` → MISS, no crash.
  * Step 2 warm ``predicate_equivalence`` + ``llm=None`` → MATCH per cached row.
  * Step 3 cold ``entity_equivalence`` + ``llm=None`` → candidate doesn't qualify.
  * Step 3 warm ``entity_equivalence`` + ``llm=None`` → candidate qualifies.
  * Same scenarios with ``llm`` provided behave identically to Phase 7
    (cold cells classify, warm cells serve from cache).
"""

from __future__ import annotations

import pytest

from src.fact_store import Fact, FactStore
from src.layer3_substrate.entity_equivalence import (
    EntityEquivalence,
)
from src.layer3_substrate.predicate_equivalence import (
    PredicateEquivalence,
)
from src.layer4_lookup import tier_u


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "tier_u.db")
    yield s
    s.close()


@pytest.fixture
def predicate_oracle(store):
    return PredicateEquivalence(store)


@pytest.fixture
def entity_oracle(store):
    return EntityEquivalence(store)


def _user_fact(store: FactStore, *, predicate: str, slots: dict,
               polarity: int = 1) -> Fact:
    fact_id = store.insert_fact(Fact(
        pattern="preference", predicate=predicate, slots=dict(slots),
        polarity=polarity, asserted_by="user",
        verification_status="user_asserted",
    ))
    return store.get_fact(fact_id)


# ============================================================================
# Step 2: predicate_equivalence
# ============================================================================


def test_step2_cold_pe_with_llm_none_returns_miss_no_crash(
    store, predicate_oracle,
):
    """Pre-Phase-8 bug: would raise RuntimeError. Phase 8 contract:
    return MISS gracefully."""
    # User has asserted "loves olives" (polarity 1); model claim is
    # "doesn't hate olives" (polarity 0, distinct predicate). Step 2
    # would try to consult predicate_equivalence on (loves, hates).
    _user_fact(
        store, predicate="loves",
        slots={"agent": "user", "object": "olives"},
    )
    claim = {
        "pattern": "preference", "predicate": "hates", "polarity": 0,
        "slots": {"agent": "user", "object": "olives"},
    }
    # llm=None and the (loves, hates) predicate_equivalence cell is cold.
    result = tier_u.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        llm=None,
    )
    assert result.outcome is tier_u.TierUOutcome.MISS


def test_step2_warm_pe_with_llm_none_matches_on_cached_row(
    store, predicate_oracle,
):
    """Pre-warm the (loves, adores) cell as 'equivalent'. With llm=None,
    the cached row resolves the lookup."""
    _user_fact(
        store, predicate="loves",
        slots={"agent": "user", "object": "olives"},
    )
    predicate_oracle.record(
        "preference", "loves", "adores", "equivalent",
        slot_reversal="none", reason="setup",
    )
    claim = {
        "pattern": "preference", "predicate": "adores", "polarity": 1,
        "slots": {"agent": "user", "object": "olives"},
    }
    result = tier_u.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        llm=None,
    )
    assert result.outcome is tier_u.TierUOutcome.MATCH
    assert result.via == ["predicate_equivalence"]


def test_step2_warm_pe_contradictory_polarity_flip_with_llm_none(
    store, predicate_oracle,
):
    """The cheetahs case: cached (likes, hates) as 'contradictory';
    user said 'likes p=1', model says 'hates p=0' → MATCH via
    polarity flip. Pre-Phase-8 worked under llm=client; Phase 8
    extends to llm=None on warm cells."""
    _user_fact(
        store, predicate="likes",
        slots={"agent": "user", "object": "cheetahs"}, polarity=1,
    )
    predicate_oracle.record(
        "preference", "hates", "likes", "contradictory",
        slot_reversal="none", reason="setup",
    )
    claim = {
        "pattern": "preference", "predicate": "hates", "polarity": 0,
        "slots": {"agent": "user", "object": "cheetahs"},
    }
    result = tier_u.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        llm=None,
    )
    assert result.outcome is tier_u.TierUOutcome.MATCH
    assert result.polarity_flipped is True


# ============================================================================
# Step 3: entity_equivalence
# ============================================================================


def test_step3_cold_ee_with_llm_none_skips_candidate_no_crash(
    store, predicate_oracle, entity_oracle,
):
    """Pre-Phase-8 bug: would raise RuntimeError on cold ee cell when
    step 3 tried to qualify a candidate. Phase 8 contract: candidate
    fails to qualify; step 3 yields no result; lookup falls through
    to MISS gracefully."""
    # User: "I love NYC". Model claim: "I love New York City".
    # Step 1 (literal) misses. Step 2 misses (same predicate). Step 3
    # would consult entity_equivalence on (NYC, New York City) — cold.
    _user_fact(
        store, predicate="loves",
        slots={"agent": "user", "object": "NYC"},
    )
    claim = {
        "pattern": "preference", "predicate": "loves", "polarity": 1,
        "slots": {"agent": "user", "object": "New York City"},
    }
    result = tier_u.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        llm=None,
        entity_oracle=entity_oracle,
    )
    assert result.outcome is tier_u.TierUOutcome.MISS


def test_step3_warm_ee_with_llm_none_matches_via_alias(
    store, predicate_oracle, entity_oracle,
):
    """Pre-warm (NYC, New York City) as 'same'. With llm=None,
    the alias resolves and the candidate qualifies."""
    _user_fact(
        store, predicate="loves",
        slots={"agent": "user", "object": "NYC"},
    )
    entity_oracle.record(
        "NYC", "New York City", "same", reason="setup",
    )
    claim = {
        "pattern": "preference", "predicate": "loves", "polarity": 1,
        "slots": {"agent": "user", "object": "New York City"},
    }
    result = tier_u.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        llm=None,
        entity_oracle=entity_oracle,
    )
    assert result.outcome is tier_u.TierUOutcome.MATCH
    assert "entity_equivalence" in result.via


def test_step3_warm_ee_different_label_doesnt_qualify(
    store, predicate_oracle, entity_oracle,
):
    """Pre-warm (NYC, Newark) as 'different'. The candidate doesn't
    qualify; lookup MISSES."""
    _user_fact(
        store, predicate="loves",
        slots={"agent": "user", "object": "NYC"},
    )
    entity_oracle.record(
        "Newark", "NYC", "different", reason="setup",
    )
    claim = {
        "pattern": "preference", "predicate": "loves", "polarity": 1,
        "slots": {"agent": "user", "object": "Newark"},
    }
    result = tier_u.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        llm=None,
        entity_oracle=entity_oracle,
    )
    assert result.outcome is tier_u.TierUOutcome.MISS


# ============================================================================
# Mode parity: llm=client behaves identically to Phase 7
# ============================================================================


class _StubLLM:
    """Records calls; never returns a verdict (used for parity check
    where we expect NO LLM calls because cells are pre-warmed)."""

    def __init__(self):
        self.calls: list = []

    def extract_with_tool(self, **kwargs):
        self.calls.append(kwargs)
        raise AssertionError("LLM was called when warm cache should hit")


def test_warm_cache_doesnt_invoke_llm_in_step2(
    store, predicate_oracle,
):
    """Even with llm=client, a warm pe cell never triggers an LLM call."""
    _user_fact(
        store, predicate="loves",
        slots={"agent": "user", "object": "olives"},
    )
    predicate_oracle.record(
        "preference", "loves", "adores", "equivalent",
        slot_reversal="none", reason="setup",
    )
    claim = {
        "pattern": "preference", "predicate": "adores", "polarity": 1,
        "slots": {"agent": "user", "object": "olives"},
    }
    llm = _StubLLM()
    result = tier_u.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        llm=llm,
    )
    assert result.outcome is tier_u.TierUOutcome.MATCH
    assert llm.calls == []


def test_warm_cache_doesnt_invoke_llm_in_step3(
    store, predicate_oracle, entity_oracle,
):
    """Warm entity_equivalence and same-predicate alias path: no LLM call."""
    _user_fact(
        store, predicate="loves",
        slots={"agent": "user", "object": "NYC"},
    )
    entity_oracle.record(
        "NYC", "New York City", "same", reason="setup",
    )
    claim = {
        "pattern": "preference", "predicate": "loves", "polarity": 1,
        "slots": {"agent": "user", "object": "New York City"},
    }
    llm = _StubLLM()
    result = tier_u.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        llm=llm,
        entity_oracle=entity_oracle,
    )
    assert result.outcome is tier_u.TierUOutcome.MATCH
    assert llm.calls == []


# ============================================================================
# llm=None doesn't break the literal-match path
# ============================================================================


def test_step1_literal_match_with_llm_none(
    store, predicate_oracle,
):
    """Step 1 doesn't consult any oracle, so llm=None has always
    worked. Pin behavior."""
    _user_fact(
        store, predicate="loves",
        slots={"agent": "user", "object": "olives"},
    )
    claim = {
        "pattern": "preference", "predicate": "loves", "polarity": 1,
        "slots": {"agent": "user", "object": "olives"},
    }
    result = tier_u.lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        llm=None,
    )
    assert result.outcome is tier_u.TierUOutcome.MATCH
    assert result.via == []
