"""Tests for the v0.14 inspector endpoints: /api/facts, /api/patterns,
/api/cache, /api/turns.

The substrate inspectors and routing-memo inspector have their own
test files; this covers the inspector endpoints introduced in
v0.14.1 to power the Memory + Patterns + World inspector panels.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.app import _set_store, app
from src.fact_store import Fact, FactStore


@pytest.fixture
def isolated_store(tmp_path):
    store = FactStore(str(tmp_path / "inspector_test.db"))
    _set_store(store)
    yield store
    store.close()
    _set_store(None)


@pytest.fixture
def client(isolated_store):
    return TestClient(app)


# ============================================================================
# /api/facts
# ============================================================================


def _seed(store: FactStore) -> None:
    """Three facts: a user-asserted cross-session, a user-asserted
    session-local in session 'A', and a python_verifier-asserted row."""
    store.insert_fact(Fact(
        pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"},
        polarity=1, asserted_by="user",
        verification_status="user_asserted",
        is_session_local=0, session_ids=[],
    ))
    store.insert_fact(Fact(
        pattern="spatial_temporal", predicate="lives_in",
        slots={"entity": "user", "location": "Berlin"},
        polarity=1, asserted_by="user",
        verification_status="user_asserted",
        is_session_local=1, session_ids=["A"],
    ))
    store.insert_fact(Fact(
        pattern="quantitative", predicate="has_count",
        slots={"subject": "strawberry", "property": "letter_r", "value": 3},
        polarity=1, asserted_by="python_verifier",
        verification_status="verified",
        is_session_local=0, session_ids=[],
    ))


def test_facts_lists_all_when_unfiltered(client, isolated_store):
    _seed(isolated_store)
    rows = client.get("/api/facts").json()
    assert isinstance(rows, list)
    assert len(rows) == 3


def test_facts_filtered_by_asserted_by(client, isolated_store):
    _seed(isolated_store)
    rows = client.get("/api/facts?asserted_by=user").json()
    assert len(rows) == 2
    assert all(r["asserted_by"] == "user" for r in rows)


def test_facts_filtered_by_session_local(client, isolated_store):
    _seed(isolated_store)
    rows = client.get("/api/facts?is_session_local=1").json()
    assert len(rows) == 1
    assert rows[0]["slots"] == {"entity": "user", "location": "Berlin"}


def test_facts_filtered_by_current_session_keeps_session_local_only_in_session(
    client, isolated_store,
):
    """When ``current_session`` is given, session-local rows are only
    visible if the session id is in their session_ids. Cross-session
    rows are unaffected."""
    _seed(isolated_store)
    # Session A: includes the Berlin row.
    rows_a = client.get("/api/facts?current_session=A").json()
    assert len(rows_a) == 3
    # Session B: excludes the Berlin row.
    rows_b = client.get("/api/facts?current_session=B").json()
    assert len(rows_b) == 2
    assert not any(
        r["slots"].get("location") == "Berlin" for r in rows_b
    )


def test_facts_filtered_by_pattern_and_predicate(client, isolated_store):
    _seed(isolated_store)
    rows = client.get("/api/facts?pattern=preference&predicate=likes").json()
    assert len(rows) == 1
    assert rows[0]["pattern"] == "preference"


# ============================================================================
# /api/patterns
# ============================================================================


def test_patterns_returns_nine(client, isolated_store):
    rows = client.get("/api/patterns").json()
    names = {r["name"] for r in rows}
    expected = {
        "preference", "propositional_attitude", "spatial_temporal",
        "categorical", "role_assignment", "relational",
        "quantitative", "event", "mereological",
    }
    assert names == expected


def test_patterns_each_has_slot_schema(client, isolated_store):
    rows = client.get("/api/patterns").json()
    for r in rows:
        assert "slots" in r and isinstance(r["slots"], list)
        for s in r["slots"]:
            assert {"name", "type", "required"} <= set(s.keys())


# ============================================================================
# /api/cache
# ============================================================================


def test_cache_empty_returns_zero_stats(client, isolated_store):
    body = client.get("/api/cache").json()
    assert body["stats"]["total_entries"] == 0
    assert body["stats"]["lookups"] == 0
    assert body["stats"]["hit_rate"] is None
    assert body["entries"] == []


def test_cache_with_rows_returns_entries(client, isolated_store):
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(hours=24)).isoformat()
    isolated_store._conn.execute(
        "INSERT INTO verification_cache "
        "(canonical_key, pattern, predicate, verdict, evidence, "
        " stability_class, cached_at, expires_at, hit_count, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "k1", "spatial_temporal", "located_in", "SUPPORTED",
            json.dumps({"snippets": []}), "decade_stable",
            now.isoformat(), expires, 3, now.isoformat(),
        ),
    )
    isolated_store._conn.commit()

    body = client.get("/api/cache").json()
    assert body["stats"]["total_entries"] == 1
    assert body["stats"]["total_hits"] == 3
    assert body["stats"]["immutable_entries"] == 0
    assert len(body["entries"]) == 1
    assert body["entries"][0]["is_expired"] is False


def test_cache_marks_expired_entries(client, isolated_store):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    isolated_store._conn.execute(
        "INSERT INTO verification_cache "
        "(canonical_key, pattern, predicate, verdict, evidence, "
        " stability_class, cached_at, expires_at, hit_count, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "k1", "preference", "likes", "SUPPORTED", "{}",
            "days_stable", past, past, 0, past,
        ),
    )
    isolated_store._conn.commit()
    body = client.get("/api/cache").json()
    assert body["entries"][0]["is_expired"] is True


def test_cache_hit_rate_from_pipeline_events(client, isolated_store):
    turn_id = isolated_store.insert_turn(role="user", content="x")
    isolated_store.insert_pipeline_event(turn_id, "cache_lookup", {"result": "hit", "stability_class": "decade_stable"})
    isolated_store.insert_pipeline_event(turn_id, "cache_lookup", {"result": "hit", "stability_class": "years_stable"})
    isolated_store.insert_pipeline_event(turn_id, "cache_lookup", {"result": "miss"})
    body = client.get("/api/cache").json()
    s = body["stats"]
    assert s["lookups"] == 3
    assert s["lookup_hits"] == 2
    assert s["lookup_misses"] == 1
    assert s["hit_rate"] == pytest.approx(2 / 3)
    assert s["hits_by_stability"]["decade_stable"] == 1
    assert s["hits_by_stability"]["years_stable"] == 1


# ============================================================================
# /api/turns (already added in earlier commit; smoke-test the contract)
# ============================================================================


def test_turns_returns_inserted_turns(client, isolated_store):
    isolated_store.insert_turn(role="user", content="hi")
    isolated_store.insert_turn(role="assistant", content="hello back")
    rows = client.get("/api/turns").json()
    assert isinstance(rows, list)
    assert len(rows) == 2
    assert rows[0]["role"] == "user"
    assert rows[1]["role"] == "assistant"
