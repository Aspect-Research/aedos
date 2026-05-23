"""Phase E1 — per-purpose, per-provider routing in the LLM client.

These tests exercise the routing-config resolution and the provider dispatch
without any network: the OpenAI-compatible path is checked with a fake client,
the Anthropic path via its missing-key error, and config resolution directly.
"""

from __future__ import annotations

import json

import pytest

from aedos.llm.client import (
    DEFAULT_MODEL,
    DEFAULT_MODEL_BY_PURPOSE,
    ChatMessage,
    LLMClient,
    _config_for_model,
    _resolve_purpose_config,
)

_OVERRIDE = {
    "*": {
        "model": "vendor/candidate-x",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env_var": "OPENROUTER_API_KEY",
    }
}


# --------------------------------------------------------------------------
# Fake OpenAI-compatible client (OpenAI / OpenRouter share this SDK surface)
# --------------------------------------------------------------------------

class _FakeChatCompletions:
    def __init__(self, sink):
        self._sink = sink

    def create(self, model, messages, max_tokens, **kw):
        self._sink.append({"model": model, "extra_body": kw.get("extra_body")})
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
        # Phase E5 migrated extractor:* purposes to Anthropic/Haiku 4.5; this
        # test uses python_verifier (still OpenAI-routed as of v0.15) to
        # exercise the OpenAI-endpoint resolution path. Other substrate:*
        # purposes are also still OpenAI-routed and would work equivalently.
        cfg = _resolve_purpose_config("python_verifier", DEFAULT_MODEL)
        assert cfg["base_url"] == "https://api.openai.com/v1"
        assert cfg["api_key_env_var"] == "OPENAI_API_KEY"
        assert cfg["model"] == DEFAULT_MODEL_BY_PURPOSE["python_verifier"]["model"]

    def test_extractor_purpose_routes_to_anthropic_after_phase_e5(self):
        # Phase E5 (2026-05-23) routed extractor:user and extractor:assistant
        # to claude-haiku-4-5 after Phase E3 prompt-engineering produced
        # 53/53 = 100% on the cleaned extraction corpus. Earlier rc.x tags
        # had these routing to gpt-4.1-mini / gpt-4.1; the test guards
        # against the migration being silently reverted.
        for purpose in ("extractor:user", "extractor:assistant"):
            cfg = _resolve_purpose_config(purpose, DEFAULT_MODEL)
            assert cfg["base_url"] is None, purpose
            assert cfg["api_key_env_var"] == "ANTHROPIC_API_KEY", purpose
            assert cfg["model"] == "claude-haiku-4-5", purpose

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
        monkeypatch.setenv("AEDOS_MODEL_python_verifier", "claude-haiku-4-5")
        cfg = _resolve_purpose_config("python_verifier", DEFAULT_MODEL)
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
        # Phase E5 migrated extractor:* to Anthropic; this test now uses
        # python_verifier (still OpenAI-routed in v0.15) to exercise the
        # OpenAI dispatch path.
        sink: list[dict] = []
        client = LLMClient()
        monkeypatch.setattr(client, "_openai_client", lambda base_url, env: _FakeOpenAIClient(sink))
        out = client.chat("sys", [ChatMessage("user", "hi")], purpose="python_verifier")
        assert out == "fake-reply"
        # routed to the OpenAI-compatible path with the purpose's configured model
        assert sink[0]["model"] == DEFAULT_MODEL_BY_PURPOSE["python_verifier"]["model"]
        assert sink[0]["extra_body"] is None  # no override → no extra_body

    def test_extra_body_from_override_reaches_create(self, monkeypatch):
        override = {"*": {
            "model": "vendor/x", "base_url": "https://openrouter.ai/api/v1",
            "api_key_env_var": "OPENROUTER_API_KEY",
            "extra_body": {"reasoning": {"enabled": False}},
        }}
        monkeypatch.setenv("AEDOS_OVERRIDE_MODEL_BY_PURPOSE", json.dumps(override))
        sink: list[dict] = []
        client = LLMClient()
        monkeypatch.setattr(client, "_openai_client", lambda base_url, env: _FakeOpenAIClient(sink))
        client.chat("sys", [ChatMessage("user", "hi")], purpose="python_verifier")
        assert sink[0]["extra_body"] == {"reasoning": {"enabled": False}}


