"""Tests for the /v2/api/substrate/entity-taxonomy HTTP endpoints.

GET /v2/api/substrate/entity-taxonomy
    Lists every row. Supports optional ?relation_type=is_a|part_of.

GET /v2/api/substrate/entity-taxonomy/{child}/{parent}/{relation_type}
    Inspects a single (child, parent, relation_type) row. NOT
    order-invariant — directional storage. 404 on miss; 400 on
    self-pair or unknown relation_type.

Endpoint tests use a tmp_path-backed FactStore via ``_set_store``
so the real ``aedos_v2.db`` is never touched.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.app import _set_store, app
from src.fact_store import FactStore
from src.layer3_substrate.entity_taxonomy import (
    EntityTaxonomy,
)


@pytest.fixture
def isolated_store(tmp_path):
    s = FactStore(tmp_path / "et_endpoint.db")
    _set_store(s)
    yield s
    s.close()
    _set_store(None)


@pytest.fixture
def client():
    return TestClient(app)


# ---- list endpoint --------------------------------------------------------


def test_list_returns_empty_when_no_rows(client, isolated_store):
    resp = client.get("/api/substrate/entity-taxonomy")
    assert resp.status_code == 200
    assert resp.json() == {"rows": []}


def test_list_returns_recorded_rows(client, isolated_store):
    oracle = EntityTaxonomy(isolated_store)
    oracle.record("dog", "mammal", "is_a",
                  "child_subsumed_by_parent", "kind of")
    oracle.record("Williamstown", "Massachusetts", "part_of",
                  "child_subsumed_by_parent", "town in state")
    resp = client.get("/api/substrate/entity-taxonomy")
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) == 2
    keys = {(r["child"], r["parent"], r["relation_type"])
            for r in rows}
    assert keys == {
        ("dog", "mammal", "is_a"),
        ("Williamstown", "Massachusetts", "part_of"),
    }


def test_list_filtered_by_relation_type(client, isolated_store):
    oracle = EntityTaxonomy(isolated_store)
    oracle.record("dog", "mammal", "is_a",
                  "child_subsumed_by_parent", None)
    oracle.record("Williamstown", "Massachusetts", "part_of",
                  "child_subsumed_by_parent", None)
    resp_isa = client.get(
        "/api/substrate/entity-taxonomy?relation_type=is_a",
    )
    rows_isa = resp_isa.json()["rows"]
    assert len(rows_isa) == 1
    assert rows_isa[0]["relation_type"] == "is_a"

    resp_partof = client.get(
        "/api/substrate/entity-taxonomy?relation_type=part_of",
    )
    rows_partof = resp_partof.json()["rows"]
    assert len(rows_partof) == 1
    assert rows_partof[0]["relation_type"] == "part_of"


def test_list_rejects_unknown_relation_type_filter(
    client, isolated_store,
):
    resp = client.get(
        "/api/substrate/entity-taxonomy?relation_type=subclass_of",
    )
    assert resp.status_code == 400


def test_list_row_payload_includes_audit_fields(
    client, isolated_store,
):
    oracle = EntityTaxonomy(isolated_store)
    oracle.record("dog", "mammal", "is_a",
                  "child_subsumed_by_parent", "kind of")
    resp = client.get("/api/substrate/entity-taxonomy")
    row = resp.json()["rows"][0]
    for field in (
        "id", "child", "parent", "relation_type", "label", "reason",
        "affirmed_count", "contradicted_count", "confidence",
        "created_at", "last_consulted_at",
    ):
        assert field in row, f"missing field {field!r}"
    assert row["affirmed_count"] == 0
    assert row["contradicted_count"] == 0
    assert row["confidence"] == 0.5


# ---- single-entry endpoint ------------------------------------------------


def test_get_single_entry(client, isolated_store):
    oracle = EntityTaxonomy(isolated_store)
    oracle.record("dog", "mammal", "is_a",
                  "child_subsumed_by_parent", "kind of")
    resp = client.get(
        "/api/substrate/entity-taxonomy/dog/mammal/is_a",
    )
    assert resp.status_code == 200
    row = resp.json()
    assert row["child"] == "dog"
    assert row["parent"] == "mammal"
    assert row["relation_type"] == "is_a"
    assert row["label"] == "child_subsumed_by_parent"


def test_get_single_entry_NOT_order_invariant(client, isolated_store):
    """Directional: the swapped ordering is a separate row, so a
    swapped lookup against an unswapped recorded row 404s."""
    oracle = EntityTaxonomy(isolated_store)
    oracle.record("dog", "mammal", "is_a",
                  "child_subsumed_by_parent", "natural")
    resp_natural = client.get(
        "/api/substrate/entity-taxonomy/dog/mammal/is_a",
    )
    assert resp_natural.status_code == 200
    # Swap arguments — separate row, not yet recorded.
    resp_swap = client.get(
        "/api/substrate/entity-taxonomy/mammal/dog/is_a",
    )
    assert resp_swap.status_code == 404


def test_get_single_entry_distinct_relation_types(
    client, isolated_store,
):
    """Same (child, parent) under different relation_types are
    distinct rows."""
    oracle = EntityTaxonomy(isolated_store)
    oracle.record("Williamstown", "Massachusetts", "part_of",
                  "child_subsumed_by_parent", "town")
    oracle.record("Williamstown", "Massachusetts", "is_a",
                  "neither",
                  "Williamstown is part of Mass not a kind of it")
    a = client.get(
        "/api/substrate/entity-taxonomy/"
        "Williamstown/Massachusetts/part_of",
    ).json()
    b = client.get(
        "/api/substrate/entity-taxonomy/"
        "Williamstown/Massachusetts/is_a",
    ).json()
    assert a["id"] != b["id"]
    assert a["label"] == "child_subsumed_by_parent"
    assert b["label"] == "neither"


def test_get_single_entry_case_sensitive(client, isolated_store):
    """Apple/fruit and apple/fruit are distinct rows."""
    oracle = EntityTaxonomy(isolated_store)
    oracle.record("Apple", "fruit", "is_a", "neither",
                  "company is not a fruit")
    resp_capital = client.get(
        "/api/substrate/entity-taxonomy/Apple/fruit/is_a",
    )
    assert resp_capital.status_code == 200
    # Lowercase apple is a different entity, distinct row, 404.
    resp_lower = client.get(
        "/api/substrate/entity-taxonomy/apple/fruit/is_a",
    )
    assert resp_lower.status_code == 404


def test_get_single_entry_404_on_miss(client, isolated_store):
    resp = client.get(
        "/api/substrate/entity-taxonomy/dog/mammal/is_a",
    )
    assert resp.status_code == 404
    assert "no entity_taxonomy row" in resp.json()["detail"]


def test_get_single_entry_400_on_self_pair(client, isolated_store):
    resp = client.get(
        "/api/substrate/entity-taxonomy/dog/dog/is_a",
    )
    assert resp.status_code == 400
    assert "self-pair" in resp.json()["detail"]


def test_get_single_entry_400_on_unknown_relation_type(
    client, isolated_store,
):
    resp = client.get(
        "/api/substrate/entity-taxonomy/dog/mammal/subclass_of",
    )
    assert resp.status_code == 400
    assert "relation_type" in resp.json()["detail"]
