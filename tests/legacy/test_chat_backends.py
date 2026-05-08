"""Tests for the chat-backend factory and the AnthropicChatBackend wrapper."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from src.legacy.fact_store import FactStore
from src.llm_client import ChatMessage
from src.llm_clients import build_chat_backend
from src.llm_clients.anthropic_chat import AnthropicChatBackend


@dataclass
class FakeLLM:
    model: str = "claude-fake"
    chats: list[str] = field(default_factory=list)
    last_call: dict | None = None

    def chat(self, system, messages, max_tokens=4096, **_kwargs):
        self.last_call = {
            "system": system,
            "messages": list(messages),
            "max_tokens": max_tokens,
        }
        return self.chats.pop(0)


def test_anthropic_backend_delegates_to_llmclient_and_logs_event(tmp_path):
    store = FactStore(tmp_path / "t.db")
    turn_id = store.insert_turn("assistant", "")
    fake = FakeLLM(chats=["resp"])
    backend = AnthropicChatBackend(fake)

    out = backend.chat(
        system="sys",
        messages=[ChatMessage("user", "hi")],
        max_tokens=128,
        store=store,
        turn_id=turn_id,
    )
    assert out == "resp"
    assert fake.last_call["system"] == "sys"
    assert fake.last_call["max_tokens"] == 128

    events = [e for e in store.get_pipeline_events(turn_id)
              if e["stage"] == "chat_model_call"]
    assert len(events) == 1
    data = events[0]["data"]
    assert data["provider"] == "anthropic"
    assert data["model"] == "claude-fake"
    assert data["response_chars"] == 4
    assert data["error"] is None


def test_anthropic_backend_logs_event_on_failure(tmp_path):
    class BoomLLM:
        model = "claude-fake"

        def chat(self, system, messages, max_tokens=4096, **_kwargs):
            raise RuntimeError("kaboom")

    store = FactStore(tmp_path / "t.db")
    turn_id = store.insert_turn("assistant", "")
    backend = AnthropicChatBackend(BoomLLM())
    with pytest.raises(RuntimeError, match="kaboom"):
        backend.chat(
            system="s", messages=[ChatMessage("user", "x")],
            store=store, turn_id=turn_id,
        )
    events = [e for e in store.get_pipeline_events(turn_id)
              if e["stage"] == "chat_model_call"]
    assert len(events) == 1
    assert events[0]["data"]["error"] is not None
    assert "RuntimeError" in events[0]["data"]["error"]


def test_factory_returns_anthropic_backend():
    """v0.7.15: Anthropic is the only chat backend; the factory just
    wraps the LLMClient. The Modal/GLM provider was removed alongside
    the Modal backend itself."""
    fake = FakeLLM()
    backend = build_chat_backend(llm=fake)
    assert isinstance(backend, AnthropicChatBackend)


def test_factory_requires_llm():
    with pytest.raises(RuntimeError, match="AnthropicChatBackend requires"):
        build_chat_backend(llm=None)
