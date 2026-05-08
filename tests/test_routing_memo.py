"""Tests for src.layer2_routing.routing_memo.

Coverage:

  * Empty table: lookup() returns None.
  * record() inserts a fresh row with counts at (0, 0).
  * record() is UPSERT — calling twice for the same (pattern,
    predicate) updates method/reason/last_consulted_at and
    PRESERVES counts.
  * touch_consulted() bumps last_consulted_at without touching
    counts (principle 3: reads are not writes).
  * Multiple distinct (pattern, predicate) keys coexist.
  * PRIMARY KEY uniqueness enforced.
  * The CHECK on method rejects invalid values at the SQL layer.
  * Counts stay 0 across 50 hits — the calibration commitment as a
    unit test (the live calibration test piles on a 50-call check
    too, but this unit test runs without RUN_API_TESTS).
"""

from __future__ import annotations

import sqlite3

import pytest

from src.fact_store import FactStore
from src.layer2_routing.routing_memo import (
    ROUTING_METHODS,
    RoutingMemo,
)


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "memo.db")
    yield s
    s.close()


@pytest.fixture
def memo(store):
    return RoutingMemo(store)


# ---- empty table ----


def test_lookup_on_empty_table_returns_none(memo):
    assert memo.lookup("preference", "likes") is None


def test_list_all_on_empty_table_returns_empty_list(memo):
    assert memo.list_all() == []


# ---- record ----


def test_record_inserts_row_with_zero_counts(memo):
    entry = memo.record(
        "quantitative", "has_count", "python", "pure computation"
    )
    assert entry.pattern == "quantitative"
    assert entry.predicate == "has_count"
    assert entry.method == "python"
    assert entry.reason == "pure computation"
    assert entry.affirmed_count == 0
    assert entry.contradicted_count == 0
    assert entry.created_at is not None
    assert entry.last_consulted_at is not None


def test_record_returns_post_write_entry(memo):
    entry = memo.record("preference", "likes", "user_authoritative", "user")
    fetched = memo.lookup("preference", "likes")
    assert fetched is not None
    assert fetched == entry


def test_record_rejects_unknown_method(memo):
    with pytest.raises(ValueError, match="method"):
        memo.record("preference", "likes", "not_a_method", "x")


def test_record_with_null_reason_is_allowed(memo):
    """The reason column is nullable. Tests pin that None survives
    the round trip."""
    entry = memo.record("preference", "likes", "user_authoritative", None)
    assert entry.reason is None


# ---- UPSERT semantics + count preservation ----


def test_record_upserts_method_on_same_key(memo):
    """A second record() call updates method/reason on the same key."""
    memo.record("relational", "founded_by", "retrieval", "external")
    memo.record("relational", "founded_by", "python", "now thinks computable")
    after = memo.lookup("relational", "founded_by")
    assert after.method == "python"
    assert after.reason == "now thinks computable"


def test_record_preserves_created_at_on_upsert(memo):
    """created_at marks first insertion. UPSERT must not bump it —
    drift would make the 'how long has this row existed' inspector
    column lie."""
    first = memo.record("relational", "founded_by", "retrieval", "x")
    second = memo.record("relational", "founded_by", "python", "y")
    assert second.created_at == first.created_at


def test_record_preserves_counts_on_upsert(memo):
    """Per principle 3, counts never change on a memo write — only
    operator action increments them. UPSERT must preserve."""
    memo.record("relational", "founded_by", "retrieval", "x")
    # Simulate operator action: bump affirmed_count by 1 directly.
    memo._store._conn.execute(
        "UPDATE routing_memo SET affirmed_count = 7, contradicted_count = 2 "
        "WHERE pattern = ? AND predicate = ?",
        ("relational", "founded_by"),
    )
    memo._store._conn.commit()
    # Now UPSERT should NOT clobber the counts.
    memo.record("relational", "founded_by", "python", "drift")
    after = memo.lookup("relational", "founded_by")
    assert after.affirmed_count == 7
    assert after.contradicted_count == 2
    assert after.method == "python"


def test_record_updates_last_consulted_at_on_upsert(memo):
    """last_consulted_at is the freshness column; UPSERT bumps it."""
    first = memo.record("preference", "likes", "user_authoritative", "x")
    # Sleep is not necessary — ISO timestamps include microseconds and
    # the SECOND call's timestamp is generated at insertion time. But
    # in case the OS clock has very coarse resolution, just compare
    # for >= rather than >.
    second = memo.record("preference", "likes", "user_authoritative", "y")
    assert second.last_consulted_at >= first.last_consulted_at


