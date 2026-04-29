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


def test_with_active_model_swaps_only_chat_model(monkeypatch):
    """v0.8.0 narrowed contract: with_active_model only swaps the
    CHAT model. Internal calls (extractor, corrector, judge, etc.)
    flow through DEFAULT_MODEL_BY_PURPOSE so the operator picking
    Opus 4.7 to test chat-side hallucination doesn't blow up
    internal-call cost."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = LLMClient(
        model="claude-opus-4-7",
        extractor_model="claude-opus-4-7",
        corrector_model="claude-opus-4-7",
    )
    with c.with_active_model("claude-sonnet-4-6"):
        assert c.model == "claude-sonnet-4-6"
        # extractor + corrector unchanged — internal calls follow
        # the per-purpose routing, not the operator's chat selection.
        assert c.extractor_model == "claude-opus-4-7"
        assert c.corrector_model == "claude-opus-4-7"
    # Restored after the block.
    assert c.model == "claude-opus-4-7"


def test_with_active_model_none_is_no_op(monkeypatch):
    """Passing None preserves the current model state — used as the
    default value of run_turn(model=...) so the existing pipeline
    behaviour is unchanged."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = LLMClient(model="claude-opus-4-7", corrector_model="claude-opus-4-7")
    with c.with_active_model(None):
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


