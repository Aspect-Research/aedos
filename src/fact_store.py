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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    # v0.4 additions — code-generated verification stages
    "code_triage",
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
    # v0.6 — end-of-turn cost aggregate. One per turn. Sum of all
    # LLM calls (extractor + router + code-writer + judge + corrector +
    # any classifiers) into total_usd / by_model breakdown.
    "turn_cost",
}

# Confidence adjustments
_CONFIDENCE_BOOST = 0.02
_CONFIDENCE_CAP = 0.99
_CONFIDENCE_FLOOR_ON_CLOSE = 0.4


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


DEFAULT_USER_ID = "default_user"

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,
    predicate TEXT NOT NULL,
    slots TEXT NOT NULL,                -- JSON object keyed by slot name
    polarity INTEGER NOT NULL,
    confidence REAL NOT NULL,
    asserted_by TEXT NOT NULL,
    verification_status TEXT NOT NULL,
    valid_from TEXT,
    valid_until TEXT,
    source_turn_id INTEGER,
    source_text TEXT,
    created_at TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT 'default_user'  -- v0.5.x: cross-session scoping
);

CREATE INDEX IF NOT EXISTS idx_facts_pattern ON facts(pattern);
CREATE INDEX IF NOT EXISTS idx_facts_predicate ON facts(predicate);
CREATE INDEX IF NOT EXISTS idx_facts_valid_until ON facts(valid_until);
-- idx_facts_user_id is created in _migrate_user_id (after the column
-- exists on legacy DBs that didn't ship with it).

CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    original_content TEXT,
    created_at TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT 'default_user'  -- v0.5.x: cross-session scoping
);
-- idx_turns_user_id is created in _migrate_user_id.

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
    cached_at TEXT NOT NULL,
    expires_at TEXT,                         -- NULL = immutable (never expires)
    hit_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_verification_cache_key
    ON verification_cache(canonical_key);
CREATE INDEX IF NOT EXISTS idx_verification_cache_expires
    ON verification_cache(expires_at);

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
    confidence: float
    asserted_by: str
    verification_status: str
    valid_from: str | None = None
    valid_until: str | None = None
    source_turn_id: int | None = None
    source_text: str | None = None
    created_at: str = field(default_factory=_now_iso)
    id: int | None = None
    user_id: str = DEFAULT_USER_ID  # v0.5.x: cross-session scoping

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
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate_user_id()
        self._conn.commit()

    def _migrate_user_id(self) -> None:
        """v0.5.x: add user_id column to pre-existing facts/turns tables.

        SQLite's CREATE TABLE IF NOT EXISTS doesn't backfill columns when
        the table already exists with the older shape, so older databases
        (v0.3 / v0.4 / pre-v0.5.x) need an explicit ALTER. The DEFAULT
        clause on the column means existing rows get 'default_user'
        automatically — solo dogfooding still works without further
        migration. Indexes are created here regardless (idempotent on
        new DBs that already have the column)."""
        for table in ("facts", "turns"):
            cols = {r["name"] for r in self._conn.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()}
            if "user_id" not in cols:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN user_id TEXT "
                    f"NOT NULL DEFAULT '{DEFAULT_USER_ID}'"
                )
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_user_id ON {table}(user_id)"
            )

    # ---- facts ------------------------------------------------------------

    def insert_fact(self, fact: Fact) -> int:
        _validate_fact(fact)
        valid_from = fact.valid_from or _now_iso()
        cur = self._conn.execute(
            """
            INSERT INTO facts (
                pattern, predicate, slots, polarity, confidence,
                asserted_by, verification_status, valid_from, valid_until,
                source_turn_id, source_text, created_at, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    ) -> list[Fact]:
        """Currently-valid facts matching pattern + optional predicate + slot subset.

        ``slot_match`` is ANDed against ``slots`` JSON via case-insensitive
        equality. The match values are stringified before comparison.

        ``user_id`` scopes the lookup. Defaults to ``DEFAULT_USER_ID``
        for solo dogfooding; multi-user deployments thread their own.
        """
        clauses = ["pattern = ?", "valid_until IS NULL", "user_id = ?"]
        params: list[Any] = [pattern, user_id]
        if predicate is not None:
            clauses.append("predicate = ?")
            params.append(predicate)
        if polarity is not None:
            clauses.append("polarity = ?")
            params.append(polarity)
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
    ) -> list[Fact]:
        opposite = 0 if polarity == 1 else 1
        return self.find_currently_valid(
            pattern, predicate=predicate, slot_match=slot_match,
            polarity=opposite, user_id=user_id,
        )

    def boost_confidence(self, fact_id: int, amount: float = _CONFIDENCE_BOOST) -> float:
        row = self._conn.execute(
            "SELECT confidence FROM facts WHERE id = ?", (fact_id,)
        ).fetchone()
        if not row:
            raise LookupError(f"fact {fact_id} does not exist")
        new = min(float(row["confidence"]) + amount, _CONFIDENCE_CAP)
        self._conn.execute("UPDATE facts SET confidence = ? WHERE id = ?", (new, fact_id))
        self._conn.commit()
        return new

    def close_fact(
        self,
        fact_id: int,
        valid_until: str | None = None,
        new_confidence: float | None = _CONFIDENCE_FLOOR_ON_CLOSE,
    ) -> None:
        valid_until = valid_until or _now_iso()
        if new_confidence is None:
            self._conn.execute(
                "UPDATE facts SET valid_until = ? WHERE id = ?",
                (valid_until, fact_id),
            )
        else:
            self._conn.execute(
                "UPDATE facts SET valid_until = ?, confidence = ? WHERE id = ?",
                (valid_until, new_confidence, fact_id),
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
        )
        self._conn.executescript(SCHEMA)
        # Run the same idempotent migration init() does so the user_id
        # indexes get re-created (SCHEMA only declares the columns;
        # the indexes are created in _migrate_user_id since they need
        # to wait for the column to exist on legacy DBs).
        self._migrate_user_id()
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
    )
