"""Tests for the /v2/api/substrate/predicate-distribution endpoints.

GET /v2/api/substrate/predicate-distribution
    Lists every row. Supports optional ?pattern= and ?polarity=
    filters (combinable).

GET /v2/api/substrate/predicate-distribution/{pattern}/{predicate}/
    {polarity}/{relation_type}
    Inspects a single 4-tuple row. 404 on miss; 400 on unknown
    polarity or relation_type.

Endpoint tests use a tmp_path-backed FactStore via ``_set_store``
so the real ``aedos_v2.db`` is never touched.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.app import _set_store, app
from src.fact_store import FactStore
from src.layer3_substrate.predicate_distribution import (
    PredicateDistribution,
)


@pytest.fixture
def isolated_store(tmp_path):
    s = FactStore(tmp_path / "pd_endpoint.db")
    _set_store(s)
    yield s
    s.close()
    _set_store(None)


@pytest.fixture
def client():
    return TestClient(app)


# ---- list endpoint --------------------------------------------------------


def test_list_returns_empty_when_no_rows(client, isolated_store):
    resp = client.get("/api/substrate/predicate-distribution")
    assert resp.status_code == 200
    assert resp.json() == {"rows": []}


def test_list_returns_recorded_rows(client, isolated_store):
    oracle = PredicateDistribution(isolated_store)
    oracle.record("preference", "likes", 1, "is_a",
                  "distributes_down", "categorical attitude")
    oracle.record("spatial_temporal", "lives_in", 1, "part_of",
                  "distributes_up", "compositional residence")
    resp = client.get("/api/substrate/predicate-distribution")
    rows = resp.json()["rows"]
    assert len(rows) == 2
    keys = {
        (r["pattern"], r["predicate"], r["polarity"],
         r["taxonomy_relation_type"])
        for r in rows
    }
    assert keys == {
        ("preference", "likes", 1, "is_a"),
        ("spatial_temporal", "lives_in", 1, "part_of"),
    }


def test_list_filtered_by_pattern(client, isolated_store):
    oracle = PredicateDistribution(isolated_store)
    oracle.record("preference", "likes", 1, "is_a",
                  "distributes_down", None)
    oracle.record("spatial_temporal", "lives_in", 1, "part_of",
                  "distributes_up", None)
    resp = client.get(
        "/api/substrate/predicate-distribution?pattern=preference",
    )
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["pattern"] == "preference"


def test_list_filtered_by_polarity(client, isolated_store):
    oracle = PredicateDistribution(isolated_store)
    oracle.record("preference", "likes", 1, "is_a",
                  "distributes_down", None)
    oracle.record("preference", "likes", 0, "is_a",
                  "neither", None)
    resp = client.get(
        "/api/substrate/predicate-distribution?polarity=1",
    )
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["polarity"] == 1


def test_list_filtered_by_pattern_and_polarity(
    client, isolated_store,
):
    oracle = PredicateDistribution(isolated_store)
    oracle.record("preference", "likes", 1, "is_a",
                  "distributes_down", None)
    oracle.record("preference", "likes", 0, "is_a",
                  "neither", None)
    oracle.record("spatial_temporal", "lives_in", 1, "part_of",
                  "distributes_up", None)
    resp = client.get(
        "/api/substrate/predicate-distribution"
        "?pattern=preference&polarity=1",
    )
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["pattern"] == "preference"
    assert rows[0]["polarity"] == 1


def test_list_rejects_unknown_polarity_filter(client, isolated_store):
    resp = client.get(
        "/api/substrate/predicate-distribution?polarity=2",
    )
    assert resp.status_code == 400


def test_list_row_payload_includes_audit_fields(
    client, isolated_store,
):
    oracle = PredicateDistribution(isolated_store)
    oracle.record("preference", "likes", 1, "is_a",
                  "distributes_down", "categorical attitude")
    resp = client.get("/api/substrate/predicate-distribution")
    row = resp.json()["rows"][0]
    for field in (
        "id", "pattern", "predicate", "polarity",
        "taxonomy_relation_type", "label", "reason",
        "affirmed_count", "contradicted_count", "confidence",
        "created_at", "last_consulted_at",
    ):
        assert field in row, f"missing field {field!r}"
    assert row["affirmed_count"] == 0
    assert row["confidence"] == 0.5


# ---- single-entry endpoint ------------------------------------------------


def test_get_single_entry(client, isolated_store):
    oracle = PredicateDistribution(isolated_store)
    oracle.record("preference", "likes", 1, "is_a",
                  "distributes_down", "categorical attitude")
    resp = client.get(
        "/api/substrate/predicate-distribution/"
        "preference/likes/1/is_a",
    )
    assert resp.status_code == 200
    row = resp.json()
    assert row["pattern"] == "preference"
    assert row["predicate"] == "likes"
    assert row["polarity"] == 1
    assert row["taxonomy_relation_type"] == "is_a"
    assert row["label"] == "distributes_down"


def test_get_single_entry_distinct_relation_types(
    client, isolated_store,
):
    """The directional-asymmetry endpoint behavior: same (pattern,
    predicate, polarity) at different relation_types are different
    rows."""
    oracle = PredicateDistribution(isolated_store)
    oracle.record("spatial_temporal", "lives_in", 1, "is_a",
                  "neither", None)
    oracle.record("spatial_temporal", "lives_in", 1, "part_of",
                  "distributes_up", None)
    a = client.get(
        "/api/substrate/predicate-distribution/"
        "spatial_temporal/lives_in/1/is_a",
    ).json()
    b = client.get(
        "/api/substrate/predicate-distribution/"
        "spatial_temporal/lives_in/1/part_of",
    ).json()
    assert a["id"] != b["id"]
    assert a["label"] == "neither"
    assert b["label"] == "distributes_up"


def test_get_single_entry_distinct_polarities(client, isolated_store):
    """Same (pattern, predicate, relation_type) at different
    polarities are different rows."""
    oracle = PredicateDistribution(isolated_store)
    oracle.record("preference", "likes", 1, "is_a",
                  "distributes_down", None)
    oracle.record("preference", "likes", 0, "is_a",
                  "neither", None)
    pos = client.get(
        "/api/substrate/predicate-distribution/"
        "preference/likes/1/is_a",
    ).json()
    neg = client.get(
        "/api/substrate/predicate-distribution/"
        "preference/likes/0/is_a",
    ).json()
    assert pos["id"] != neg["id"]


def test_get_single_entry_404_on_miss(client, isolated_store):
    resp = client.get(
        "/api/substrate/predicate-distribution/"
        "preference/likes/1/is_a",
    )
    assert resp.status_code == 404
    assert "no predicate_distribution row" in resp.json()["detail"]


def test_get_single_entry_400_on_unknown_polarity(
    client, isolated_store,
):
    resp = client.get(
        "/api/substrate/predicate-distribution/"
        "preference/likes/2/is_a",
    )
    assert resp.status_code == 400


def test_get_single_entry_400_on_unknown_relation_type(
    client, isolated_store,
):
    resp = client.get(
        "/api/substrate/predicate-distribution/"
        "preference/likes/1/subclass_of",
    )
    assert resp.status_code == 400
    assert "relation_type" in resp.json()["detail"]


def test_get_single_entry_predicate_case_normalized(
    client, isolated_store,
):
    """Predicate is lowercased on lookup; URL can use either case."""
    oracle = PredicateDistribution(isolated_store)
    oracle.record("preference", "likes", 1, "is_a",
                  "distributes_down", None)
    resp_lower = client.get(
        "/api/substrate/predicate-distribution/"
        "preference/likes/1/is_a",
    )
    resp_upper = client.get(
        "/api/substrate/predicate-distribution/"
        "preference/LIKES/1/is_a",
    )
    assert resp_lower.status_code == 200
    assert resp_upper.status_code == 200
    assert resp_lower.json()["id"] == resp_upper.json()["id"]
