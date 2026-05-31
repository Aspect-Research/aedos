from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..audit.log import log_event
from ..layer1_extraction.extractor import Claim
from ..layer5_result.trace import JustificationTrace


# The six-way verdict set. Three base verdicts
# (the existing base semantics) and three dual-designation verdicts
# tagged with `_given_assertion` when the chain includes any
# `asserted_unverified` Tier U premise. The aggregator's count buckets,
# the chat-wrapper's intervention selection, the corpus runner's
# comparison, and the structural-consistency test all key off these
# tuples — single source of truth so the six verdicts stay synchronized
# across every site they appear in.

BASE_VERDICTS: tuple[str, ...] = ("verified", "contradicted", "no_grounding_found")

GIVEN_ASSERTION_VERDICTS: tuple[str, ...] = (
    "verified_given_assertion",
    "contradicted_given_assertion",
    "abstained_given_assertion",
)

ALL_VERDICTS: tuple[str, ...] = BASE_VERDICTS + GIVEN_ASSERTION_VERDICTS

# Dual designation collapses to its base verdict for intervention
# selection. The dual flag is observability metadata;
# user-facing behavior keys off the base verdict only.
_BASE_OF_DUAL: dict[str, str] = {
    "verified_given_assertion": "verified",
    "contradicted_given_assertion": "contradicted",
    "abstained_given_assertion": "no_grounding_found",
}

# Mapping from each verdict (base or dual) to its bucket label in the
# `aggregate_metadata` counts. The three base counts
# (`verified` / `contradicted` / `abstained`) stay as the user-facing
# rollup that `select_intervention` reads; the three dual counts are
# additive observability.
_VERDICT_TO_BASE_COUNT: dict[str, str] = {
    "verified": "verified",
    "contradicted": "contradicted",
    "no_grounding_found": "abstained",
    "verified_given_assertion": "verified",
    "contradicted_given_assertion": "contradicted",
    "abstained_given_assertion": "abstained",
}


def base_verdict_of(verdict: str) -> str:
    """Collapse a dual-designation verdict to its base verdict; pass base
    verdicts through unchanged. Used by intervention selection and any
    caller that needs the base-shaped verdict (verified / contradicted /
    no_grounding_found) without the assertion-source qualifier.
    """
    return _BASE_OF_DUAL.get(verdict, verdict)


def is_given_assertion(verdict: str) -> bool:
    """True iff the verdict is one of the three `*_given_assertion` variants."""
    return verdict in GIVEN_ASSERTION_VERDICTS


@dataclass(frozen=True)
class ClaimVerdict:
    """Per-claim verdict shape consumed by the per-claim intervention layer.
    Carries the claim and its verdict plus
    the intervention-relevant metadata the chat-wrapper needs to compose
    a per-claim annotation. The aggregator builds one of these per claim
    during `aggregate` and exposes the list as `VerificationResult.claim_verdicts`.

    `abstention_reason` is sourced from `WalkResult.abstention_reason` when
    the verdict is one of the abstention shapes (`no_grounding_found` or
    `abstained_given_assertion`); None otherwise.

    `contradicting_value` (WS5) is the KB/Tier-U value the source holds
    that contradicts a CONTRADICTED claim, extracted from the trace's
    contradicted premise_lookup edge (`metadata['contradicting_value']`)
    by `_extract_contradicting_value`. None when the verdict is not
    contradicted, or when the contradicted path carried no distinct value
    (e.g. polarity-conflict, or a subsumption-fallback contradiction).
    `contradicting_value_type` is the Statement value_type
    (entity|literal|date|quantity) so the chat-wrapper knows whether to
    reverse-label a Q-id."""
    claim_id: str
    claim: Claim
    verdict: str
    abstention_reason: Optional[str] = None
    contradicting_value: Optional[str] = None
    contradicting_value_type: Optional[str] = None


@dataclass
class VerificationResult:
    claims_extracted: list[Claim]
    per_claim_verdicts: dict[str, str]
    per_claim_traces: dict[str, JustificationTrace]
    aggregate_metadata: dict
    audit_log_entries: list[int]
    text_input: dict
    consistency_warnings: list[dict] = field(default_factory=list)
    # Per-claim intervention: structured
    # per-claim verdict list for the intervention layer. The dict-based
    # `per_claim_verdicts` / `per_claim_traces` fields stay (callers and
    # the audit log consume them); `claim_verdicts` is the additive,
    # iteration-friendly shape `select_interventions` consumes.
    claim_verdicts: list["ClaimVerdict"] = field(default_factory=list)


