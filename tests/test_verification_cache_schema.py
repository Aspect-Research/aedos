"""Phase 6 — schema-only test for the Tier 2 verification cache.

Behavior is not wired in yet (the scoping classifier runs in observation
mode first, per the spec). This test just locks in the table shape so
the rest of the implementation has a stable foundation to build on."""

from __future__ import annotations

import sqlite3

from src.fact_store import FactStore


def test_verification_cache_table_exists(tmp_path):
    store = FactStore(tmp_path / "v.db")
    cols = store._conn.execute(
        "PRAGMA table_info(verification_cache)"
    ).fetchall()
    col_names = {c["name"] for c in cols}
    assert col_names == {
        "id", "canonical_key", "pattern", "predicate", "verdict",
        "evidence", "stability_class", "cached_at", "expires_at",
        "hit_count", "created_at",
    }
    store.close()


def test_verification_cache_has_unique_key_index(tmp_path):
    store = FactStore(tmp_path / "v.db")
    indexes = store._conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND tbl_name='verification_cache'"
    ).fetchall()
    by_name = {row["name"]: row["sql"] for row in indexes}
    assert "idx_verification_cache_key" in by_name
    assert "UNIQUE" in (by_name["idx_verification_cache_key"] or "").upper()
    assert "idx_verification_cache_expires" in by_name
    store.close()


def test_verification_cache_unique_key_constraint(tmp_path):
    store = FactStore(tmp_path / "v.db")
    store._conn.execute(
        "INSERT INTO verification_cache (canonical_key, pattern, predicate, "
        "verdict, stability_class, cached_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("k1", "spatial_temporal", "located_in", "verified",
         "decade_stable", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    store._conn.commit()
    # Inserting the same canonical_key again should violate the unique
    # index — that's how lookup-then-write avoids duplicate rows.
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO verification_cache (canonical_key, pattern, predicate, "
            "verdict, stability_class, cached_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("k1", "spatial_temporal", "located_in", "verified",
             "decade_stable", "2026-01-02T00:00:00", "2026-01-02T00:00:00"),
        )
    store.close()


def test_cache_pipeline_event_stages_registered(tmp_path):
    """The 4 cache-related stages must be in PIPELINE_STAGES so the
    observation-mode scoping classifier can log without raising."""
    from src.fact_store import PIPELINE_STAGES
    for stage in (
        "cache_scoping_decision",
        "cache_stability_decision",
        "cache_lookup",
        "cache_write",
    ):
        assert stage in PIPELINE_STAGES, f"missing pipeline stage: {stage}"

    # And insertable.
    store = FactStore(tmp_path / "v.db")
    turn_id = store.insert_turn("user", "test")
    for stage in (
        "cache_scoping_decision", "cache_stability_decision",
        "cache_lookup", "cache_write",
    ):
        store.insert_pipeline_event(turn_id, stage, {"smoke": True})
    events = store.get_pipeline_events(turn_id)
    stages = {e["stage"] for e in events}
    assert stages.issuperset({"cache_scoping_decision",
                              "cache_stability_decision",
                              "cache_lookup", "cache_write"})
    store.close()


def test_legacy_db_migration_adds_verification_cache(tmp_path):
    """A pre-v0.6 DB without the table should pick it up on FactStore
    open. ``CREATE TABLE IF NOT EXISTS`` in SCHEMA handles this — this
    test just confirms the contract."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern TEXT NOT NULL, predicate TEXT NOT NULL,
            slots TEXT NOT NULL, polarity INTEGER NOT NULL,
            confidence REAL NOT NULL, asserted_by TEXT NOT NULL,
            verification_status TEXT NOT NULL,
            valid_from TEXT, valid_until TEXT,
            source_turn_id INTEGER, source_text TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL, content TEXT NOT NULL,
            original_content TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE pipeline_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id INTEGER NOT NULL, stage TEXT NOT NULL,
            data TEXT NOT NULL, created_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()

    store = FactStore(db)
    cols = store._conn.execute("PRAGMA table_info(verification_cache)").fetchall()
    assert len(cols) > 0, "verification_cache table not created on legacy DB open"
    store.close()
