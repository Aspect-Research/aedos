"""Tests for the v0.6 PROTOTYPE — unique-value-slot detection.

The prototype catches the 'I was born in MA in turn N, then say I was
born in VA in turn M' adversarial pattern from the corpus turn 26
'BIG MISS'.

OFF by default; opt in with AEDOS_UNIQUE_VALUE_SLOTS=1. Even when
enabled, the rule only applies to pattern.predicate combos in the
hardcoded _UNIQUE_VALUE_SLOTS map. Currently:

    spatial_temporal.was_born_in (entity → location)

is the only entry — it's the canonical case and the one we know
matters. Adding more should be a deliberate operator choice.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.legacy.fact_store import DEFAULT_USER_ID, Fact, FactStore
from src.legacy.llm_router import RoutingDecision
from src.legacy.pattern_registry import load_default_registry, reset_cache
from src.legacy.router import RoutingOutcome, Router


@pytest.fixture(autouse=True)
def _reset():
    reset_cache()
    yield
    reset_cache()


def _user_birthplace(location: str) -> dict:
    return {
        "pattern": "spatial_temporal",
        "predicate": "was_born_in",
        "slots": {"entity": "user", "location": location,
                  "relation_kind": "birthplace"},
        "polarity": 1,
        "source_text": f"I was born in {location}",
    }


def _build_router(tmp_path):
    store = FactStore(tmp_path / "u.db")
    registry = load_default_registry()
    return store, Router(
        store, registry,
        routing_fn=lambda c: RoutingDecision(
            method="user_authoritative", reason="x",
        ),
    )


# ---- default behavior (env var OFF) — no change ----


def test_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("AEDOS_UNIQUE_VALUE_SLOTS", raising=False)
    store, router = _build_router(tmp_path)
    # Turn 1: store user born in Williamstown
    d1 = router.route(_user_birthplace("Williamstown, MA"),
                      origin="user", source_turn_id=1)
    assert d1.outcome == RoutingOutcome.USER_STORED

    # Turn 2: user says born in Williamsburg, VA. With env var OFF,
    # the existing per-key-slot model says "different location → no
    # contradiction" → just stores it.
    d2 = router.route(_user_birthplace("Williamsburg, VA"),
                      origin="user", source_turn_id=2)
    assert d2.outcome == RoutingOutcome.USER_STORED
    assert d2.outcome != RoutingOutcome.USER_CONTRADICTED_SELF
    store.close()


# ---- env var ON — flag self-contradictions ----


def test_self_contradiction_flagged_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("AEDOS_UNIQUE_VALUE_SLOTS", "1")
    store, router = _build_router(tmp_path)
    # Turn 1: born in Williamstown
    d1 = router.route(_user_birthplace("Williamstown, MA"),
                      origin="user", source_turn_id=1)
    assert d1.outcome == RoutingOutcome.USER_STORED

    # Turn 2: born in Williamsburg, VA — same entity (user), same
    # predicate, different location. Should flag.
    d2 = router.route(_user_birthplace("Williamsburg, VA"),
                      origin="user", source_turn_id=2)
    assert d2.outcome == RoutingOutcome.USER_CONTRADICTED_SELF
    # Still stores the new fact (we don't presume which one is right).
    assert d2.stored_fact_id is not None
    # Notes mention the conflict.
    assert any("contradicting themselves" in n for n in d2.notes)
    store.close()


def test_same_value_no_flag(tmp_path, monkeypatch):
    """Repeating the same fact is not a contradiction — that's
    USER_DUPLICATE."""
    monkeypatch.setenv("AEDOS_UNIQUE_VALUE_SLOTS", "1")
    store, router = _build_router(tmp_path)
    router.route(_user_birthplace("Williamstown, MA"),
                 origin="user", source_turn_id=1)
    d2 = router.route(_user_birthplace("Williamstown, MA"),
                     origin="user", source_turn_id=2)
    # Repeated fact → boost, not contradiction.
    assert d2.outcome == RoutingOutcome.USER_DUPLICATE
    store.close()


def test_different_predicate_does_not_trigger(tmp_path, monkeypatch):
    """The check is keyed on (pattern, predicate, identity_slot,
    value_slot). A different predicate doesn't trigger — even if it's
    the same pattern + entity."""
    monkeypatch.setenv("AEDOS_UNIQUE_VALUE_SLOTS", "1")
    store, router = _build_router(tmp_path)
    router.route(_user_birthplace("Williamstown, MA"),
                 origin="user", source_turn_id=1)
    # Different predicate (lives_in, not was_born_in).
    lives_claim = {
        "pattern": "spatial_temporal",
        "predicate": "lives_in",  # not in _UNIQUE_VALUE_SLOTS
        "slots": {"entity": "user", "location": "Boston, MA",
                  "relation_kind": "residence"},
        "polarity": 1,
        "source_text": "I live in Boston",
    }
    d = router.route(lives_claim, origin="user", source_turn_id=2)
    # No flag — lives_in isn't unique-per-entity (people move).
    assert d.outcome == RoutingOutcome.USER_STORED
    store.close()


def test_different_user_does_not_trigger(tmp_path, monkeypatch):
    """The check scopes to the same user_id. A different user with a
    different birthplace is not a contradiction."""
    monkeypatch.setenv("AEDOS_UNIQUE_VALUE_SLOTS", "1")
    store = FactStore(tmp_path / "u.db")
    registry = load_default_registry()
    alice_router = Router(
        store, registry,
        routing_fn=lambda c: RoutingDecision(
            method="user_authoritative", reason="x",
        ),
        user_id="alice",
    )
    bob_router = Router(
        store, registry,
        routing_fn=lambda c: RoutingDecision(
            method="user_authoritative", reason="x",
        ),
        user_id="bob",
    )
    # Alice says born in MA.
    alice_router.route(_user_birthplace("Williamstown, MA"),
                       origin="user", source_turn_id=1)
    # Bob says born in VA. Different user; no conflict.
    d = bob_router.route(_user_birthplace("Williamsburg, VA"),
                         origin="user", source_turn_id=2)
    assert d.outcome == RoutingOutcome.USER_STORED
    store.close()


def test_routing_outcome_enum_includes_user_contradicted_self():
    """Lock the new RoutingOutcome value into place — downstream code
    (corrector, UI) may dispatch on it."""
    assert RoutingOutcome.USER_CONTRADICTED_SELF.value == "user_contradicted_self"
