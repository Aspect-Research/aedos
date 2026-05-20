"""Tests for the v0.15 FastAPI application."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aedos import __version__


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AEDOS_DB_PATH", str(tmp_path / "test.db"))
    # F3 §6 / F-013: app.lifespan calls load_dotenv_if_present(). When the
    # project's .env carries RUN_LIVE_KB=1 (the Phase-10.5-ready setup),
    # the lifespan would set RUN_LIVE_KB in process env and poison
    # subsequent fixture-mode tests in test_wikidata_adapter.py. The
    # pre-existing empty-string values block dotenv's default
    # override=False from overwriting them; monkeypatch restores the
    # original (typically unset) state on teardown.
    monkeypatch.setenv("RUN_LIVE_KB", "")
    monkeypatch.setenv("RUN_LIVE_TESTS", "")
    from aedos.app import app
    with TestClient(app) as c:
        yield c


class TestHealthEndpoint:
    def test_returns_200(self, client):
        response = client.get("/health")
        assert response.status_code == 200

    def test_returns_correct_body(self, client):
        response = client.get("/health")
        data = response.json()
        assert data["status"] == "ok"
        assert data["version"] == __version__

    def test_version_is_0_15(self, client):
        response = client.get("/health")
        assert "0.15" in response.json()["version"]


class TestAuditEndpoints:
    def test_substrate_rows_returns_200(self, client):
        response = client.get("/audit/substrate-rows")
        assert response.status_code == 200

    def test_consistency_checks_returns_200(self, client):
        response = client.get("/audit/consistency-checks")
        assert response.status_code == 200

    def test_circuit_breakers_returns_200(self, client):
        response = client.get("/audit/circuit-breakers")
        assert response.status_code == 200

    def test_retractions_returns_200(self, client):
        response = client.get("/audit/retractions")
        assert response.status_code == 200
