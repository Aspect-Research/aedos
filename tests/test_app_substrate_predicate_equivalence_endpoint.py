"""Tests for the /v2/api/substrate/predicate-equivalence HTTP endpoints.

GET /v2/api/substrate/predicate-equivalence
    Lists every row (optional ?pattern= filter).

GET /v2/api/substrate/predicate-equivalence/{pattern}/{a}/{b}
    Inspects a single canonical-pair row; 404 on miss; 400 on self-
    pair (matches the canonical helper's contract).

Endpoint tests use a tmp_path-backed FactStore via ``_set_store`` so
the real ``aedos_v2.db`` is never touched.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.app import _set_store, app
from src.fact_store import FactStore
from src.layer3_substrate.predicate_equivalence import (
    PredicateEquivalence,
)


@pytest.fixture
def isolated_store(tmp_path):
    s = FactStore(tmp_path / "pe_endpoint.db")
    _set_store(s)
    yield s
    s.close()
    _set_store(None)


@pytest.fixture
def client():
    return TestClient(app)


# ---- list endpoint --------------------------------------------------------


def test_list_returns_empty_when_no_rows(client, isolated_store):
    resp = client.get("/api/substrate/predicate-equivalence")
    assert resp.status_code == 200
    assert resp.json() == {"rows": []}


def test_list_returns_recorded_rows(client, isolated_store):
    oracle = PredicateEquivalence(isolated_store)
    oracle.record("preference", "likes", "dislikes",
                  "contradictory", "none", "antonyms")
    oracle.record("relational", "wrote", "authored_by",
                  "equivalent", "subject_object_swap", "active/passive")
    resp = client.get("/api/substrate/predicate-equivalence")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) == 2
    keys = {(r["pattern"], r["predicate_a"], r["predicate_b"]) for r in rows}
    assert keys == {
        ("preference", "dislikes", "likes"),
        ("relational", "authored_by", "wrote"),
    }


def test_list_filters_by_pattern(client, isolated_store):
    oracle = PredicateEquivalence(isolated_store)
    oracle.record("preference", "likes", "dislikes",
                  "contradictory", "none", "antonyms")
    oracle.record("relational", "wrote", "authored_by",
                  "equivalent", "subject_object_swap", "active/passive")
    resp = client.get(
        "/api/substrate/predicate-equivalence?pattern=preference"
    )
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["pattern"] == "preference"


def test_list_row_payload_includes_audit_fields(client, isolated_store):
    """The trace UI relies on confidence + counts + timestamps in the
    row payload — make sure ``to_dict`` emitted them via the endpoint."""
    oracle = PredicateEquivalence(isolated_store)
    oracle.record("preference", "likes", "dislikes",
                  "contradictory", "none", "antonyms")
    resp = client.get("/api/substrate/predicate-equivalence")
    row = resp.json()["rows"][0]
    for field in (
        "id", "pattern", "predicate_a", "predicate_b",
        "label", "slot_reversal", "reason",
        "affirmed_count", "contradicted_count", "confidence",
        "created_at", "last_consulted_at",
    ):
        assert field in row, f"missing field {field!r} in payload"
    assert row["affirmed_count"] == 0
    assert row["contradicted_count"] == 0
    assert row["confidence"] == 0.5  # Beta(1,1) at zero counts


# ---- single-entry endpoint ------------------------------------------------


def test_get_single_entry(client, isolated_store):
    oracle = PredicateEquivalence(isolated_store)
    oracle.record("preference", "likes", "dislikes",
                  "contradictory", "none", "antonyms")
    resp = client.get(
        "/api/substrate/predicate-equivalence/preference/likes/dislikes"
    )
    assert resp.status_code == 200
    row = resp.json()
    assert row["pattern"] == "preference"
    assert row["predicate_a"] == "dislikes"  # canonical order
    assert row["predicate_b"] == "likes"
    assert row["label"] == "contradictory"


def test_get_single_entry_order_invariant(client, isolated_store):
    """Caller can pass predicates in either order; the endpoint
    returns the canonical row both ways."""
    oracle = PredicateEquivalence(isolated_store)
    oracle.record("preference", "likes", "dislikes",
                  "contradictory", "none", "antonyms")
    a = client.get(
        "/api/substrate/predicate-equivalence/preference/likes/dislikes"
    ).json()
    b = client.get(
        "/api/substrate/predicate-equivalence/preference/dislikes/likes"
    ).json()
    assert a["id"] == b["id"]


def test_get_single_entry_404_on_miss(client, isolated_store):
    resp = client.get(
        "/api/substrate/predicate-equivalence/preference/likes/loves"
    )
    assert resp.status_code == 404
    assert "no predicate_equivalence row" in resp.json()["detail"]


def test_get_single_entry_400_on_self_pair(client, isolated_store):
    resp = client.get(
        "/api/substrate/predicate-equivalence/preference/likes/likes"
    )
    assert resp.status_code == 400
    assert "self-pair" in resp.json()["detail"]
