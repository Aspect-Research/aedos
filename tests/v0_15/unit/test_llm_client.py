"""Tests for the v0.15 LLM client (mocked transport)."""

from __future__ import annotations

import pytest

from src.aedos_v0_15.llm.client import (
    DEFAULT_MODEL,
    DEFAULT_MODEL_BY_PURPOSE,
    LLMClient,
    ChatMessage,
    _resolve_purpose_model,
    is_openai_model,
)


class FakeTransport:
    def __init__(self, chat_return="response text", extract_return=None):
        self._chat_return = chat_return
        self._extract_return = extract_return or {"result": "ok"}
        self.calls = []

    def chat(self, system, messages, model="", purpose=None):
        self.calls.append({"type": "chat", "purpose": purpose, "model": model})
        return self._chat_return

    def extract_with_tool(self, system, user_message, tool, model="", purpose=None):
        self.calls.append({"type": "extract_with_tool", "tool": tool["name"], "purpose": purpose})
        return self._extract_return


@pytest.fixture
def transport():
    return FakeTransport()


@pytest.fixture
def client(transport):
    return LLMClient(_transport=transport)


class TestModelResolution:
    def test_is_openai_model_gpt(self):
        assert is_openai_model("gpt-4.1-mini")
        assert is_openai_model("gpt-4.1")

    def test_is_openai_model_claude(self):
        assert not is_openai_model("claude-haiku-4-5")
        assert not is_openai_model("claude-sonnet-4-6")

    def test_resolve_chat_uses_default(self):
        model = _resolve_purpose_model("chat", DEFAULT_MODEL)
        assert model == DEFAULT_MODEL_BY_PURPOSE["chat"]

    def test_resolve_unknown_purpose_uses_fallback(self):
        model = _resolve_purpose_model("unknown_purpose_xyz", "fallback-model")
        assert model == "fallback-model"

    def test_resolve_substrate_predicate_translation(self):
        model = _resolve_purpose_model("substrate:predicate_translation", DEFAULT_MODEL)
        assert model == DEFAULT_MODEL_BY_PURPOSE["substrate:predicate_translation"]


class TestChat:
    def test_chat_returns_text(self, client, transport):
        transport._chat_return = "hello world"
        result = client.chat("system", [ChatMessage("user", "hi")])
        assert result == "hello world"

    def test_chat_records_call(self, client, transport):
        client.chat("system", [ChatMessage("user", "test")])
        assert len(transport.calls) == 1
        assert transport.calls[0]["type"] == "chat"

    def test_complete_alias(self, client, transport):
        transport._chat_return = "complete result"
        result = client.complete("system", [ChatMessage("user", "x")])
        assert result == "complete result"

    def test_chat_stream_calls_on_token(self, client, transport):
        transport._chat_return = "streamed text"
        tokens = []
        result = client.chat_stream("system", [ChatMessage("user", "hello")], on_token=tokens.append)
        assert result == "streamed text"
        assert "streamed text" in tokens

    def test_purpose_passed_to_transport(self, client, transport):
        client.chat("system", [ChatMessage("user", "x")], purpose="substrate:predicate_translation")
        assert transport.calls[0]["purpose"] == "substrate:predicate_translation"


class TestExtractWithTool:
    def test_returns_dict(self, client, transport):
        transport._extract_return = {"claims": [], "count": 0}
        result = client.extract_with_tool(
            "system",
            "extract claims",
            {"name": "extract_claims", "description": "Extract", "input_schema": {}},
        )
        assert result == {"claims": [], "count": 0}

    def test_records_tool_call(self, client, transport):
        client.extract_with_tool(
            "system", "msg",
            {"name": "my_tool", "description": "", "input_schema": {}}
        )
        assert transport.calls[0]["tool"] == "my_tool"


class TestCallRecords:
    def test_pop_call_records_empties_list(self, client, transport):
        client.chat("system", [ChatMessage("user", "x")])
        client.chat("system", [ChatMessage("user", "y")])
        records = client.pop_call_records()
        assert len(records) == 0  # transport doesn't push real records
        # transport is mocked; call count via transport.calls
        assert len(transport.calls) == 2

    def test_rewrite_works(self, client, transport):
        transport._chat_return = "rewritten"
        result = client.rewrite("system", "original text")
        assert result == "rewritten"
