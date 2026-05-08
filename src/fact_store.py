"""SQLite-backed fact store for v0.14.

Schema deltas relative to v0.13 (``src/fact_store.py``):

  - ``reinforcement_count`` -> ``affirmed_count``
  - new column ``contradicted_count``
  - new column ``is_session_local``
  - ``session_id`` removed; replaced by ``session_ids`` (JSON array)
    with a CHECK constraint enforcing array length <= 1 when
    ``is_session_local = 1``

All other columns are identical. ``turns``, ``pipeline_events``,
``retrieval_cache``, ``verification_cache``, ``cache_invalidation_log``,
and the ``facts_flat`` view are byte-identical ports.

Phase 0 ships only the schema, the ``Fact`` dataclass, and CRUD;
``boost_confidence`` increments ``affirmed_count`` and recomputes
confidence via ``layer2_routing.constants.confidence_from_counts``.
The contradicted-count write side is left for Phase 6, when the
session model rewrite makes use of it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class _LockedConnection:
    """Thread-safety wrapper around ``sqlite3.Connection``.

    Identical to the v0.13 wrapper. Per-claim parallel verify in the
    pipeline (introduced in v0.9.0 of the legacy stack) writes to
    ``pipeline_events`` and ``facts`` from worker threads; without a
    process-wide RLock SQLite raises
    ``cannot start a transaction within a transaction``. The wrapper
    keeps Python-side transaction state consistent with one statement
    in flight at a time.
    """

    __slots__ = ("_conn", "_lock")

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_lock", lock)

    def execute(self, *args, **kwargs):
        with self._lock:
            return self._conn.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        with self._lock:
            return self._conn.executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        with self._lock:
            return self._conn.executescript(*args, **kwargs)

    def commit(self):
        with self._lock:
            return self._conn.commit()

    def rollback(self):
        with self._lock:
            return self._conn.rollback()

    def close(self):
        with self._lock:
            return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name in ("_conn", "_lock"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._conn, name, value)


# Valid enumerations.
POLARITIES = {0, 1}
ASSERTED_BY = {"user", "model", "python_verifier", "external"}
VERIFICATION_STATUSES = {
    "verified",
    "contradicted",
    "user_asserted",
    "unverifiable_in_principle",
    "retrieval_inconclusive",
    "retrieval_failed",
    "unverifiable_pending_implementation",
    "routing_anomaly",
}
PIPELINE_STAGES = {
    "user_extraction",
    "user_storage",
    "assistant_draft",
    "assistant_extraction",
    "verification",
    "correction",
    "final",
    "routing_anomaly_detected",
    "retrieval_query_attempt",
    "verifier_failure",
    "code_prompt_built",
    "code_prompt_leakage_detected",
    "code_generated",
    "code_executed",
    "code_unusual_behavior",
    "code_comparison",
    "routing_decision",
    "canonical_constants_cross_check",
    "canonical_constants_disagreement",
    "chat_model_call",
    "cache_scoping_decision",
    "cache_stability_decision",
    "cache_lookup",
    "cache_write",
    "cache_contradiction_replaced",
    "cache_pruned",
    "cache_savings",
    "tier_lookup",
    "comparative_detected",
    "judge_retry_after_inconclusive",
    "reformulation_emitted",
    "reformulation_failed",
    "turn_cost",
    "extractor_substitution_warning",
    # v0.14 Phase 2 — Layer 2 validator + routing memo events.
    # routing_validation_failed: validator's first-failed invariant
    # payload (one per anomaly claim). routing_memo_hit: memo lookup
    # short-circuited the LLM router. routing_memo_write: the memo
    # row was created (or upserted) after the LLM router ran.
    "routing_validation_failed",
    "routing_memo_hit",
    "routing_memo_write",
    # v0.14 Phase 3 — predicate_equivalence oracle. *_hit fires on
    # SQL cache hit (no LLM call); *_write fires after the LLM
    # classifier ran and a row was UPSERTed; *_classification_failed
    # fires when the LLM produced malformed tool output (unknown
    # label, missing fields). The shared oracle_consulted stage gives
    # the trace UI a single grep-target across all four substrate
    # oracles when Phase 4-5 land — its data payload always includes
    # an `oracle` key naming which one was consulted.
    "predicate_equivalence_hit",
    "predicate_equivalence_write",
    "predicate_equivalence_classification_failed",
    "oracle_consulted",
    # v0.14 Phase 4 — entity_equivalence oracle. Same shape as the
    # predicate_equivalence events: *_hit on SQL cache hit, *_write
    # after LLM-driven UPSERT, *_classification_failed on malformed
    # tool output. The shared oracle_consulted stage continues to
    # serve as the trace-UI grep target across all four substrate
    # oracles; its data payload's ``oracle`` key disambiguates.
    "entity_equivalence_hit",
    "entity_equivalence_write",
    "entity_equivalence_classification_failed",
    # v0.14 Phase 5 — entity_taxonomy and predicate_distribution
    # oracles. Same event shape as Phases 3-4. Both oracles are
    # DORMANT in Phase 5: no Tier U / W / derivation consumer wires
    # them. They populate via direct calls (tests + inspector
    # endpoints). Phase 7's derivation walker is the consumer that
    # turns these substrate rows into derived verdicts.
    "entity_taxonomy_hit",
    "entity_taxonomy_write",
    "entity_taxonomy_classification_failed",
    "predicate_distribution_hit",
    "predicate_distribution_write",
    "predicate_distribution_classification_failed",
    # v0.14 Phase 6 — Tier U session model. tier_u_storage fires
    # on every user-fact insertion, cross-session reaffirmation, or
    # same-session noop. Payload carries the StoreUserFactResult
    # fields (outcome, fact_id, session_ids_after,
    # affirmed_count_after, is_session_local, current_session) plus
    # the matched session-marker phrase when one fired
    # (marker_detected_phrase) and the marker_ignored_no_session
    # flag for the marker-with-no-active-session case.
    "tier_u_storage",
    # v0.14 Phase 7 — Tier W (world cache) and the walker /
    # derivation orchestration. tier_w_hit / _write fire on cache
    # operations; tier_w_lookup is the per-stage outcome of the
    # three-stage resolution chain (literal → predicate_equivalence
    # → entity_equivalence). derivation_walk_* events trace the
    # BFS engine's expansion / abort decisions. walker_decision
    # is the final Layer-4 verdict that flows to Layer 5 in Phase
    # 8. fresh_dispatch fires when the walker falls through to
    # verifier dispatch. cache_contradiction_replaced is reused
    # from v1's vocabulary when Tier W's write_verifier_result
    # overwrites an opposite-verdict prior row.
    "tier_w_hit",
    "tier_w_write",
    "tier_w_lookup",
    "derivation_walk_attempt",
    "derivation_walk_completed",
    "derivation_walk_aborted_depth",
    "derivation_walk_aborted_reliability",
    # v0.14 Phase 8 — bounded-active classification budget on derivation
    # walks. derivation_walk_active_classification fires once per cold
    # predicate_distribution row classified during a walk (budget > 0).
    # derivation_walk_budget_exhausted fires once per walk when a cold
    # row is encountered after the budget is depleted; the walker treats
    # subsequent cold rows as if llm were None (graceful fall-through).
    "derivation_walk_active_classification",
    "derivation_walk_budget_exhausted",
    "walker_decision",
    "fresh_dispatch",
    # v0.14 Phase 8 — operator-action endpoints. The ONLY paths that
    # increment oracle row counts (architecture principle 3 mutation
    # discipline). Payload: {oracle, row_id, affirmed_count,
    # contradicted_count, confidence} after the increment.
    "oracle_affirmed",
    "oracle_contradicted",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


DEFAULT_USER_ID = "default_user"
DEFAULT_SESSION_ID = "default_session"


SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,
    predicate TEXT NOT NULL,
    slots TEXT NOT NULL,                -- JSON object keyed by slot name
    polarity INTEGER NOT NULL,
    -- Beta(1,1)-smoothed posterior over (affirmed_count, contradicted_count).
    -- Denormalized so the read path doesn't recompute on every query.
    confidence REAL NOT NULL,
    -- v0.14: counts are independent-external-evidence only (architecture
    -- principle 3). Cache hits, oracle-mediated equivalence resolutions,
    -- and subsumption-derived matches do NOT increment these.
    affirmed_count INTEGER NOT NULL DEFAULT 0,
    contradicted_count INTEGER NOT NULL DEFAULT 0,
    -- v0.14: session model. is_session_local=1 means the fact only
    -- exists within the listed sessions ("let's say for this conversation").
    -- is_session_local=0 means cross-session: the session_ids list
    -- accumulates which sessions reaffirmed the fact and drives the
    -- Beta-posterior reinforcement on cross-session reaffirmation.
    is_session_local INTEGER NOT NULL DEFAULT 0,
    session_ids TEXT NOT NULL DEFAULT '[]',
    asserted_by TEXT NOT NULL,
    verification_status TEXT NOT NULL,
    valid_from TEXT,
    valid_until TEXT,
    source_turn_id INTEGER,
    source_text TEXT,
    created_at TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT 'default_user',
    -- Session-local facts must have at most one session in their list.
    -- "let's say for this conversation" hypotheticals are scoped to a
    -- single session by design (architecture: "session_ids set"
    -- terminology applies to cross-session facts; session-locals are
    -- effectively single-session). When is_session_local=0 the array
    -- is unconstrained and grows with each new affirming session.
    CHECK (
        is_session_local = 0
        OR json_array_length(session_ids) <= 1
    )
);

CREATE INDEX IF NOT EXISTS idx_facts_pattern          ON facts(pattern);
CREATE INDEX IF NOT EXISTS idx_facts_predicate        ON facts(predicate);
CREATE INDEX IF NOT EXISTS idx_facts_valid_until      ON facts(valid_until);
CREATE INDEX IF NOT EXISTS idx_facts_user_id          ON facts(user_id);
CREATE INDEX IF NOT EXISTS idx_facts_is_session_local ON facts(is_session_local);

CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    original_content TEXT,
    created_at TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT 'default_user'
);
CREATE INDEX IF NOT EXISTS idx_turns_user_id ON turns(user_id);

CREATE TABLE IF NOT EXISTS pipeline_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id INTEGER NOT NULL,
    stage TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (turn_id) REFERENCES turns(id)
);
CREATE INDEX IF NOT EXISTS idx_events_turn ON pipeline_events(turn_id);

CREATE TABLE IF NOT EXISTS retrieval_cache (
    query TEXT PRIMARY KEY,
    snippets TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verification_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key TEXT NOT NULL,
    pattern TEXT NOT NULL,
    predicate TEXT NOT NULL,
    verdict TEXT NOT NULL,
    evidence TEXT,
    stability_class TEXT NOT NULL,
    cached_at TEXT NOT NULL,
    expires_at TEXT,
    hit_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    evidence_hash TEXT,
    source_urls TEXT,
    last_refreshed_at TEXT,
    refresh_count INTEGER NOT NULL DEFAULT 0,
    contradiction_count INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_verification_cache_key
    ON verification_cache(canonical_key);
CREATE INDEX IF NOT EXISTS idx_verification_cache_expires
    ON verification_cache(expires_at);

CREATE TABLE IF NOT EXISTS cache_invalidation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reason TEXT NOT NULL,
    primary_key TEXT NOT NULL,
    propagated_to_keys TEXT,
    detail TEXT,
    triggered_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_invalidation_created_at
    ON cache_invalidation_log(created_at);

-- v0.14 Phase 2 — routing memo. One row per (pattern, predicate)
-- pair the LLM router has classified. Hits short-circuit the router
-- to <5ms; the schema's affirmed_count / contradicted_count columns
-- exist for principle-3 frequentist tracking but are NOT incremented
-- by Phase 2 code paths. Counts only ever change via operator-driven
-- re-judgment endpoints, which arrive in Phase 8 with the substrate
-- inspectors. last_consulted_at is observability metadata (operator
-- can see "this row hasn't been consulted in 30 days; consider
-- whether the predicate is still in use") and is intentionally
-- updated on hit — it is NOT a reinforcement signal under principle
-- 3, just a freshness column.
CREATE TABLE IF NOT EXISTS routing_memo (
    pattern TEXT NOT NULL,
    predicate TEXT NOT NULL,
    method TEXT NOT NULL CHECK (method IN (
        'python',
        'python_with_canonical_constants',
        'retrieval',
        'user_authoritative',
        'unverifiable'
    )),
    reason TEXT,
    affirmed_count INTEGER NOT NULL DEFAULT 0,
    contradicted_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_consulted_at TEXT,
    PRIMARY KEY (pattern, predicate)
);

-- v0.14 Phase 3 — predicate_equivalence oracle. One row per unordered
-- pair of predicates within a pattern, with predicate_a < predicate_b
-- enforced by CHECK so canonical ordering is a SQL-layer invariant.
-- The label tells callers what the pair means and slot_reversal
-- describes how slot values transform when the predicates swap.
--
-- slot_reversal is TEXT-typed (not bool) per the architecture doc:
--   * 'none'                 — slot mapping is identity (preference's
--                              likes/dislikes; symmetric relations)
--   * 'subject_object_swap'  — active/passive inversion swaps the
--                              subject and object slots (defeated/
--                              defeated_by, founded/founded_by, wrote/
--                              authored_by)
--   * 'participant_reorder'  — event pattern: participants list order
--                              matters. Not used in Phase 3's
--                              predicate_equivalence corpus, but the
--                              enum reserves it now to avoid a future
--                              schema migration.
--
-- Counts (affirmed/contradicted) are independent-external-evidence only
-- per principle 3. consult-time hits do NOT increment them; only the
-- operator-driven re-judgment endpoint (Phase 8) and contradiction
-- propagation (Phase 7+) do. last_consulted_at is observability
-- metadata that DOES update on hit (same discipline as routing_memo).
CREATE TABLE IF NOT EXISTS predicate_equivalence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,
    predicate_a TEXT NOT NULL,
    predicate_b TEXT NOT NULL,
    label TEXT NOT NULL CHECK (label IN (
        'equivalent', 'contradictory', 'distinct'
    )),
    slot_reversal TEXT NOT NULL DEFAULT 'none' CHECK (slot_reversal IN (
        'none', 'subject_object_swap', 'participant_reorder'
    )),
    reason TEXT,
    affirmed_count INTEGER NOT NULL DEFAULT 0,
    contradicted_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_consulted_at TEXT,
    UNIQUE (pattern, predicate_a, predicate_b),
    CHECK (predicate_a < predicate_b)
);
CREATE INDEX IF NOT EXISTS idx_predicate_equivalence_pattern
    ON predicate_equivalence(pattern);

-- v0.14 Phase 4 — entity_equivalence oracle. Two-label classifier
-- ('same' | 'different'). NO pattern column: entities are pattern-
-- independent — "NYC" denotes the same entity whether it appears as
-- a spatial_temporal location, a preference object, or anywhere
-- else. Keying on pattern would create unnecessary row duplication
-- and miss the cross-pattern equivalences that Phase 7's derivation
-- walker relies on.
--
-- Case-sensitive storage. The canonical helper for this oracle does
-- ``strip()`` only (NO lowercase) — case carries entity-disambiguation
-- signal (apple vs Apple, turkey vs Turkey, mercury vs Mercury). The
-- CHECK on entity_a < entity_b is case-sensitive lex; "Apple" sorts
-- before "apple" by ASCII.
--
-- No slot_reversal column. Entities don't have argument-order
-- semantics; equivalence is symmetric in label and the canonical-
-- helper trick from predicate_equivalence applies cleanly.
--
-- Counts (affirmed/contradicted) are independent-external-evidence
-- only per principle 3. consult-time hits do NOT increment them;
-- only the operator-driven re-judgment endpoint (Phase 8) and
-- contradiction propagation (Phase 7+) do.
CREATE TABLE IF NOT EXISTS entity_equivalence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_a TEXT NOT NULL,
    entity_b TEXT NOT NULL,
    label TEXT NOT NULL CHECK (label IN ('same', 'different')),
    reason TEXT,
    affirmed_count INTEGER NOT NULL DEFAULT 0,
    contradicted_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_consulted_at TEXT,
    UNIQUE (entity_a, entity_b),
    CHECK (entity_a < entity_b)
);
CREATE INDEX IF NOT EXISTS idx_entity_equivalence_entity_a
    ON entity_equivalence(entity_a);
CREATE INDEX IF NOT EXISTS idx_entity_equivalence_entity_b
    ON entity_equivalence(entity_b);

-- v0.14 Phase 5 — entity_taxonomy oracle. Four-label classifier over
-- (child, parent, relation_type) triples:
--   * 'child_subsumed_by_parent' — natural direction: the entity in
--     the child column is subsumed by (is_a / part_of) the entity in
--     the parent column. e.g. (golden retriever, dog, is_a) or
--     (Williamstown, Massachusetts, part_of).
--   * 'parent_subsumed_by_child' — caller passed them in inverted
--     order. e.g. (mammal, golden retriever, is_a). The oracle still
--     records under the column positions the caller used; the LABEL
--     tells the consumer which direction the subsumption goes.
--   * 'equivalent' — same level under the relation. Holland and the
--     Netherlands; Burma and Myanmar. Rare but possible.
--   * 'neither' — no taxonomic relation under this relation_type.
--     (apple, fruit, is_a) is child_subsumed_by_parent, but
--     (Apple, fruit, is_a) — the company and the fruit category — is
--     neither. (doctor, hospital, is_a) is neither (functional
--     relation, not categorical).
--
-- Pattern-independent (no pattern column) — taxonomy holds across
-- all patterns, and pattern-keying would force re-classification.
-- Mirrors entity_equivalence in this respect.
--
-- DIRECTIONAL — no canonical-pair swap. The (child, parent) ordering
-- is positional and meaningful. The label encodes which direction
-- the subsumption goes regardless of which way the caller passed
-- them. UNIQUE on (child, parent, relation_type) with NO CHECK
-- constraint on column ordering. The CHECK (child != parent)
-- prevents self-pairs at the SQL layer.
--
-- Case-sensitive — strip-only normalization in the oracle's
-- entry-point validators. Apple (company) and apple (fruit) are
-- distinct entities; case carries disambiguation signal.
--
-- relation_type enum {is_a, part_of}. The taxonomy unifies
-- categorical (is_a) and constitutive parthood (part_of) chains
-- in one table; the column distinguishes them. Phase 7's derivation
-- walker filters by relation_type when navigating.
--
-- Counts: independent-external-evidence only per principle 3.
-- consult-time hits do NOT increment them; only the operator
-- endpoint (Phase 8) and contradiction propagation (Phase 7+) do.
CREATE TABLE IF NOT EXISTS entity_taxonomy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child TEXT NOT NULL,
    parent TEXT NOT NULL,
    relation_type TEXT NOT NULL CHECK (relation_type IN ('is_a', 'part_of')),
    label TEXT NOT NULL CHECK (label IN (
        'child_subsumed_by_parent',
        'parent_subsumed_by_child',
        'equivalent',
        'neither'
    )),
    reason TEXT,
    affirmed_count INTEGER NOT NULL DEFAULT 0,
    contradicted_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_consulted_at TEXT,
    UNIQUE (child, parent, relation_type),
    CHECK (child != parent)
);
CREATE INDEX IF NOT EXISTS idx_entity_taxonomy_child
    ON entity_taxonomy(child);
CREATE INDEX IF NOT EXISTS idx_entity_taxonomy_parent
    ON entity_taxonomy(parent);
CREATE INDEX IF NOT EXISTS idx_entity_taxonomy_relation_type
    ON entity_taxonomy(relation_type);

-- v0.14 Phase 5 — predicate_distribution oracle. Four-label
-- classifier over (pattern, predicate, polarity, taxonomy_relation_
-- type) tuples. Asks: "for this predicate at this polarity, when we
-- walk a chain of this taxonomy relation_type, does the predicate's
-- truth propagate?"
--
-- Labels:
--   * 'distributes_up'   — truth at the more-specific entity
--                          propagates UP to the more-general. e.g.
--                          (spatial_temporal, lives_in, p=1, part_of)
--                          — lives in Williamstown ⇒ lives in
--                          Massachusetts.
--   * 'distributes_down' — truth at the more-general entity
--                          propagates DOWN to the more-specific.
--                          e.g. (preference, likes, p=1, is_a) —
--                          likes animals ⇒ likes cheetahs.
--   * 'both'             — distributes both directions. Rare;
--                          reserved for Phase 7+ discovery.
--   * 'neither'          — no propagation in either direction. e.g.
--                          (quantitative, weighs, p=1, is_a) — an
--                          object's weight is a property of itself,
--                          not a category-inheritance.
--
-- DIRECTIONAL — no canonical-pair swap because there are no pairs.
-- Each row is a singleton verdict for a 4-tuple key. The
-- architectural commitment in classifier_base.py: the symmetric-
-- pair encapsulation (predicate_equivalence, entity_equivalence)
-- and the directional-pair encapsulation (entity_taxonomy) do NOT
-- generalize to predicate_distribution; this oracle owns its own
-- key handling.
--
-- Pattern-keyed — predicates are pattern-scoped (a quantitative.
-- has_count is a different relation than a hypothetical categorical.
-- has_count). Polarity-keyed — distribution behavior can differ
-- across polarities (positive dislikes distributes down is_a;
-- negative dislikes likely doesn't, since "I don't dislike X"
-- doesn't say what I think about X's instances).
--
-- Predicate names are lowercased and stripped per Phase 3's
-- _normalize_predicate convention — distribution behavior is a
-- property of the predicate's semantics, not its capitalization.
--
-- relation_type enum {is_a, part_of} (named taxonomy_relation_type
-- here to avoid ambiguity with predicate semantics; the column
-- references entity_taxonomy.relation_type values).
--
-- Counts: independent-external-evidence only per principle 3.
-- consult-time hits do NOT increment them; only the operator
-- endpoint (Phase 8) and contradiction propagation (Phase 7+) do.
CREATE TABLE IF NOT EXISTS predicate_distribution (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,
    predicate TEXT NOT NULL,
    polarity INTEGER NOT NULL CHECK (polarity IN (0, 1)),
    taxonomy_relation_type TEXT NOT NULL CHECK (
        taxonomy_relation_type IN ('is_a', 'part_of')
    ),
    label TEXT NOT NULL CHECK (label IN (
        'distributes_up',
        'distributes_down',
        'both',
        'neither'
    )),
    reason TEXT,
    affirmed_count INTEGER NOT NULL DEFAULT 0,
    contradicted_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_consulted_at TEXT,
    UNIQUE (pattern, predicate, polarity, taxonomy_relation_type)
);
CREATE INDEX IF NOT EXISTS idx_predicate_distribution_pattern_predicate
    ON predicate_distribution(pattern, predicate);

-- Flat projection of facts for the UI / quick inspection. The pattern
-- determines which slot fills "subject" and "object" semantically.
-- v0.14 Phase 1: mereological's `part` projects as subject and `whole`
-- as object so the flat view stays consistent for the new pattern.
-- Listed last in each COALESCE so existing pattern projections are
-- unaffected when both legacy and mereological slots happen to coexist
-- (they don't, but the order is defensive).
CREATE VIEW IF NOT EXISTS facts_flat AS
SELECT
    id,
    pattern,
    predicate,
    COALESCE(
        json_extract(slots, '$.agent'),
        json_extract(slots, '$.subject'),
        json_extract(slots, '$.entity'),
        json_extract(slots, '$.event_type'),
        json_extract(slots, '$.part')
    ) AS subject,
    COALESCE(
        json_extract(slots, '$.role'),
        json_extract(slots, '$.object'),
        json_extract(slots, '$.category'),
        json_extract(slots, '$.location'),
        json_extract(slots, '$.proposition'),
        json_extract(slots, '$.value'),
        json_extract(slots, '$.relation'),
        json_extract(slots, '$.whole')
    ) AS object,
    polarity,
    confidence,
    asserted_by,
    verification_status,
    valid_from,
    valid_until,
    source_turn_id,
    source_text,
    created_at,
    slots
FROM facts;
"""


