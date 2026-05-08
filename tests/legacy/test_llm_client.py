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


# ---- per-purpose model dispatch ----


def test_extract_with_tool_follows_per_purpose_routing(monkeypatch):
    """v0.8.0: extract_with_tool resolves its model through
    DEFAULT_MODEL_BY_PURPOSE (overridable via env). The chat model
    setting on self.llm has no effect on internal-purpose calls."""
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


# ---- v0.9.x: prompt-cache token reading (Anthropic side) ------------


def test_record_call_reads_anthropic_cache_creation_tokens(monkeypatch):
    """When the Anthropic usage object reports cache_creation_input_tokens
    (first call after deploy fills the cache), the cost ledger must
    include them billed at the 1.25× write multiplier — not silently drop."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = LLMClient()

    class _Usage:
        input_tokens = 100
        cache_creation_input_tokens = 2000
        cache_read_input_tokens = 0
        output_tokens = 50

    class _Resp:
        usage = _Usage()

    c._record_call("claude-opus-4-7", _Resp(),
                   purpose="extractor:user", duration_ms=10.0)
    [cc] = c.pop_recorded_calls()
    assert cc.input_tokens == 100
    assert cc.cache_creation_tokens == 2000
    assert cc.cache_read_tokens == 0
    # 2000 cache_creation tokens × $15/MTok × 1.25 = $0.0375
    import pytest
    assert cc.cache_creation_usd == pytest.approx(2000 / 1_000_000 * 15.00 * 1.25)


def test_record_call_reads_anthropic_cache_read_tokens(monkeypatch):
    """A cache hit on a subsequent call must show up as cache_read at 0.10×."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = LLMClient()

    class _Usage:
        input_tokens = 100
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 2000
        output_tokens = 50

    class _Resp:
        usage = _Usage()

    c._record_call("claude-haiku-4-5", _Resp(), purpose="chat", duration_ms=5.0)
    [cc] = c.pop_recorded_calls()
    assert cc.cache_read_tokens == 2000
    import pytest
    assert cc.cache_read_usd == pytest.approx(2000 / 1_000_000 * 1.00 * 0.10)


def test_record_call_handles_missing_cache_fields(monkeypatch):
    """Older mocks / responses without the cache fields must not crash —
    cache_creation_tokens / cache_read_tokens default to 0."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = LLMClient()

    class _Usage:
        input_tokens = 100
        output_tokens = 50

    class _Resp:
        usage = _Usage()

    c._record_call("claude-opus-4-7", _Resp())
    [cc] = c.pop_recorded_calls()
    assert cc.cache_creation_tokens == 0
    assert cc.cache_read_tokens == 0


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
    assert not is_openai_model("claude-haiku-4-5")


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


# ---- v0.9.x: OpenAI prompt-cache token reading ---------------------------


def test_call_cost_from_openai_usage_splits_cached_from_prompt():
    """OpenAI's prompt_tokens INCLUDES the cached portion. The helper
    must subtract cached_tokens out so the cache discount is applied
    once, not double-billed at the full input rate."""
    from src.openai_client import _call_cost_from_openai_usage

    class _Details:
        cached_tokens = 800

    class _Usage:
        prompt_tokens = 1000        # total: 200 uncached + 800 cached
        completion_tokens = 50
        prompt_tokens_details = _Details()

    cc = _call_cost_from_openai_usage(
        "gpt-4.1-mini", _Usage(), purpose="router", duration_ms=12.3,
    )
    assert cc.input_tokens == 200
    assert cc.cache_read_tokens == 800
    # gpt-4.1-mini: $0.40/MTok input. Cached portion: 800 × $0.20/MTok.
    import pytest
    assert cc.input_usd == pytest.approx(200 / 1_000_000 * 0.40)
    assert cc.cache_read_usd == pytest.approx(800 / 1_000_000 * 0.40 * 0.50)


def test_call_cost_from_openai_usage_no_cache_details():
    """OpenAI may omit prompt_tokens_details for short prompts that
    don't qualify for caching. cached_tokens defaults to 0."""
    from src.openai_client import _call_cost_from_openai_usage

    class _Usage:
        prompt_tokens = 200
        completion_tokens = 50
        # No prompt_tokens_details attr.

    cc = _call_cost_from_openai_usage(
        "gpt-4.1-mini", _Usage(), purpose="router", duration_ms=5.0,
    )
    assert cc.input_tokens == 200
    assert cc.cache_read_tokens == 0
    assert cc.cache_read_usd == 0.0


def test_call_cost_from_openai_usage_clamps_negative_uncached():
    """If a malformed usage object reports cached > prompt (shouldn't
    happen but defend), uncached clamps to 0 instead of negative."""
    from src.openai_client import _call_cost_from_openai_usage

    class _Details:
        cached_tokens = 500

    class _Usage:
        prompt_tokens = 100   # impossible: cached > prompt
        completion_tokens = 0
        prompt_tokens_details = _Details()

    cc = _call_cost_from_openai_usage(
        "gpt-4.1-mini", _Usage(), purpose="router", duration_ms=1.0,
    )
    assert cc.input_tokens == 0
    assert cc.cache_read_tokens == 500


# ---- v0.9.0 streaming chat -----------------------------------------------


class _FakeAnthropicStreamCtx:
    """Context manager mimicking Anthropic's messages.stream() return."""

    def __init__(self, deltas: list[str]):
        self._deltas = deltas

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        return iter(self._deltas)

    def get_final_message(self):
        class _U:
            input_tokens = 10
            output_tokens = 4
        class _M:
            usage = _U()
        return _M()


def test_chat_stream_calls_on_token_for_each_delta(monkeypatch):
    """v0.9.0: chat_stream invokes on_token for every text delta and
    returns the full accumulated text."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = LLMClient(model="claude-haiku-4-5")

    deltas = ["Hel", "lo, ", "world", "!"]

    class _FakeAnthropic:
        def __init__(self):
            self.messages = self
            self.last_kwargs = None

        def stream(self, **kwargs):
            self.last_kwargs = kwargs
            return _FakeAnthropicStreamCtx(deltas)

    fake = _FakeAnthropic()
    c._client = fake

    received: list[str] = []
    full = c.chat_stream("sys", [], on_token=received.append)

    assert received == deltas
    assert full == "Hello, world!"
    # Cost still recorded from the final message.
    calls = c.pop_recorded_calls()
    assert len(calls) == 1
    assert calls[0].input_tokens == 10
    assert calls[0].output_tokens == 4
    assert calls[0].purpose == "chat"


def test_chat_stream_swallows_on_token_exceptions(monkeypatch):
    """A buggy on_token must not break the chat call. Tokens after
    the failing one still get attempted; the full text returns."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    c = LLMClient(model="claude-haiku-4-5")

    class _FakeAnthropic:
        def __init__(self):
            self.messages = self
        def stream(self, **kwargs):
            return _FakeAnthropicStreamCtx(["a", "b", "c"])

    c._client = _FakeAnthropic()

    received: list[str] = []
    def boom(delta):
        received.append(delta)
        if delta == "b":
            raise RuntimeError("token handler failed")

    full = c.chat_stream("sys", [], on_token=boom)
    assert received == ["a", "b", "c"]
    assert full == "abc"
