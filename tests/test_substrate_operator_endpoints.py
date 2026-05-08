"""Operator-action endpoint tests (v0.14 Phase 8e).

POST /v2/api/substrate/{slug}/{row_id}/affirm
POST /v2/api/substrate/{slug}/{row_id}/contradict

These are the ONLY code paths that increment oracle row counts
(architecture principle 3). Each request is one operator click =
one independent external evidence event; NOT idempotent.

Tests cover:
  * affirm increments affirmed_count by 1 across all 4 oracles
  * contradict increments contradicted_count by 1 across all 4 oracles
  * confidence is recomputed correctly (Beta posterior)
  * last_consulted_at touches forward
  * 404 on missing row id
  * 400 on unknown oracle slug
  * NOT idempotent: two POSTs increment by 2
  * Pipeline event fires (oracle_affirmed / oracle_contradicted)
"""

from __future__ import annotations

import time
import pytest
from fastapi.testclient import TestClient

from src.app import _set_store, app
from src.fact_store import FactStore
from src.layer3_substrate.entity_equivalence import (
    EntityEquivalence,
)
from src.layer3_substrate.entity_taxonomy import (
    EntityTaxonomy,
)
from src.layer3_substrate.predicate_distribution import (
    PredicateDistribution,
)
from src.layer3_substrate.predicate_equivalence import (
    PredicateEquivalence,
)


@pytest.fixture
def isolated_store(tmp_path):
    s = FactStore(tmp_path / "operator_endpoints.db")
    _set_store(s)
    yield s
    s.close()
    _set_store(None)


@pytest.fixture
def client():
    return TestClient(app)


# ============================================================================
# Per-oracle row setup helpers
# ============================================================================


def _seed_pe(store) -> int:
    oracle = PredicateEquivalence(store)
    oracle.record(
        "preference", "likes", "dislikes",
        "contradictory", slot_reversal="none",
        reason="antonyms",
    )
    row = store._conn.execute(
        "SELECT id FROM predicate_equivalence"
    ).fetchone()
    return int(row["id"])


def _seed_ee(store) -> int:
    oracle = EntityEquivalence(store)
    oracle.record("NYC", "New York City", "same", reason="alias")
    row = store._conn.execute(
        "SELECT id FROM entity_equivalence"
    ).fetchone()
    return int(row["id"])


def _seed_et(store) -> int:
    oracle = EntityTaxonomy(store)
    oracle.record(
        "Williamstown", "Massachusetts", "part_of",
        "child_subsumed_by_parent", reason="setup",
    )
    row = store._conn.execute(
        "SELECT id FROM entity_taxonomy"
    ).fetchone()
    return int(row["id"])


def _seed_pd(store) -> int:
    oracle = PredicateDistribution(store)
    oracle.record(
        "spatial_temporal", "lives_in", 1, "part_of",
        "distributes_up", reason="setup",
    )
    row = store._conn.execute(
        "SELECT id FROM predicate_distribution"
    ).fetchone()
    return int(row["id"])


# ============================================================================
# Affirm — happy path across all 4 oracles
# ============================================================================