# --------------------------------------------------------------------------
# Missing-key errors are clear, not silent
# --------------------------------------------------------------------------

class TestWholeRunOverride:
    """AEDOS_OVERRIDE_MODEL_BY_PURPOSE — the Phase E comparison hook."""

    def test_wildcard_override_applies_to_internal_purpose(self, monkeypatch):
        monkeypatch.setenv("AEDOS_OVERRIDE_MODEL_BY_PURPOSE", json.dumps(_OVERRIDE))
        cfg = _resolve_purpose_config("substrate:predicate_translation", DEFAULT_MODEL)
        assert cfg["model"] == "vendor/candidate-x"
        assert cfg["base_url"] == "https://openrouter.ai/api/v1"
        assert cfg["api_key_env_var"] == "OPENROUTER_API_KEY"

    def test_wildcard_override_does_not_touch_chat(self, monkeypatch):
        monkeypatch.setenv("AEDOS_OVERRIDE_MODEL_BY_PURPOSE", json.dumps(_OVERRIDE))
        cfg = _resolve_purpose_config("chat", DEFAULT_MODEL)
        assert cfg["base_url"] is None  # the chat slot is never overridden

    def test_malformed_override_is_ignored(self, monkeypatch):
        monkeypatch.setenv("AEDOS_OVERRIDE_MODEL_BY_PURPOSE", "not json{")
        cfg = _resolve_purpose_config("python_verifier", DEFAULT_MODEL)
        assert cfg["model"] == DEFAULT_MODEL_BY_PURPOSE["python_verifier"]["model"]

    def test_override_carries_extra_body(self, monkeypatch):
        override = {"*": dict(_OVERRIDE["*"], extra_body={"reasoning": {"enabled": False}})}
        monkeypatch.setenv("AEDOS_OVERRIDE_MODEL_BY_PURPOSE", json.dumps(override))
        cfg = _resolve_purpose_config("python_verifier", DEFAULT_MODEL)
        assert cfg["extra_body"] == {"reasoning": {"enabled": False}}


class _BrokenChatCompletions:
    """Returns a malformed response with `choices = None` and a populated
    `usage` — mimics OpenRouter's bad-response shape (the V4-Flash failure
    mode: `_record` runs, then `resp.choices[0]` raises 'NoneType' is not
    subscriptable)."""

    def create(self, model, messages, max_tokens, **kw):
        usage = type("U", (), {"prompt_tokens": 5, "completion_tokens": 0})()
        class _R:
            def model_dump(self):
                return {"choices": None, "usage": {"prompt_tokens": 5, "completion_tokens": 0}}
        r = _R()
        r.choices = None
        r.usage = usage
        return r


class _BrokenOpenAIClient:
    def __init__(self):
        self.chat = type("C", (), {"completions": _BrokenChatCompletions()})()


class TestRawResponseAttachedOnFailure:
    """When a malformed response makes downstream parsing raise, the raised
    exception should carry the raw response on `._raw_response` so the
    diagnostic transcript can record what the model actually returned."""

    def test_extract_with_tool_attaches_raw_response_on_parse_failure(self, monkeypatch):
        # Phase E5 routed extractor:* to Anthropic; this test exercises the
        # OpenAI parse-failure path, so use python_verifier (still OpenAI).
        client = LLMClient()
        monkeypatch.setattr(client, "_openai_client", lambda b, e: _BrokenOpenAIClient())
        with pytest.raises(TypeError) as exc_info:
            client.extract_with_tool(
                "sys", "msg",
                {"name": "extract_claims", "description": "x", "input_schema": {}},
                purpose="python_verifier",
            )
        raw = getattr(exc_info.value, "_raw_response", None)
        assert raw is not None
        # model_dump path preferred → a dict; otherwise repr string
        assert isinstance(raw, (dict, str))


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
