from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..audit.log import log_event
from .retraction import RetractionPropagator, VerdictRetraction

# Tables that carry a retracted_at column and may hold a verdict's premises.
# v0.16 WS3 §3E: `predicate_distribution` dropped — no walker edge ever stamps
# a predicate_distribution row id (aggregator._TRACE_ROW_ID_KEYS has no
# distribution key), so the entry was dead. The remaining four are exactly the
# tables a trace's source_rows can reference.
_RETRACTABLE_TABLES = {
    "tier_u",
    "predicate_translation",
    "subsumption",
    "entity_resolution_cache",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContradictionTracer:
    """v0.16 WS3 §3E: on an external premise correction/retraction, retract the
    contributing row(s) and mark dependent *_given_assertion verdicts STALE for
    lazy re-derivation. Replaces the dormant eager-cascade tracer — the per-row
    retracted_at UPDATE stays (the actual retraction), but the cascade is now
    the propagator's provenance-driven stale-marking, whose return this method
    consumes."""

    def __init__(
        self,
        db=None,
        retraction_propagator: Optional[RetractionPropagator] = None,
    ) -> None:
        self._db = db
        if retraction_propagator is not None:
            self._propagator = retraction_propagator
        else:
            # A self-constructed propagator must replay persisted
            # verdict_recorded events, else trace_contradiction is blind to
            # verdicts recorded by earlier processes. A propagator passed in is
            # the caller's responsibility (build_pipeline replays the one it
            # wires).
            self._propagator = RetractionPropagator(db=db)
            self._propagator.replay()

    def trace_contradiction(
        self,
        contradicted_claim_id: str,
        contradicting_premise: dict,
    ) -> list[VerdictRetraction]:
        """Given an external correction, retract the verdict's source rows and
        mark dependent *_given_assertion verdicts STALE.

        contradicting_premise: dict with at least {"source": "tier_u" | "kb" | "python", ...}
        Returns the propagator's stale-aware VerdictRetraction list for all
        affected verdicts (the return is now load-bearing — the caller surfaces
        which verdicts went stale).

        For each contributing row this issues the `retracted_at` UPDATE on the
        row itself (architecture 7.3) and then propagates the retraction, which
        marks dependent *_given_assertion verdicts stale for lazy re-derivation.
        """
        source_rows = self._propagator._trace_index.get(contradicted_claim_id, [])
        all_retracted: list[VerdictRetraction] = []
        now = _now()

        for table, row_id in source_rows:
            # Issue the actual retraction on the contributing substrate/Tier U
            # row. The table name comes from the propagator's trace index, which
            # is populated only with the fixed set of substrate tables.
            if self._db is not None and table in _RETRACTABLE_TABLES:
                # v0.16.3 Batch B (piece 2): a PINNED predicate_translation row is
                # operator-authoritative and is never retracted, even by a
                # contradiction trace. Skip the UPDATE (still propagate so dependent
                # verdicts are re-derived against the surviving pinned row) and log.
                if table == "predicate_translation" and self._is_pinned(row_id):
                    log_event(
                        self._db,
                        event_type="pin_retraction_blocked",
                        event_subject=f"predicate_translation:{row_id}",
                        event_data={
                            "reason": f"contradiction_trace:{contradicted_claim_id}",
                            "source": "contradiction_tracer",
                        },
                    )
                else:
                    self._db.execute(
                        f"UPDATE {table} SET retracted_at=?, retraction_reason=? WHERE id=?",
                        (now, f"contradiction_trace:{contradicted_claim_id}", row_id),
                    )
                    self._db.commit()

            # Consume the stale-aware propagation result (§3E).
            all_retracted.extend(self._propagator.propagate_retraction(table, row_id))

            if self._db is not None:
                log_event(
                    self._db,
                    event_type="contradiction_traced",
                    event_subject=contradicted_claim_id,
                    event_data={
                        "contradicting_premise": contradicting_premise,
                        "retracted_table": table,
                        "retracted_row_id": row_id,
                    },
                )

        return all_retracted

    def _is_pinned(self, row_id) -> bool:
        """True if predicate_translation row `row_id` is pinned. Defensive against
        a pre-migration DB lacking the column (returns False → unchanged behavior)."""
        if self._db is None:
            return False
        try:
            row = self._db.execute(
                "SELECT pinned FROM predicate_translation WHERE id=?", (row_id,)
            ).fetchone()
        except Exception:
            return False
        return bool(row[0]) if row is not None else False
