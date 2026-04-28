"""Tests for the v0.6 /api/cache inspector endpoint."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.cache import VerificationCache
from src.cache.stability_classifier import STABILITY_TTL_SECONDS
from src.fact_store import FactStore


@pytest.fixture
def client_with_cache(tmp_path, monkeypatch):
    """Build a TestClient backed by an isolated DB with cache entries."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    db_path = tmp_path / "api.db"
    monkeypatch.setenv("AEDOS_DB_PATH", str(db_path))

    # Pre-populate the cache directly (without going through the
    # pipeline — the API is a read-only inspector).
    store = FactStore(str(db_path))
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k_immutable",
        pattern="quantitative", predicate="has_count",
        verdict="verified", stability_class="immutable",
        ttl_seconds=None,
        evidence={"explanation": "fixed"},
    )
    cache.write(
        canonical_key="k_decade",
        pattern="spatial_temporal", predicate="located_in",
        verdict="verified", stability_class="decade_stable",
        ttl_seconds=STABILITY_TTL_SECONDS["decade_stable"],
    )
    cache.write(
        canonical_key="k_contradicted",
        pattern="spatial_temporal", predicate="located_in",
        verdict="contradicted", stability_class="decade_stable",
        ttl_seconds=STABILITY_TTL_SECONDS["decade_stable"],
    )
    # Force one entry to be expired.
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        "UPDATE verification_cache SET expires_at = ? "
        "WHERE canonical_key = 'k_contradicted'", (past,),
    )
    store._conn.commit()
    cache.lookup("k_immutable")  # bump hit_count to 1
    cache.lookup("k_immutable")  # bump to 2
    cache.lookup("k_decade")     # bump to 1
    store.close()

    from src.app import app
    with TestClient(app) as c:
        yield c


def test_cache_endpoint_returns_stats_and_entries(client_with_cache):
    r = client_with_cache.get("/api/cache")
    assert r.status_code == 200
    body = r.json()
    assert body["stats"]["total_entries"] == 3
    assert body["stats"]["immutable_entries"] == 1
    assert body["stats"]["total_hits"] == 3  # 2 + 1

    entries = body["entries"]
    assert len(entries) == 3
    keys = {e["canonical_key"] for e in entries}
    assert keys == {"k_immutable", "k_decade", "k_contradicted"}


def test_cache_endpoint_marks_expired_entries(client_with_cache):
    r = client_with_cache.get("/api/cache")
    body = r.json()
    by_key = {e["canonical_key"]: e for e in body["entries"]}
    assert by_key["k_contradicted"]["is_expired"] is True
    assert by_key["k_immutable"]["is_expired"] is False
    assert by_key["k_decade"]["is_expired"] is False


def test_cache_endpoint_respects_limit(client_with_cache):
    r = client_with_cache.get("/api/cache?limit=2")
    assert r.status_code == 200
    body = r.json()
    assert len(body["entries"]) == 2


def test_cache_endpoint_when_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AEDOS_DB_PATH", str(tmp_path / "empty.db"))

    from src.app import app
    with TestClient(app) as c:
        r = c.get("/api/cache")
        assert r.status_code == 200
        body = r.json()
        assert body["stats"]["total_entries"] == 0
        assert body["entries"] == []
        # Empty cache → no lookups recorded → hit_rate is None.
        assert body["stats"]["lookups"] == 0
        assert body["stats"]["hit_rate"] is None


def test_cache_endpoint_reports_live_hit_rate_from_pipeline_events(
    tmp_path, monkeypatch,
):
    """Beyond the static cache-table totals, /api/cache reports the
    live hit/miss rate from cache_lookup pipeline_events. This is what
    the operator wants to see — what fraction of retrieval calls were
    short-circuited by the cache."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    db_path = tmp_path / "rate.db"
    monkeypatch.setenv("AEDOS_DB_PATH", str(db_path))

    # Seed pipeline_events with 6 hits, 4 misses, 1 error.
    store = FactStore(str(db_path))
    asst = store.insert_turn("assistant", "x")
    for _ in range(6):
        store.insert_pipeline_event(asst, "cache_lookup", {
            "canonical_key": "k|alpha",
            "result": "hit",
            "verdict": "verified",
            "stability_class": "decade_stable",
            "hit_count": 1,
        })
    for _ in range(4):
        store.insert_pipeline_event(asst, "cache_lookup", {
            "canonical_key": "k|beta",
            "result": "miss",
        })
    store.insert_pipeline_event(asst, "cache_lookup", {
        "canonical_key": "k|err",
        "error": "OperationalError: locked",
    })
    store.close()

    from src.app import app
    with TestClient(app) as c:
        r = c.get("/api/cache")
        assert r.status_code == 200
        stats = r.json()["stats"]
        assert stats["lookups"] == 10  # hits + misses; errors not counted
        assert stats["lookup_hits"] == 6
        assert stats["lookup_misses"] == 4
        assert stats["lookup_errors"] == 1
        assert stats["hit_rate"] == 0.6
        # Per-stability hits aggregated.
        assert stats["hits_by_stability"] == {"decade_stable": 6}


def test_cache_endpoint_hits_by_stability_groups_classes(
    tmp_path, monkeypatch,
):
    """When hits span multiple stability classes (immutable + months_
    stable, etc.), the per-class breakdown reports each independently
    so the operator can see which class is actually saving work."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    db_path = tmp_path / "stab.db"
    monkeypatch.setenv("AEDOS_DB_PATH", str(db_path))

    store = FactStore(str(db_path))
    asst = store.insert_turn("assistant", "x")
    for stab, n in (("immutable", 3), ("decade_stable", 2),
                    ("months_stable", 1)):
        for _ in range(n):
            store.insert_pipeline_event(asst, "cache_lookup", {
                "canonical_key": f"k|{stab}",
                "result": "hit",
                "verdict": "verified",
                "stability_class": stab,
                "hit_count": 1,
            })
    store.close()

    from src.app import app
    with TestClient(app) as c:
        r = c.get("/api/cache")
        stats = r.json()["stats"]
        assert stats["hits_by_stability"] == {
            "immutable": 3, "decade_stable": 2, "months_stable": 1,
        }
