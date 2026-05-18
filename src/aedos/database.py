"""SQLite schema creation and connection management for Aedos v0.15."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Generator


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tier_u (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asserting_party TEXT NOT NULL,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    polarity INTEGER NOT NULL CHECK(polarity IN (0, 1)),
    resolved_subject_id TEXT,
    resolved_object_id TEXT,
    valid_from TEXT,
    valid_until TEXT,
    valid_during_ref TEXT,
    source_text TEXT NOT NULL,
    source_context TEXT,
    asserted_at TEXT NOT NULL,
    retracted_at TEXT,
    retraction_reason TEXT,
    UNIQUE(asserting_party, subject, predicate, object, polarity, asserted_at)
);

CREATE TABLE IF NOT EXISTS predicate_translation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aedos_predicate TEXT NOT NULL,
    object_type TEXT NOT NULL,
    user_subject_required INTEGER DEFAULT 0,
    distinct_slots TEXT,
    routing_hint TEXT NOT NULL,
    kb_namespace TEXT,
    kb_property TEXT,
    slot_to_qualifier TEXT,
    single_valued INTEGER DEFAULT 0,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_consulted_at TEXT,
    used_count INTEGER DEFAULT 0,
    retracted_at TEXT,
    retraction_reason TEXT,
    UNIQUE(aedos_predicate, kb_namespace)
);

CREATE TABLE IF NOT EXISTS subsumption (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_a_namespace TEXT NOT NULL,
    entity_a_identifier TEXT NOT NULL,
    entity_b_namespace TEXT NOT NULL,
    entity_b_identifier TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    verdict TEXT NOT NULL,
    source TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_consulted_at TEXT,
    used_count INTEGER DEFAULT 0,
    retracted_at TEXT,
    retraction_reason TEXT,
    UNIQUE(entity_a_namespace, entity_a_identifier, entity_b_namespace, entity_b_identifier, relation_type)
);

CREATE TABLE IF NOT EXISTS predicate_distribution (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aedos_predicate TEXT NOT NULL,
    polarity INTEGER NOT NULL CHECK(polarity IN (0, 1)),
    relation_type TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_consulted_at TEXT,
    used_count INTEGER DEFAULT 0,
    retracted_at TEXT,
    retraction_reason TEXT,
    UNIQUE(aedos_predicate, polarity, relation_type)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    event_subject TEXT NOT NULL,
    event_data TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    verification_context TEXT
);

CREATE TABLE IF NOT EXISTS consistency_circuit_breaker (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_signature TEXT NOT NULL UNIQUE,
    cycle_count INTEGER NOT NULL DEFAULT 0,
    last_triggered_at TEXT NOT NULL,
    unresolvable INTEGER NOT NULL DEFAULT 0,
    unresolvable_reason TEXT
);

CREATE TABLE IF NOT EXISTS entity_resolution_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reference TEXT NOT NULL,
    local_context_signature TEXT NOT NULL,
    resolved_kb_namespace TEXT NOT NULL,
    resolved_kb_identifier TEXT NOT NULL,
    provenance TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    used_count INTEGER DEFAULT 0,
    retracted_at TEXT,
    retraction_reason TEXT,
    UNIQUE(reference, local_context_signature)
);

CREATE INDEX IF NOT EXISTS idx_tier_u_party_pred ON tier_u(asserting_party, predicate);
CREATE INDEX IF NOT EXISTS idx_predicate_translation_pred ON predicate_translation(aedos_predicate);
CREATE INDEX IF NOT EXISTS idx_audit_log_type ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_log_occurred ON audit_log(occurred_at);
"""


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_SQL)
    # Migration guard (N6): a database created before single_valued was added
    # to predicate_translation lacks the column — CREATE TABLE IF NOT EXISTS
    # will not add it to an existing table. ALTER it in idempotently. On a
    # fresh DB the column already exists from the CREATE above, so the ALTER
    # raises OperationalError ("duplicate column name") and is swallowed.
    try:
        conn.execute(
            "ALTER TABLE predicate_translation ADD COLUMN single_valued INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    create_schema(conn)
    return conn


def open_memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    create_schema(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection, None, None]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


TABLE_NAMES = [
    "tier_u",
    "predicate_translation",
    "subsumption",
    "predicate_distribution",
    "audit_log",
    "consistency_circuit_breaker",
    "entity_resolution_cache",
]
