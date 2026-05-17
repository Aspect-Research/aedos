"""Integration: audit-log query endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# App factory with synthetic audit data
# ---------------------------------------------------------------------------

def _make_test_app():
    from src.aedos_v0_15.app import app
    from src.aedos_v0_15.database import open_memory_db

    db = open_memory_db()

    # Seed audit_log with synthetic events
    rows = [
        ("row_created", "predicate_translation:1", '{"table": "predicate_translation", "row_id": 1, "predicate": "holds_role"}'),
        ("row_created", "predicate_translation:2", '{"table": "predicate_translation", "row_id": 2, "predicate": "born_in"}'),
        ("row_created", "subsumption:3", '{"table": "subsumption", "row_id": 3}'),
        ("consistency_violation", "predicate_translation", '{"inconsistency_class": "transitive_equivalence_violation", "table": "predicate_translation"}'),
        ("consistency_violation", "subsumption", '{"inconsistency_class": "contradicting_subsumption", "table": "subsumption"}'),
        ("circuit_breaker_triggered", "predicate_translation:transitive_equivalence_violation", '{"signature": "predicate_translation:transitive_equivalence_violation:x=1", "cycle_count": 3}'),
        ("row_retracted", "predicate_translation:1", '{"table": "predicate_translation", "row_id": 1, "reason": "consistency_conflict"}'),
        ("row_retracted", "subsumption:3", '{"table": "subsumption", "row_id": 3, "reason": "consistency_conflict"}'),
    ]
    import json
    for event_type, subject, event_data in rows:
        db.execute(
            "INSERT INTO audit_log (event_type, event_subject, event_data, occurred_at) VALUES (?, ?, ?, datetime('now'))",
            (event_type, subject, event_data),
        )
    db.commit()

    import src.aedos_v0_15.app as _app_module
    _app_module._db = db
    _app_module._chat_wrapper = None
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /audit/substrate-rows
# ---------------------------------------------------------------------------

class TestAuditSubstrateRows:
    def test_returns_200(self):
        client = _make_test_app()
        resp = client.get("/audit/substrate-rows")
        assert resp.status_code == 200

    def test_returns_events_key(self):
        client = _make_test_app()
        resp = client.get("/audit/substrate-rows")
        body = resp.json()
        assert "events" in body

    def test_returns_row_created_events(self):
        client = _make_test_app()
        resp = client.get("/audit/substrate-rows")
        events = resp.json()["events"]
        assert len(events) > 0

    def test_limit_parameter(self):
        client = _make_test_app()
        resp = client.get("/audit/substrate-rows?limit=1")
        events = resp.json()["events"]
        assert len(events) <= 1

    def test_no_db_returns_503(self):
        from src.aedos_v0_15.app import app
        import src.aedos_v0_15.app as _app_module
        original = _app_module._db
        _app_module._db = None
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/audit/substrate-rows")
        _app_module._db = original
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# GET /audit/consistency-checks
# ---------------------------------------------------------------------------

class TestAuditConsistencyChecks:
    def test_returns_200(self):
        client = _make_test_app()
        resp = client.get("/audit/consistency-checks")
        assert resp.status_code == 200

    def test_returns_events_key(self):
        client = _make_test_app()
        body = client.get("/audit/consistency-checks").json()
        assert "events" in body

    def test_returns_consistency_events(self):
        client = _make_test_app()
        events = client.get("/audit/consistency-checks").json()["events"]
        assert len(events) >= 2

    def test_events_have_correct_type(self):
        client = _make_test_app()
        events = client.get("/audit/consistency-checks").json()["events"]
        for e in events:
            assert e["event_type"] == "consistency_violation"

    def test_limit_parameter(self):
        client = _make_test_app()
        events = client.get("/audit/consistency-checks?limit=1").json()["events"]
        assert len(events) <= 1


# ---------------------------------------------------------------------------
# GET /audit/circuit-breakers
# ---------------------------------------------------------------------------

class TestAuditCircuitBreakers:
    def test_returns_200(self):
        client = _make_test_app()
        resp = client.get("/audit/circuit-breakers")
        assert resp.status_code == 200

    def test_returns_events_key(self):
        client = _make_test_app()
        body = client.get("/audit/circuit-breakers").json()
        assert "events" in body

    def test_returns_circuit_breaker_events(self):
        client = _make_test_app()
        events = client.get("/audit/circuit-breakers").json()["events"]
        assert len(events) >= 1

    def test_events_have_correct_type(self):
        client = _make_test_app()
        events = client.get("/audit/circuit-breakers").json()["events"]
        for e in events:
            assert e["event_type"] == "circuit_breaker_triggered"


# ---------------------------------------------------------------------------
# GET /audit/retractions
# ---------------------------------------------------------------------------

class TestAuditRetractions:
    def test_returns_200(self):
        client = _make_test_app()
        resp = client.get("/audit/retractions")
        assert resp.status_code == 200

    def test_returns_events_key(self):
        client = _make_test_app()
        body = client.get("/audit/retractions").json()
        assert "events" in body

    def test_returns_retraction_events(self):
        client = _make_test_app()
        events = client.get("/audit/retractions").json()["events"]
        assert len(events) >= 2

    def test_events_have_correct_type(self):
        client = _make_test_app()
        events = client.get("/audit/retractions").json()["events"]
        for e in events:
            assert e["event_type"] == "row_retracted"

    def test_limit_parameter(self):
        client = _make_test_app()
        events = client.get("/audit/retractions?limit=1").json()["events"]
        assert len(events) <= 1


# ---------------------------------------------------------------------------
# Endpoint isolation — none mutate substrate rows
# ---------------------------------------------------------------------------

class TestAuditEndpointsReadOnly:
    def test_substrate_rows_is_get_only(self):
        client = _make_test_app()
        resp = client.post("/audit/substrate-rows", json={})
        assert resp.status_code == 405

    def test_consistency_checks_is_get_only(self):
        client = _make_test_app()
        resp = client.post("/audit/consistency-checks", json={})
        assert resp.status_code == 405

    def test_circuit_breakers_is_get_only(self):
        client = _make_test_app()
        resp = client.post("/audit/circuit-breakers", json={})
        assert resp.status_code == 405

    def test_retractions_is_get_only(self):
        client = _make_test_app()
        resp = client.post("/audit/retractions", json={})
        assert resp.status_code == 405
