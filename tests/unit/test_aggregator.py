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


class TestClaimVerdictContradictingValue:
    """WS5 (part b): the aggregator populates ClaimVerdict.contradicting_value /
    contradicting_value_type by scanning the trace for a CONTRADICTED
    premise_lookup edge carrying the value the source holds. Only done for
    contradicted-family verdicts; None otherwise."""

    def _contradicted_walk_result(self, verdict, value, value_type):
        from aedos.layer5_result.trace import TraceEdge
        root = TraceNode("claim")
        trace = JustificationTrace(root=root)
        trace.edges.append(TraceEdge(
            edge_type="premise_lookup",
            source=root,
            target=TraceNode("kb_statement", {"entity": "Q76"}),
            metadata={
                "source": "kb",
                "verdict": "contradicted",
                "contradicting_value": value,
                "contradicting_value_type": value_type,
                "kb_property": "P19",
            },
        ))
        return WalkResult(
            verdict=verdict,
            trace=trace,
            budget_consumption=BudgetConsumption(wall_clock_ms=10.0, llm_calls=1),
        )

    def test_aggregate_populates_contradicting_value(self):
        agg = Aggregator()
        c = _claim("c1")
        wr = self._contradicted_walk_result("contradicted", "Q18094", "entity")
        vr = agg.aggregate([c], [wr])
        cv = vr.claim_verdicts[0]
        assert cv.contradicting_value == "Q18094"
        assert cv.contradicting_value_type == "entity"

    def test_aggregate_populates_for_contradicted_given_assertion(self):
        # base_verdict_of collapses the dual to contradicted, so the scan runs.
        agg = Aggregator()
        c = _claim("c1")
        wr = self._contradicted_walk_result(
            "contradicted_given_assertion", "Paris", "literal")
        vr = agg.aggregate([c], [wr])
        cv = vr.claim_verdicts[0]
        assert cv.contradicting_value == "Paris"
        assert cv.contradicting_value_type == "literal"

    def test_quantity_value_stringified(self):
        # _extract_contradicting_value coerces the value to str (a numeric
        # kb_value flows through as a string for the chat-wrapper).
        agg = Aggregator()
        c = _claim("c1")
        wr = self._contradicted_walk_result("contradicted", 67000000, "quantity")
        vr = agg.aggregate([c], [wr])
        cv = vr.claim_verdicts[0]
        assert cv.contradicting_value == "67000000"
        assert cv.contradicting_value_type == "quantity"

    def test_verified_verdict_has_no_contradicting_value(self):
        agg = Aggregator()
        c = _claim("c1")
        vr = agg.aggregate([c], [_walk_result("verified")])
        cv = vr.claim_verdicts[0]
        assert cv.contradicting_value is None
        assert cv.contradicting_value_type is None

    def test_contradicted_without_value_edge_is_none(self):
        # A CONTRADICTED verdict whose trace carries no distinct value
        # (polarity-conflict / subsumption-fallback) → None, never invented.
        agg = Aggregator()
        c = _claim("c1")
        vr = agg.aggregate([c], [_walk_result("contradicted")])
        cv = vr.claim_verdicts[0]
        assert cv.contradicting_value is None
        assert cv.contradicting_value_type is None


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

    def test_intervention_no_longer_collapses_dual_to_base(self):
        # WS5 (part d): INVERTED from the v0.15 collapse invariant. The
        # `*_given_assertion` qualifier is NO LONGER transparent to
        # intervention selection — a conditional verdict now produces a
        # DIFFERENT (conditional) plan than its base verdict. `base_verdict_of`
        # still drives the aggregate COUNT buckets (tested elsewhere), but it
        # no longer governs the user-surface intervention.
        from aedos.deployment.chat_wrapper import (
            ClaimActionType, InterventionType, select_interventions,
        )
        agg = Aggregator()

        # verified_given_assertion DIVERGES from verified: base verified is a
        # silent PASS_THROUGH; the dual surfaces a CONFIRM_CONDITIONAL note.
        c = _claim("c1")
        dual_vr = agg.aggregate([c], [_walk_result("verified_given_assertion")])
        base_vr = agg.aggregate([c], [_walk_result("verified")])
        dual_plan = select_interventions(dual_vr.claim_verdicts)
        base_plan = select_interventions(base_vr.claim_verdicts)
        assert base_plan.overall == InterventionType.PASS_THROUGH
        assert dual_plan.overall == InterventionType.INTERVENE
        assert dual_plan.overall != base_plan.overall
        assert len(dual_plan.per_claim_actions) == 1
        assert (
            dual_plan.per_claim_actions[0].action_type
            == ClaimActionType.CONFIRM_CONDITIONAL
        )

        # contradicted_given_assertion keeps CORRECT (same action_type as its
        # base) but the annotation diverges — it carries the conditional suffix.
        c = _claim("c1")
        dual_vr = agg.aggregate([c], [_walk_result("contradicted_given_assertion")])
        base_vr = agg.aggregate([c], [_walk_result("contradicted")])
        dual_plan = select_interventions(dual_vr.claim_verdicts)
        base_plan = select_interventions(base_vr.claim_verdicts)
        assert dual_plan.overall == base_plan.overall == InterventionType.INTERVENE
        assert (
            dual_plan.per_claim_actions[0].action_type
            == base_plan.per_claim_actions[0].action_type
            == ClaimActionType.CORRECT
        )
        assert (
            dual_plan.per_claim_actions[0].annotation
            != base_plan.per_claim_actions[0].annotation
        )
        assert "rests on your own prior assertion" in dual_plan.per_claim_actions[0].annotation

        # abstained_given_assertion keeps ABSTAIN but with the conditional suffix.
        c = _claim("c1")
        dual_vr = agg.aggregate([c], [_walk_result("abstained_given_assertion")])
        base_vr = agg.aggregate([c], [_walk_result("no_grounding_found")])
        dual_plan = select_interventions(dual_vr.claim_verdicts)
        base_plan = select_interventions(base_vr.claim_verdicts)
        assert (
            dual_plan.per_claim_actions[0].action_type
            == base_plan.per_claim_actions[0].action_type
            == ClaimActionType.ABSTAIN
        )
        assert (
            dual_plan.per_claim_actions[0].annotation
            != base_plan.per_claim_actions[0].annotation
        )

    def test_trace_serialization_round_trip_carries_assertion_flag(self):
        # JustificationTrace.chain_includes_assertion (now DERIVED from the
        # provenance term, v0.16 WS3 §3A) survives trace_to_json. Populate the
        # term with an assertion-conditional literal; the derived boolean must
        # serialize True and the provenance view must be present.
        from aedos.layer5_result.trace import (
            ProvenanceLiteral,
            ProvenanceTerm,
            trace_to_json,
        )
        trace = JustificationTrace(root=TraceNode("claim"))
        trace.provenance.add_alternative(
            ProvenanceTerm.lit(ProvenanceLiteral(source="tier_u", assertion=True))
        )
        j = trace_to_json(trace)
        assert j["chain_includes_assertion"] is True
        assert "provenance" in j

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


