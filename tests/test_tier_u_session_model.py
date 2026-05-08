"""Phase 6 — session-locality filtering on Tier U lookup.

Pins the lookup-side invariants:

  * cross-session facts visible from any session (and from
    current_session=None)
  * session-local facts visible only from their originating
    session
  * the oracle resolution chain (literal → entity_equivalence →
    predicate_equivalence) runs unchanged on the session-filtered
    candidate set; the cheetahs case stored as session-local
    matches in-session and misses cross-session
  * the Q3 tie-breaker: when a session-local and a cross-session
    row of the same proposition both exist in the current session,
    the lookup prefers the session-local (more specific contextual
    signal)
  * the Phase 4 alias-broadening cost story: a session-local fact
    in a DIFFERENT session never contributes a candidate, and the
    entity oracle is not consulted on it — bounded-by-memoization
    is reduced by session filtering

Storage-path bookkeeping is tested in
``test_session_local_lifetime.py``.
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
from src.layer4_lookup.tier_u import (
    TierUOutcome,
    lookup,
)


# ---- shared fixtures + LLM stub ------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "tier_u_session.db")
    yield s
    s.close()


@pytest.fixture
def predicate_oracle(store):
    return PredicateEquivalence(store)


@pytest.fixture
def entity_oracle(store):
    return EntityEquivalence(store)


class _MockLLM:
    """Same MockLLM shape as test_tier_u_with_oracle.py."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def extract_with_tool(self, *, system, user_message, tool, purpose):
        self.calls.append({"user_message": user_message, "purpose": purpose})
        if not self.responses:
            raise AssertionError(
                f"MockLLM ran out of responses for purpose={purpose}; "
                f"unexpected LLM call"
            )
        return self.responses.pop(0)


def _store_fact(
    store: FactStore, *,
    pattern: str, predicate: str, slots: dict, polarity: int,
    is_session_local: int = 0, session_ids: list[str] | None = None,
) -> Fact:
    fact_id = store.insert_fact(Fact(
        pattern=pattern, predicate=predicate, slots=dict(slots),
        polarity=polarity,
        asserted_by="user", verification_status="user_asserted",
        is_session_local=is_session_local,
        session_ids=list(session_ids or []),
        affirmed_count=1,
    ))
    fact = store.get_fact(fact_id)
    assert fact is not None
    return fact


# ============================================================================
# Section 1 — cross-session visibility (the always-visible class)
# ============================================================================


def test_cross_session_fact_visible_from_originating_session(
    store, predicate_oracle,
):
    stored = _store_fact(
        store, pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"}, polarity=1,
        is_session_local=0, session_ids=["A"],
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "olives"},
        "polarity": 1, "source_text": "user likes olives",
    }
    result = lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        current_session="A", llm=_MockLLM(),
    )
    assert result.outcome is TierUOutcome.MATCH
    assert result.matching_fact.id == stored.id


def test_cross_session_fact_visible_from_other_session(
    store, predicate_oracle,
):
    """A cross-session fact created in A is visible from B."""
    stored = _store_fact(
        store, pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"}, polarity=1,
        is_session_local=0, session_ids=["A"],
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "olives"},
        "polarity": 1, "source_text": "user likes olives",
    }
    result = lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        current_session="B", llm=_MockLLM(),
    )
    assert result.outcome is TierUOutcome.MATCH
    assert result.matching_fact.id == stored.id


def test_cross_session_fact_visible_with_no_active_session(
    store, predicate_oracle,
):
    """current_session=None — show only cross-session facts. They
    are visible."""
    stored = _store_fact(
        store, pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"}, polarity=1,
        is_session_local=0, session_ids=["A", "B"],
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "olives"},
        "polarity": 1, "source_text": "user likes olives",
    }
    result = lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        current_session=None, llm=_MockLLM(),
    )
    assert result.outcome is TierUOutcome.MATCH
    assert result.matching_fact.id == stored.id


# ============================================================================
# Section 2 — session-local visibility (the gated class)
# ============================================================================


