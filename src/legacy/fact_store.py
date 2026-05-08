"""SQLite-backed fact store (v0.3 — pattern + slots schema).

A fact is stored as ``(pattern, predicate, slots_json)`` where ``slots`` is
a JSON object whose keys come from the pattern's slot schema. The
flexible slots column replaces the rigid (subject, predicate, object,
object_type) layout from v0.1/v0.2.

The ``facts_flat`` view exposes a denormalized subject/object projection
for the UI. It uses pattern-aware coalescing of the canonical "subject"
and "object" slots — see SCHEMA below.
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

    Python's sqlite3 module accepts ``check_same_thread=False`` so a
    single connection can be used from multiple threads, but the
    connection's transaction state is shared. Concurrent ``execute``
    calls on different threads can race on the implicit BEGIN /
    COMMIT, surfacing as ``cannot start a transaction within a
    transaction``.

    v0.9.0 introduces per-claim parallelism in
    ``Pipeline._stage_verify``; with multiple worker threads writing
    pipeline_events + facts concurrently, that race is hit immediately.
    This wrapper serializes every operation on a single RLock so the
    underlying connection sees one statement at a time. SQLite's own
    locking already serializes writes; the lock just keeps the
    Python-side transaction state consistent.

    The wrapper is transparent: every attribute not handled here
    delegates to the wrapped connection (so ``conn.row_factory`` etc.
    still work). RLock allows nested access on the same thread without
    deadlock — useful when init holds the lock and the SCHEMA
    executescript runs nested.
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
    # v0.3 split: see ARCHITECTURE.md "Verification status semantics"
    "retrieval_inconclusive",  # verifier ran, judge said insufficient evidence
    "retrieval_failed",        # verifier didn't get useful signal at all
    "unverifiable_pending_implementation",  # python verifier inconclusive / lookup miss
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
    # v0.3 additions
    "retrieval_query_attempt",  # one event per query attempt; trace shows the strategy
    "verifier_failure",         # the verifier didn't produce useful signal
    # v0.4 additions — code-generated verification stages.
    # (``code_triage`` was removed in v0.5: the LLM router decides
    # python-verifiability before code generation runs, so a separate
    # triage stage is dead. Kept out of this enum to fail-loud if
    # anything tries to write it.)
    "code_prompt_built",
    "code_prompt_leakage_detected",
    "code_generated",
    "code_executed",
    "code_unusual_behavior",
    "code_comparison",
    # v0.5 additions — LLM-based routing + canonical-constants cross-check
    "routing_decision",
    "canonical_constants_cross_check",
    "canonical_constants_disagreement",
    # v0.5.x — chat model under test (provider-pluggable via AEDOS_CHAT_MODEL_PROVIDER).
    # One event per assistant draft generation. Captures provider/model, prompt
    # shape, response, latency, and any error so the trace UI can show what the
    # chat model did even when the rest of the pipeline is unchanged.
    "chat_model_call",
    # v0.6 — Tier 2 verification cache. The scoping classifier decides
    # whether each claim is user_specific / session_specific / world_fact;
    # only world_fact claims are eligible for caching. Stability classifies
    # eligible claims into TTL buckets. Lookup hits/misses log here too.
    "cache_scoping_decision",       # per claim: scope + reason + confidence
    "cache_stability_decision",     # per claim: stability_class + TTL + reason
    "cache_lookup",                 # per claim: hit/miss + cached_key + age
    "cache_write",                  # per claim: insert/update + canonical_key
    # v0.7.8 — fired when a cache write replaces a prior verdict with a
    # different one (e.g. retrieval first said SUPPORTED, later said
    # CONTRADICTED). Surfaces source drift / verification flakiness
    # without burying it inside the regular cache_write event.
    "cache_contradiction_replaced",
    # v0.7.8 — fired once at app startup with the prune_expired result
    # (number of dead rows reclaimed).
    "cache_pruned",
    # v0.7.12 — end-of-turn: how much $ the cache saved this turn
    # (rough estimate: each hit avoids one judge LLM call).
    "cache_savings",
    # v0.7.14 — fired on every tier-1/tier-2 short-circuit hit so the
    # operator can see which precedence layer served each claim and
    # which underlying fact the lookup matched.
    "tier_lookup",
    # v0.7.9 — comparative / superlative claim detection in the
    # retrieval verifier. The detector decomposes a comparative claim
    # into {subject, superlative, measure, domain} and the verifier
    # prepends comparative-aware Wikipedia query templates ahead of
    # the standard pattern queries. The retry-on-inconclusive event
    # fires when the first viable judge pass lands INSUFFICIENT and
    # the verifier steps to the next viable attempt.
    "comparative_detected",
    "judge_retry_after_inconclusive",
    # v0.12.x (Phase 2b) — LLM-driven query reformulation. Fires once
    # per claim AFTER the pattern's static query strategies have all
    # come back INSUFFICIENT_EVIDENCE. ``reformulation_emitted`` carries
    # the new query + the queries already tried + the judge's
    # justification (so the trace shows what gap was being targeted).
    # ``reformulation_failed`` fires if the rewriter call itself
    # crashed.
    "reformulation_emitted",
    "reformulation_failed",
    # v0.6 — end-of-turn cost aggregate. One per turn. Sum of all
    # LLM calls (extractor + router + code-writer + judge + corrector +
    # any classifiers) into total_usd / by_model breakdown.
    "turn_cost",
    # Defense-in-depth: extractor flagged a fact whose source_text
    # isn't a substring of the input — strong signal that the
    # extractor rewrote the model's claim with its own world
    # knowledge. The fact still goes through, but the trace UI
    # surfaces the warning prominently so the operator can spot
    # corruption of the verification pipeline.
    "extractor_substitution_warning",
}

# Confidence is computed from observed counts (reinforcement_count
# on facts, refresh_count + contradiction_count on cache entries) via
# src.router.constants.confidence_from_counts. The denormalized
# `confidence` column on facts mirrors the count-derived value; on
# cache entries confidence is a @property computed on read.


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


DEFAULT_USER_ID = "default_user"
# v0.7.14: per-conversation context identifier. Solo dogfooding stays
# on a single rolling session; multi-session deployments thread their
# own. Microtheory entries are session_id-scoped; cross-session
# user-asserted facts use NULL session_id.
DEFAULT_SESSION_ID = "default_session"

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,
    predicate TEXT NOT NULL,
    slots TEXT NOT NULL,                -- JSON object keyed by slot name
    polarity INTEGER NOT NULL,
    -- Confidence is computed from observed counts via
    -- src.router.constants.confidence_from_counts. Beta(1,1) Laplace-
    -- smoothed posterior of P(true | evidence). Denormalized here so
    -- the fact-store query path doesn't have to recompute on every read.
    confidence REAL NOT NULL,
    -- Number of times this fact has been re-confirmed by a verifier
    -- (boost_confidence calls). The contradiction count for facts is
    -- always 0 — a contradicted fact gets closed, not updated in place.
    reinforcement_count INTEGER NOT NULL DEFAULT 0,
    -- Scopes the fact to a specific conversation session.
    -- NULL = cross-session (default; the existing user-facts behavior).
    -- Non-NULL = microtheory entry that ONLY applies within the
    -- named session. Tiered verification consults microtheory rows
    -- BEFORE cross-session ones so this conversation's overrides win.
    session_id TEXT,
    asserted_by TEXT NOT NULL,
    verification_status TEXT NOT NULL,
    valid_from TEXT,
    valid_until TEXT,
    source_turn_id INTEGER,
    source_text TEXT,
    created_at TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT 'default_user'
);

CREATE INDEX IF NOT EXISTS idx_facts_pattern ON facts(pattern);
CREATE INDEX IF NOT EXISTS idx_facts_predicate ON facts(predicate);
CREATE INDEX IF NOT EXISTS idx_facts_valid_until ON facts(valid_until);
CREATE INDEX IF NOT EXISTS idx_facts_user_id ON facts(user_id);
CREATE INDEX IF NOT EXISTS idx_facts_session_id
    ON facts(session_id) WHERE session_id IS NOT NULL;

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

-- Tier 2 verification cache (v0.6 — see ARCHITECTURE.md when wired up).
-- Caches per-claim VERDICTS (not raw retrieval snippets — that's the
-- table above). Keyed by a canonicalized claim shape so equivalent
-- claims hit. Every cached verdict is provisional; cached_at + TTL
-- via stability_class (immutable / decade_stable / years_stable /
-- months_stable / days_stable / volatile). The cache is a
-- performance optimization for retrieval, not a knowledge base.
CREATE TABLE IF NOT EXISTS verification_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key TEXT NOT NULL,             -- subject + predicate + normalized object
    pattern TEXT NOT NULL,
    predicate TEXT NOT NULL,
    verdict TEXT NOT NULL,                   -- verified / contradicted / inconclusive
    evidence TEXT,                           -- JSON: snippets + judge justification
    stability_class TEXT NOT NULL,           -- immutable | decade_stable | ... | volatile
    cached_at TEXT NOT NULL,                 -- first time this entry was written (preserved across UPSERT)
    expires_at TEXT,                         -- NULL = immutable (never expires)
    hit_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    -- Provenance + bookkeeping.
    evidence_hash TEXT,                      -- SHA-256 of evidence JSON; stable identity for "same answer"
    source_urls TEXT,                        -- JSON array of source URLs the judge consulted
    last_refreshed_at TEXT,                  -- bumped on every refresh; cached_at stays as first-cache
    -- The (refresh_count, contradiction_count) pair drives confidence
    -- via confidence_from_counts(R, C); refreshed +1 when the same
    -- verdict re-fires, contradiction_count +1 when verdict flips.
    refresh_count INTEGER NOT NULL DEFAULT 0,
    contradiction_count INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_verification_cache_key
    ON verification_cache(canonical_key);
CREATE INDEX IF NOT EXISTS idx_verification_cache_expires
    ON verification_cache(expires_at);

-- v0.7.11: every cache invalidation (manual, contradiction-cascade,
-- drift) gets a row here so the operator can audit "why did N entries
-- vanish from the cache?". Bounded — pruned alongside expired cache
-- rows in prune_expired().
CREATE TABLE IF NOT EXISTS cache_invalidation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reason TEXT NOT NULL,                    -- 'manual_by_slot' | 'contradiction_cascade' | 'drift' | 'admin_one'
    primary_key TEXT NOT NULL,               -- the canonical_key that triggered the invalidation
    propagated_to_keys TEXT,                 -- JSON array of cascaded canonical_keys
    detail TEXT,                             -- JSON: free-form context (slot/value, prior verdict, etc.)
    triggered_by TEXT NOT NULL,              -- 'user' | 'auto'
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_invalidation_created_at
    ON cache_invalidation_log(created_at);

-- A flat projection of facts for the UI / quick inspection. The pattern
-- determines which slot fills "subject" and "object" semantically.
CREATE VIEW IF NOT EXISTS facts_flat AS
SELECT
    id,
    pattern,
    predicate,
    COALESCE(
        json_extract(slots, '$.agent'),
        json_extract(slots, '$.subject'),
        json_extract(slots, '$.entity'),
        json_extract(slots, '$.event_type')
    ) AS subject,
    COALESCE(
        json_extract(slots, '$.role'),
        json_extract(slots, '$.object'),
        json_extract(slots, '$.category'),
        json_extract(slots, '$.location'),
        json_extract(slots, '$.proposition'),
        json_extract(slots, '$.value'),
        json_extract(slots, '$.relation')
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
    """A single stored fact under the v0.3 pattern/slots schema."""

    pattern: str
    predicate: str
    slots: dict[str, Any]
    polarity: int
    asserted_by: str
    verification_status: str
    # v0.13: Beta(1,1) prior at zero observed counts. Subsequent
    # reinforcements / contradictions update via confidence_from_counts.
    confidence: float = 0.5
    valid_from: str | None = None
    valid_until: str | None = None
    source_turn_id: int | None = None
    source_text: str | None = None
    created_at: str = field(default_factory=_now_iso)
    id: int | None = None
    user_id: str = DEFAULT_USER_ID  # cross-session scoping
    # Number of times this fact has been re-confirmed by a verifier
    # (boost_confidence calls). Confidence is recomputed from this
    # count via confidence_from_counts(R, 0).
    reinforcement_count: int = 0
    # NULL = cross-session (the existing user-fact behavior).
    # Non-NULL = microtheory entry that ONLY applies within the named
    # session. Tier 1 lookup in _stage_verify checks session_id
    # matches before falling through to cross-session and cache.
    session_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # Convenience accessors mirroring the pattern catalog's canonical slots.
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
        # v0.9.0: wrap the connection so every execute/commit goes
        # through a shared RLock. Required for the per-claim parallel
        # verify in Pipeline._stage_verify — without this, two worker
        # threads racing on insert_pipeline_event hit
        # "cannot start a transaction within a transaction".
        self._db_lock = threading.RLock()
        self._conn = _LockedConnection(raw_conn, self._db_lock)
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        # v0.6: per-event subscribers (set by /api/chat/stream so the
        # SSE handler can push pipeline_events to the client as they
        # land). Single-process, single-user dev tool — for multi-user
        # concurrency this needs to become per-request thread-local.
        self._event_subscribers: list[Any] = []

    def register_event_subscriber(self, cb: Any) -> Any:
        """Add ``cb`` to the list of callables fired on every
        pipeline_events insert. Returns ``cb`` as the unregister
        token. ``cb(turn_id, stage, data)`` — exceptions are swallowed
        so a buggy subscriber can't crash a turn."""
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
                asserted_by, verification_status, valid_from, valid_until,
                source_turn_id, source_text, created_at, user_id, session_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact.pattern,
                fact.predicate,
                json.dumps(fact.slots, default=str),
                fact.polarity,
                fact.confidence,
                fact.asserted_by,
                fact.verification_status,
                valid_from,
                fact.valid_until,
                fact.source_turn_id,
                fact.source_text,
                fact.created_at,
                fact.user_id,
                fact.session_id,
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
        session_id: Any = "_any_session",
        asserted_by: str | None = None,
    ) -> list[Fact]:
        """Currently-valid facts matching pattern + optional predicate + slot subset.

        ``slot_match`` is ANDed against ``slots`` JSON via case-insensitive
        equality. The match values are stringified before comparison.

        ``user_id`` scopes the lookup. Defaults to ``DEFAULT_USER_ID``
        for solo dogfooding; multi-user deployments thread their own.

        ``session_id`` (v0.7.14):
          * ``"_any_session"`` (sentinel default) — return facts at
            ANY session scope (cross-session AND any session-scoped
            row that matches). Preserves pre-v0.7.14 semantics for
            existing callers.
          * ``None`` — return only cross-session rows (session_id IS NULL).
            Tier 2 of the verification short-circuit (user store).
          * specific string — return only rows scoped to that session.
            Tier 1 of the verification short-circuit (microtheory).

        ``asserted_by`` (v0.14 Phase 8.6):
          * ``None`` (default) — no filter; return facts regardless of
            their author. Backwards-compatible with all pre-Phase-8.6
            callers.
          * specific value (e.g. ``"user"``) — return only facts whose
            ``asserted_by`` equals the given value. Used by
            ``store_lookup_verify`` to enforce the architectural
            commitment that Tier 2 (user store) lookups match only
            user-asserted facts. Without this, a python_verifier-asserted
            fact (a corrected world claim) silently matches as if it
            were user-asserted, polluting the trace's "served from
            user store" verdict with rows the user never claimed.
        """
        clauses = ["pattern = ?", "valid_until IS NULL", "user_id = ?"]
        params: list[Any] = [pattern, user_id]
        if predicate is not None:
            clauses.append("predicate = ?")
            params.append(predicate)
        if polarity is not None:
            clauses.append("polarity = ?")
            params.append(polarity)
        if session_id is None:
            clauses.append("session_id IS NULL")
        elif session_id != "_any_session":
            clauses.append("session_id = ?")
            params.append(session_id)
        if asserted_by is not None:
            clauses.append("asserted_by = ?")
            params.append(asserted_by)
        for k, v in (slot_match or {}).items():
            clauses.append(f"LOWER(json_extract(slots, '$.{_safe_key(k)}')) = LOWER(?)")
            params.append(str(v))
        rows = self._conn.execute(
            f"SELECT * FROM facts WHERE {' AND '.join(clauses)} ORDER BY id",
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
        asserted_by: str | None = None,
    ) -> list[Fact]:
        opposite = 0 if polarity == 1 else 1
        return self.find_currently_valid(
            pattern, predicate=predicate, slot_match=slot_match,
            polarity=opposite, user_id=user_id,
            asserted_by=asserted_by,
        )

    def boost_confidence(self, fact_id: int) -> float:
        """Reinforce a stored fact: increment ``reinforcement_count``
        and recompute confidence from the new count.

        v0.13: confidence is purely ``confidence_from_counts(R, 0)``
        where R is the post-increment reinforcement_count. No path-
        prior input, no LLM-emitted base. The flat +0.02-step and
        the saturating-curve-with-base modes are gone — the only
        thing that drives confidence is the count history."""
        row = self._conn.execute(
            "SELECT reinforcement_count FROM facts WHERE id = ?",
            (fact_id,),
        ).fetchone()
        if not row:
            raise LookupError(f"fact {fact_id} does not exist")
        new_count = int(row["reinforcement_count"] or 0) + 1
        from src.legacy.router.constants import confidence_from_counts
        new_conf = confidence_from_counts(new_count, 0)
        self._conn.execute(
            "UPDATE facts SET confidence = ?, reinforcement_count = ? WHERE id = ?",
            (new_conf, new_count, fact_id),
        )
        self._conn.commit()
        return new_conf

    def close_fact(
        self,
        fact_id: int,
        valid_until: str | None = None,
    ) -> None:
        """Close a fact (set valid_until). v0.13: confidence on
        closed facts is left as-is — confidence comes from counts,
        not from a status-driven floor. Closing a fact removes it
        from active queries; its historical confidence at close time
        is informational."""
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
        """Query facts, scoped to ``user_id`` by default. Pass ``user_id=None``
        to disable the filter (admin / inspector views only)."""
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
        """List turns, scoped to ``user_id`` by default. Pass ``user_id=None``
        for the inspector view (all users)."""
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
        # Fan out to subscribers AFTER commit so consumers see the
        # data is durably stored. Iterate over a copy so a subscriber
        # registering/unregistering during the call doesn't mutate
        # mid-iteration.
        for sub in list(self._event_subscribers):
            try:
                sub(turn_id, stage, data)
            except Exception:
                pass  # subscribers are observers — never break the turn
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
        # Drop EVERY table the app owns so "Reset DB" really wipes
        # the slate. Forgetting verification_cache here meant world
        # memory survived a reset while user/conversation memory
        # didn't — confusing and load-bearing for tests of the
        # tiered verifier.
        self._conn.executescript(
            "DROP VIEW IF EXISTS facts_flat;"
            "DROP TABLE IF EXISTS facts;"
            "DROP TABLE IF EXISTS turns;"
            "DROP TABLE IF EXISTS pipeline_events;"
            "DROP TABLE IF EXISTS retrieval_cache;"
            "DROP TABLE IF EXISTS verification_cache;"
            "DROP TABLE IF EXISTS cache_invalidation_log;"
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
        reinforcement_count=int(row["reinforcement_count"] or 0),
        session_id=row["session_id"],
    )
