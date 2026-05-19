from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..audit.log import log_event
from ..layer1_extraction.extractor import Claim
from ..layer5_result.trace import JustificationTrace


@dataclass
class VerificationResult:
    claims_extracted: list[Claim]
    per_claim_verdicts: dict[str, str]
    per_claim_traces: dict[str, JustificationTrace]
    aggregate_metadata: dict
    audit_log_entries: list[int]
    text_input: dict
    consistency_warnings: list[dict] = field(default_factory=list)


# Trace-edge metadata keys that carry a retractable substrate/Tier U row id,
# mapped to the table the id belongs to.
_TRACE_ROW_ID_KEYS = {
    "tier_u_row_id": "tier_u",
    "predicate_translation_row_id": "predicate_translation",
    "subsumption_row_id": "subsumption",
}


def _extract_source_rows(trace: JustificationTrace) -> list[tuple[str, int]]:
    """Pull the (table, row_id) pairs a verdict's justification trace depended
    on. These feed the retraction propagator's dependency index so that
    retracting a contributing row propagates to this verdict (architecture 7.3).
    """
    rows: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for edge in trace.edges:
        for key, table in _TRACE_ROW_ID_KEYS.items():
            row_id = edge.metadata.get(key)
            if row_id is not None and (table, row_id) not in seen:
                seen.add((table, row_id))
                rows.append((table, row_id))
    return rows


class Aggregator:
    def __init__(self, retraction_propagator=None, db=None) -> None:
        self._propagator = retraction_propagator
        self._db = db

    def aggregate(
        self,
        claims: list[Claim],
        per_claim_results: list,  # list[WalkResult]
        text_input: Optional[dict] = None,
    ) -> VerificationResult:
        per_claim_verdicts: dict[str, str] = {}
        per_claim_traces: dict[str, JustificationTrace] = {}
        consistency_warnings: list[dict] = []
        audit_log_entries: list[int] = []

        verdict_counts: dict[str, int] = {"verified": 0, "contradicted": 0, "abstained": 0}
        total_llm_calls = 0
        max_depth = 0
        source_breakdown: dict[str, int] = {}
        budget_exceedances = 0

        for claim, result in zip(claims, per_claim_results):
            cid = claim.claim_id
            per_claim_verdicts[cid] = result.verdict
            per_claim_traces[cid] = result.trace

            if result.verdict == "verified":
                verdict_counts["verified"] += 1
            elif result.verdict == "contradicted":
                verdict_counts["contradicted"] += 1
            else:
                verdict_counts["abstained"] += 1

            consumption = result.budget_consumption
            total_llm_calls += consumption.llm_calls
            depth = result.trace.walk_metadata.get("depth_reached", 0)
            if depth > max_depth:
                max_depth = depth

            for src, cnt in result.trace.source_breakdown.items():
                source_breakdown[src] = source_breakdown.get(src, 0) + cnt

            if result.abstention_reason and "budget" in result.abstention_reason:
                budget_exceedances += 1

            if result.abstention_reason == "circuit_breaker_triggered":
                consistency_warnings.append({
                    "claim_id": cid,
                    "reason": "circuit_breaker_triggered",
                })

            # M2: register the verdict's trace with the retraction propagator so
            # that retracting a contributing row propagates to this verdict.
            if self._propagator is not None:
                source_rows = _extract_source_rows(result.trace)
                self._propagator.record_verdict_trace(cid, result.verdict, source_rows)
                # m6: a recorded verdict trace is an audit-log entry created
                # during this verification; reference it in the result.
                if self._db is not None:
                    entry_id = log_event(
                        self._db,
                        event_type="verdict_recorded",
                        event_subject=f"claim:{cid}",
                        event_data={"verdict": result.verdict, "source_rows": source_rows},
                    )
                    audit_log_entries.append(entry_id)

        aggregate_metadata: dict[str, Any] = {
            "claim_count": len(claims),
            **verdict_counts,
            "total_llm_calls": total_llm_calls,
            "max_depth_reached": max_depth,
            "source_breakdown": source_breakdown,
            "budget_exceedances": budget_exceedances,
        }

        return VerificationResult(
            claims_extracted=claims,
            per_claim_verdicts=per_claim_verdicts,
            per_claim_traces=per_claim_traces,
            aggregate_metadata=aggregate_metadata,
            audit_log_entries=audit_log_entries,
            text_input=text_input or {},
            consistency_warnings=consistency_warnings,
        )
