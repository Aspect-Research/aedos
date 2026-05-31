from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..audit.log import log_event

_NOW = lambda: datetime.now(timezone.utc).isoformat()


class SubstrateExceptionCache:
    """v0.16 WS3 §3D: bounded nogood cache (P2303-style exception_to_constraint).

    Stores ONLY negative facts: a `(exception_kind, relation_type, property_path,
    source, target)` tuple confirmed NOT to hold. Positive subsumption stays a
    re-verifiable hypothesis — never cached here (asymmetric trust). A stale
    nogood can only cause a *false abstain* (we decline a path that might now
    hold), never a false-verify, so the cache is strictly on the safe side of
    the §3.2 never-false-verify invariant.

    NOGOODS are DISCOVERED — recorded EAGERLY on a negative ASK
    (`reason='ask_false'`). No seed/leak-guard rows are hand-authored; the leak
    stays closed because the discovered `ask_false` nogood short-circuits future
    consults of the same (source, target, path) without re-hitting SPARQL.

    Capacity-bounded by plain LRU (oldest `last_consulted_at` evicted first).
    Eviction is safe: a miss simply re-runs the ASK, which re-derives and
    re-caches the nogood.
    """

    def __init__(self, db, max_rows: int = 5000) -> None:
        self._db = db
        self._max_rows = max_rows

    # ------------------------------------------------------------------
    # Read paths — multiple call conventions, all reduce to one query.
    # ------------------------------------------------------------------
    def is_nogood(
        self,
        relation_type: str,
        source_identifier: str,
        target_identifier: str,
        *,
        exception_kind: str = "transitive_path",
        property_path: Optional[str] = None,
    ) -> bool:
        """True iff a non-retracted nogood matches (eager LRU touch on hit).

        The walker (`_nogood_vetoes`) and the adapter (`verify_transitive_path`)
        both consult this. `property_path` is optional: when None the match is
        on (kind, relation_type, source, target) regardless of the recorded
        path — a nogood is direction/path-specific by relation, so a widened
        alternation cannot resurrect a closed leak for that subtree."""
        if property_path is None:
            row = self._db.execute(
                """SELECT id FROM substrate_exceptions
                   WHERE exception_kind=? AND relation_type=?
                   AND source_identifier=? AND target_identifier=?
                   AND retracted_at IS NULL ORDER BY id LIMIT 1""",
                (exception_kind, relation_type, source_identifier, target_identifier),
            ).fetchone()
        else:
            row = self._db.execute(
                """SELECT id FROM substrate_exceptions
                   WHERE exception_kind=? AND relation_type=? AND property_path=?
                   AND source_identifier=? AND target_identifier=?
                   AND retracted_at IS NULL ORDER BY id LIMIT 1""",
                (exception_kind, relation_type, property_path,
                 source_identifier, target_identifier),
            ).fetchone()
        if row is None:
            return False
        self._db.execute(
            "UPDATE substrate_exceptions SET used_count=used_count+1, "
            "last_consulted_at=? WHERE id=?",
            (_NOW(), row["id"]),
        )
        self._db.commit()
        return True

    def vetoes(self, predicate: str, property_path: str, subject_qid: str) -> bool:
        """v0.16 WS1 binding-loop gate. The KBVerifier consults this to skip a
        binding whose `(predicate, property, subject)` is a cached nogood. Maps
        onto the same row as a `subsumption`-kind exception keyed on the binding
        property as the relation and the subject as the source (target is the
        binding property — an opaque key for the per-binding veto). Path-
        agnostic match (any target) when the subject alone is enough."""
        row = self._db.execute(
            """SELECT id FROM substrate_exceptions
               WHERE exception_kind='subsumption' AND relation_type=?
               AND property_path=? AND source_identifier=?
               AND retracted_at IS NULL ORDER BY id LIMIT 1""",
            (predicate, property_path, subject_qid),
        ).fetchone()
        if row is None:
            return False
        self._db.execute(
            "UPDATE substrate_exceptions SET used_count=used_count+1, "
            "last_consulted_at=? WHERE id=?",
            (_NOW(), row["id"]),
        )
        self._db.commit()
        return True

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------
    def record_nogood(
        self,
        relation_type: str,
        source_identifier: str,
        target_identifier: str,
        *,
        exception_kind: str = "transitive_path",
        property_path: str = "",
        reason: str = "ask_false",
    ) -> int:
        """EAGERLY record a discovered nogood. Idempotent via the table's
        UNIQUE constraint (INSERT OR IGNORE). Returns the row id (existing row's
        id on a collision). Triggers LRU eviction if over capacity."""
        now = _NOW()
        self._db.execute(
            """INSERT OR IGNORE INTO substrate_exceptions
               (exception_kind, relation_type, property_path, source_identifier,
                target_identifier, reason, created_at, last_consulted_at, used_count)
               VALUES (?,?,?,?,?,?,?,?,0)""",
            (exception_kind, relation_type, property_path, source_identifier,
             target_identifier, reason, now, now),
        )
        self._db.commit()
        row = self._db.execute(
            """SELECT id FROM substrate_exceptions
               WHERE exception_kind=? AND relation_type=? AND property_path=?
               AND source_identifier=? AND target_identifier=?""",
            (exception_kind, relation_type, property_path,
             source_identifier, target_identifier),
        ).fetchone()
        row_id = row["id"] if row is not None else self._db.execute(
            "SELECT last_insert_rowid()").fetchone()[0]
        self._evict_if_over_cap()
        log_event(
            self._db,
            event_type="substrate_exception_recorded",
            event_subject=f"{exception_kind}:{relation_type}:{source_identifier}->{target_identifier}",
            event_data={"property_path": property_path, "reason": reason},
        )
        return row_id

    def retract(self, row_id: int, reason: str) -> None:
        """Soft-delete a nogood (e.g. an operator confirms the path now holds)."""
        self._db.execute(
            "UPDATE substrate_exceptions SET retracted_at=?, retraction_reason=? WHERE id=?",
            (_NOW(), reason, row_id),
        )
        self._db.commit()

    def _evict_if_over_cap(self) -> None:
        """Plain LRU eviction: hard-delete the surplus oldest-consulted rows.
        Re-derivation on a later miss re-runs the ASK, which is safe."""
        count = self._db.execute(
            "SELECT COUNT(*) FROM substrate_exceptions WHERE retracted_at IS NULL"
        ).fetchone()[0]
        if count <= self._max_rows:
            return
        surplus = count - self._max_rows
        self._db.execute(
            """DELETE FROM substrate_exceptions WHERE id IN (
                 SELECT id FROM substrate_exceptions WHERE retracted_at IS NULL
                 ORDER BY COALESCE(last_consulted_at, created_at) ASC LIMIT ?)""",
            (surplus,),
        )
        self._db.commit()
