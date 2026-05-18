from __future__ import annotations

import json
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

    The index is in-memory, populated within a process by record_verdict_trace()
    during aggregation. replay() rehydrates it from persisted `verdict_recorded`
    audit events at process startup, so retraction propagation survives process
    restarts (D6 — architecture 7.3, over-time soundness).
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

    def replay(self) -> int:
        """Rehydrate the trace index from persisted `verdict_recorded` audit
        events (D6 — over-time soundness across process restarts).

        The aggregator logs one `verdict_recorded` event per verdict, carrying
        its `source_rows`. A fresh process starts with an empty in-memory index;
        calling replay() at startup reconstructs exactly the state the
        in-process record_verdict_trace() calls produced — events applied in id
        order, last-wins per claim_id (mirroring record_verdict_trace's
        overwrite). propagate_retraction() then walks the same index whether it
        was filled in-process or by replay. Idempotent; returns the count
        hydrated.
        """
        if self._db is None:
            return 0
        rows = self._db.execute(
            "SELECT event_subject, event_data FROM audit_log "
            "WHERE event_type='verdict_recorded' ORDER BY id"
        ).fetchall()
        count = 0
        for row in rows:
            try:
                data = json.loads(row["event_data"])
            except (json.JSONDecodeError, TypeError):
                continue
            subject = row["event_subject"]
            # The aggregator sets event_subject="claim:{claim_id}"; the index is
            # keyed on the bare claim_id that record_verdict_trace() uses.
            claim_id = subject[len("claim:"):] if subject.startswith("claim:") else subject
            # source_rows round-trips through JSON as lists; the index and
            # propagate_retraction's membership test use (table, row_id) tuples.
            source_rows = [tuple(r) for r in data.get("source_rows", [])]
            self._trace_index[claim_id] = source_rows
            self._verdict_index[claim_id] = data.get("verdict", "unknown")
            count += 1
        return count

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
