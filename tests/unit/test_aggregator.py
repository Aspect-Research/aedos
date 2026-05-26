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
        assert vr.claim_verdicts == []


# ---------------------------------------------------------------------------
# Per-claim structured verdicts (Phase 10.5 Session 2 Item 1)
# ---------------------------------------------------------------------------

class TestClaimVerdictsField:
    def test_aggregate_populates_claim_verdicts(self):
        from aedos.layer5_result.aggregator import ClaimVerdict
        agg = Aggregator()
        c1 = _claim("c1", "Obama")
        c2 = _claim("c2", "Paris")
        result = agg.aggregate(
            [c1, c2],
            [_walk_result("verified"),
             _walk_result("no_grounding_found", abstention_reason="no_kb_path")],
        )
        assert len(result.claim_verdicts) == 2
        assert all(isinstance(cv, ClaimVerdict) for cv in result.claim_verdicts)
        assert result.claim_verdicts[0].claim_id == "c1"
        assert result.claim_verdicts[0].claim is c1
        assert result.claim_verdicts[0].verdict == "verified"
        assert result.claim_verdicts[0].abstention_reason is None
        assert result.claim_verdicts[1].verdict == "no_grounding_found"
        assert result.claim_verdicts[1].abstention_reason == "no_kb_path"

    def test_claim_verdicts_match_per_claim_verdicts_dict(self):
        agg = Aggregator()
        claims = [_claim(f"c{i}") for i in range(3)]
        results = [_walk_result("verified"),
                   _walk_result("contradicted"),
                   _walk_result("verified_given_assertion")]
        vr = agg.aggregate(claims, results)
        # Both representations carry the same verdicts; the list is
        # iteration-friendly, the dict is keyed by claim_id.
        for cv in vr.claim_verdicts:
            assert vr.per_claim_verdicts[cv.claim_id] == cv.verdict

    def test_empty_claims_empty_claim_verdicts(self):
        agg = Aggregator()
        vr = agg.aggregate([], [])
        assert vr.claim_verdicts == []


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


class TestAggregatorDualDesignation:
    """Phase H Cluster 2 step 1: the six-way verdict set. Base counts
    (verified / contradicted / abstained) collapse the dual designations
    so existing callers — including select_intervention — keep working;
    the three new *_given_assertion counts are additive observability."""

    def test_verified_given_assertion_counts_as_verified(self):
        agg = Aggregator()
        c = _claim("c1")
        vr = agg.aggregate([c], [_walk_result("verified_given_assertion")])
        meta = vr.aggregate_metadata
        assert meta["verified"] == 1
        assert meta["verified_given_assertion"] == 1
        assert meta["contradicted"] == 0
        assert meta["abstained"] == 0

    def test_contradicted_given_assertion_counts_as_contradicted(self):
        agg = Aggregator()
        c = _claim("c1")
        vr = agg.aggregate([c], [_walk_result("contradicted_given_assertion")])
        meta = vr.aggregate_metadata
        assert meta["contradicted"] == 1
        assert meta["contradicted_given_assertion"] == 1

    def test_abstained_given_assertion_counts_as_abstained(self):
        agg = Aggregator()
        c = _claim("c1")
        vr = agg.aggregate([c], [_walk_result("abstained_given_assertion")])
        meta = vr.aggregate_metadata
        assert meta["abstained"] == 1
        assert meta["abstained_given_assertion"] == 1

    def test_per_claim_verdict_preserves_dual_designation(self):
        # The base-count collapse is observability behavior; the per-claim
        # verdict stored in VerificationResult must be the un-collapsed
        # value so audit and downstream readers see the actual designation.
        agg = Aggregator()
        c = _claim("c1")
        vr = agg.aggregate([c], [_walk_result("verified_given_assertion")])
        assert vr.per_claim_verdicts["c1"] == "verified_given_assertion"

    def test_mixed_six_way_distribution(self):
        # All six verdict types in one batch — every count bucket
        # populates correctly.
        agg = Aggregator()
        verdicts = [
            "verified", "contradicted", "no_grounding_found",
            "verified_given_assertion", "contradicted_given_assertion",
            "abstained_given_assertion",
        ]
        claims = [_claim(f"c{i}") for i in range(len(verdicts))]
        results = [_walk_result(v) for v in verdicts]
        vr = agg.aggregate(claims, results)
        meta = vr.aggregate_metadata
        assert meta["verified"] == 2  # base + given_assertion
        assert meta["contradicted"] == 2
        assert meta["abstained"] == 2
        assert meta["verified_given_assertion"] == 1
        assert meta["contradicted_given_assertion"] == 1
        assert meta["abstained_given_assertion"] == 1
        assert meta["claim_count"] == 6


