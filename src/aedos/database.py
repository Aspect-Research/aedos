"""SQLite schema creation and connection management for Aedos."""

from __future__ import annotations

import os
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
    valid_from_ref TEXT,
    valid_until_ref TEXT,
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
    bindings TEXT,
    premise_properties TEXT,
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

CREATE TABLE IF NOT EXISTS property_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kb_namespace TEXT NOT NULL,
    kb_property TEXT NOT NULL,        -- the property the relations are ABOUT
    relation_type TEXT NOT NULL,      -- subject_type_constraint(P2302/Q21503250) |
                                      -- value_type_constraint(P2302/Q21510865) |
                                      -- inverse(P1696) | subproperty(P1647) |
                                      -- related(P1659) | single_value(P2302/Q19474404)
    related_value TEXT,               -- a Q-id (type/inverse) or P-id (subproperty/related)
    source TEXT NOT NULL,             -- 'wikidata_p2302' | 'wikidata_p1647' | ...
    created_at TEXT NOT NULL,
    last_consulted_at TEXT,
    used_count INTEGER DEFAULT 0,
    retracted_at TEXT,
    retraction_reason TEXT,
    UNIQUE(kb_namespace, kb_property, relation_type, related_value)
);

CREATE TABLE IF NOT EXISTS substrate_exceptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exception_kind TEXT NOT NULL,          -- 'subsumption' | 'transitive_path'
    relation_type TEXT NOT NULL,           -- e.g. 'part_of', 'is_a'
    property_path TEXT NOT NULL,           -- canonical "P131|P30|P17" alternation
    source_identifier TEXT NOT NULL,       -- subject / subtree root Q-id
    target_identifier TEXT NOT NULL,       -- object Q-id
    reason TEXT NOT NULL,                  -- 'ask_false' | 'operator_marked' | 'leak_guard'
    created_at TEXT NOT NULL,
    last_consulted_at TEXT,
    used_count INTEGER DEFAULT 0,
    retracted_at TEXT,
    retraction_reason TEXT,
    UNIQUE(exception_kind, relation_type, property_path, source_identifier, target_identifier)
);

CREATE INDEX IF NOT EXISTS idx_property_relations_prop
    ON property_relations(kb_namespace, kb_property);
CREATE INDEX IF NOT EXISTS idx_substrate_exceptions_lookup
    ON substrate_exceptions(exception_kind, relation_type, source_identifier, target_identifier);

CREATE INDEX IF NOT EXISTS idx_tier_u_party_pred ON tier_u(asserting_party, predicate);
CREATE INDEX IF NOT EXISTS idx_predicate_translation_pred ON predicate_translation(aedos_predicate);
CREATE INDEX IF NOT EXISTS idx_audit_log_type ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_log_occurred ON audit_log(occurred_at);

-- v0.16.2 observability: durable verification store backing GET /verification/{id}.
-- The full per-claim walk result (verdict + lossless trace + resolved QIDs +
-- directed-over-enumerate signals + per-claim budget) is captured at verify time so
-- the audit read needs ZERO re-walk and survives process restart. Party-scoped on
-- the core row (mirrors tier_u isolation). persist() is delete-then-insert per
-- verification_id, so a stale re-derivation re-persists with no orphan child rows.
CREATE TABLE IF NOT EXISTS verification (
    verification_id TEXT PRIMARY KEY,
    asserting_party TEXT NOT NULL,             -- party scope (session:<id>)
    created_at TEXT NOT NULL,                  -- ISO; when the verification finished
    source_kind TEXT NOT NULL,                 -- 'chat' | 'verify'
    user_message TEXT,                         -- chat only (text_input.message)
    draft_message TEXT,                        -- the verified text (text_input.draft / raw)
    final_message TEXT,                        -- chat only (composed reply)
    intervention_type TEXT,                    -- chat only
    aggregate_metadata TEXT NOT NULL,          -- JSON: full vr.aggregate_metadata
    consistency_warnings TEXT NOT NULL DEFAULT '[]',  -- JSON list[dict]
    audit_log_entries TEXT NOT NULL DEFAULT '[]',     -- JSON list[int] (audit_log.id refs)
    not_assessed_claims TEXT NOT NULL DEFAULT '[]',   -- JSON list[dict] (Phase D peripheral)
    selection_summary TEXT,
    extracted_claims TEXT NOT NULL DEFAULT '[]',      -- JSON list[dict]: EVERY extracted claim
                                                      -- (incl. extraction-abstained, with reason)
    per_claim_actions TEXT NOT NULL DEFAULT '[]'      -- JSON list[dict]: intervention actions
                                                      -- (action_type + annotation) per claim
);
CREATE INDEX IF NOT EXISTS idx_verification_party ON verification(asserting_party, created_at);

CREATE TABLE IF NOT EXISTS verification_claim (
    verification_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    claim_index INTEGER NOT NULL,              -- order within the verification
    -- denormalized Claim
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    polarity INTEGER NOT NULL,
    source_text TEXT,
    asserting_party TEXT NOT NULL,
    claim_abstention_reason TEXT,              -- extraction-layer reason (Claim.abstention_reason)
    valid_from TEXT, valid_until TEXT,
    valid_during_ref TEXT, valid_from_ref TEXT, valid_until_ref TEXT,
    -- verdict
    verdict TEXT NOT NULL,
    base_verdict TEXT NOT NULL,
    is_given_assertion INTEGER NOT NULL DEFAULT 0,
    abstention_reason TEXT,                     -- walk/KB-layer reason (closed bucket)
    contradicting_value TEXT,
    contradicting_value_type TEXT,
    -- resolved entity refs (pulled from the trace at write time)
    resolved_subject_qid TEXT,
    resolved_subject_cache_row_id INTEGER,
    resolved_value_qid TEXT,
    -- per-claim budget (threaded from WalkResult; NOT in trace_to_json)
    wall_clock_ms REAL,
    llm_calls INTEGER,
    -- directed-over-enumerate signals (from the KB edge metadata stamp)
    functional_value_known INTEGER,
    value_known_entity INTEGER,
    functional_entity_predicate INTEGER,
    PRIMARY KEY (verification_id, claim_id)
);
CREATE INDEX IF NOT EXISTS idx_vclaim_vid ON verification_claim(verification_id, claim_index);
CREATE INDEX IF NOT EXISTS idx_vclaim_pred_verdict ON verification_claim(predicate, base_verdict);