def test_session_local_fact_visible_in_originating_session(
    store, predicate_oracle,
):
    stored = _store_fact(
        store, pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"}, polarity=1,
        is_session_local=1, session_ids=["A"],
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "olives"},
        "polarity": 1, "source_text": "user likes olives",
    }
    result = lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        current_session="A", llm=_MockLLM(),
    )
    assert result.outcome is TierUOutcome.MATCH
    assert result.matching_fact.id == stored.id


def test_session_local_fact_invisible_from_other_session(
    store, predicate_oracle,
):
    """The session-A session-local fact is invisible from session B.
    The lookup returns MISS. No oracle calls."""
    _store_fact(
        store, pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"}, polarity=1,
        is_session_local=1, session_ids=["A"],
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "olives"},
        "polarity": 1, "source_text": "user likes olives",
    }
    llm = _MockLLM()  # MUST NOT be called
    result = lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        current_session="B", llm=llm,
    )
    assert result.outcome is TierUOutcome.MISS
    assert len(llm.calls) == 0


def test_session_local_fact_invisible_with_no_active_session(
    store, predicate_oracle,
):
    """current_session=None — session-locals invisible by design.
    The cross-session-only filter."""
    _store_fact(
        store, pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"}, polarity=1,
        is_session_local=1, session_ids=["A"],
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "olives"},
        "polarity": 1, "source_text": "user likes olives",
    }
    llm = _MockLLM()
    result = lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        current_session=None, llm=llm,
    )
    assert result.outcome is TierUOutcome.MISS
    assert len(llm.calls) == 0


# ============================================================================
# Section 3 — cheetahs case under both storage modes
# ============================================================================


def test_cheetahs_case_cross_session_matches_from_any_session(
    store, predicate_oracle,
):
    """Stored as cross-session — the canonical Phase 3 cheetahs
    resolution still fires regardless of current_session."""
    stored = _store_fact(
        store, pattern="preference", predicate="dislikes",
        slots={"agent": "user", "object": "cheetahs"}, polarity=1,
        is_session_local=0, session_ids=["A"],
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "cheetahs"},
        "polarity": 0, "source_text": "you really don't like cheetahs",
    }
    llm = _MockLLM(responses=[
        {"label": "contradictory", "slot_reversal": "none",
         "reason": "antonyms"},
    ])
    result = lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        current_session="B", llm=llm,
    )
    assert result.outcome is TierUOutcome.MATCH
    assert result.matching_fact.id == stored.id
    assert result.via == ["predicate_equivalence"]
    assert result.polarity_flipped is True


def test_cheetahs_case_session_local_matches_in_session(
    store, predicate_oracle,
):
    """Stored as session-local in A — cheetahs resolution still
    fires when the lookup runs in session A."""
    stored = _store_fact(
        store, pattern="preference", predicate="dislikes",
        slots={"agent": "user", "object": "cheetahs"}, polarity=1,
        is_session_local=1, session_ids=["A"],
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "cheetahs"},
        "polarity": 0, "source_text": "you really don't like cheetahs",
    }
    llm = _MockLLM(responses=[
        {"label": "contradictory", "slot_reversal": "none",
         "reason": "antonyms"},
    ])
    result = lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        current_session="A", llm=llm,
    )
    assert result.outcome is TierUOutcome.MATCH
    assert result.matching_fact.id == stored.id
    assert result.via == ["predicate_equivalence"]
    assert result.polarity_flipped is True


def test_cheetahs_case_session_local_misses_in_other_session(
    store, predicate_oracle,
):
    """Stored as session-local in A — invisible from B. The lookup
    returns MISS without consulting the oracle (no candidate set
    means no oracle calls)."""
    _store_fact(
        store, pattern="preference", predicate="dislikes",
        slots={"agent": "user", "object": "cheetahs"}, polarity=1,
        is_session_local=1, session_ids=["A"],
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "cheetahs"},
        "polarity": 0, "source_text": "you really don't like cheetahs",
    }
    llm = _MockLLM()  # MUST NOT be called — no candidates visible
    result = lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        current_session="B", llm=llm,
    )
    assert result.outcome is TierUOutcome.MISS
    assert len(llm.calls) == 0


