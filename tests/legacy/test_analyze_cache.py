"""Tests for scripts/analyze_cache.py — the cache hit-rate analyzer.

The script is read-only; we drive it by seeding a FactStore with
cache_lookup and cache_write events, running main(), and asserting
on stdout.
"""

from __future__ import annotations

import pytest

from src.legacy.fact_store import FactStore
from scripts.analyze_cache import main


def _seed_with_hits_and_misses(db_path):
    store = FactStore(db_path)
    user = store.insert_turn("user", "x")
    asst = store.insert_turn("assistant", "y")
    # 3 hits (one key reused, one one-shot), 2 misses, 1 error
    store.insert_pipeline_event(asst, "cache_lookup", {
        "canonical_key": "k|alpha",
        "result": "hit",
        "verdict": "verified",
        "stability_class": "decade_stable",
        "hit_count": 1,
    })
    store.insert_pipeline_event(asst, "cache_lookup", {
        "canonical_key": "k|alpha",
        "result": "hit",
        "verdict": "verified",
        "stability_class": "decade_stable",
        "hit_count": 2,
    })
    store.insert_pipeline_event(asst, "cache_lookup", {
        "canonical_key": "k|beta",
        "result": "hit",
        "verdict": "verified",
        "stability_class": "immutable",
        "hit_count": 1,
    })
    store.insert_pipeline_event(asst, "cache_lookup", {
        "canonical_key": "k|gamma",
        "result": "miss",
    })
    store.insert_pipeline_event(asst, "cache_lookup", {
        "canonical_key": "k|delta",
        "result": "miss",
    })
    store.insert_pipeline_event(asst, "cache_lookup", {
        "canonical_key": "k|err",
        "error": "RuntimeError: boom",
    })

    # 2 successful writes + 1 error
    store.insert_pipeline_event(asst, "cache_write", {
        "canonical_key": "k|gamma",
        "verdict": "verified",
        "stability_class": "years_stable",
        "ttl_seconds": 86400 * 365,
    })
    store.insert_pipeline_event(asst, "cache_write", {
        "canonical_key": "k|delta",
        "verdict": "contradicted",
        "stability_class": "months_stable",
        "ttl_seconds": 86400 * 30,
    })
    store.insert_pipeline_event(asst, "cache_write", {
        "canonical_key": "k|errwrite",
        "error": "OperationalError: locked",
    })

    store.close()


def test_analyze_cache_summarizes_correctly(tmp_path, capsys):
    db = tmp_path / "c.db"
    _seed_with_hits_and_misses(db)
    rc = main(["analyze_cache", str(db)])
    assert rc == 0
    out = capsys.readouterr().out

    assert "total cache lookups:   5" in out  # hits+misses (errors excluded)
    assert "hits:                3" in out
    assert "misses:              2" in out
    assert "errors:              1" in out
    assert "hit rate:              60.0%" in out

    assert "total cache writes:    3" in out
    assert "successful:          2" in out
    assert "errors:              1" in out

    # Stability class buckets
    assert "decade_stable" in out
    assert "immutable" in out
    assert "years_stable" in out  # write side
    assert "months_stable" in out

    # Top reused: alpha was hit twice — max hit_count=2
    assert "k|alpha" in out
    assert "hit_count=2" in out


def test_analyze_cache_handles_missing_db(tmp_path, capsys):
    rc = main(["analyze_cache", str(tmp_path / "nonexistent.db")])
    assert rc == 2


def test_analyze_cache_handles_empty_db(tmp_path, capsys):
    db = tmp_path / "empty.db"
    FactStore(db).close()
    rc = main(["analyze_cache", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no cache events" in out


def test_analyze_cache_top_flag_limits_keys(tmp_path, capsys):
    """--top N caps how many canonical keys appear in the most-reused
    list."""
    db = tmp_path / "many.db"
    store = FactStore(db)
    store.insert_turn("user", "x")
    asst = store.insert_turn("assistant", "y")
    for i in range(10):
        store.insert_pipeline_event(asst, "cache_lookup", {
            "canonical_key": f"key|{i}",
            "result": "hit",
            "verdict": "verified",
            "stability_class": "immutable",
            "hit_count": i + 1,
        })
    store.close()

    rc = main(["analyze_cache", str(db), "--top", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    top_lines = [line for line in out.splitlines()
                 if "hit_count=" in line]
    assert len(top_lines) == 3


def test_analyze_cache_handles_only_writes_no_lookups(tmp_path, capsys):
    """If a session only wrote to the cache (e.g. cache enabled
    mid-session), the write summary should still render and the
    lookup section should show zeros gracefully."""
    db = tmp_path / "writes_only.db"
    store = FactStore(db)
    store.insert_turn("user", "x")
    asst = store.insert_turn("assistant", "y")
    store.insert_pipeline_event(asst, "cache_write", {
        "canonical_key": "k|only",
        "verdict": "verified",
        "stability_class": "decade_stable",
        "ttl_seconds": 86400 * 3650,
    })
    store.close()

    rc = main(["analyze_cache", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "total cache lookups:   0" in out
    assert "total cache writes:    1" in out
    assert "decade_stable" in out
