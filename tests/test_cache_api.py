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
    monkeypatch.delenv("AEDOS_CACHE_SCOPING", raising=False)
    monkeypatch.delenv("AEDOS_CACHE_STABILITY", raising=False)
    monkeypatch.delenv("AEDOS_CACHE_WRITES", raising=False)

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
