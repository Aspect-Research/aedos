"""Integration test — cost telemetry from LLMClient into the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from src.cost import CallCost
from src.llm_client import LLMClient


def _fake_response(input_tokens: int, output_tokens: int):
    """Stand-in for an Anthropic response with usage info."""
    @dataclass
    class _Usage:
        input_tokens: int
        output_tokens: int

    @dataclass
    class _Block:
        type: str = "text"
        text: str = "ok"

    @dataclass
    class _Resp:
        usage: _Usage = field(default_factory=lambda: _Usage(0, 0))
        content: list = field(default_factory=lambda: [_Block()])
        stop_reason: str = "end_turn"

    return _Resp(usage=_Usage(input_tokens, output_tokens),
                 content=[_Block()])


def _client(monkeypatch, model="claude-sonnet-4-6"):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = LLMClient(corrector_model=model, model=model, extractor_model=model)
    return c


def test_chat_records_cost(monkeypatch):
    c = _client(monkeypatch)
    fake_resp = _fake_response(100, 50)

    class _Anthropic:
        messages = type("M", (), {})()
        def __init__(self): pass

    a = _Anthropic()
    a.messages.create = lambda **kw: fake_resp  # type: ignore[attr-defined]
    c._client = a

    from src.llm_client import ChatMessage
    c.chat("sys", [ChatMessage(role="user", content="hi")])

    calls = c.pop_recorded_calls()
    assert len(calls) == 1
    assert calls[0].model == "claude-sonnet-4-6"
    assert calls[0].input_tokens == 100
    assert calls[0].output_tokens == 50
    assert calls[0].total_usd > 0


def test_extract_with_tool_records_cost(monkeypatch):
    c = _client(monkeypatch, model="claude-haiku-4-5")
    fake_resp = _fake_response(200, 100)
    fake_resp.content = [
        type("ToolUse", (), {
            "type": "tool_use", "name": "my_tool", "input": {"x": 1},
        })()
    ]

    class _Anthropic:
        messages = type("M", (), {})()
        def __init__(self): pass

    a = _Anthropic()
    a.messages.create = lambda **kw: fake_resp  # type: ignore[attr-defined]
    c._client = a

    c.extract_with_tool("sys", "msg",
                        {"name": "my_tool",
                         "description": "x",
                         "input_schema": {"type": "object", "properties": {}}})
    calls = c.pop_recorded_calls()
    assert len(calls) == 1
    assert calls[0].model == "claude-haiku-4-5"


def test_rewrite_records_cost(monkeypatch):
    c = _client(monkeypatch, model="claude-opus-4-7")
    fake_resp = _fake_response(50, 25)

    class _Anthropic:
        messages = type("M", (), {})()
        def __init__(self): pass

    a = _Anthropic()
    a.messages.create = lambda **kw: fake_resp  # type: ignore[attr-defined]
    c._client = a

    c.rewrite("sys", "msg")
    calls = c.pop_recorded_calls()
    assert len(calls) == 1
    assert calls[0].model == "claude-opus-4-7"
    # Opus is 15/75 — 50 input * 15/M + 25 output * 75/M
    expected = (50 * 15.0 + 25 * 75.0) / 1_000_000
    assert calls[0].total_usd == pytest.approx(expected, rel=1e-6)


def test_rewrite_with_model_override_records_override(monkeypatch):
    """When rewrite() is called with model='X', cost should use X."""
    c = _client(monkeypatch, model="claude-opus-4-7")
    fake_resp = _fake_response(50, 25)

    class _Anthropic:
        messages = type("M", (), {})()
        def __init__(self): pass

    a = _Anthropic()
    a.messages.create = lambda **kw: fake_resp  # type: ignore[attr-defined]
    c._client = a

    c.rewrite("sys", "msg", model="claude-sonnet-4-6")
    calls = c.pop_recorded_calls()
    assert calls[0].model == "claude-sonnet-4-6"


def test_pop_clears_ledger(monkeypatch):
    c = _client(monkeypatch)
    fake_resp = _fake_response(10, 10)

    class _Anthropic:
        messages = type("M", (), {})()
        def __init__(self): pass

    a = _Anthropic()
    a.messages.create = lambda **kw: fake_resp  # type: ignore[attr-defined]
    c._client = a

    from src.llm_client import ChatMessage
    c.chat("sys", [ChatMessage(role="user", content="hi")])
    assert len(c.pop_recorded_calls()) == 1
    assert len(c.pop_recorded_calls()) == 0  # cleared


def test_record_call_tolerates_missing_usage():
    """A response with no usage attribute shouldn't crash; it just
    doesn't record a cost."""
    from src.llm_client import LLMClient
    import os
    os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
    c = LLMClient()

    class _NoUsage:
        pass

    # Should not raise.
    c._record_call("claude-opus-4-7", _NoUsage())
    assert c.pop_recorded_calls() == []


