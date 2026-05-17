from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

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


class Aggregator:
    def __init__(self, audit_log=None) -> None:
        self._audit = audit_log

    def aggregate(
        self,
        claims: list[Claim],
        per_claim_results: list,  # list[WalkResult]
        text_input: Optional[dict] = None,
    ) -> VerificationResult:
        per_claim_verdicts: dict[str, str] = {}
        per_claim_traces: dict[str, JustificationTrace] = {}
        consistency_warnings: list[dict] = []

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
            audit_log_entries=[],
            text_input=text_input or {},
            consistency_warnings=consistency_warnings,
        )
