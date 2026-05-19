"""Phase E1 — per-purpose, per-provider routing in the LLM client.

These tests exercise the routing-config resolution and the provider dispatch
without any network: the OpenAI-compatible path is checked with a fake client,
the Anthropic path via its missing-key error, and config resolution directly.
"""

from __future__ import annotations

import pytest

from aedos.llm.client import (
    DEFAULT_MODEL,
    DEFAULT_MODEL_BY_PURPOSE,
    ChatMessage,
    LLMClient,
    _config_for_model,
    _resolve_purpose_config,
)


# --------------------------------------------------------------------------
# Fake OpenAI-compatible client (OpenAI / OpenRouter share this SDK surface)
# --------------------------------------------------------------------------

class _FakeChatCompletions:
    def __init__(self, sink):
        self._sink = sink

    def create(self, model, messages, max_tokens, **kw):
        self._sink.append(model)
        usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()
        msg = type("M", (), {"content": "fake-reply", "tool_calls": None})()
        choice = type("C", (), {"message": msg})()
        return type("R", (), {"choices": [choice], "usage": usage})()


class _FakeOpenAIClient:
    def __init__(self, sink):
        self.chat = type("Chat", (), {"completions": _FakeChatCompletions(sink)})()


# --------------------------------------------------------------------------
# Routing-config resolution
# --------------------------------------------------------------------------

class TestPurposeRouting:
    def test_openai_purpose_carries_openai_endpoint(self):
        cfg = _resolve_purpose_config("extractor:user", DEFAULT_MODEL)
        assert cfg["base_url"] == "https://api.openai.com/v1"
        assert cfg["api_key_env_var"] == "OPENAI_API_KEY"
        assert cfg["model"] == DEFAULT_MODEL_BY_PURPOSE["extractor:user"]["model"]

    def test_chat_purpose_routes_to_anthropic(self):
        cfg = _resolve_purpose_config("chat", DEFAULT_MODEL)
        assert cfg["base_url"] is None
        assert cfg["api_key_env_var"] == "ANTHROPIC_API_KEY"

    def test_client_cfg_chat_and_untagged_route_to_anthropic(self):
        client = LLMClient(_transport=object())  # no real keys needed
        assert client._cfg("chat")["base_url"] is None
        assert client._cfg(None)["base_url"] is None

    def test_aedos_model_override_infers_provider(self, monkeypatch):
        # A claude-* override of a normally-OpenAI purpose flips it to Anthropic
        # (.env.example's pure-Anthropic deployment recipe).
        monkeypatch.setenv("AEDOS_MODEL_walker", "claude-haiku-4-5")
        cfg = _resolve_purpose_config("walker", DEFAULT_MODEL)
        assert cfg["model"] == "claude-haiku-4-5"
        assert cfg["base_url"] is None

    def test_config_for_model_distinguishes_providers(self):
        assert _config_for_model("gpt-4.1-mini")["base_url"] == "https://api.openai.com/v1"
        assert _config_for_model("claude-haiku-4-5")["base_url"] is None


# --------------------------------------------------------------------------
# Provider dispatch
# --------------------------------------------------------------------------

class TestProviderDispatch:
    def test_openai_purpose_dispatches_to_openai_path(self, monkeypatch):
        sink: list[str] = []
        client = LLMClient()
        monkeypatch.setattr(client, "_openai_client", lambda base_url, env: _FakeOpenAIClient(sink))
        out = client.chat("sys", [ChatMessage("user", "hi")], purpose="extractor:user")
        assert out == "fake-reply"
        # routed to the OpenAI-compatible path with the purpose's configured model
        assert sink == [DEFAULT_MODEL_BY_PURPOSE["extractor:user"]["model"]]


# --------------------------------------------------------------------------
# Missing-key errors are clear, not silent
# --------------------------------------------------------------------------

class TestMissingKeyError:
    def test_missing_openrouter_key_raises_named_error(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        client = LLMClient()
        with pytest.raises(RuntimeError) as exc:
            client._openai_client("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY")
        assert "OPENROUTER_API_KEY" in str(exc.value)

    def test_missing_anthropic_key_raises_on_chat(self, monkeypatch):
        # No ANTHROPIC_API_KEY → chat (Anthropic-routed) raises a clear error
        # rather than failing silently deep in the SDK.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        client = LLMClient()
        with pytest.raises(RuntimeError) as exc:
            client.chat("sys", [ChatMessage("user", "hi")])
        assert "ANTHROPIC_API_KEY" in str(exc.value)
