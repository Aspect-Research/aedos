"""Tests for the v0.15 FastAPI application."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.aedos_v0_15 import __version__


@pytest.fixture
def client(tmp_path):
    import os
    os.environ["AEDOS_DB_PATH"] = str(tmp_path / "test.db")
    from src.aedos_v0_15.app import app
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
