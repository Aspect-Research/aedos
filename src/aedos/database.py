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
    subject_surface TEXT,
    object_surface TEXT,
    status TEXT NOT NULL DEFAULT 'asserted_unverified'
        CHECK(status IN ('asserted_unverified', 'externally_verified',
                         'contradicted_by_externally_verified')),
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
    single_valued INTEGER NOT NULL DEFAULT 0,
    subject_entity_types TEXT,
    object_entity_types TEXT,
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


def create_schema(conn: sqlite3.Connection, load_seeds: bool = False) -> None:
    """Create the v0.15 schema on `conn`. Idempotent.

    Phase H Cluster 3 (2026-05-26): when `load_seeds=True` and the
    `predicate_translation` table is empty, auto-loads
    `seeds/predicate_translation.json` and emits a `seeds_loaded`
    audit event. The empty-table gate preserves operator
    modifications and retractions across re-opens — re-running
    `create_schema(load_seeds=True)` on an already-seeded DB is a
    no-op. Callers wanting to refresh seeds in-place should invoke
    `aedos.seed_loader.load_seeds_into_connection` directly.

    `open_db(path)` defaults to `load_seeds=True` (production
    behavior). `open_memory_db()` defaults to `load_seeds=False`
    (test-fixture convention; tests build their own substrate).
    The corpus runner explicitly opts in to seeded mode for its
    in-vocabulary measurement; the cold-start mode (the v0.15
    default measurement) leaves load_seeds=False so every predicate
    consultation hits the LLM oracle.
    """
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
    # Phase G D33 (2026-05-23): subject_entity_types / object_entity_types
    # columns. Same idempotent ALTER pattern as single_valued. NULL default
    # — predicates without entity types skip the type filter, preserving
    # current behavior.
    for col in ("subject_entity_types", "object_entity_types"):
        try:
            conn.execute(
                f"ALTER TABLE predicate_translation ADD COLUMN {col} TEXT"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
    # Phase H D47 (2026-05-23): subject_surface / object_surface columns on
    # tier_u. With the Wikipedia normalizer wired, TierU keys subject /
    # object on the normalized (canonical) form so cross-utterance
    # references to the same entity dedupe to one row. The original
    # surface form is preserved in *_surface for trace inspection. Same
    # idempotent ALTER pattern as the D33 columns; NULL default
    # preserves pre-D47 row semantics.
    for col in ("subject_surface", "object_surface"):
        try:
            conn.execute(f"ALTER TABLE tier_u ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass

    if load_seeds:
        _maybe_load_seeds(conn)
    # Phase H Cluster 2 step 1: tier_u.status. Three-value enum tracking
    # provenance of the row — `asserted_unverified` (entered via user
    # assertion promotion), `externally_verified` (either pre-seeded as
    # established fact or upgraded by a successful KB/Python grounding
    # walk), `contradicted_by_externally_verified` (cross-source KB-wins
    # outcome). ALTER cannot add a CHECK constraint; the application
    # code (TierU.write / mark_externally_verified) enforces validity.
    # The CREATE TABLE above carries the CHECK for fresh DBs.
    #
    # Migration: pre-Cluster-2 rows pre-date the promotion path, so
    # they represent established external knowledge (operator seeds,
    # runbook Step 3 inserts) rather than in-session assertions. The
    # ALTER's `DEFAULT 'asserted_unverified'` would mis-label them; we
    # flip pre-existing rows to `externally_verified` only when the
    # ALTER succeeded (signal that the column is new to this DB).
    try:
        conn.execute(
            "ALTER TABLE tier_u ADD COLUMN status TEXT NOT NULL "
            "DEFAULT 'asserted_unverified'"
        )
        conn.execute(
            "UPDATE tier_u SET status='externally_verified' "
            "WHERE status='asserted_unverified'"
        )
    except sqlite3.OperationalError:
        pass  # column already exists; rows already carry meaningful status
    conn.commit()


def _maybe_load_seeds(conn: sqlite3.Connection) -> None:
    """Load the seed pack into `conn` iff `predicate_translation` is empty.

    Emits a `seeds_loaded` audit event on success capturing the seed
    file path and the number of entries loaded. On an already-seeded
    DB this is a no-op, so re-opens never clobber operator
    modifications.
    """
    existing = conn.execute(
        "SELECT COUNT(*) FROM predicate_translation"
    ).fetchone()[0]
    if existing > 0:
        return
    # Lazy imports — `aedos.audit.log` and `aedos.seed_loader` import
    # `aedos.database` transitively in some test paths; deferring keeps
    # the import graph acyclic.
    from .audit.log import log_event
    from .seed_loader import DEFAULT_SEED_FILE, load_seeds_into_connection
    loaded = load_seeds_into_connection(conn)
    log_event(
        conn,
        event_type="seeds_loaded",
        event_subject="predicate_translation",
        event_data={
            "seed_file": str(DEFAULT_SEED_FILE),
            "entries_loaded": loaded,
        },
    )


def open_db(path: str, load_seeds: bool = True) -> sqlite3.Connection:
    """Open a persistent SQLite DB at `path`.

    Defaults to `load_seeds=True` — production deployments expect the
    seed pack present in `predicate_translation`. The empty-table
    gate in `create_schema` means re-opening an existing DB is a
    no-op for the seed table.
    """
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    create_schema(conn, load_seeds=load_seeds)
    return conn


def open_memory_db(load_seeds: bool = False) -> sqlite3.Connection:
    """Open a fresh in-memory SQLite DB.

    Defaults to `load_seeds=False` — the test-fixture convention is
    that `open_memory_db()` produces a minimal schema-only DB into
    which tests insert their own state. Callers wanting production-
    like seeded behavior (corpus runner's seeded measurement mode)
    explicitly pass `load_seeds=True`.
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    create_schema(conn, load_seeds=load_seeds)
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