class TestExtractSourceRows:
    """v0.16 WS3 §3C/D13: _extract_source_rows feeds the retraction propagator's
    dependency index. It prefers the structured provenance term when populated
    (the walker's source of truth) and falls back to the legacy edge-metadata
    scan for hand-built test traces. The D13 entity_resolution_cache key makes
    KB-grounded verdicts retractable when a cached entity resolution is retracted.
    """

    def test_entity_resolution_cache_key_registered(self):
        from aedos.layer5_result.aggregator import _TRACE_ROW_ID_KEYS
        assert _TRACE_ROW_ID_KEYS["entity_resolution_cache_row_id"] == "entity_resolution_cache"

    def test_edge_scan_extracts_entity_resolution_cache_row(self):
        # D13: a KB premise edge stamping entity_resolution_cache_row_id is
        # pulled into source_rows via the legacy edge scan (no provenance term).
        from aedos.layer5_result.aggregator import _extract_source_rows
        from aedos.layer5_result.trace import TraceEdge
        root = TraceNode("claim")
        trace = JustificationTrace(root=root)
        trace.edges.append(TraceEdge(
            edge_type="premise_lookup", source=root,
            target=TraceNode("kb_statement"),
            metadata={"source": "kb", "entity_resolution_cache_row_id": 42},
        ))
        assert _extract_source_rows(trace) == [("entity_resolution_cache", 42)]

    def test_provenance_term_preferred_over_edge_scan(self):
        # When the provenance term carries source rows, it is the single source
        # of truth — the edge scan is not consulted (so a term-only KB verdict,
        # with no edge metadata, still surfaces its entity_resolution_cache dep).
        from aedos.layer5_result.aggregator import _extract_source_rows
        from aedos.layer5_result.trace import ProvenanceLiteral, ProvenanceTerm
        root = TraceNode("claim")
        trace = JustificationTrace(root=root)
        trace.provenance.add_alternative(ProvenanceTerm.lit(
            ProvenanceLiteral(source="kb", table="entity_resolution_cache", row_id=9)))
        assert _extract_source_rows(trace) == [("entity_resolution_cache", 9)]

    def test_empty_trace_extracts_nothing(self):
        from aedos.layer5_result.aggregator import _extract_source_rows
        trace = JustificationTrace(root=TraceNode("claim"))
        assert _extract_source_rows(trace) == []

    def test_aggregate_records_entity_resolution_cache_dependency(self):
        # End of the chain: the aggregator records the D13 dependency into the
        # propagator's trace index, so retracting that cache row propagates to
        # the KB verdict.
        from aedos.layer5_result.retraction import RetractionPropagator
        from aedos.layer5_result.trace import ProvenanceLiteral, ProvenanceTerm
        prop = RetractionPropagator()
        agg = Aggregator(retraction_propagator=prop)
        c = _claim("c1")
        wr = _walk_result("verified")
        wr.trace.provenance.add_alternative(ProvenanceTerm.lit(
            ProvenanceLiteral(source="kb", table="entity_resolution_cache", row_id=7)))
        agg.aggregate([c], [wr])
        assert ("entity_resolution_cache", 7) in prop._trace_index["c1"]
        retracted = prop.propagate_retraction("entity_resolution_cache", 7)
        assert any(r.claim_id == "c1" for r in retracted)


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
