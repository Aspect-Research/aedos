"""Tests for the /v2/api/substrate/entity-equivalence HTTP endpoints.

GET /v2/api/substrate/entity-equivalence
    Lists every row. No pattern filter (entity_equivalence is
    pattern-independent).

GET /v2/api/substrate/entity-equivalence/{a}/{b}
    Inspects a single canonical-pair row; 404 on miss; 400 on
    self-pair.

Endpoint tests use a tmp_path-backed FactStore via ``_set_store``
so the real ``aedos_v2.db`` is never touched.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.app import _set_store, app
from src.fact_store import FactStore
from src.layer3_substrate.entity_equivalence import (
    EntityEquivalence,
)


@pytest.fixture
def isolated_store(tmp_path):
    s = FactStore(tmp_path / "ee_endpoint.db")
    _set_store(s)
    yield s
    s.close()
    _set_store(None)


@pytest.fixture
def client():
    return TestClient(app)


# ---- list endpoint --------------------------------------------------------


def test_list_returns_empty_when_no_rows(client, isolated_store):
    resp = client.get("/api/substrate/entity-equivalence")
    assert resp.status_code == 200
    assert resp.json() == {"rows": []}


def test_list_returns_recorded_rows(client, isolated_store):
    oracle = EntityEquivalence(isolated_store)
    oracle.record("NYC", "New York City", "same", "alias")
    oracle.record("Apple", "apple", "different", "case disambig")
    resp = client.get("/api/substrate/entity-equivalence")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) == 2
    keys = {(r["entity_a"], r["entity_b"]) for r in rows}
    assert keys == {
        ("NYC", "New York City"),
        ("Apple", "apple"),
    }


def test_list_row_payload_includes_audit_fields(client, isolated_store):
    """The trace UI relies on confidence + counts + timestamps."""
    oracle = EntityEquivalence(isolated_store)
    oracle.record("NYC", "New York City", "same", "alias")
    resp = client.get("/api/substrate/entity-equivalence")
    row = resp.json()["rows"][0]
    for field in (
        "id", "entity_a", "entity_b", "label", "reason",
        "affirmed_count", "contradicted_count", "confidence",
        "created_at", "last_consulted_at",
    ):
        assert field in row, f"missing field {field!r}"
    assert row["affirmed_count"] == 0
    assert row["contradicted_count"] == 0
    assert row["confidence"] == 0.5


# ---- single-entry endpoint ------------------------------------------------


def test_get_single_entry(client, isolated_store):
    oracle = EntityEquivalence(isolated_store)
    oracle.record("NYC", "New York City", "same", "alias")
    resp = client.get(
        "/api/substrate/entity-equivalence/NYC/New York City"
    )
    assert resp.status_code == 200
    row = resp.json()
    assert row["entity_a"] == "NYC"
    assert row["entity_b"] == "New York City"
    assert row["label"] == "same"


def test_get_single_entry_order_invariant(client, isolated_store):
    """Caller passes entities in either order; endpoint returns
    the canonical row both ways."""
    oracle = EntityEquivalence(isolated_store)
    oracle.record("NYC", "New York City", "same", "alias")
    a = client.get(
        "/api/substrate/entity-equivalence/NYC/New York City"
    ).json()
    b = client.get(
        "/api/substrate/entity-equivalence/New York City/NYC"
    ).json()
    assert a["id"] == b["id"]


def test_get_single_entry_case_sensitive(client, isolated_store):
    """apple/Apple is a distinct row from apple/APPLE."""
    oracle = EntityEquivalence(isolated_store)
    oracle.record("Apple", "apple", "different", "case disambig")
    # The canonical row exists.
    resp = client.get("/api/substrate/entity-equivalence/Apple/apple")
    assert resp.status_code == 200
    # A different casing pair (Apple, APPLE) is a 404 — distinct row.
    resp = client.get("/api/substrate/entity-equivalence/Apple/APPLE")
    assert resp.status_code == 404


def test_get_single_entry_404_on_miss(client, isolated_store):
    resp = client.get(
        "/api/substrate/entity-equivalence/NYC/Boston"
    )
    assert resp.status_code == 404
    assert "no entity_equivalence row" in resp.json()["detail"]


def test_get_single_entry_400_on_self_pair(client, isolated_store):
    resp = client.get("/api/substrate/entity-equivalence/Apple/Apple")
    assert resp.status_code == 400
    assert "self-pair" in resp.json()["detail"]
