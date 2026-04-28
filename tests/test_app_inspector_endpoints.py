"""Tests for the FastAPI inspector endpoints in src/app.py.

The inspector endpoints (/api/turns, /api/trace/{id}, /api/facts,
/api/patterns) are read-only and don't make LLM calls. They were
under-tested — coverage analysis flagged app.py at 75%. This brings
the inspector path to ~100%.

The chat endpoint (/api/chat) makes LLM calls, so it stays gated
behind RUN_API_TESTS=1.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from src.fact_store import DEFAULT_USER_ID, Fact, FactStore


@pytest.fixture
def client_with_seed_data(tmp_path, monkeypatch):
    """Build a TestClient with a seeded DB so inspector endpoints have
    something to return."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # Suppress GLM dispatch path so /api/models reports it unavailable
    # in this fixture (deterministic).
    monkeypatch.delenv("MODAL_API_KEY", raising=False)

    db_path = tmp_path / "api.db"
    monkeypatch.setenv("AEDOS_DB_PATH", str(db_path))

    # Seed: a user turn, an assistant turn (with one event), and a fact
    # under each user_id.
    store = FactStore(str(db_path))
    user_turn = store.insert_turn("user", "hi", user_id="alice")
    asst_turn = store.insert_turn("assistant", "hello", user_id="alice")
    store.insert_pipeline_event(
        asst_turn, "assistant_draft", {"content": "hello"},
    )
    store.insert_fact(Fact(
        pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "tea"},
        polarity=1, confidence=0.95,
        asserted_by="user", verification_status="user_asserted",
        user_id="alice",
    ))
    store.insert_fact(Fact(
        pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "coffee"},
        polarity=1, confidence=0.95,
        asserted_by="user", verification_status="user_asserted",
        user_id="bob",
    ))
    store.close()

    from src.app import app
    with TestClient(app) as c:
        yield c


# ---- /api/turns -----------------------------------------------------


def test_turns_endpoint_returns_all_users_turns(client_with_seed_data):
    r = client_with_seed_data.get("/api/turns")
    assert r.status_code == 200
    body = r.json()
    # Inspector view: both Alice's turns (no Bob's because we didn't
    # seed any), regardless of user_id.
    assert len(body) == 2
    assert body[0]["role"] == "user"
    assert body[1]["role"] == "assistant"


# ---- /api/trace/{turn_id} -------------------------------------------


def test_trace_endpoint_returns_events(client_with_seed_data):
    # Find the assistant turn id from /api/turns.
    turns = client_with_seed_data.get("/api/turns").json()
    asst = next(t for t in turns if t["role"] == "assistant")
    r = client_with_seed_data.get(f"/api/trace/{asst['id']}")
    assert r.status_code == 200
    events = r.json()
    assert len(events) == 1
    assert events[0]["stage"] == "assistant_draft"


def test_trace_endpoint_404_for_unknown_turn(client_with_seed_data):
    r = client_with_seed_data.get("/api/trace/99999")
    assert r.status_code == 404
    assert "no events" in r.json()["detail"]


# ---- /api/facts -----------------------------------------------------


def test_facts_endpoint_returns_all_users(client_with_seed_data):
    """Inspector view shows every user's facts."""
    r = client_with_seed_data.get("/api/facts")
    assert r.status_code == 200
    body = r.json()
    user_ids = {f["user_id"] for f in body}
    assert user_ids == {"alice", "bob"}


def test_facts_endpoint_pattern_filter(client_with_seed_data):
    r = client_with_seed_data.get("/api/facts?pattern=preference")
    assert r.status_code == 200
    body = r.json()
    assert all(f["pattern"] == "preference" for f in body)
    assert len(body) == 2  # Alice's tea + Bob's coffee


def test_facts_endpoint_pattern_filter_no_match(client_with_seed_data):
    r = client_with_seed_data.get("/api/facts?pattern=spatial_temporal")
    assert r.status_code == 200
    assert r.json() == []


def test_facts_endpoint_only_valid_filter(client_with_seed_data):
    r = client_with_seed_data.get("/api/facts?only_valid=true")
    assert r.status_code == 200
    body = r.json()
    # Both seeded facts have valid_until=NULL → both qualify.
    assert len(body) == 2


