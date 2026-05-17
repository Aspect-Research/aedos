"""pytest fixtures for Aedos v0.15 tests."""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.aedos_v0_15.database import open_memory_db
from src.aedos_v0_15.llm.client import LLMClient, ChatMessage


class MockTransport:
    """Canned-response LLM transport for tests.

    Calls are recorded; responses come from a pre-configured map keyed by
    purpose, or a default response if no specific entry matches.
    """

    def __init__(self, responses: dict[str, Any] | None = None, default: str = "mocked response"):
        self._responses = responses or {}
        self._default = default
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        system: str,
        messages: list[ChatMessage],
        model: str = "",
        purpose: str | None = None,
    ) -> str:
        self.calls.append({"type": "chat", "system": system, "messages": messages, "model": model, "purpose": purpose})
        key = purpose or "chat"
        return self._responses.get(key, self._default)

    def extract_with_tool(
        self,
        system: str,
        user_message: str,
        tool: dict[str, Any],
        model: str = "",
        purpose: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"type": "extract_with_tool", "tool": tool["name"], "purpose": purpose})
        key = f"extract:{tool['name']}"
        result = self._responses.get(key, self._responses.get("extract", {}))
        return result if isinstance(result, dict) else {}


@pytest.fixture
def db() -> sqlite3.Connection:
    """Fresh in-memory SQLite database with v0.15 schema."""
    conn = open_memory_db()
    yield conn
    conn.close()


@pytest.fixture
def mock_transport() -> MockTransport:
    return MockTransport()


@pytest.fixture
def llm_client_mock(mock_transport: MockTransport) -> LLMClient:
    """LLM client backed by MockTransport — no real API calls."""
    return LLMClient(_transport=mock_transport)


@pytest.fixture
def kb_mock() -> MagicMock:
    """Placeholder KB adapter mock. Populated in Phase 4."""
    kb = MagicMock()
    kb.resolve_entity.return_value = []
    kb.lookup_statements.return_value = []
    kb.subsumption.return_value = MagicMock(verdict="unrelated")
    return kb


@pytest.fixture
def temp_audit_log(db: sqlite3.Connection):
    """Isolated audit log backed by the fresh in-memory db."""
    from src.aedos_v0_15.audit import log
    return log
