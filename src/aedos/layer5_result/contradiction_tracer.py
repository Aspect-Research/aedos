from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from .retraction import RetractionPropagator, VerdictRetraction

# Tables that carry a retracted_at column and may hold a verdict's premises.
_RETRACTABLE_TABLES = {
    "tier_u",
    "predicate_translation",
    "subsumption",
    "predicate_distribution",
    "entity_resolution_cache",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContradictionTracer:
    """Walk a verdict's justification trace and retract contributing rows."""

    def __init__(
        self,
        db=None,
        audit_log=None,
        retraction_propagator: Optional[RetractionPropagator] = None,
    ) -> None:
        self._db = db
        self._audit = audit_log
        self._propagator = retraction_propagator or RetractionPropagator(db=db, audit_log=audit_log)

    def trace_contradiction(
        self,
        contradicted_claim_id: str,
        contradicting_premise: dict,
    ) -> list[VerdictRetraction]:
        """Given an external correction, retract the verdict's source rows.

        contradicting_premise: dict with at least {"source": "tier_u" | "kb" | "python", ...}
        Returns list of VerdictRetraction for all affected verdicts.

        For each contributing row this issues the `retracted_at` UPDATE on the
        row itself (architecture 7.3) and then propagates the retraction to
        every verdict whose trace included that row.
        """
        source_rows = self._propagator._trace_index.get(contradicted_claim_id, [])
        all_retracted: list[VerdictRetraction] = []
        now = _now()

        for table, row_id in source_rows:
            # Issue the actual retraction on the contributing substrate/Tier U
            # row. The table name comes from the propagator's trace index, which
            # is populated only with the fixed set of substrate tables.
            if self._db is not None and table in _RETRACTABLE_TABLES:
                self._db.execute(
                    f"UPDATE {table} SET retracted_at=?, retraction_reason=? WHERE id=?",
                    (now, f"contradiction_trace:{contradicted_claim_id}", row_id),
                )
                self._db.commit()

            retracted = self._propagator.propagate_retraction(table, row_id)
            all_retracted.extend(retracted)

            if self._audit:
                self._audit.log(
                    event_type="contradiction_traced",
                    event_subject=contradicted_claim_id,
                    event_data=json.dumps({
                        "contradicting_premise": contradicting_premise,
                        "retracted_table": table,
                        "retracted_row_id": row_id,
                    }),
                )

        return all_retracted
