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


# ---- single-model selection (with_active_model) ----


def test_with_active_model_swaps_all_three_model_attrs(monkeypatch):
    """One model selector drives chat, extractor, and corrector calls
    for the duration of the block. This is the core of the UI's
    single-model contract."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = LLMClient(
        model="claude-opus-4-7",
        extractor_model="claude-opus-4-7",
        corrector_model="claude-opus-4-7",
    )
    with c.with_active_model("claude-sonnet-4-6"):
        assert c.model == "claude-sonnet-4-6"
        assert c.extractor_model == "claude-sonnet-4-6"
        assert c.corrector_model == "claude-sonnet-4-6"
    # Restored after the block.
    assert c.model == "claude-opus-4-7"
    assert c.extractor_model == "claude-opus-4-7"
    assert c.corrector_model == "claude-opus-4-7"


def test_with_active_model_none_is_no_op(monkeypatch):
    """Passing None preserves the current model state — used as the
    default value of run_turn(model=...) so the existing pipeline
    behaviour is unchanged."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = LLMClient(model="claude-opus-4-7", corrector_model="claude-opus-4-7")
    with c.with_active_model(None):
        assert c.model == "claude-opus-4-7"
        assert c.corrector_model == "claude-opus-4-7"


def test_with_active_model_glm_is_no_op_on_anthropic_side(monkeypatch):
    """GLM is dispatched at the Pipeline level (Modal chat backend);
    on the Anthropic-backed LLMClient it must be a no-op so internal
    calls (extraction etc.) keep their prior model. GLM doesn't do
    tool use so it can't run those anyway."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = LLMClient(model="claude-opus-4-7", corrector_model="claude-opus-4-7")
    with c.with_active_model("glm-5.1"):
        assert c.model == "claude-opus-4-7"
        assert c.corrector_model == "claude-opus-4-7"


def test_with_active_model_rejects_unknown(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = LLMClient()
    with pytest.raises(ValueError, match="unknown model"):
        with c.with_active_model("claude-totally-fake"):
            pass


def test_with_active_model_restores_on_exception(monkeypatch):
    """Even when the block raises, the model attrs are restored."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = LLMClient(model="claude-opus-4-7", corrector_model="claude-opus-4-7")
    with pytest.raises(RuntimeError):
        with c.with_active_model("claude-haiku-4-5"):
            assert c.model == "claude-haiku-4-5"
            raise RuntimeError("boom")
    assert c.model == "claude-opus-4-7"
    assert c.corrector_model == "claude-opus-4-7"


def test_chat_uses_active_model(monkeypatch):
    """End-to-end: with_active_model('claude-sonnet-4-6') makes the
    next chat() call hit the API with that model id."""
    c, fake = _client_with_fake(monkeypatch, "claude-opus-4-7")
    with c.with_active_model("claude-sonnet-4-6"):
        c.chat("sys", [])
    assert fake.last_kwargs["model"] == "claude-sonnet-4-6"


def test_extract_with_tool_uses_active_model(monkeypatch):
    """Same for the extractor — single selection drives every step."""
    c, fake = _client_with_fake(monkeypatch, "claude-opus-4-7")
    # extract_with_tool hits the same fake; we need a tool_use block
    # in the response, so use a custom response.

    class _ToolResp:
        usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()
        content = [type("T", (), {"type": "tool_use", "name": "t",
                                  "input": {"facts": []}})()]
        stop_reason = "tool_use"

    fake.create = lambda **kw: setattr(fake, "last_kwargs", kw) or _ToolResp()
    with c.with_active_model("claude-haiku-4-5"):
        c.extract_with_tool(
            "sys", "msg",
            {"name": "t", "input_schema": {"type": "object"}},
        )
    assert fake.last_kwargs["model"] == "claude-haiku-4-5"


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
