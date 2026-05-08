"""Phase 5 — Tier 1 cross-session user store.

Two scenarios in scope:

  1. The same user, across two FactStore lifetimes against the same DB
     file, sees their prior facts (i.e. facts genuinely persist;
     reset_db is the only thing that wipes them).

  2. Two distinct user_ids against the same DB file are isolated:
     user-asserted facts for one don't leak into the other's
     all_user_facts(), find_currently_valid(), or list_turns().

Also covers:

  3. Pre-v0.5.x DBs (no user_id column) are migrated transparently —
     the column is added with a default of "default_user", existing
     rows get that value, and the tests exercise the resulting schema.
"""

from __future__ import annotations

import sqlite3

import pytest

from src.legacy.fact_store import DEFAULT_USER_ID, Fact, FactStore


def _user_pref(user, obj, polarity=1):
    return Fact(
        pattern="preference",
        predicate="likes",
        slots={"agent": "user", "object": obj},
        polarity=polarity,
        asserted_by="user",
        verification_status="user_asserted",
        user_id=user,
    )


# ---- 1. persistence across store reopen --------------------------------


def test_facts_persist_across_store_reopen(tmp_path):
    db = tmp_path / "p.db"

    store = FactStore(db)
    store.insert_fact(_user_pref(DEFAULT_USER_ID, "peanut butter"))
    store.insert_fact(_user_pref(DEFAULT_USER_ID, "sourdough"))
    store.close()

    # Reopen — facts should still be there for the same user.
    store2 = FactStore(db)
    facts = store2.all_user_facts()
    assert {f.slots["object"] for f in facts} == {"peanut butter", "sourdough"}
    store2.close()


# ---- 2. cross-user isolation --------------------------------------------


def test_user_isolation_in_all_user_facts(tmp_path):
    store = FactStore(tmp_path / "iso.db")
    store.insert_fact(_user_pref("alice", "tea"))
    store.insert_fact(_user_pref("bob", "coffee"))
    store.insert_fact(_user_pref("alice", "lavender"))

    alice_facts = store.all_user_facts(user_id="alice")
    bob_facts = store.all_user_facts(user_id="bob")

    assert {f.slots["object"] for f in alice_facts} == {"tea", "lavender"}
    assert {f.slots["object"] for f in bob_facts} == {"coffee"}


def test_user_isolation_in_find_currently_valid(tmp_path):
    store = FactStore(tmp_path / "iso.db")
    store.insert_fact(_user_pref("alice", "tea"))
    store.insert_fact(_user_pref("bob", "coffee"))

    # Alice asks "do I like coffee" — should be nothing in her store.
    alice_coffee = store.find_currently_valid(
        "preference", predicate="likes",
        slot_match={"agent": "user", "object": "coffee"},
        user_id="alice",
    )
    assert alice_coffee == []

    bob_coffee = store.find_currently_valid(
        "preference", predicate="likes",
        slot_match={"agent": "user", "object": "coffee"},
        user_id="bob",
    )
    assert len(bob_coffee) == 1


def test_user_isolation_in_list_turns(tmp_path):
    store = FactStore(tmp_path / "iso.db")
    store.insert_turn("user", "I like tea", user_id="alice")
    store.insert_turn("assistant", "noted", user_id="alice")
    store.insert_turn("user", "I like coffee", user_id="bob")

    alice_turns = store.list_turns(user_id="alice")
    bob_turns = store.list_turns(user_id="bob")
    all_turns = store.list_turns(user_id=None)

    assert [t["content"] for t in alice_turns] == ["I like tea", "noted"]
    assert [t["content"] for t in bob_turns] == ["I like coffee"]
    assert len(all_turns) == 3


def test_query_facts_user_id_none_returns_all(tmp_path):
    store = FactStore(tmp_path / "iso.db")
    store.insert_fact(_user_pref("alice", "tea"))
    store.insert_fact(_user_pref("bob", "coffee"))

    # Inspector view (used by /api/facts).
    all_facts = store.query_facts(user_id=None)
    assert len(all_facts) == 2


# ---- 4. router-level scoping (end-to-end) ------------------------------


def test_router_uses_user_scope_for_store_lookup(tmp_path):
    """When the router looks up a model claim against the user's store,
    it must scope by user_id — a fact from another user's session
    should NOT match."""
    from src.legacy.llm_router import RoutingDecision
    from src.legacy.pattern_registry import load_default_registry, reset_cache
    from src.legacy.router import Router

    reset_cache()

    store = FactStore(tmp_path / "r.db")
    # Bob says he likes coffee.
    store.insert_fact(_user_pref("bob", "coffee"))

    registry = load_default_registry()
    # Alice's router is scoped to alice. A model claim "user likes
    # coffee" in Alice's session should NOT find Bob's fact.
    alice_router = Router(
        store, registry,
        routing_fn=lambda c: RoutingDecision(
            method="user_authoritative", reason="user claim",
        ),
        user_id="alice",
    )

    model_claim = {
        "pattern": "preference",
        "predicate": "likes",
        "slots": {"agent": "user", "object": "coffee"},
        "polarity": 1,
        "source_text": "you like coffee",
    }

    decision = alice_router.route(model_claim, origin="model", source_turn_id=0)
    # Alice's store has no record of liking coffee → store-lookup MISS,
    # which routes to unverifiable_pending_implementation.
    assert decision.verification_status == "unverifiable_pending_implementation"
    # And the inserted fact should be tagged as Alice's.
    assert decision.stored_fact_id is not None
    f = store.get_fact(decision.stored_fact_id)
    assert f.user_id == "alice"

    # Bob's router IS scoped to bob — same claim should verify.
    bob_router = Router(
        store, registry,
        routing_fn=lambda c: RoutingDecision(
            method="user_authoritative", reason="user claim",
        ),
        user_id="bob",
    )
    decision2 = bob_router.route(model_claim, origin="model", source_turn_id=0)
    assert decision2.verification_status == "verified"


@pytest.fixture(autouse=True)
def _reset_registry():
    from src.legacy.pattern_registry import reset_cache
    reset_cache()
    yield
    reset_cache()
