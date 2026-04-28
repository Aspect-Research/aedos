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