# ============================================================================
# Section 4 — alias-broadening (Phase 4) under session filtering
# ============================================================================


def test_alias_broadening_skips_invisible_session_local_candidates(
    store, predicate_oracle, entity_oracle,
):
    """A session-local fact in B with NYC ↔ "New York City" alias
    case must NOT contribute candidates to a lookup in session A.
    The entity oracle is never consulted because the candidate set
    is empty under the session filter — the cold-start cost-
    correctness story (tier_u docstring) gets a real reduction
    under session filtering, not just memoization."""
    _store_fact(
        store, pattern="spatial_temporal", predicate="lives_in",
        slots={"entity": "user", "location": "NYC"}, polarity=1,
        is_session_local=1, session_ids=["B"],
    )
    claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "slots": {"entity": "user", "location": "New York City"},
        "polarity": 1, "source_text": "you live in New York City",
    }
    llm = _MockLLM()  # MUST NOT be called
    result = lookup(
        claim, store, predicate_oracle,
        key_slot_names=["entity", "location"],
        current_session="A", llm=llm, entity_oracle=entity_oracle,
    )
    assert result.outcome is TierUOutcome.MISS
    assert len(llm.calls) == 0


def test_alias_broadening_works_on_session_local_in_same_session(
    store, predicate_oracle, entity_oracle,
):
    """Sanity: the alias-resolution chain still works on a session-
    local fact when the lookup is in the originating session."""
    stored = _store_fact(
        store, pattern="spatial_temporal", predicate="lives_in",
        slots={"entity": "user", "location": "NYC"}, polarity=1,
        is_session_local=1, session_ids=["A"],
    )
    claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "slots": {"entity": "user", "location": "New York City"},
        "polarity": 1, "source_text": "you live in New York City",
    }
    llm = _MockLLM(responses=[
        {"label": "same", "reason": "common abbreviation"},
    ])
    result = lookup(
        claim, store, predicate_oracle,
        key_slot_names=["entity", "location"],
        current_session="A", llm=llm, entity_oracle=entity_oracle,
    )
    assert result.outcome is TierUOutcome.MATCH
    assert result.matching_fact.id == stored.id
    assert result.via == ["entity_equivalence"]


# ============================================================================
# Section 5 — Q3 tie-breaker: prefer session-local when both visible
# ============================================================================


def test_coexistence_lookup_prefers_session_local(
    store, predicate_oracle,
):
    """When a session-local AND a cross-session row of the same
    proposition are both visible in the current session, the lookup
    prefers the session-local. The user's session-scoped assertion
    is the more recent contextual signal; the SQL helper enforces
    this with ORDER BY is_session_local DESC."""
    cross_session = _store_fact(
        store, pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"}, polarity=1,
        is_session_local=0, session_ids=["A"],
    )
    session_local = _store_fact(
        store, pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"}, polarity=1,
        is_session_local=1, session_ids=["A"],
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "olives"},
        "polarity": 1, "source_text": "user likes olives",
    }
    result = lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        current_session="A", llm=_MockLLM(),
    )
    assert result.outcome is TierUOutcome.MATCH
    # The session-local (id 2) is preferred over the cross-session
    # (id 1) — even though id 1 was inserted first.
    assert result.matching_fact.id == session_local.id
    assert result.matching_fact.id != cross_session.id


def test_coexistence_falls_back_to_cross_session_in_other_session(
    store, predicate_oracle,
):
    """Same coexistence setup, but the lookup runs in session B.
    Session-local invisible; cross-session visible; the cross-session
    fact matches."""
    cross_session = _store_fact(
        store, pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"}, polarity=1,
        is_session_local=0, session_ids=["A"],
    )
    _store_fact(
        store, pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"}, polarity=1,
        is_session_local=1, session_ids=["A"],
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "olives"},
        "polarity": 1, "source_text": "user likes olives",
    }
    result = lookup(
        claim, store, predicate_oracle,
        key_slot_names=["agent", "object"],
        current_session="B", llm=_MockLLM(),
    )
    assert result.outcome is TierUOutcome.MATCH
    assert result.matching_fact.id == cross_session.id
