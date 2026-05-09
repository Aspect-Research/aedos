"""Tests for POST /api/chat + /api/chat/stream.

Sync /api/chat: TestClient drives the endpoint, asserts the TurnTrace
shape. /api/chat/stream: TestClient drives the SSE endpoint and
parses the event frames; we assert the ``done`` frame carries the
trace and that pipeline_event frames fired in between.

The endpoint shares the v0.14 Pipeline; we inject a stub Pipeline
that returns a canned TurnTrace so the tests don't need a real LLM.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from src.app import _set_pipeline, _set_store, app
from src.fact_store import FactStore
from src.pipeline import TurnTrace


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    store = FactStore(str(tmp_path / "chat_test.db"))
    _set_store(store)
    yield store
    store.close()
    _set_store(None)


@pytest.fixture
def client(isolated_store):
    return TestClient(app)


@dataclass
class FakePipeline:
    """Minimal pipeline stub. run_turn writes some pipeline_events to
    the store so the SSE channel has frames to surface, then returns
    a canned TurnTrace."""
    store: FactStore

    def run_turn(self, user_message: str) -> TurnTrace:
        user_turn = self.store.insert_turn(role="user", content=user_message)
        asst_turn = self.store.insert_turn(role="assistant", content="hi back")
        self.store.insert_pipeline_event(user_turn, "user_extraction", {"facts": []})
        self.store.insert_pipeline_event(asst_turn, "chat_model_call", {"draft_length": 7})
        self.store.insert_pipeline_event(asst_turn, "assistant_extraction", {"facts": []})
        self.store.insert_pipeline_event(asst_turn, "final", {"final_length": 7, "rewrote": False})
        return TurnTrace(
            user_turn_id=user_turn,
            assistant_turn_id=asst_turn,
            final_content="hi back",
            original_content=None,
            user_extraction={"facts": []},
            user_decisions=[],
            assistant_extraction={"facts": []},
            verification_decisions=[],
            interventions=[],
            routing_anomalies=[],
        )


# ============================================================================
# Sync /api/chat
# ============================================================================


def test_chat_sync_returns_turn_trace(client, isolated_store):
    _set_pipeline(FakePipeline(store=isolated_store))
    resp = client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["final_content"] == "hi back"
    assert body["original_content"] is None
    assert body["user_decisions"] == []
    assert body["verification_decisions"] == []
    # Both turns landed in the store.
    turns = isolated_store.list_turns(user_id=None)
    assert len(turns) == 2


def test_chat_sync_rejects_empty(client, isolated_store):
    resp = client.post("/api/chat", json={"message": "   "})
    assert resp.status_code == 400


def test_chat_sync_502_on_pipeline_error(client, isolated_store):
    class ErrPipeline:
        store = isolated_store
        def run_turn(self, msg):
            raise RuntimeError("boom")
    _set_pipeline(ErrPipeline())
    resp = client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["error_type"] == "RuntimeError"
    assert detail["error_message"] == "boom"


# ============================================================================
# Streaming /api/chat/stream
# ============================================================================


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """Parse SSE frames out of a string. Returns list of (event, data)
    tuples. Skips comment frames (lines starting with ':')."""
    out = []
    blocks = text.split("\n\n")
    for blk in blocks:
        event_name = None
        data_lines = []
        for line in blk.splitlines():
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if event_name and data_lines:
            try:
                data = json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                data = {"raw": "\n".join(data_lines)}
            out.append((event_name, data))
    return out


def test_chat_stream_emits_pipeline_events_then_done(client, isolated_store):
    _set_pipeline(FakePipeline(store=isolated_store))
    with client.stream("POST", "/api/chat/stream", json={"message": "hi"}) as resp:
        assert resp.status_code == 200
        body = resp.read().decode("utf-8")
    frames = _parse_sse(body)
    event_names = [name for name, _ in frames]
    # The started + done frames bracket the stream.
    assert "started" in event_names
    assert "done" in event_names
    # Pipeline events fired in between (FakePipeline writes 4).
    pipeline_events = [d for n, d in frames if n == "pipeline_event"]
    assert len(pipeline_events) >= 1
    stages = {e["stage"] for e in pipeline_events}
    assert "user_extraction" in stages
    assert "final" in stages
    # done frame carries the trace.
    done_data = next(d for n, d in frames if n == "done")
    assert done_data["final_content"] == "hi back"


def test_chat_stream_error_frame_on_pipeline_failure(client, isolated_store):
    class ErrPipeline:
        store = isolated_store
        def run_turn(self, msg):
            raise ValueError("bad input")
    _set_pipeline(ErrPipeline())
    with client.stream("POST", "/api/chat/stream", json={"message": "hi"}) as resp:
        body = resp.read().decode("utf-8")
    frames = _parse_sse(body)
    error_frames = [d for n, d in frames if n == "error"]
    assert len(error_frames) == 1
    assert error_frames[0]["error_type"] == "ValueError"


def test_chat_stream_rejects_empty(client, isolated_store):
    resp = client.post("/api/chat/stream", json={"message": "   "})
    assert resp.status_code == 400