# Trace-edge metadata keys that carry a retractable substrate/Tier U row id,
# mapped to the table the id belongs to.
_TRACE_ROW_ID_KEYS = {
    "tier_u_row_id": "tier_u",
    "predicate_translation_row_id": "predicate_translation",
    "subsumption_row_id": "subsumption",
    # v0.16 WS3: KB premise edges stamp the resolver's cache row id so
    # KB-grounded verdicts become retractable when a cached entity
    # resolution is retracted. The walker stamps the id in a later phase;
    # the key is registered here (additive) so the dependency footprint
    # picks it up once present.
    "entity_resolution_cache_row_id": "entity_resolution_cache",
}


def _extract_contradicting_value(
    trace: JustificationTrace,
) -> tuple[Optional[str], Optional[str]]:
    """WS5: pull the contradicting value (and its value_type) from the
    contradicted premise_lookup edge a CONTRADICTED verdict rests on.
    Returns (value_as_str, value_type) or (None, None). Scans edges for a
    premise_lookup whose metadata verdict == 'contradicted' carrying a
    non-None 'contradicting_value'. First such edge wins (a CONTRADICTED
    walk short-circuits at the first contradiction, so there is at most one
    in practice). Returns (None, None) when no distinct value was captured
    (polarity-conflict, or a subsumption-fallback contradiction)."""
    for edge in trace.edges:
        md = edge.metadata
        if md.get("verdict") != "contradicted":
            continue
        cv = md.get("contradicting_value")
        if cv is None:
            continue
        return (str(cv), md.get("contradicting_value_type"))
    return (None, None)


def _extract_source_rows(trace: JustificationTrace) -> list[tuple[str, int]]:
    """Pull the (table, row_id) pairs a verdict's justification trace depended
    on. These feed the retraction propagator's dependency index so that
    retracting a contributing row propagates to this verdict (architecture 7.3).

    v0.16 WS3 §3C: prefer the structured provenance term as the single source
    of truth when the walker populated it; the term's source_rows() is the
    retraction dependency footprint. Fall back to the legacy edge scan for
    traces that carry only edge metadata (hand-built test traces), so both
    shapes feed the propagator.
    """
    prov_rows = trace.provenance.source_rows()
    if prov_rows:
        return prov_rows
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
        claim_verdicts: list[ClaimVerdict] = []
        consistency_warnings: list[dict] = []
        audit_log_entries: list[int] = []

        # Base-count buckets stay verified /
        # contradicted / abstained (the rollup `select_intervention`
        # consumes — dual designations collapse to their
        # base). Additive observability counts for the three
        # `*_given_assertion` variants give a per-claim
        # source-of-grounding view without changing the base-shaped behavior.
        verdict_counts: dict[str, int] = {"verified": 0, "contradicted": 0, "abstained": 0}
        given_assertion_counts: dict[str, int] = {
            "verified_given_assertion": 0,
            "contradicted_given_assertion": 0,
            "abstained_given_assertion": 0,
        }
        total_llm_calls = 0
        max_depth = 0
        source_breakdown: dict[str, int] = {}
        budget_exceedances = 0

        for claim, result in zip(claims, per_claim_results):
            cid = claim.claim_id
            per_claim_verdicts[cid] = result.verdict
            per_claim_traces[cid] = result.trace
            # WS5(b): for contradicted-family verdicts only, scan the trace for
            # the contradicting value the source holds (cheap guard — skip the
            # scan entirely for verified/abstained).
            cv_value, cv_value_type = (None, None)
            if base_verdict_of(result.verdict) == "contradicted":
                cv_value, cv_value_type = _extract_contradicting_value(result.trace)
            claim_verdicts.append(ClaimVerdict(
                claim_id=cid,
                claim=claim,
                verdict=result.verdict,
                abstention_reason=result.abstention_reason,
                contradicting_value=cv_value,
                contradicting_value_type=cv_value_type,
            ))

            base_count_bucket = _VERDICT_TO_BASE_COUNT.get(result.verdict)
            if base_count_bucket is not None:
                verdict_counts[base_count_bucket] += 1
            if is_given_assertion(result.verdict):
                given_assertion_counts[result.verdict] += 1

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
            **given_assertion_counts,
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
            claim_verdicts=claim_verdicts,
        )