@dataclass
class Fact:
    """A single stored fact under the v0.14 schema."""

    pattern: str
    predicate: str
    slots: dict[str, Any]
    polarity: int
    asserted_by: str
    verification_status: str
    confidence: float = 0.5  # Beta(1,1) prior at zero observed counts.
    valid_from: str | None = None
    valid_until: str | None = None
    source_turn_id: int | None = None
    source_text: str | None = None
    created_at: str = field(default_factory=_now_iso)
    id: int | None = None
    user_id: str = DEFAULT_USER_ID
    affirmed_count: int = 0
    contradicted_count: int = 0
    is_session_local: int = 0
    session_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def agent_or_subject(self) -> Any:
        for k in ("agent", "subject", "entity", "event_type"):
            if k in self.slots:
                return self.slots[k]
        return None


class FactStore:
    """Thin SQLite wrapper. One instance per database file."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        raw_conn = sqlite3.connect(self.db_path, check_same_thread=False)
        raw_conn.row_factory = sqlite3.Row
        self._db_lock = threading.RLock()
        self._conn = _LockedConnection(raw_conn, self._db_lock)
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._event_subscribers: list[Any] = []

    def register_event_subscriber(self, cb: Any) -> Any:
        """Add ``cb`` to the list of callables fired on every
        pipeline_events insert. Returns ``cb`` as the unregister
        token. Exceptions in subscribers are swallowed."""
        self._event_subscribers.append(cb)
        return cb

    def unregister_event_subscriber(self, token: Any) -> None:
        try:
            self._event_subscribers.remove(token)
        except ValueError:
            pass

    # ---- facts ------------------------------------------------------------

    def insert_fact(self, fact: Fact) -> int:
        _validate_fact(fact)
        valid_from = fact.valid_from or _now_iso()
        cur = self._conn.execute(
            """
            INSERT INTO facts (
                pattern, predicate, slots, polarity, confidence,
                affirmed_count, contradicted_count,
                is_session_local, session_ids,
                asserted_by, verification_status, valid_from, valid_until,
                source_turn_id, source_text, created_at, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact.pattern,
                fact.predicate,
                json.dumps(fact.slots, default=str),
                fact.polarity,
                fact.confidence,
                fact.affirmed_count,
                fact.contradicted_count,
                fact.is_session_local,
                json.dumps(list(fact.session_ids)),
                fact.asserted_by,
                fact.verification_status,
                valid_from,
                fact.valid_until,
                fact.source_turn_id,
                fact.source_text,
                fact.created_at,
                fact.user_id,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def get_fact(self, fact_id: int) -> Fact | None:
        row = self._conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        return _row_to_fact(row) if row else None

    def find_currently_valid(
        self,
        pattern: str,
        predicate: str | None = None,
        slot_match: dict[str, Any] | None = None,
        polarity: int | None = None,
        user_id: str = DEFAULT_USER_ID,
        current_session: str | None = None,
    ) -> list[Fact]:
        """Currently-valid facts matching pattern + optional predicate + slot subset.

        Phase 6 adds the session-locality filter:

          * ``current_session=None`` (the default) — cross-session-only:
            only facts with ``is_session_local=0`` are returned. This
            is the right behavior for callers that don't know what
            session they're in (operator endpoints, batch tools) AND
            for the cross-session lookup path.
          * ``current_session="<id>"`` — return cross-session facts
            PLUS session-local facts whose ``session_ids`` includes
            ``<id>``. The session-local filter uses a SQL EXISTS over
            ``json_each(session_ids)``; SQLite 3.45+ supports this
            directly and the JSON column is well-formed by construction
            (CHECK constraints on insert).

        The ORDER BY is ``is_session_local DESC, id`` so that when both
        a session-local and a cross-session row could match the lookup,
        the session-local appears first — encoding the Q3 tie-breaker:
        prefer the more specific contextual signal. The cheaper SQL-
        only ordering means consumers don't need to re-sort.
        """
        clauses = ["pattern = ?", "valid_until IS NULL", "user_id = ?"]
        params: list[Any] = [pattern, user_id]
        if current_session is None:
            clauses.append("is_session_local = 0")
        else:
            clauses.append(
                "(is_session_local = 0 OR (is_session_local = 1 AND EXISTS "
                "(SELECT 1 FROM json_each(session_ids) WHERE value = ?)))"
            )
            params.append(current_session)
        if predicate is not None:
            clauses.append("predicate = ?")
            params.append(predicate)
        if polarity is not None:
            clauses.append("polarity = ?")
            params.append(polarity)
        for k, v in (slot_match or {}).items():
            # Case-sensitive comparison on slot values. v0.13's
            # find_currently_valid used LOWER() on both sides as a
            # pre-oracle fuzziness hack; v0.14's entity_equivalence
            # oracle (Phase 4) is the right place for that work.
            # Keeping SQL exact means the oracle's case-disambiguation
            # signal (apple vs Apple, mercury vs Mercury) isn't lost
            # before the oracle runs.
            clauses.append(f"json_extract(slots, '$.{_safe_key(k)}') = ?")
            params.append(str(v))
        rows = self._conn.execute(
            f"SELECT * FROM facts WHERE {' AND '.join(clauses)} "
            f"ORDER BY is_session_local DESC, id",
            params,
        ).fetchall()
        return [_row_to_fact(r) for r in rows]

    def find_contradictions(
        self,
        pattern: str,
        predicate: str,
        slot_match: dict[str, Any],
        polarity: int,
        user_id: str = DEFAULT_USER_ID,
        current_session: str | None = None,
    ) -> list[Fact]:
        opposite = 0 if polarity == 1 else 1
        return self.find_currently_valid(
            pattern, predicate=predicate, slot_match=slot_match,
            polarity=opposite, user_id=user_id,
            current_session=current_session,
        )

    def boost_confidence(self, fact_id: int) -> float:
        """Reinforce a stored fact: increment ``affirmed_count`` and
        recompute confidence via ``confidence_from_counts``.

        Generic helper — touches ``affirmed_count`` only and leaves
        ``session_ids`` untouched. Used by operator-action endpoints
        (Phase 8) and any caller that has no session-ids work to do.
        The Tier U storage path uses the more specialized
        ``reaffirm_cross_session`` instead so the append-and-increment
        is one atomic UPDATE.
        """
        row = self._conn.execute(
            "SELECT affirmed_count, contradicted_count FROM facts WHERE id = ?",
            (fact_id,),
        ).fetchone()
        if not row:
            raise LookupError(f"fact {fact_id} does not exist")
        new_affirmed = int(row["affirmed_count"] or 0) + 1
        contradicted = int(row["contradicted_count"] or 0)
        from src.layer2_routing.constants import confidence_from_counts
        new_conf = confidence_from_counts(new_affirmed, contradicted)
        self._conn.execute(
            "UPDATE facts SET confidence = ?, affirmed_count = ? WHERE id = ?",
            (new_conf, new_affirmed, fact_id),
        )
        self._conn.commit()
        return new_conf

    def reaffirm_cross_session(
        self, fact_id: int, current_session: str,
    ) -> tuple[int, list[str], float]:
        """Atomic append-and-increment for a cross-session fact's
        ``session_ids`` and ``affirmed_count``.

        Behavior:

          * If ``current_session`` is already in ``session_ids``:
            no-op. Returns the row's existing values; the underlying
            UPDATE doesn't run, so confidence and counts are unchanged.
            Encodes principle 3's "same-session repetition is not new
            evidence."
          * Otherwise: append ``current_session`` to ``session_ids``,
            increment ``affirmed_count`` by 1, recompute confidence,
            and persist all three columns in one UPDATE statement.

        Returns ``(new_affirmed_count, new_session_ids, new_confidence)``
        either way.

        Raises ``ValueError`` if called on a session-local row — the
        storage path should NOOP same-session repetition for session-
        local facts (their session_ids is a single-element CHECK-
        constrained list, so this helper has nothing to do for them).
        Raises ``LookupError`` if the fact id doesn't exist.
        """
        row = self._conn.execute(
            "SELECT affirmed_count, contradicted_count, session_ids, "
            "is_session_local FROM facts WHERE id = ?",
            (fact_id,),
        ).fetchone()
        if not row:
            raise LookupError(f"fact {fact_id} does not exist")
        if int(row["is_session_local"] or 0) == 1:
            raise ValueError(
                f"reaffirm_cross_session called on session-local fact "
                f"{fact_id}; session-local same-session repetition should "
                f"NOOP at the storage path level"
            )
        existing_sessions = json.loads(row["session_ids"] or "[]")
        affirmed = int(row["affirmed_count"] or 0)
        contradicted = int(row["contradicted_count"] or 0)
        from src.layer2_routing.constants import confidence_from_counts

        if current_session in existing_sessions:
            current_conf = confidence_from_counts(affirmed, contradicted)
            return affirmed, list(existing_sessions), current_conf

        new_sessions = list(existing_sessions) + [current_session]
        new_affirmed = affirmed + 1
        new_conf = confidence_from_counts(new_affirmed, contradicted)
        self._conn.execute(
            "UPDATE facts SET session_ids = ?, affirmed_count = ?, "
            "confidence = ? WHERE id = ?",
            (json.dumps(new_sessions), new_affirmed, new_conf, fact_id),
        )
        self._conn.commit()
        return new_affirmed, new_sessions, new_conf

    def close_fact(
        self,
        fact_id: int,
        valid_until: str | None = None,
    ) -> None:
        """Close a fact (set valid_until). Confidence on closed facts
        is left as-is — confidence comes from counts, not from a
        status-driven floor."""
        valid_until = valid_until or _now_iso()
        self._conn.execute(
            "UPDATE facts SET valid_until = ? WHERE id = ?",
            (valid_until, fact_id),
        )
        self._conn.commit()

    def query_facts(
        self,
        pattern: str | None = None,
        predicate: str | None = None,
        asserted_by: str | None = None,
        verification_status: str | None = None,
        only_valid: bool = False,
        user_id: str | None = DEFAULT_USER_ID,
    ) -> list[Fact]:
        """Query facts, scoped to ``user_id`` by default."""
        clauses: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if pattern is not None:
            clauses.append("pattern = ?")
            params.append(pattern)
        if predicate is not None:
            clauses.append("predicate = ?")
            params.append(predicate)
        if asserted_by is not None:
            clauses.append("asserted_by = ?")
            params.append(asserted_by)
        if verification_status is not None:
            clauses.append("verification_status = ?")
            params.append(verification_status)
        if only_valid:
            clauses.append("valid_until IS NULL")

        q = "SELECT * FROM facts"
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY id DESC"
        rows = self._conn.execute(q, params).fetchall()
        return [_row_to_fact(r) for r in rows]

    def all_user_facts(self, user_id: str = DEFAULT_USER_ID) -> list[Fact]:
        rows = self._conn.execute(
            """
            SELECT * FROM facts
            WHERE asserted_by = 'user'
              AND valid_until IS NULL
              AND verification_status = 'user_asserted'
              AND user_id = ?
            ORDER BY id
            """,
            (user_id,),
        ).fetchall()
        return [_row_to_fact(r) for r in rows]

    # ---- turns ------------------------------------------------------------

    def insert_turn(
        self,
        role: str,
        content: str,
        original_content: str | None = None,
        user_id: str = DEFAULT_USER_ID,
    ) -> int:
        if role not in ("user", "assistant"):
            raise ValueError(f"role must be 'user' or 'assistant', got {role!r}")
        cur = self._conn.execute(
            "INSERT INTO turns (role, content, original_content, created_at, user_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (role, content, original_content, _now_iso(), user_id),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def update_turn_content(
        self, turn_id: int, content: str, original_content: str | None
    ) -> None:
        self._conn.execute(
            "UPDATE turns SET content = ?, original_content = ? WHERE id = ?",
            (content, original_content, turn_id),
        )
        self._conn.commit()

    def get_turn(self, turn_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
        return dict(row) if row else None

    def list_turns(self, user_id: str | None = DEFAULT_USER_ID) -> list[dict[str, Any]]:
        if user_id is None:
            rows = self._conn.execute("SELECT * FROM turns ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM turns WHERE user_id = ? ORDER BY id",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- pipeline events --------------------------------------------------

    def insert_pipeline_event(
        self, turn_id: int, stage: str, data: dict[str, Any] | list[Any]
    ) -> int:
        if stage not in PIPELINE_STAGES:
            raise ValueError(
                f"stage must be one of {sorted(PIPELINE_STAGES)}, got {stage!r}"
            )
        cur = self._conn.execute(
            "INSERT INTO pipeline_events (turn_id, stage, data, created_at) VALUES (?, ?, ?, ?)",
            (turn_id, stage, json.dumps(data, default=str), _now_iso()),
        )
        self._conn.commit()
        for sub in list(self._event_subscribers):
            try:
                sub(turn_id, stage, data)
            except Exception:
                pass
        return int(cur.lastrowid)

    def get_pipeline_events(self, turn_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM pipeline_events WHERE turn_id = ? ORDER BY id",
            (turn_id,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "turn_id": r["turn_id"],
                "stage": r["stage"],
                "data": json.loads(r["data"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ---- retrieval cache --------------------------------------------------

    def cache_retrieval(self, query: str, snippets: list[dict[str, Any]]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO retrieval_cache (query, snippets, fetched_at) "
            "VALUES (?, ?, ?)",
            (query, json.dumps(snippets), _now_iso()),
        )
        self._conn.commit()

    def get_cached_retrieval(
        self, query: str, ttl_seconds: int
    ) -> list[dict[str, Any]] | None:
        row = self._conn.execute(
            "SELECT snippets, fetched_at FROM retrieval_cache WHERE query = ?",
            (query,),
        ).fetchone()
        if not row:
            return None
        fetched = datetime.fromisoformat(row["fetched_at"])
        age = (datetime.now(timezone.utc) - fetched).total_seconds()
        if age > ttl_seconds:
            return None
        return json.loads(row["snippets"])

    # ---- lifecycle --------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def reset(self) -> None:
        self._conn.executescript(
            "DROP VIEW IF EXISTS facts_flat;"
            "DROP TABLE IF EXISTS facts;"
            "DROP TABLE IF EXISTS turns;"
            "DROP TABLE IF EXISTS pipeline_events;"
            "DROP TABLE IF EXISTS retrieval_cache;"
            "DROP TABLE IF EXISTS verification_cache;"
            "DROP TABLE IF EXISTS cache_invalidation_log;"
            "DROP TABLE IF EXISTS routing_memo;"
            "DROP TABLE IF EXISTS predicate_equivalence;"
            "DROP TABLE IF EXISTS entity_equivalence;"
            "DROP TABLE IF EXISTS entity_taxonomy;"
            "DROP TABLE IF EXISTS predicate_distribution;"
        )
        self._conn.executescript(SCHEMA)
        self._conn.commit()


def _safe_key(k: str) -> str:
    """JSON path keys can't contain ' or special chars. Whitelist alnum + underscore."""
    if not k.replace("_", "").isalnum():
        raise ValueError(f"slot key not safe for SQL json_extract: {k!r}")
    return k


def _validate_fact(fact: Fact) -> None:
    if not fact.pattern:
        raise ValueError("fact.pattern must be non-empty")
    if not fact.predicate:
        raise ValueError("fact.predicate must be non-empty")
    if not isinstance(fact.slots, dict):
        raise ValueError(f"fact.slots must be a dict, got {type(fact.slots).__name__}")
    if fact.polarity not in POLARITIES:
        raise ValueError(f"polarity must be 0 or 1, got {fact.polarity!r}")
    if fact.asserted_by not in ASSERTED_BY:
        raise ValueError(f"asserted_by {fact.asserted_by!r} not in {sorted(ASSERTED_BY)}")
    if fact.verification_status not in VERIFICATION_STATUSES:
        raise ValueError(
            f"verification_status {fact.verification_status!r} "
            f"not in {sorted(VERIFICATION_STATUSES)}"
        )
    if not (0.0 <= fact.confidence <= 1.0):
        raise ValueError(f"confidence must be in [0.0, 1.0], got {fact.confidence}")
    if fact.is_session_local not in (0, 1):
        raise ValueError(
            f"is_session_local must be 0 or 1, got {fact.is_session_local!r}"
        )
    if not isinstance(fact.session_ids, list):
        raise ValueError(
            f"session_ids must be a list, got {type(fact.session_ids).__name__}"
        )
    # The CHECK constraint catches the same condition at the SQL layer,
    # but raising here gives a Python-level error before the round trip.
    if fact.is_session_local == 1 and len(fact.session_ids) > 1:
        raise ValueError(
            "session_ids may have at most 1 element when is_session_local=1, "
            f"got {len(fact.session_ids)}"
        )


def _row_to_fact(row: sqlite3.Row) -> Fact:
    return Fact(
        id=row["id"],
        pattern=row["pattern"],
        predicate=row["predicate"],
        slots=json.loads(row["slots"]),
        polarity=row["polarity"],
        confidence=row["confidence"],
        asserted_by=row["asserted_by"],
        verification_status=row["verification_status"],
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        source_turn_id=row["source_turn_id"],
        source_text=row["source_text"],
        created_at=row["created_at"],
        user_id=row["user_id"],
        affirmed_count=int(row["affirmed_count"] or 0),
        contradicted_count=int(row["contradicted_count"] or 0),
        is_session_local=int(row["is_session_local"] or 0),
        session_ids=json.loads(row["session_ids"] or "[]"),
    )
