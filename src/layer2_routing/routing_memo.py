"""Routing memo — (pattern, predicate) → routing method cache.

The architectural commitment: after warm-up, routing classification
costs ~zero (no LLM call). v1's router fired the LLM router on every
model claim; that's wasteful — once we know that
``(quantitative, has_count)`` routes to ``python``, every subsequent
``(quantitative, has_count)`` claim should skip the LLM.

The memoization key is ``(pattern, predicate)``. The extractor is
held to the discipline that semantically distinct claim subtypes get
distinct predicate labels (see CLAUDE.md and the implementation plan's
"Resolved open questions" — has_letter_count vs has_population, not
has_count for both). Phase 1's extractor prompt bakes this in; the
memo table assumes it.

Three operations:

  * ``lookup(pattern, predicate)``: returns the row if present, else
    None. **Pure read** — does NOT update last_consulted_at. The
    orchestrator decides when to bump that timestamp (see
    ``touch_consulted``).
  * ``record(pattern, predicate, method, reason)``: UPSERT on the key.
    Method/reason/last_consulted_at are written; counts are
    PRESERVED across overwrites. Per principle 3, only operator
    action via the inspector endpoint (Phase 8) increments counts.
  * ``touch_consulted(pattern, predicate)``: bumps last_consulted_at.
    NOT a reinforcement signal — it's observability metadata for the
    operator (the trace UI shows "this row hasn't been consulted in
    30 days; consider whether the predicate is still in use"). Kept
    distinct from count updates so principle 3 stays auditable in
    code: anything that touches affirmed_count or contradicted_count
    lives in the operator-action endpoints, never here.

The memo never raises on a missing row; lookup returns None. Operations
on an unknown (pattern, predicate) for ``touch_consulted`` no-op
(no row exists to bump). ``record`` is the only path that creates rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.fact_store import FactStore


# The five routing methods the LLM router can choose from. Mirrored
# in the table's CHECK constraint and in llm_router.ROUTING_METHODS;
# kept here as a Python tuple so consumers can validate without
# round-tripping through the DB.
ROUTING_METHODS: tuple[str, ...] = (
    "python",
    "python_with_canonical_constants",
    "retrieval",
    "user_authoritative",
    "unverifiable",
)


@dataclass(frozen=True)
class RoutingMemoEntry:
    """A single row from ``routing_memo``.

    Snapshot at read time; not connected to the DB. Use
    ``RoutingMemo.lookup`` to fetch a fresh entry.
    """

    pattern: str
    predicate: str
    method: str
    reason: Optional[str]
    affirmed_count: int
    contradicted_count: int
    created_at: str
    last_consulted_at: Optional[str]

    def to_dict(self) -> dict:
        return {
            "pattern": self.pattern,
            "predicate": self.predicate,
            "method": self.method,
            "reason": self.reason,
            "affirmed_count": self.affirmed_count,
            "contradicted_count": self.contradicted_count,
            "created_at": self.created_at,
            "last_consulted_at": self.last_consulted_at,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RoutingMemo:
    """Thin wrapper over the ``routing_memo`` table.

    One instance per FactStore. Operations are synchronous and
    transactional — the wrapped FactStore's ``_conn`` is the same
    connection the rest of the v2 stack uses, so memo writes commit
    atomically with whatever event the orchestrator emits alongside
    them.
    """

    def __init__(self, store: FactStore):
        self._store = store

    def lookup(self, pattern: str, predicate: str) -> Optional[RoutingMemoEntry]:
        """Pure read — no side effects. Returns None on miss."""
        row = self._store._conn.execute(
            "SELECT pattern, predicate, method, reason, "
            "affirmed_count, contradicted_count, created_at, "
            "last_consulted_at "
            "FROM routing_memo WHERE pattern = ? AND predicate = ?",
            (pattern, predicate),
        ).fetchone()
        if row is None:
            return None
        return RoutingMemoEntry(
            pattern=row["pattern"],
            predicate=row["predicate"],
            method=row["method"],
            reason=row["reason"],
            affirmed_count=int(row["affirmed_count"] or 0),
            contradicted_count=int(row["contradicted_count"] or 0),
            created_at=row["created_at"],
            last_consulted_at=row["last_consulted_at"],
        )

    def record(
        self,
        pattern: str,
        predicate: str,
        method: str,
        reason: Optional[str],
    ) -> RoutingMemoEntry:
        """UPSERT a memo row. Counts are preserved across overwrites
        (only operator action via the Phase 8 inspector endpoints
        increments them). Method, reason, and last_consulted_at are
        updated on every call. Returns the post-write entry.

        Validation: ``method`` must be one of ``ROUTING_METHODS``.
        The DB's CHECK constraint enforces this too, but raising
        here gives a Python-level error before the round trip.
        """
        if method not in ROUTING_METHODS:
            raise ValueError(
                f"method {method!r} not in {ROUTING_METHODS}"
            )
        now = _now_iso()
        # ON CONFLICT preserves affirmed_count / contradicted_count by
        # not naming them in the UPDATE clause. created_at is also
        # preserved — the row was created when it was first written;
        # repeated record() calls are observations of the same
        # (pattern, predicate) routing, not a re-creation.
        self._store._conn.execute(
            """
            INSERT INTO routing_memo (
                pattern, predicate, method, reason,
                affirmed_count, contradicted_count,
                created_at, last_consulted_at
            ) VALUES (?, ?, ?, ?, 0, 0, ?, ?)
            ON CONFLICT (pattern, predicate) DO UPDATE SET
                method = excluded.method,
                reason = excluded.reason,
                last_consulted_at = excluded.last_consulted_at
            """,
            (pattern, predicate, method, reason, now, now),
        )
        self._store._conn.commit()
        entry = self.lookup(pattern, predicate)
        assert entry is not None  # we just wrote it
        return entry

    def touch_consulted(self, pattern: str, predicate: str) -> None:
        """Bump ``last_consulted_at`` to now. No-op if the row doesn't
        exist (calling this on a miss is a programming error; we
        prefer silent no-op over crashing because the orchestrator's
        flow is "lookup → on hit, touch_consulted" — the row exists
        by construction).

        IMPORTANT: this is observability metadata, NOT a reinforcement
        signal. ``affirmed_count`` and ``contradicted_count`` are
        deliberately NOT touched here — that would violate principle 3
        (reads are not writes). The trace UI uses last_consulted_at
        to surface stale-row hints; the Beta posterior over counts
        stays untouched.
        """
        self._store._conn.execute(
            "UPDATE routing_memo SET last_consulted_at = ? "
            "WHERE pattern = ? AND predicate = ?",
            (_now_iso(), pattern, predicate),
        )
        self._store._conn.commit()

    def list_all(self) -> list[RoutingMemoEntry]:
        """List every row, ordered by (pattern, predicate). Used by
        the inspector endpoint at /v2/api/routing-memo."""
        rows = self._store._conn.execute(
            "SELECT pattern, predicate, method, reason, "
            "affirmed_count, contradicted_count, created_at, "
            "last_consulted_at "
            "FROM routing_memo ORDER BY pattern, predicate"
        ).fetchall()
        return [
            RoutingMemoEntry(
                pattern=r["pattern"],
                predicate=r["predicate"],
                method=r["method"],
                reason=r["reason"],
                affirmed_count=int(r["affirmed_count"] or 0),
                contradicted_count=int(r["contradicted_count"] or 0),
                created_at=r["created_at"],
                last_consulted_at=r["last_consulted_at"],
            )
            for r in rows
        ]