# ---- touch_consulted ----


def test_touch_consulted_updates_timestamp_only(memo):
    """touch_consulted must NOT touch counts. Principle 3: reads
    are not writes."""
    memo.record("preference", "likes", "user_authoritative", "x")
    # Pre-load counts to nonzero so we can detect any mutation.
    memo._store._conn.execute(
        "UPDATE routing_memo SET affirmed_count = 5 "
        "WHERE pattern = ? AND predicate = ?",
        ("preference", "likes"),
    )
    memo._store._conn.commit()
    before = memo.lookup("preference", "likes")
    memo.touch_consulted("preference", "likes")
    after = memo.lookup("preference", "likes")
    assert after.affirmed_count == before.affirmed_count == 5
    assert after.contradicted_count == before.contradicted_count
    assert after.method == before.method
    # last_consulted_at must have moved forward (or at least not
    # gone backwards).
    assert after.last_consulted_at >= before.last_consulted_at


def test_touch_consulted_on_missing_row_is_noop(memo):
    """No row → no exception. The UPDATE matches zero rows; the call
    returns. The orchestrator's flow is 'lookup then touch on hit',
    so this is a defensive no-op rather than expected behavior."""
    memo.touch_consulted("preference", "never_seen")
    # Verify nothing was created.
    assert memo.lookup("preference", "never_seen") is None


# ---- multiple keys ----


def test_distinct_keys_coexist(memo):
    memo.record("preference", "likes", "user_authoritative", "a")
    memo.record("preference", "dislikes", "user_authoritative", "b")
    memo.record("quantitative", "has_count", "python", "c")
    rows = memo.list_all()
    assert len(rows) == 3
    keys = {(r.pattern, r.predicate) for r in rows}
    assert keys == {
        ("preference", "likes"),
        ("preference", "dislikes"),
        ("quantitative", "has_count"),
    }


# ---- counts stay 0 across many hits ----


def test_counts_stay_zero_across_50_hits(memo):
    """The architectural commitment as a unit test: the calibration
    test for memo invariance asserts the same property over a live
    LLM run, but this fast version pins the contract without an API
    key."""
    memo.record("quantitative", "has_count", "python", "pure")
    for _ in range(50):
        entry = memo.lookup("quantitative", "has_count")
        memo.touch_consulted(entry.pattern, entry.predicate)
    final = memo.lookup("quantitative", "has_count")
    assert final.affirmed_count == 0
    assert final.contradicted_count == 0
    assert final.method == "python"  # unchanged


# ---- DB-level constraints ----


def test_invalid_method_at_sql_layer_is_rejected(store):
    """Defense-in-depth: even if the Python validator is bypassed,
    the CHECK constraint on the table rejects bogus methods."""
    raw_conn = store._conn
    with pytest.raises(sqlite3.IntegrityError):
        raw_conn.execute(
            "INSERT INTO routing_memo (pattern, predicate, method, reason, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            ("preference", "likes", "not_a_method", "x", "2026-01-01"),
        )
        raw_conn.commit()


def test_primary_key_uniqueness(store):
    """A second raw INSERT (without ON CONFLICT) on the same key
    must error. record() uses ON CONFLICT DO UPDATE; this test
    verifies the underlying constraint is real."""
    raw_conn = store._conn
    raw_conn.execute(
        "INSERT INTO routing_memo (pattern, predicate, method, reason, "
        "created_at) VALUES (?, ?, ?, ?, ?)",
        ("preference", "likes", "user_authoritative", "x", "2026-01-01"),
    )
    raw_conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        raw_conn.execute(
            "INSERT INTO routing_memo (pattern, predicate, method, reason, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            ("preference", "likes", "retrieval", "y", "2026-01-02"),
        )
        raw_conn.commit()


def test_routing_methods_match_check_constraint():
    """The Python tuple ROUTING_METHODS must match the SQL CHECK
    constraint enum, otherwise the Python validator and the DB
    validator can disagree."""
    assert ROUTING_METHODS == (
        "python",
        "python_with_canonical_constants",
        "retrieval",
        "user_authoritative",
        "unverifiable",
    )