@pytest.mark.parametrize("slug,seed_fn,table", [
    ("predicate-equivalence", _seed_pe, "predicate_equivalence"),
    ("entity-equivalence", _seed_ee, "entity_equivalence"),
    ("entity-taxonomy", _seed_et, "entity_taxonomy"),
    ("predicate-distribution", _seed_pd, "predicate_distribution"),
])
def test_affirm_increments_affirmed_count_by_one(
    client, isolated_store, slug, seed_fn, table,
):
    row_id = seed_fn(isolated_store)
    resp = client.post(f"/api/substrate/{slug}/{row_id}/affirm")
    assert resp.status_code == 200
    body = resp.json()
    assert body["row_id"] == row_id
    assert body["affirmed_count"] == 1
    assert body["contradicted_count"] == 0
    assert body["confidence"] == pytest.approx(2 / 3)  # Beta(1,1) on (1,0)

    # SQL-level confirmation.
    db_row = isolated_store._conn.execute(
        f"SELECT affirmed_count, contradicted_count FROM {table} WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert db_row["affirmed_count"] == 1
    assert db_row["contradicted_count"] == 0


@pytest.mark.parametrize("slug,seed_fn,table", [
    ("predicate-equivalence", _seed_pe, "predicate_equivalence"),
    ("entity-equivalence", _seed_ee, "entity_equivalence"),
    ("entity-taxonomy", _seed_et, "entity_taxonomy"),
    ("predicate-distribution", _seed_pd, "predicate_distribution"),
])
def test_contradict_increments_contradicted_count_by_one(
    client, isolated_store, slug, seed_fn, table,
):
    row_id = seed_fn(isolated_store)
    resp = client.post(f"/api/substrate/{slug}/{row_id}/contradict")
    assert resp.status_code == 200
    body = resp.json()
    assert body["affirmed_count"] == 0
    assert body["contradicted_count"] == 1
    assert body["confidence"] == pytest.approx(1 / 3)  # Beta(1,1) on (0,1)


# ============================================================================
# NOT idempotent
# ============================================================================


def test_affirm_not_idempotent_two_posts_increment_by_two(
    client, isolated_store,
):
    row_id = _seed_pe(isolated_store)
    client.post(f"/api/substrate/predicate-equivalence/{row_id}/affirm")
    resp2 = client.post(f"/api/substrate/predicate-equivalence/{row_id}/affirm")
    assert resp2.status_code == 200
    assert resp2.json()["affirmed_count"] == 2


def test_contradict_not_idempotent(client, isolated_store):
    row_id = _seed_ee(isolated_store)
    for _ in range(3):
        resp = client.post(
            f"/api/substrate/entity-equivalence/{row_id}/contradict"
        )
    assert resp.status_code == 200
    assert resp.json()["contradicted_count"] == 3


def test_mixed_affirm_contradict_each_increments_independently(
    client, isolated_store,
):
    row_id = _seed_et(isolated_store)
    client.post(f"/api/substrate/entity-taxonomy/{row_id}/affirm")
    client.post(f"/api/substrate/entity-taxonomy/{row_id}/affirm")
    resp = client.post(f"/api/substrate/entity-taxonomy/{row_id}/contradict")
    body = resp.json()
    assert body["affirmed_count"] == 2
    assert body["contradicted_count"] == 1
    # Beta(1,1) on (2,1) = 3/5 = 0.6
    assert body["confidence"] == pytest.approx(0.6)


# ============================================================================
# 404 on missing row, 400 on unknown slug
# ============================================================================


def test_affirm_missing_row_404(client, isolated_store):
    resp = client.post("/api/substrate/predicate-equivalence/9999/affirm")
    assert resp.status_code == 404
    assert "9999" in resp.json()["detail"]


def test_contradict_missing_row_404(client, isolated_store):
    resp = client.post("/api/substrate/entity-equivalence/9999/contradict")
    assert resp.status_code == 404


def test_affirm_unknown_oracle_slug_400(client, isolated_store):
    resp = client.post("/api/substrate/no-such-oracle/1/affirm")
    assert resp.status_code == 400
    assert "no-such-oracle" in resp.json()["detail"]


def test_contradict_unknown_oracle_slug_400(client, isolated_store):
    resp = client.post("/api/substrate/no-such-oracle/1/contradict")
    assert resp.status_code == 400


def test_affirm_underscore_slug_rejected(client, isolated_store):
    """The URL slug uses dashes; underscored variants must NOT match."""
    resp = client.post("/api/substrate/predicate_equivalence/1/affirm")
    assert resp.status_code == 400


# ============================================================================
# Pipeline event emission
# ============================================================================


def test_affirm_emits_oracle_affirmed_event(client, isolated_store):
    row_id = _seed_pe(isolated_store)
    client.post(f"/api/substrate/predicate-equivalence/{row_id}/affirm")

    events = isolated_store._conn.execute(
        "SELECT stage, data FROM pipeline_events "
        "WHERE stage = 'oracle_affirmed'"
    ).fetchall()
    assert len(events) == 1
    import json
    payload = json.loads(events[0]["data"])
    assert payload["oracle"] == "predicate_equivalence"
    assert payload["row_id"] == row_id
    assert payload["affirmed_count"] == 1


def test_contradict_emits_oracle_contradicted_event(client, isolated_store):
    row_id = _seed_pd(isolated_store)
    client.post(f"/api/substrate/predicate-distribution/{row_id}/contradict")

    events = isolated_store._conn.execute(
        "SELECT stage, data FROM pipeline_events "
        "WHERE stage = 'oracle_contradicted'"
    ).fetchall()
    assert len(events) == 1
    import json
    payload = json.loads(events[0]["data"])
    assert payload["oracle"] == "predicate_distribution"
    assert payload["row_id"] == row_id
    assert payload["contradicted_count"] == 1


def test_repeated_affirm_emits_one_event_per_request(client, isolated_store):
    row_id = _seed_ee(isolated_store)
    client.post(f"/api/substrate/entity-equivalence/{row_id}/affirm")
    client.post(f"/api/substrate/entity-equivalence/{row_id}/affirm")
    client.post(f"/api/substrate/entity-equivalence/{row_id}/affirm")
    events = isolated_store._conn.execute(
        "SELECT COUNT(*) AS c FROM pipeline_events "
        "WHERE stage = 'oracle_affirmed'"
    ).fetchone()
    assert events["c"] == 3


# ============================================================================
# last_consulted_at update
# ============================================================================


def test_affirm_touches_last_consulted_at(client, isolated_store):
    """Operator affirmation updates last_consulted_at as a side effect.
    The architecture treats last_consulted_at as observability metadata
    (not reinforcement signal); it's still useful to refresh on
    operator actions so the freshness column reflects activity."""
    row_id = _seed_pe(isolated_store)
    before = isolated_store._conn.execute(
        "SELECT last_consulted_at FROM predicate_equivalence WHERE id = ?",
        (row_id,),
    ).fetchone()["last_consulted_at"]
    # Sleep so the timestamp is detectably different.
    time.sleep(0.005)
    client.post(f"/api/substrate/predicate-equivalence/{row_id}/affirm")
    after = isolated_store._conn.execute(
        "SELECT last_consulted_at FROM predicate_equivalence WHERE id = ?",
        (row_id,),
    ).fetchone()["last_consulted_at"]
    assert after != before
    assert after >= before  # ISO timestamps lex-compare correctly


# ============================================================================
# Doesn't touch other rows
# ============================================================================


def test_affirm_doesnt_touch_other_oracle_rows(client, isolated_store):
    """Affirming a predicate_equivalence row must not change any
    entity_equivalence / entity_taxonomy / predicate_distribution row."""
    pe_id = _seed_pe(isolated_store)
    ee_id = _seed_ee(isolated_store)
    et_id = _seed_et(isolated_store)
    pd_id = _seed_pd(isolated_store)

    client.post(f"/api/substrate/predicate-equivalence/{pe_id}/affirm")

    for table in ("entity_equivalence", "entity_taxonomy",
                  "predicate_distribution"):
        rows = isolated_store._conn.execute(
            f"SELECT affirmed_count, contradicted_count FROM {table}"
        ).fetchall()
        for r in rows:
            assert r["affirmed_count"] == 0, (
                f"{table} row should be untouched by other-table affirm"
            )
            assert r["contradicted_count"] == 0


# ============================================================================
# Confidence math
# ============================================================================


def test_confidence_recomputes_on_increment(client, isolated_store):
    """Beta(1,1) over (a, c): confidence = (a+1)/(a+c+2). Verify
    several increments produce the expected sequence.

    NB: substrate oracles compute confidence on-read from counts (no
    confidence column on the four oracle tables). Pin via the API
    responses, which return the computed value.
    """
    row_id = _seed_pe(isolated_store)

    # First affirm: (1, 0) → 2/3 ≈ 0.667
    r1 = client.post(
        f"/api/substrate/predicate-equivalence/{row_id}/affirm"
    ).json()
    assert r1["confidence"] == pytest.approx(2 / 3)

    # Second affirm: (2, 0) → 3/4 = 0.75
    r2 = client.post(
        f"/api/substrate/predicate-equivalence/{row_id}/affirm"
    ).json()
    assert r2["confidence"] == pytest.approx(0.75)

    # Contradict: (2, 1) → 3/5 = 0.6
    r3 = client.post(
        f"/api/substrate/predicate-equivalence/{row_id}/contradict"
    ).json()
    assert r3["confidence"] == pytest.approx(0.6)
