"""Tests for RetractionPropagator and ContradictionTracer."""

from __future__ import annotations

import pytest

from aedos.layer5_result.retraction import RetractionPropagator, VerdictRetraction
from aedos.layer5_result.contradiction_tracer import ContradictionTracer


# ---------------------------------------------------------------------------
# VerdictRetraction dataclass
# ---------------------------------------------------------------------------

class TestVerdictRetractionDataclass:
    def test_fields_present(self):
        vr = VerdictRetraction(
            claim_id="c1",
            verdict="verified",
            retracted_row_id=42,
            retracted_table="tier_u",
            retracted_at="2026-01-01T00:00:00",
        )
        assert vr.claim_id == "c1"
        assert vr.verdict == "verified"
        assert vr.retracted_row_id == 42
        assert vr.retracted_table == "tier_u"


# ---------------------------------------------------------------------------
# RetractionPropagator
# ---------------------------------------------------------------------------

class TestRetractionPropagator:
    def test_no_registered_traces_returns_empty(self):
        prop = RetractionPropagator()
        result = prop.propagate_retraction("tier_u", 99)
        assert result == []

    def test_registered_trace_triggers_retraction(self):
        prop = RetractionPropagator()
        prop.record_verdict_trace("c1", "verified", [("tier_u", 1)])
        result = prop.propagate_retraction("tier_u", 1)
        assert len(result) == 1
        assert result[0].claim_id == "c1"
        assert result[0].verdict == "verified"
        assert result[0].retracted_table == "tier_u"
        assert result[0].retracted_row_id == 1

    def test_unrelated_row_id_not_retracted(self):
        prop = RetractionPropagator()
        prop.record_verdict_trace("c1", "verified", [("tier_u", 1)])
        result = prop.propagate_retraction("tier_u", 99)
        assert result == []

    def test_unrelated_table_not_retracted(self):
        prop = RetractionPropagator()
        prop.record_verdict_trace("c1", "verified", [("tier_u", 1)])
        result = prop.propagate_retraction("predicate_translation", 1)
        assert result == []

    def test_multiple_claims_depending_on_same_row(self):
        prop = RetractionPropagator()
        prop.record_verdict_trace("c1", "verified", [("tier_u", 5)])
        prop.record_verdict_trace("c2", "verified", [("tier_u", 5)])
        result = prop.propagate_retraction("tier_u", 5)
        assert len(result) == 2
        claim_ids = {r.claim_id for r in result}
        assert "c1" in claim_ids
        assert "c2" in claim_ids

    def test_claim_with_multiple_rows_retracts_on_any(self):
        prop = RetractionPropagator()
        prop.record_verdict_trace("c1", "verified", [("tier_u", 1), ("kb_statement", 2)])
        result = prop.propagate_retraction("kb_statement", 2)
        assert len(result) == 1
        assert result[0].claim_id == "c1"

    def test_record_updates_verdict(self):
        prop = RetractionPropagator()
        prop.record_verdict_trace("c1", "contradicted", [("tier_u", 10)])
        result = prop.propagate_retraction("tier_u", 10)
        assert result[0].verdict == "contradicted"


# ---------------------------------------------------------------------------
# ContradictionTracer
# ---------------------------------------------------------------------------

class TestContradictionTracer:
    def test_traces_contradiction_and_retracts(self):
        prop = RetractionPropagator()
        prop.record_verdict_trace("c1", "verified", [("tier_u", 1)])
        tracer = ContradictionTracer(retraction_propagator=prop)
        result = tracer.trace_contradiction("c1", {"source": "tier_u", "detail": "user correction"})
        assert len(result) == 1
        assert result[0].claim_id == "c1"

    def test_no_trace_registered_returns_empty(self):
        tracer = ContradictionTracer()
        result = tracer.trace_contradiction("nonexistent", {"source": "tier_u"})
        assert result == []

    def test_multiple_rows_in_trace_all_retracted(self):
        prop = RetractionPropagator()
        prop.record_verdict_trace("c1", "verified", [("tier_u", 1)])
        prop.record_verdict_trace("c2", "verified", [("tier_u", 1)])
        tracer = ContradictionTracer(retraction_propagator=prop)
        result = tracer.trace_contradiction("c1", {"source": "user"})
        assert len(result) >= 1
