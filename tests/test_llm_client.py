"""Tests for LLMClient — focused on the temperature-drop behavior added
when Anthropic deprecated ``temperature`` for claude-opus-4-7."""

from __future__ import annotations

import logging

import pytest

from src.llm_client import LLMClient, _model_accepts_temperature


def test_model_accepts_temperature_classification():
    assert _model_accepts_temperature("claude-sonnet-4-6") is True
    assert _model_accepts_temperature("claude-haiku-4-5") is True
    assert _model_accepts_temperature("claude-opus-4-7") is False
    assert _model_accepts_temperature("claude-opus-4-7-20260101") is False
    # Older opus is fine.
    assert _model_accepts_temperature("claude-opus-4-6") is True


class _FakeAnthropicClient:
    """Captures the kwargs Anthropic.messages.create was called with."""

    def __init__(self):
        self.messages = self
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs

        class _Block:
            type = "text"
            text = "ok"

        class _Resp:
            content = [_Block()]
            stop_reason = "end_turn"

        return _Resp()


def _client_with_fake(monkeypatch, corrector_model):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fake = _FakeAnthropicClient()
    c = LLMClient(corrector_model=corrector_model)
    c._client = fake
    return c, fake


def test_rewrite_passes_temperature_for_supported_model(monkeypatch):
    c, fake = _client_with_fake(monkeypatch, "claude-sonnet-4-6")
    c.rewrite("sys", "user", temperature=0.3)
    assert fake.last_kwargs["temperature"] == 0.3


def test_rewrite_drops_temperature_for_opus_4_7(monkeypatch, caplog):
    c, fake = _client_with_fake(monkeypatch, "claude-opus-4-7")
    with caplog.at_level(logging.WARNING, logger="src.llm_client"):
        c.rewrite("sys", "user", temperature=0.3)
    assert "temperature" not in fake.last_kwargs
    assert any("dropping temperature" in r.message for r in caplog.records)


def test_rewrite_omits_temperature_when_none(monkeypatch):
    c, fake = _client_with_fake(monkeypatch, "claude-sonnet-4-6")
    c.rewrite("sys", "user")
    assert "temperature" not in fake.last_kwargs


def test_rewrite_omits_temperature_when_none_on_opus(monkeypatch):
    c, fake = _client_with_fake(monkeypatch, "claude-opus-4-7")
    c.rewrite("sys", "user")
    assert "temperature" not in fake.last_kwargs


# ---- error paths ----


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY not set"):
        LLMClient(api_key=None)


def test_extract_with_tool_no_tool_use_block_raises(monkeypatch):
    """If the model returns a response with no tool_use block, raise
    a clear error rather than returning silently."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = LLMClient()

    class _NoToolResp:
        usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()
        # Only a text block, no tool_use block
        content = [type("Text", (), {"type": "text", "text": "I won't call the tool"})()]
        stop_reason = "end_turn"

    class _Anthropic:
        messages = type("M", (), {})()
    a = _Anthropic()
    a.messages.create = lambda **kw: _NoToolResp()  # type: ignore[attr-defined]
    c._client = a

    with pytest.raises(RuntimeError, match="did not call tool"):
        c.extract_with_tool("sys", "msg",
                            {"name": "my_tool", "description": "x",
                             "input_schema": {"type": "object", "properties": {}}})


def test_first_text_no_text_block_raises():
    """_first_text raises RuntimeError when no text block is present
    (rare but possible — e.g. a tool-only response routed through chat)."""
    from src.llm_client import _first_text

    class _NoTextResp:
        content = [type("ToolUse", (), {"type": "tool_use"})()]
        stop_reason = "end_turn"

    with pytest.raises(RuntimeError, match="no text block"):
        _first_text(_NoTextResp())


def test_record_call_swallows_exceptions(monkeypatch):
    """If usage parsing throws (malformed response shape), _record_call
    should swallow — never break the calling chat()."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = LLMClient()

    class _BadUsage:
        @property
        def input_tokens(self):
            raise ValueError("usage shape changed")

    class _Resp:
        usage = _BadUsage()

    # Should not raise.
    c._record_call("claude-opus-4-7", _Resp())
    assert c.pop_recorded_calls() == []


def test_record_external_call_swallows_exceptions(monkeypatch):
    """The public hook for non-Anthropic backends must also be
    bulletproof — never raise."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = LLMClient()
    # Pass a value that can't be int()'d.
    c.record_external_call("claude-opus-4-7", "not-a-number", 5)  # type: ignore[arg-type]
    assert c.pop_recorded_calls() == []
