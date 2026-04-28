"""Tests for the v0.6 VerificationCache + canonicalize_claim_key."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from src.cache.verification_cache import (
    VerificationCache,
    canonicalize_claim_key,
)
from src.fact_store import FactStore


# ---- canonicalize_claim_key --------------------------------------------


def test_canonicalize_basic():
    claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"},
        "polarity": 1,
    }
    key = canonicalize_claim_key(claim)
    assert key == "spatial_temporal|located_in|p=1|entity=tokyo&location=japan"


def test_canonicalize_case_insensitive_for_pattern_predicate_strings():
    a = canonicalize_claim_key({
        "pattern": "Spatial_Temporal", "predicate": "Located_In",
        "slots": {"entity": "TOKYO", "location": "japan"}, "polarity": 1,
    })
    b = canonicalize_claim_key({
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "tokyo", "location": "Japan"}, "polarity": 1,
    })
    assert a == b


def test_canonicalize_slot_order_independent():
    a = canonicalize_claim_key({
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"}, "polarity": 1,
    })
    b = canonicalize_claim_key({
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"location": "Japan", "entity": "Tokyo"}, "polarity": 1,
    })
    assert a == b


def test_canonicalize_whitespace_normalized():
    a = canonicalize_claim_key({
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "  Tokyo  ", "location": "Japan"}, "polarity": 1,
    })
    b = canonicalize_claim_key({
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"}, "polarity": 1,
    })
    assert a == b


def test_canonicalize_polarity_distinguishes():
    pos = canonicalize_claim_key({
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"}, "polarity": 1,
    })
    neg = canonicalize_claim_key({
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"}, "polarity": 0,
    })
    assert pos != neg


def test_canonicalize_numeric_value_stable():
    a = canonicalize_claim_key({
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"subject": "strawberry", "property": "letter_r", "value": 3},
        "polarity": 1,
    })
    b = canonicalize_claim_key({
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"subject": "strawberry", "property": "letter_r", "value": 3},
        "polarity": 1,
    })
    assert a == b


# ---- VerificationCache: lookup / write / TTL ---------------------------


def test_write_then_lookup_returns_entry(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1",
        pattern="spatial_temporal", predicate="located_in",
        verdict="verified",
        stability_class="decade_stable",
        ttl_seconds=10 * 365 * 24 * 3600,
        evidence={"snippets": [{"title": "wiki", "url": "x"}]},
    )
    hit = cache.lookup("k1")
    assert hit is not None
    assert hit.verdict == "verified"
    assert hit.stability_class == "decade_stable"
    assert hit.evidence == {"snippets": [{"title": "wiki", "url": "x"}]}
    assert hit.expires_at is not None
    store.close()


def test_lookup_miss_returns_none(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    assert cache.lookup("nonexistent") is None
    store.close()


def test_immutable_entry_has_no_expiry(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1",
        pattern="quantitative", predicate="has_count",
        verdict="verified",
        stability_class="immutable",
        ttl_seconds=None,
    )
    hit = cache.lookup("k1")
    assert hit is not None
    assert hit.expires_at is None
    store.close()


def test_volatile_ttl_zero_does_not_persist(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1",
        pattern="quantitative", predicate="stock_price",
        verdict="verified",
        stability_class="volatile",
        ttl_seconds=0,
    )
    # Volatile-TTL writes are deliberately no-ops — caller should
    # have gated, but if they didn't we don't store stale-on-arrival.
    assert cache.lookup("k1") is None
    store.close()


def test_expired_entry_returns_none(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1",
        pattern="quantitative", predicate="stock_price",
        verdict="verified",
        stability_class="days_stable",
        ttl_seconds=24 * 3600,
    )
    # Force expiry to the past.
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store._conn.execute(
        "UPDATE verification_cache SET expires_at = ? WHERE canonical_key = ?",
        (past, "k1"),
    )
    store._conn.commit()
    assert cache.lookup("k1") is None
    store.close()


def test_lookup_increments_hit_count(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1",
        pattern="spatial_temporal", predicate="located_in",
        verdict="verified",
        stability_class="decade_stable",
        ttl_seconds=10 * 365 * 24 * 3600,
    )
    h1 = cache.lookup("k1")
    h2 = cache.lookup("k1")
    h3 = cache.lookup("k1")
    assert h1.hit_count == 1
    assert h2.hit_count == 2
    assert h3.hit_count == 3
    store.close()


def test_write_upserts_existing_key(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1",
        pattern="spatial_temporal", predicate="located_in",
        verdict="verified",
        stability_class="decade_stable",
        ttl_seconds=10 * 365 * 24 * 3600,
    )
    # Same key, updated verdict.
    cache.write(
        canonical_key="k1",
        pattern="spatial_temporal", predicate="located_in",
        verdict="contradicted",
        stability_class="years_stable",
        ttl_seconds=365 * 24 * 3600,
    )
    hit = cache.lookup("k1")
    assert hit.verdict == "contradicted"
    assert hit.stability_class == "years_stable"
    # Only one row in the table — UPSERT, not INSERT.
    rows = store._conn.execute(
        "SELECT COUNT(*) AS n FROM verification_cache"
    ).fetchone()
    assert rows["n"] == 1
    store.close()


def test_expire_now(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(
        canonical_key="k1",
        pattern="x", predicate="y", verdict="verified",
        stability_class="decade_stable",
        ttl_seconds=10 * 365 * 24 * 3600,
    )
    assert cache.lookup("k1") is not None
    cache.expire_now("k1")
    assert cache.lookup("k1") is None
    store.close()


def test_stats_aggregates_correctly(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cache = VerificationCache(store)
    cache.write(canonical_key="k1", pattern="x", predicate="y",
                verdict="verified", stability_class="immutable",
                ttl_seconds=None)
    cache.write(canonical_key="k2", pattern="x", predicate="y",
                verdict="verified", stability_class="decade_stable",
                ttl_seconds=10 * 365 * 24 * 3600)
    cache.lookup("k1")
    cache.lookup("k1")
    cache.lookup("k2")

    s = cache.stats()
    assert s["total_entries"] == 2
    assert s["immutable_entries"] == 1
    assert s["total_hits"] == 3
    store.close()
