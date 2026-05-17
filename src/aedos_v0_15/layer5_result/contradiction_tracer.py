from __future__ import annotations

import json
from typing import Optional

from .retraction import RetractionPropagator, VerdictRetraction


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
        """
        source_rows = self._propagator._trace_index.get(contradicted_claim_id, [])
        all_retracted: list[VerdictRetraction] = []

        for table, row_id in source_rows:
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
