"""Tests for the /v2/api/routing-memo HTTP endpoints.

GET /v2/api/routing-memo lists every memo row (operator UI).
GET /v2/api/routing-memo/{pattern}/{predicate} inspects a single row;
404 on miss.

Endpoint tests use a tmp_path-backed FactStore via _set_store so the
real ``aedos_v2.db`` is never touched.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.app import _set_store, app
from src.fact_store import FactStore
from src.layer2_routing.routing_memo import RoutingMemo


@pytest.fixture
def isolated_store(tmp_path):
    s = FactStore(tmp_path / "memo_endpoint.db")
    _set_store(s)
    yield s
    s.close()
    _set_store(None)


@pytest.fixture
def client():
    return TestClient(app)


def test_list_returns_empty_when_no_rows(client, isolated_store):
    resp = client.get("/api/routing-memo")
    assert resp.status_code == 200
    assert resp.json() == {"rows": []}


def test_list_returns_recorded_rows(client, isolated_store):
    memo = RoutingMemo(isolated_store)
    memo.record("preference", "likes", "user_authoritative", "user pref")
    memo.record("quantitative", "has_count", "python", "pure")
    resp = client.get("/api/routing-memo")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) == 2
    keys = {(r["pattern"], r["predicate"]) for r in rows}
    assert keys == {
        ("preference", "likes"),
        ("quantitative", "has_count"),
    }
    methods = {r["method"] for r in rows}
    assert methods == {"user_authoritative", "python"}


def test_get_single_entry(client, isolated_store):
    memo = RoutingMemo(isolated_store)
    memo.record("relational", "founded_by", "retrieval", "external")
    resp = client.get("/api/routing-memo/relational/founded_by")
    assert resp.status_code == 200
    body = resp.json()
    assert body["method"] == "retrieval"
    assert body["reason"] == "external"
    assert body["affirmed_count"] == 0
    assert body["contradicted_count"] == 0


def test_get_unknown_entry_returns_404(client, isolated_store):
    resp = client.get("/api/routing-memo/preference/never_seen")
    assert resp.status_code == 404
    assert "no routing memo" in resp.json()["detail"]