class TestVerdictSetStructuralConsistency:
    """Phase H Cluster 2 step 1: D36-pattern structural test. The six
    verdict types appear at multiple sites (aggregator counts,
    intervention selection, trace serialization, audit log, corpus
    runner). This test pins them all to a single canonical source
    (`aggregator.ALL_VERDICTS`) so the dual designation cannot drift
    silently as new code touches verdict handling.
    """

    def test_canonical_verdict_set_size(self):
        from aedos.layer5_result.aggregator import (
            ALL_VERDICTS, BASE_VERDICTS, GIVEN_ASSERTION_VERDICTS,
        )
        assert len(BASE_VERDICTS) == 3
        assert len(GIVEN_ASSERTION_VERDICTS) == 3
        assert len(ALL_VERDICTS) == 6
        # No overlap between the two families.
        assert set(BASE_VERDICTS).isdisjoint(set(GIVEN_ASSERTION_VERDICTS))

    def test_base_verdict_of_collapses_dual_to_base(self):
        from aedos.layer5_result.aggregator import (
            BASE_VERDICTS, GIVEN_ASSERTION_VERDICTS, base_verdict_of,
        )
        # Every dual collapses to a base.
        for dual in GIVEN_ASSERTION_VERDICTS:
            assert base_verdict_of(dual) in BASE_VERDICTS
        # Every base passes through unchanged.
        for base in BASE_VERDICTS:
            assert base_verdict_of(base) == base

    def test_aggregator_recognizes_every_verdict(self):
        # Each of the six verdicts produces a well-formed
        # aggregate_metadata when it is the sole verdict in a batch.
        from aedos.layer5_result.aggregator import ALL_VERDICTS
        agg = Aggregator()
        for v in ALL_VERDICTS:
            c = _claim(f"c_{v}")
            vr = agg.aggregate([c], [_walk_result(v)])
            assert vr.per_claim_verdicts[f"c_{v}"] == v
            # claim_count is always 1; verified+contradicted+abstained
            # always sums to claim_count (base-count invariant).
            meta = vr.aggregate_metadata
            assert meta["verified"] + meta["contradicted"] + meta["abstained"] == 1

    def test_intervention_collapses_dual_to_base(self):
        # select_intervention reads only the base counts; for any verdict
        # mix, replacing each verdict with its base-counterpart must
        # produce the same intervention. That's the invariant — the
        # dual designation is transparent to intervention selection.
        from aedos.deployment.chat_wrapper import select_intervention
        from aedos.layer5_result.aggregator import base_verdict_of, ALL_VERDICTS
        agg = Aggregator()

        # Exhaustive 1-of-6 single-claim check: every dual matches its base.
        for v in ALL_VERDICTS:
            c = _claim("c1")
            dual_vr = agg.aggregate([c], [_walk_result(v)])
            base_vr = agg.aggregate([c], [_walk_result(base_verdict_of(v))])
            assert select_intervention(dual_vr) == select_intervention(base_vr), (
                f"intervention diverged for verdict {v!r} vs its base "
                f"{base_verdict_of(v)!r}"
            )

        # Multi-claim mixed batch: 3 verified_given_assertion + 1
        # contradicted_given_assertion should match 3 verified + 1
        # contradicted (a CORRECT — contradicted present but minority).
        claims = [_claim(f"c{i}") for i in range(4)]
        dual_results = [_walk_result(v) for v in (
            "verified_given_assertion", "verified_given_assertion",
            "verified_given_assertion", "contradicted_given_assertion",
        )]
        base_results = [_walk_result(v) for v in (
            "verified", "verified", "verified", "contradicted",
        )]
        dual_vr = agg.aggregate(claims, dual_results)
        base_vr = agg.aggregate(claims, base_results)
        assert select_intervention(dual_vr) == select_intervention(base_vr)
        assert select_intervention(dual_vr).value == "correct"

    def test_trace_serialization_round_trip_carries_assertion_flag(self):
        # JustificationTrace.chain_includes_assertion survives trace_to_json.
        from aedos.layer5_result.trace import trace_to_json
        trace = JustificationTrace(
            root=TraceNode("claim"),
            chain_includes_assertion=True,
        )
        j = trace_to_json(trace)
        assert j["chain_includes_assertion"] is True

    def test_corpus_runner_accepts_every_verdict(self):
        # The corpus runner's expected-verdict comparison must accept all
        # six verdict strings as recognized values (no unknown-verdict
        # ValueError or AttributeError). The runner's branching is
        # explicit on the three v0.15 verdicts plus its non-standard
        # categories; step 5 will extend that comparison to the dual
        # verdicts. For step 1 we assert the strings are recognized at
        # the *aggregator* boundary (the layer the corpus runner reads).
        from aedos.layer5_result.aggregator import ALL_VERDICTS, _VERDICT_TO_BASE_COUNT
        for v in ALL_VERDICTS:
            assert v in _VERDICT_TO_BASE_COUNT, (
                f"verdict {v!r} missing from _VERDICT_TO_BASE_COUNT — "
                "the six-way verdict set has drifted"
            )


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
