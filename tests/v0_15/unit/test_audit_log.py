"""Tests for v0.15 audit log."""

from __future__ import annotations

import pytest

from src.aedos_v0_15.audit.log import log_event, query_events, get_event
from src.aedos_v0_15.database import open_memory_db


@pytest.fixture
def conn():
    c = open_memory_db()
    yield c
    c.close()


class TestLogEvent:
    def test_writes_row(self, conn):
        event_id = log_event(conn, "row_created", "predicate_translation:1", {"predicate": "holds_role"})
        assert isinstance(event_id, int)
        assert event_id > 0

    def test_event_data_round_trips(self, conn):
        data = {"predicate": "lives_in", "routing_hint": "kb_resolvable", "count": 42}
        event_id = log_event(conn, "row_created", "predicate_translation:2", data)
        event = get_event(conn, event_id)
        assert event is not None
        assert event["event_data"] == data

    def test_verification_context_nullable(self, conn):
        event_id = log_event(conn, "budget_exceeded", "claim:abc", {"budget": "wall_clock"})
        event = get_event(conn, event_id)
        assert event["verification_context"] is None

    def test_verification_context_stored(self, conn):
        event_id = log_event(conn, "row_retracted", "tier_u:5", {}, verification_context="vc-123")
        event = get_event(conn, event_id)
        assert event["verification_context"] == "vc-123"

    def test_occurred_at_populated(self, conn):
        event_id = log_event(conn, "circuit_breaker_triggered", "substrate:q1", {})
        event = get_event(conn, event_id)
        assert event["occurred_at"] is not None
        assert "T" in event["occurred_at"]


class TestQueryEvents:
    def test_filter_by_event_type(self, conn):
        log_event(conn, "row_created", "predicate_translation:1", {})
        log_event(conn, "row_retracted", "predicate_translation:1", {})
        log_event(conn, "row_created", "subsumption:1", {})

        created = query_events(conn, event_type="row_created")
        assert len(created) == 2
        assert all(e["event_type"] == "row_created" for e in created)

    def test_filter_by_subject(self, conn):
        log_event(conn, "row_created", "predicate_translation:1", {})
        log_event(conn, "row_created", "predicate_translation:2", {})
        log_event(conn, "row_created", "subsumption:1", {})

        pt_events = query_events(conn, event_subject="predicate_translation:1")
        assert len(pt_events) == 1

    def test_combined_filter(self, conn):
        log_event(conn, "row_created", "predicate_translation:1", {})
        log_event(conn, "row_retracted", "predicate_translation:1", {})

        result = query_events(conn, event_type="row_retracted", event_subject="predicate_translation:1")
        assert len(result) == 1
        assert result[0]["event_type"] == "row_retracted"

    def test_returns_empty_when_no_match(self, conn):
        result = query_events(conn, event_type="nonexistent_type")
        assert result == []

    def test_limit_respected(self, conn):
        for i in range(10):
            log_event(conn, "row_created", f"item:{i}", {})
        result = query_events(conn, event_type="row_created", limit=3)
        assert len(result) == 3