def test_facts_endpoint_status_filter(client_with_seed_data):
    r = client_with_seed_data.get(
        "/api/facts?verification_status=user_asserted",
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2  # both seeded as user_asserted


# ---- /api/patterns --------------------------------------------------


def test_patterns_endpoint_returns_all_eight(client_with_seed_data):
    r = client_with_seed_data.get("/api/patterns")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 8  # the v0.5 pattern catalog
    names = {p["name"] for p in body}
    assert "preference" in names
    assert "quantitative" in names
    assert "spatial_temporal" in names


def test_patterns_endpoint_entries_well_shaped(client_with_seed_data):
    r = client_with_seed_data.get("/api/patterns")
    body = r.json()
    p = body[0]
    assert "description" in p
    assert "slots" in p
    assert "example_predicates" in p
    assert "query_strategy" in p
    assert "disambiguation_notes" in p
    # Removed fields should NOT be present.
    assert "verification_rules" not in p
    assert "flag_non_user_as_anomaly" not in p


# ---- /api/chat ------------------------------------------------------


def test_chat_endpoint_rejects_empty_message(client_with_seed_data):
    """The empty-message path doesn't need an LLM call — it 400s
    before invoking the pipeline."""
    r = client_with_seed_data.post("/api/chat", json={"message": ""})
    assert r.status_code == 400
    assert "must not be empty" in r.json()["detail"]

    r = client_with_seed_data.post("/api/chat", json={"message": "   "})
    assert r.status_code == 400


def test_health_endpoint(client_with_seed_data):
    """Health check confirms pipeline + DB + chat backend metadata."""
    r = client_with_seed_data.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["user_id"] == "default_user"  # or whatever default
    assert "chat_provider" in body
    assert "chat_model" in body
    assert "db_path" in body
    assert isinstance(body["turns_in_db"], int)
    assert "cache_enabled" in body
    assert "scoping_enabled" in body


def test_chat_endpoint_returns_structured_error_on_pipeline_failure(
    tmp_path, monkeypatch,
):
    """When the pipeline raises, /api/chat returns a 502 with a
    structured body (error_type, error_message, hint) so the UI can
    show a useful message instead of a generic 500."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AEDOS_DB_PATH", str(tmp_path / "fail.db"))

    from src.app import app

    with TestClient(app) as c:
        # Replace the pipeline's run_turn with one that raises. Accept
        # **kwargs since the chat endpoint now threads ``model=``
        # through to run_turn.
        c.app.state.pipeline.run_turn = (
            lambda _msg, **_kw: (_ for _ in ()).throw(
                RuntimeError("backend down")
            )
        )
        r = c.post("/api/chat", json={"message": "hi"})
        assert r.status_code == 502
        body = r.json()["detail"]
        assert body["error_type"] == "RuntimeError"
        assert "backend down" in body["error_message"]
        assert "pipeline raised" in body["hint"].lower()


def test_models_endpoint_returns_full_list(client_with_seed_data):
    """/api/models drives the chat UI dropdown. Returns the canonical
    id list, the server's default, and an availability flag per model
    (so GLM can be greyed out when MODAL_API_KEY is missing)."""
    r = client_with_seed_data.get("/api/models")
    assert r.status_code == 200
    body = r.json()
    ids = [m["id"] for m in body["models"]]
    assert ids == [
        "claude-opus-4-7", "claude-sonnet-4-6",
        "claude-haiku-4-5", "glm-5.1",
    ]
    # Every entry has a label + availability bool.
    for m in body["models"]:
        assert isinstance(m["label"], str) and m["label"]
        assert isinstance(m["available"], bool)
    # GLM unavailable in this fixture (no MODAL_API_KEY).
    glm = next(m for m in body["models"] if m["id"] == "glm-5.1")
    assert glm["available"] is False
    # Anthropic models always available (the LLMClient is constructed).
    for cid in ("claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"):
        m = next(x for x in body["models"] if x["id"] == cid)
        assert m["available"] is True
    # Default is some real model.
    assert body["default"] in ids


def test_chat_endpoint_threads_model_into_run_turn(tmp_path, monkeypatch):
    """The chat POST passes ``model`` into Pipeline.run_turn so the
    operator's selection drives the turn."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AEDOS_DB_PATH", str(tmp_path / "m.db"))

    from src.app import app
    captured: dict = {}

    class _StubTrace:
        def to_dict(self):
            return {}

    with TestClient(app) as c:
        c.app.state.pipeline.run_turn = (
            lambda msg, **kw: captured.update({"msg": msg, **kw}) or _StubTrace()
        )
        r = c.post("/api/chat", json={
            "message": "hi", "model": "claude-haiku-4-5",
        })
        assert r.status_code == 200
        assert captured["model"] == "claude-haiku-4-5"
        assert captured["msg"] == "hi"


def test_chat_endpoint_omitted_model_passes_none(tmp_path, monkeypatch):
    """When the UI omits ``model`` (older clients), the pipeline gets
    None and runs with its default model — no 422, no required-field
    error."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AEDOS_DB_PATH", str(tmp_path / "n.db"))

    from src.app import app
    captured: dict = {}

    class _StubTrace:
        def to_dict(self):
            return {}

    with TestClient(app) as c:
        c.app.state.pipeline.run_turn = (
            lambda msg, **kw: captured.update({"msg": msg, **kw}) or _StubTrace()
        )
        r = c.post("/api/chat", json={"message": "hi"})
        assert r.status_code == 200
        assert captured["model"] is None


def test_chat_stream_sse_emits_pipeline_events_then_done(tmp_path, monkeypatch):
    """POST /api/chat/stream returns Server-Sent Events: one
    pipeline_event per pipeline_events row as the turn runs, then a
    final ``done`` frame carrying the trace. The Flow View in the
    chat panel consumes these to draw the chart live."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("MODAL_API_KEY", raising=False)
    monkeypatch.setenv("AEDOS_DB_PATH", str(tmp_path / "stream.db"))

    from src.app import app

    class _FakeTrace:
        assistant_turn_id = 99
        original_content = None
        final_content = "ok"

        def to_dict(self):
            return {
                "user_turn_id": 98,
                "assistant_turn_id": 99,
                "final_content": "ok",
                "original_content": None,
                "verification_decisions": [],
                "interventions": [],
                "routing_anomalies": [],
            }

    def _fake_run_turn(msg, *, model=None):
        # Emit a few pipeline events so the SSE stream has content.
        store = app.state.pipeline.store
        turn_id = store.insert_turn("assistant", "draft")
        store.insert_pipeline_event(turn_id, "chat_model_call",
                                    {"provider": "anthropic", "model": "x"})
        store.insert_pipeline_event(turn_id, "assistant_extraction",
                                    {"valid_facts": [], "rejected_facts": []})
        store.insert_pipeline_event(turn_id, "verification",
                                    {"decisions": []})
        store.insert_pipeline_event(turn_id, "final", {"content": "ok"})
        return _FakeTrace()

    with TestClient(app) as c:
        c.app.state.pipeline.run_turn = _fake_run_turn
        with c.stream(
            "POST", "/api/chat/stream", json={"message": "hi"},
        ) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            body = b"".join(r.iter_bytes()).decode("utf-8", errors="replace")

    # Expect 4 pipeline_event frames + 1 done frame.
    pipeline_event_count = body.count("event: pipeline_event\n")
    assert pipeline_event_count == 4, body
    assert "event: done\n" in body
    # The done frame carries the trace dict — must include
    # assistant_turn_id and final_content.
    assert "\"assistant_turn_id\": 99" in body
    assert "\"final_content\": \"ok\"" in body


def test_chat_stream_emits_error_event_on_pipeline_failure(tmp_path, monkeypatch):
    """If run_turn raises, the SSE stream emits an ``error`` frame
    with error_type + error_message instead of a ``done`` frame."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("MODAL_API_KEY", raising=False)
    monkeypatch.setenv("AEDOS_DB_PATH", str(tmp_path / "err.db"))

    from src.app import app

    def _raise(_msg, **_kw):
        raise RuntimeError("backend exploded")

    with TestClient(app) as c:
        c.app.state.pipeline.run_turn = _raise
        with c.stream(
            "POST", "/api/chat/stream", json={"message": "hi"},
        ) as r:
            assert r.status_code == 200
            body = b"".join(r.iter_bytes()).decode("utf-8")

    assert "event: error\n" in body
    assert "RuntimeError" in body
    assert "backend exploded" in body
    assert "event: done\n" not in body


def test_chat_stream_rejects_empty_message(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("MODAL_API_KEY", raising=False)
    monkeypatch.setenv("AEDOS_DB_PATH", str(tmp_path / "e.db"))
    from src.app import app
    with TestClient(app) as c:
        r = c.post("/api/chat/stream", json={"message": "   "})
        assert r.status_code == 400


def test_health_endpoint_reports_db_error_with_ok_false(
    client_with_seed_data, monkeypatch,
):
    """If the SQLite read fails (e.g. corrupt DB / locked file),
    /api/health returns ok=False with the error string instead of
    crashing the request."""
    from src.app import app
    p = app.state.pipeline

    class _BoomConn:
        def execute(self, *a, **kw):
            raise RuntimeError("disk I/O error")

    # Stash the real conn, patch in the broken one, restore after.
    real = p.store._conn
    monkeypatch.setattr(p.store, "_conn", _BoomConn())
    try:
        r = client_with_seed_data.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert "RuntimeError" in body["error"]
        assert "disk I/O error" in body["error"]
    finally:
        p.store._conn = real


# ---- /api/cache -----------------------------------------------------


def test_cache_endpoint_returns_stats_and_entries(client_with_seed_data):
    """/api/cache returns the verification cache contents + aggregate
    stats. Hand-insert a couple of rows so we have something to assert
    on."""
    from src.app import app
    from datetime import datetime, timezone, timedelta
    p = app.state.pipeline
    now = datetime.now(timezone.utc)
    future = (now + timedelta(days=30)).isoformat()
    past = (now - timedelta(days=1)).isoformat()

    # Two cache rows: one immutable (no expires_at), one expired.
    p.store._conn.execute(
        "INSERT INTO verification_cache "
        "(canonical_key, pattern, predicate, verdict, stability_class, "
        " expires_at, evidence, hit_count, cached_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("k|live", "spatial_temporal", "born_in", "verified", "immutable",
         None, None, 5, now.isoformat(), now.isoformat()),
    )
    p.store._conn.execute(
        "INSERT INTO verification_cache "
        "(canonical_key, pattern, predicate, verdict, stability_class, "
        " expires_at, evidence, hit_count, cached_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("k|expired", "world_fact", "population_of", "verified",
         "months_stable", past, None, 1, now.isoformat(), now.isoformat()),
    )
    p.store._conn.commit()

    r = client_with_seed_data.get("/api/cache")
    assert r.status_code == 200
    body = r.json()
    assert body["stats"]["total_entries"] == 2
    assert body["stats"]["immutable_entries"] == 1
    assert body["stats"]["total_hits"] == 6  # 5 + 1

    # Find each entry by key and check the is_expired flag.
    by_key = {e["canonical_key"]: e for e in body["entries"]}
    assert by_key["k|live"]["is_expired"] is False  # immutable, no ttl
    assert by_key["k|expired"]["is_expired"] is True


def test_cache_endpoint_treats_malformed_expires_at_as_expired(
    client_with_seed_data,
):
    """If a cache row has a non-ISO expires_at (data corruption /
    schema migration), the endpoint marks it expired rather than
    crashing the request on ValueError."""
    from src.app import app
    p = app.state.pipeline
    p.store._conn.execute(
        "INSERT INTO verification_cache "
        "(canonical_key, pattern, predicate, verdict, stability_class, "
        " expires_at, evidence, hit_count, cached_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("k|broken", "p", "pred", "verified", "immutable",
         "not-a-date", None, 0,
         "2026-04-28T00:00:00+00:00",
         "2026-04-28T00:00:00+00:00"),
    )
    p.store._conn.commit()

    r = client_with_seed_data.get("/api/cache")
    assert r.status_code == 200
    body = r.json()
    broken = next(e for e in body["entries"] if e["canonical_key"] == "k|broken")
    assert broken["is_expired"] is True


# ---- /api/reset and / -----------------------------------------------


def test_reset_endpoint_wipes_db(client_with_seed_data):
    """/api/reset truncates the store. After calling it, /api/turns
    should return an empty list."""
    # Seed data is already there from the fixture.
    pre = client_with_seed_data.get("/api/turns").json()
    assert len(pre) > 0

    r = client_with_seed_data.post("/api/reset")
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    post = client_with_seed_data.get("/api/turns").json()
    assert post == []


def test_index_serves_html(client_with_seed_data):
    """GET / returns the SPA's index.html."""
    r = client_with_seed_data.get("/")
    assert r.status_code == 200
    # FileResponse for index.html — content type starts with text/html.
    ct = r.headers.get("content-type", "")
    assert ct.startswith("text/html")
    # Sanity: includes the well-known SPA markers.
    body = r.text.lower()
    assert "<!doctype html" in body or "<html" in body
