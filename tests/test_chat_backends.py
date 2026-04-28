"""Tests for the chat-backend factory and the AnthropicChatBackend wrapper."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from src.fact_store import FactStore
from src.llm_client import ChatMessage
from src.llm_clients import build_chat_backend
from src.llm_clients.anthropic_chat import AnthropicChatBackend
from src.llm_clients.modal_glm import ModalGLMBackend


@dataclass
class FakeLLM:
    model: str = "claude-fake"
    chats: list[str] = field(default_factory=list)
    last_call: dict | None = None

    def chat(self, system, messages, max_tokens=4096):
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

        def chat(self, system, messages, max_tokens=4096):
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


def test_factory_anthropic_default(monkeypatch):
    monkeypatch.delenv("AEDOS_CHAT_MODEL_PROVIDER", raising=False)
    fake = FakeLLM()
    backend = build_chat_backend(llm=fake)
    assert isinstance(backend, AnthropicChatBackend)


def test_factory_explicit_anthropic(monkeypatch):
    monkeypatch.setenv("AEDOS_CHAT_MODEL_PROVIDER", "anthropic")
    fake = FakeLLM()
    backend = build_chat_backend(llm=fake)
    assert isinstance(backend, AnthropicChatBackend)


def test_factory_modal(monkeypatch):
    monkeypatch.setenv("AEDOS_CHAT_MODEL_PROVIDER", "modal")
    monkeypatch.setenv("MODAL_API_KEY", "test-key")
    backend = build_chat_backend(llm=None)
    assert isinstance(backend, ModalGLMBackend)


def test_factory_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("AEDOS_CHAT_MODEL_PROVIDER", "openai")
    with pytest.raises(RuntimeError, match="AEDOS_CHAT_MODEL_PROVIDER"):
        build_chat_backend(llm=FakeLLM())


def test_factory_anthropic_requires_llm(monkeypatch):
    monkeypatch.setenv("AEDOS_CHAT_MODEL_PROVIDER", "anthropic")
    with pytest.raises(RuntimeError, match="AnthropicChatBackend requires"):
        build_chat_backend(llm=None)


def test_factory_modal_missing_key_raises(monkeypatch):
    monkeypatch.setenv("AEDOS_CHAT_MODEL_PROVIDER", "modal")
    monkeypatch.delenv("MODAL_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MODAL_API_KEY not set"):
        build_chat_backend(llm=None)