def test_extract_with_tool_follows_per_purpose_routing(monkeypatch):
    """v0.8.0: extract_with_tool resolves its model through
    DEFAULT_MODEL_BY_PURPOSE (overridable via env), NOT through
    with_active_model. The operator's chat selection doesn't
    re-route extractor calls to that model."""
    # Force the extractor purpose to a known Anthropic model so the
    # test doesn't actually try to call OpenAI when an env var
    # isn't set. AEDOS_MODEL_extractor:user wins over the default.
    monkeypatch.setenv("AEDOS_MODEL_extractor:user", "claude-haiku-4-5")
    c, fake = _client_with_fake(monkeypatch, "claude-opus-4-7")

    class _ToolResp:
        usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()
        content = [type("T", (), {"type": "tool_use", "name": "t",
                                  "input": {"facts": []}})()]
        stop_reason = "tool_use"

    fake.create = lambda **kw: setattr(fake, "last_kwargs", kw) or _ToolResp()
    # Operator sets a different chat model — extractor should ignore it.
    with c.with_active_model("claude-sonnet-4-6"):
        c.extract_with_tool(
            "sys", "msg",
            {"name": "t", "input_schema": {"type": "object"}},
            purpose="extractor:user",
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


# ---- v0.8.0: per-purpose dispatch + OpenAI routing ----------------


def test_resolve_purpose_model_uses_default_map(monkeypatch):
    """purpose without env override resolves to the default map entry."""
    monkeypatch.delenv("AEDOS_MODEL_extractor:user", raising=False)
    from src.llm_client import _resolve_purpose_model, DEFAULT_MODEL_BY_PURPOSE
    m = _resolve_purpose_model("extractor:user", "fallback")
    assert m == DEFAULT_MODEL_BY_PURPOSE["extractor:user"]


def test_resolve_purpose_model_env_override_wins(monkeypatch):
    monkeypatch.setenv("AEDOS_MODEL_router", "claude-opus-4-7")
    from src.llm_client import _resolve_purpose_model
    m = _resolve_purpose_model("router", "fallback")
    assert m == "claude-opus-4-7"


def test_resolve_purpose_model_unknown_purpose_uses_fallback(monkeypatch):
    from src.llm_client import _resolve_purpose_model
    m = _resolve_purpose_model("not_a_purpose", "fallback-model")
    assert m == "fallback-model"


def test_is_openai_model_classification():
    from src.llm_client import is_openai_model
    assert is_openai_model("gpt-4.1-mini")
    assert is_openai_model("gpt-4o")
    assert is_openai_model("o1-preview")
    assert not is_openai_model("claude-opus-4-7")
    assert not is_openai_model("zai-org/GLM-5.1")


def test_chat_purpose_bypasses_per_purpose_routing(monkeypatch):
    """The chat purpose ALWAYS uses self.model — operator selection
    is the source of truth, the per-purpose default is ignored."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # Set AEDOS_MODEL_chat to a different model to prove the chat
    # path doesn't consult it.
    monkeypatch.setenv("AEDOS_MODEL_chat", "gpt-4.1-mini")
    fake = _FakeAnthropicClient()
    c = LLMClient(model="claude-sonnet-4-6", corrector_model="claude-opus-4-7")
    c._client = fake
    c.chat("sys", [], purpose="chat")
    # Anthropic SDK got the call with self.model, not the env override.
    assert fake.last_kwargs["model"] == "claude-sonnet-4-6"


def test_dispatch_routes_gpt_purpose_to_openai(monkeypatch):
    """When a purpose resolves to a gpt-* model, the call goes to
    the OpenAI client wrapper (not the Anthropic SDK)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("AEDOS_MODEL_router", "gpt-4.1-mini")

    c = LLMClient(model="claude-opus-4-7", corrector_model="claude-opus-4-7")

    # Stub the OpenAI client wrapper before it's lazily built.
    from src import openai_client as _oc

    routed = {}

    class _StubOAClient:
        def __init__(self, *a, **kw): pass
        def extract_with_tool(self, system, user_message, tool, **kw):
            routed["model"] = kw.get("model")
            routed["purpose"] = kw.get("purpose")
            return {"facts": []}

    monkeypatch.setattr(_oc, "OpenAIClient", _StubOAClient)

    c.extract_with_tool(
        "sys", "msg",
        {"name": "t", "input_schema": {"type": "object"}},
        purpose="router",
    )
    assert routed["model"] == "gpt-4.1-mini"
    assert routed["purpose"] == "router"


def test_dispatch_routes_claude_purpose_to_anthropic(monkeypatch):
    """When a purpose resolves to a claude-* model, the call goes to
    the Anthropic SDK (not the OpenAI wrapper)."""
    monkeypatch.setenv("AEDOS_MODEL_router", "claude-haiku-4-5")
    c, fake = _client_with_fake(monkeypatch, "claude-opus-4-7")

    class _ToolResp:
        usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()
        content = [type("T", (), {"type": "tool_use", "name": "t",
                                  "input": {"facts": []}})()]
        stop_reason = "tool_use"

    fake.create = lambda **kw: setattr(fake, "last_kwargs", kw) or _ToolResp()
    c.extract_with_tool(
        "sys", "msg",
        {"name": "t", "input_schema": {"type": "object"}},
        purpose="router",
    )
    assert fake.last_kwargs["model"] == "claude-haiku-4-5"


def test_openai_client_records_cost_into_shared_ledger(monkeypatch):
    """OpenAI calls land on the same _recorded_calls list as
    Anthropic calls so per-turn cost telemetry is unified."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("AEDOS_MODEL_router", "gpt-4.1-mini")

    c = LLMClient(model="claude-opus-4-7", corrector_model="claude-opus-4-7")

    from src import openai_client as _oc

    captured_recorder = {}

    class _StubOAClient:
        def __init__(self, *a, cost_recorder=None, **kw):
            captured_recorder["fn"] = cost_recorder
        def extract_with_tool(self, system, user_message, tool, **kw):
            # Call back through the recorder with a fake cost so we
            # can assert it lands on the shared ledger.
            from src.cost import cost_for_call
            self_recorder = captured_recorder["fn"]
            self_recorder(cost_for_call(
                "gpt-4.1-mini", 100, 50,
                purpose=kw.get("purpose"), duration_ms=42.0,
            ))
            return {"facts": []}

    monkeypatch.setattr(_oc, "OpenAIClient", _StubOAClient)

    c.extract_with_tool(
        "sys", "msg",
        {"name": "t", "input_schema": {"type": "object"}},
        purpose="router",
    )
    calls = c.pop_recorded_calls()
    assert len(calls) == 1
    assert calls[0].model == "gpt-4.1-mini"
    assert calls[0].purpose == "router"
    assert calls[0].input_tokens == 100
    assert calls[0].output_tokens == 50