CREATE TABLE IF NOT EXISTS verification_trace (
    verification_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    trace_json TEXT NOT NULL,                  -- json.dumps(trace_to_json_lossless(...))
    trace_human TEXT,                          -- row-id-free human render
    PRIMARY KEY (verification_id, claim_id)
);

-- Retraction reverse-index: the durable, queryable form of the provenance
-- footprint ("which verifications depend on tier_u row 42?"). literal_index keys
-- each distinct literal so transient (NULL table/row_id) literals never collide.
CREATE TABLE IF NOT EXISTS verification_premise (
    verification_id TEXT NOT NULL,
    claim_id TEXT NOT NULL,
    literal_index INTEGER NOT NULL,
    source TEXT NOT NULL,                      -- tier_u|kb|python|subsumption|predicate_translation|entity_resolution
    source_table TEXT,                         -- NULL for transient (live KB) literals
    source_row_id INTEGER,                     -- NULL for transient literals
    premise_status TEXT,                       -- tier_u premise status, else NULL
    is_assertion INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (verification_id, claim_id, literal_index)
);
CREATE INDEX IF NOT EXISTS idx_vpremise_row ON verification_premise(source_table, source_row_id);
CREATE INDEX IF NOT EXISTS idx_vpremise_claim ON verification_premise(verification_id, claim_id);
"""


def create_schema(conn: sqlite3.Connection, load_seeds: bool = False) -> None:
    """Create the schema on `conn`. Idempotent.

    When `load_seeds=True` and the
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
    in-vocabulary measurement; the cold-start mode leaves
    load_seeds=False so every predicate consultation hits the LLM
    oracle.
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
    # subject_entity_types / object_entity_types
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
    # subject_surface / object_surface columns on
    # tier_u. With the Wikipedia normalizer wired, TierU keys subject /
    # object on the normalized (canonical) form so cross-utterance
    # references to the same entity dedupe to one row. The original
    # surface form is preserved in *_surface for trace inspection. Same
    # idempotent ALTER pattern as the entity-types columns; NULL default
    # preserves prior row semantics.
    for col in ("subject_surface", "object_surface"):
        try:
            conn.execute(f"ALTER TABLE tier_u ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass
    # v0.16.1 WS8 Stage 1: tier_u.valid_from_ref / valid_until_ref — event-relative
    # bound references mirroring valid_during_ref ("after/since <event>" lower
    # bound, "before <event>" upper bound). WRITE-ONLY metadata: no grounding /
    # verdict path reads them (Stage 2 resolver deferred). Same idempotent ALTER
    # pattern as the surface columns; additive, non-destructive, NULL default
    # preserves all prior row semantics.
    for col in ("valid_from_ref", "valid_until_ref"):
        try:
            conn.execute(f"ALTER TABLE tier_u ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass
    # v0.16: predicate_translation.bindings — JSON list of
    # the multi-property PredicateBinding ranked set discovered from
    # Wikidata's ontology. Legacy scalar columns are retained; rows with
    # bindings IS NULL read-synthesize one binding from the scalar columns
    # (_row_to_metadata / PredicateMetadata.__post_init__). Same idempotent
    # ALTER pattern as the entity-types columns; additive and non-destructive.
    try:
        conn.execute("ALTER TABLE predicate_translation ADD COLUMN bindings TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    # v0.16.1 WS3b: predicate_translation.premise_properties — JSON map from an
    # Aedos slot name to the KB property whose value is a premise for a
    # routing_hint='python' comparison predicate (the premise -> Python channel).
    # NULL preserves prior behavior (no premise fetch). Additive/non-destructive,
    # same idempotent ALTER pattern.
    try:
        conn.execute("ALTER TABLE predicate_translation ADD COLUMN premise_properties TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists

    if load_seeds:
        _maybe_load_seeds(conn)
    # tier_u.status. Three-value enum tracking
    # provenance of the row — `asserted_unverified` (entered via user
    # assertion promotion), `externally_verified` (either pre-seeded as
    # established fact or upgraded by a successful KB/Python grounding
    # walk), `contradicted_by_externally_verified` (cross-source KB-wins
    # outcome). ALTER cannot add a CHECK constraint; the application
    # code (TierU.write / mark_externally_verified) enforces validity.
    # The CREATE TABLE above carries the CHECK for fresh DBs.
    #
    # Migration: rows that pre-date the promotion path represent
    # established external knowledge (operator seeds, runbook
    # inserts) rather than in-session assertions. The
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
    # Defense-in-depth (v0.16.2 deploy hardening): create the parent dir if a
    # directory component is present. sqlite3.connect() does NOT create it and
    # would otherwise raise "unable to open database file" — e.g. when
    # AEDOS_DB_PATH points at data/<db> on a host where data/ was not
    # pre-created. A bare filename (no dir component) yields an empty dirname
    # and is left untouched; ":memory:" goes through open_memory_db, not here.
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
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
    "property_relations",
    "substrate_exceptions",
    "verification",
    "verification_claim",
    "verification_trace",
    "verification_premise",
]
