"""Tests for Aggregator + VerificationResult."""

from __future__ import annotations

import pytest

from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer4_sources.walker import BudgetConsumption, WalkResult
from aedos.layer5_result.aggregator import Aggregator, VerificationResult
from aedos.layer5_result.trace import JustificationTrace, TraceNode


def _claim(claim_id: str, subject: str = "Obama") -> Claim:
    return Claim(
        claim_id=claim_id,
        subject=subject,
        predicate="holds_role",
        object="President",
        polarity=1,
        source_text="test",
        asserting_party="user_test",
        triage_decision=TriageDecision.VERIFY,
    )


def _walk_result(verdict: str, abstention_reason=None) -> WalkResult:
    trace = JustificationTrace(
        root=TraceNode("claim"),
        source_breakdown={"tier_u": 1 if verdict == "verified" else 0, "kb": 0, "python": 0},
    )
    trace.walk_metadata = {"depth_reached": 1, "llm_calls": 2}
    return WalkResult(
        verdict=verdict,
        trace=trace,
        abstention_reason=abstention_reason,
        budget_consumption=BudgetConsumption(wall_clock_ms=100.0, llm_calls=2),
    )


# ---------------------------------------------------------------------------
# TestVerificationResultDataclass
# ---------------------------------------------------------------------------

class TestVerificationResultDataclass:
    def test_fields_present(self):
        vr = VerificationResult(
            claims_extracted=[],
            per_claim_verdicts={},
            per_claim_traces={},
            aggregate_metadata={},
            audit_log_entries=[],
            text_input={},
        )
        assert vr.claims_extracted == []
        assert vr.consistency_warnings == []


# ---------------------------------------------------------------------------
# TestAggregator
# ---------------------------------------------------------------------------

class TestAggregatorBasic:
    def test_single_verified_claim(self):
        agg = Aggregator()
        c = _claim("c1")
        result = agg.aggregate([c], [_walk_result("verified")])
        assert result.per_claim_verdicts["c1"] == "verified"

    def test_single_contradicted_claim(self):
        agg = Aggregator()
        c = _claim("c1")
        result = agg.aggregate([c], [_walk_result("contradicted")])
        assert result.per_claim_verdicts["c1"] == "contradicted"

    def test_single_abstained_claim(self):
        agg = Aggregator()
        c = _claim("c1")
        result = agg.aggregate([c], [_walk_result("no_grounding_found")])
        assert result.per_claim_verdicts["c1"] == "no_grounding_found"

    def test_multiple_claims(self):
        agg = Aggregator()
        claims = [_claim("c1"), _claim("c2"), _claim("c3")]
        results = [_walk_result("verified"), _walk_result("contradicted"), _walk_result("no_grounding_found")]
        vr = agg.aggregate(claims, results)
        assert vr.per_claim_verdicts["c1"] == "verified"
        assert vr.per_claim_verdicts["c2"] == "contradicted"
        assert vr.per_claim_verdicts["c3"] == "no_grounding_found"

    def test_claims_extracted_matches_input(self):
        agg = Aggregator()
        claims = [_claim("c1"), _claim("c2")]
        vr = agg.aggregate(claims, [_walk_result("verified"), _walk_result("verified")])
        assert len(vr.claims_extracted) == 2


class TestAggregatorMetadata:
    def test_verdict_counts_in_metadata(self):
        agg = Aggregator()
        claims = [_claim("c1"), _claim("c2"), _claim("c3")]
        results = [_walk_result("verified"), _walk_result("contradicted"), _walk_result("no_grounding_found")]
        vr = agg.aggregate(claims, results)
        meta = vr.aggregate_metadata
        assert meta["verified"] == 1
        assert meta["contradicted"] == 1
        assert meta["abstained"] == 1
        assert meta["claim_count"] == 3

    def test_total_llm_calls_summed(self):
        agg = Aggregator()
        c1, c2 = _claim("c1"), _claim("c2")
        vr = agg.aggregate([c1, c2], [_walk_result("verified"), _walk_result("verified")])
        assert vr.aggregate_metadata["total_llm_calls"] == 4  # 2 per claim

    def test_max_depth_tracked(self):
        agg = Aggregator()
        vr = agg.aggregate([_claim("c1")], [_walk_result("verified")])
        assert vr.aggregate_metadata["max_depth_reached"] >= 0

    def test_source_breakdown_aggregated(self):
        agg = Aggregator()
        c1, c2 = _claim("c1"), _claim("c2")
        vr = agg.aggregate([c1, c2], [_walk_result("verified"), _walk_result("verified")])
        assert vr.aggregate_metadata["source_breakdown"].get("tier_u", 0) >= 2

    def test_text_input_preserved(self):
        agg = Aggregator()
        vr = agg.aggregate([_claim("c1")], [_walk_result("verified")], text_input={"text": "hello"})
        assert vr.text_input == {"text": "hello"}


class TestAggregatorTraces:
    def test_per_claim_traces_populated(self):
        agg = Aggregator()
        c = _claim("c1")
        vr = agg.aggregate([c], [_walk_result("verified")])
        assert "c1" in vr.per_claim_traces
        assert vr.per_claim_traces["c1"].root.node_type == "claim"

    def test_consistency_warning_for_circuit_breaker(self):
        agg = Aggregator()
        c = _claim("c1")
        wr = _walk_result("no_grounding_found", abstention_reason="circuit_breaker_triggered")
        vr = agg.aggregate([c], [wr])
        assert len(vr.consistency_warnings) == 1
        assert vr.consistency_warnings[0]["claim_id"] == "c1"

    def test_no_consistency_warnings_normally(self):
        agg = Aggregator()
        vr = agg.aggregate([_claim("c1")], [_walk_result("verified")])
        assert vr.consistency_warnings == []
