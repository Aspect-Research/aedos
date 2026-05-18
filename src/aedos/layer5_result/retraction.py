from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..audit.log import log_event


@dataclass
class VerdictRetraction:
    claim_id: str
    verdict: str
    retracted_row_id: int
    retracted_table: str
    retracted_at: str


class RetractionPropagator:
    """Track which verdict traces depend on which substrate rows.

    Session-local in Phase 8: the index is in-memory and populated via
    record_verdict_trace() during aggregation. Persistent storage via
    audit_log is Phase 10 work.
    """

    def __init__(self, db=None) -> None:
        self._db = db
        # claim_id → list of (table, row_id) tuples
        self._trace_index: dict[str, list[tuple[str, int]]] = {}
        # claim_id → last known verdict
        self._verdict_index: dict[str, str] = {}

    def record_verdict_trace(self, claim_id: str, verdict: str, source_rows: list[tuple[str, int]]) -> None:
        """Record which rows a verdict's trace depends on."""
        self._trace_index[claim_id] = list(source_rows)
        self._verdict_index[claim_id] = verdict

    def propagate_retraction(self, table: str, row_id: int) -> list[VerdictRetraction]:
        """Find all verdicts that depend on (table, row_id) and mark them retracted."""
        now = datetime.now(timezone.utc).isoformat()
        retracted: list[VerdictRetraction] = []

        for claim_id, rows in self._trace_index.items():
            if (table, row_id) in rows:
                verdict = self._verdict_index.get(claim_id, "unknown")
                retraction = VerdictRetraction(
                    claim_id=claim_id,
                    verdict=verdict,
                    retracted_row_id=row_id,
                    retracted_table=table,
                    retracted_at=now,
                )
                retracted.append(retraction)

                if self._db is not None:
                    log_event(
                        self._db,
                        event_type="verdict_retracted",
                        event_subject=claim_id,
                        event_data={
                            "verdict": verdict,
                            "retracted_row_id": row_id,
                            "retracted_table": table,
                        },
                    )

        return retracted