# ---- pipeline integration: turn_cost event lands at end-of-turn ----


def test_turn_cost_event_emitted(tmp_path):
    """End-to-end: a Pipeline turn results in a turn_cost event with
    aggregated cost from the per-call ledger."""
    from src.corrector import Corrector
    from src.extractor import ClaimExtractor
    from src.fact_store import FactStore
    from src.llm_router import RoutingDecision
    from src.pattern_registry import load_default_registry, reset_cache
    from src.pipeline import Pipeline
    from src.router import Router

    reset_cache()

    @dataclass
    class _MockLLM:
        chats: list = field(default_factory=list)
        extracts: list = field(default_factory=list)
        rewrites: list = field(default_factory=list)
        corrector_model: str = "mock"
        # Pre-recorded "calls" the pipeline will pop.
        _recorded: list = field(default_factory=list)

        def chat(self, system, messages, max_tokens=4096):
            self._recorded.append(CallCost(
                model="claude-opus-4-7",
                input_tokens=100, output_tokens=50,
                input_usd=0.0015, output_usd=0.00375,
                total_usd=0.00525, pricing_known=True,
            ))
            return self.chats.pop(0)

        def extract_with_tool(self, system, user_message, tool, max_tokens=2048):
            self._recorded.append(CallCost(
                model="claude-opus-4-7",
                input_tokens=200, output_tokens=80,
                input_usd=0.003, output_usd=0.006,
                total_usd=0.009, pricing_known=True,
            ))
            return self.extracts.pop(0)

        def rewrite(self, system, user_message, max_tokens=2048, temperature=None):
            return self.rewrites.pop(0)

        def pop_recorded_calls(self):
            out = self._recorded
            self._recorded = []
            return out

    mock = _MockLLM(
        chats=["hi"],
        extracts=[{"facts": []}, {"facts": []}],
    )
    store = FactStore(tmp_path / "p.db")
    registry = load_default_registry()
    extractor = ClaimExtractor(mock, registry)
    router = Router(store, registry, routing_fn=lambda c: RoutingDecision(
        method="unverifiable", reason="x", confidence=0.9))
    p = Pipeline(store, registry, mock, extractor, router, Corrector(mock))
    trace = p.run_turn("hi")

    events = store.get_pipeline_events(trace.assistant_turn_id)
    cost_events = [e for e in events if e["stage"] == "turn_cost"]
    assert len(cost_events) == 1
    data = cost_events[0]["data"]
    # Two calls: chat + extract (the user-side extract). Plus the
    # assistant-side extract. So 3 total.
    assert data["total_calls"] == 3
    assert data["total_input_tokens"] == 100 + 200 + 200  # chat + 2× extract
    assert data["total_usd"] > 0
    assert "claude-opus-4-7" in data["by_model"]


def test_turn_cost_event_skipped_when_no_calls(tmp_path):
    """If no calls landed (legacy mock without pop_recorded_calls), no
    turn_cost event is emitted — but the pipeline still completes."""
    from src.corrector import Corrector
    from src.extractor import ClaimExtractor
    from src.fact_store import FactStore
    from src.llm_router import RoutingDecision
    from src.pattern_registry import load_default_registry, reset_cache
    from src.pipeline import Pipeline
    from src.router import Router

    reset_cache()

    @dataclass
    class _LegacyMock:
        chats: list = field(default_factory=list)
        extracts: list = field(default_factory=list)
        rewrites: list = field(default_factory=list)
        corrector_model: str = "mock"

        def chat(self, system, messages, max_tokens=4096):
            return self.chats.pop(0)

        def extract_with_tool(self, system, user_message, tool, max_tokens=2048):
            return self.extracts.pop(0)

        def rewrite(self, system, user_message, max_tokens=2048, temperature=None):
            return self.rewrites.pop(0)

    mock = _LegacyMock(
        chats=["hi"],
        extracts=[{"facts": []}, {"facts": []}],
    )
    store = FactStore(tmp_path / "p.db")
    registry = load_default_registry()
    p = Pipeline(
        store, registry, mock,
        ClaimExtractor(mock, registry),
        Router(store, registry, routing_fn=lambda c: RoutingDecision(
            method="unverifiable", reason="x", confidence=0.9)),
        Corrector(mock),
    )
    trace = p.run_turn("hi")
    events = store.get_pipeline_events(trace.assistant_turn_id)
    # No turn_cost event because the legacy mock has no pop_recorded_calls.
    assert "turn_cost" not in [e["stage"] for e in events]
