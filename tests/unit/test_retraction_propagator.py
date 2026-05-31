"""Tests for RetractionPropagator and ContradictionTracer."""

from __future__ import annotations

import pytest

from aedos.database import open_db
from aedos.layer5_result.aggregator import Aggregator
from aedos.layer5_result.retraction import RetractionPropagator, VerdictRetraction
from aedos.layer5_result.contradiction_tracer import ContradictionTracer


# ---------------------------------------------------------------------------
# Helpers — B4 / D6 cross-process replay
# ---------------------------------------------------------------------------

def _claim(claim_id):
    from aedos.layer1_extraction.extractor import Claim
    from aedos.layer1_extraction.triage import TriageDecision
    return Claim(
        claim_id=claim_id, subject="s", predicate="p", object="o", polarity=1,
        source_text="t", asserting_party="user_test", triage_decision=TriageDecision.VERIFY,
    )


def _walk_result(verdict="verified", tier_u_row_id=None):
    """A minimal WalkResult whose trace carries a retractable tier_u row id —
    the aggregator's _extract_source_rows pulls it into source_rows, which the
    verdict_recorded audit event then persists."""
    from aedos.layer4_sources.walker import WalkResult
    from aedos.layer5_result.trace import JustificationTrace, TraceEdge, TraceNode
    root = TraceNode("claim", {})
    trace = JustificationTrace(root=root)
    if tier_u_row_id is not None:
        trace.edges.append(TraceEdge(
            edge_type="premise_lookup", source=root,
            target=TraceNode("tier_u_row", {}),
            metadata={"tier_u_row_id": tier_u_row_id},
        ))
    return WalkResult(verdict=verdict, trace=trace)


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
# v0.16 WS3 §3E: lazy staleness, scoped to *_given_assertion verdicts
# ---------------------------------------------------------------------------

class TestRetractionStaleScoping:
    """propagate_retraction marks dependent *_given_assertion verdicts STALE
    (lazy re-derivation) but leaves base verdicts un-staled — asymmetric trust:
    a Tier U premise correction can invalidate an assertion-conditional verdict,
    but a base verified/contradicted verdict is externally grounded and stands.
    The dependency on a base verdict is still RECORDED in the returned
    VerdictRetraction (for audit), just with stale=False."""

    def test_given_assertion_verdict_marked_stale(self):
        prop = RetractionPropagator()
        prop.record_verdict_trace("c1", "verified_given_assertion", [("tier_u", 7)])
        out = prop.propagate_retraction("tier_u", 7)
        assert len(out) == 1
        assert out[0].stale is True
        assert out[0].scoped_given_assertion is True
        assert prop.is_stale("c1") is True

    def test_base_verdict_recorded_but_not_staled(self):
        prop = RetractionPropagator()
        prop.record_verdict_trace("c2", "verified", [("tier_u", 8)])
        out = prop.propagate_retraction("tier_u", 8)
        # Returned (audit) but NOT staled.
        assert len(out) == 1
        assert out[0].verdict == "verified"
        assert out[0].stale is False
        assert out[0].scoped_given_assertion is False
        assert prop.is_stale("c2") is False

    def test_contradicted_given_assertion_also_staled(self):
        prop = RetractionPropagator()
        prop.record_verdict_trace("c3", "contradicted_given_assertion", [("tier_u", 9)])
        out = prop.propagate_retraction("tier_u", 9)
        assert out[0].stale is True
        assert prop.is_stale("c3") is True

    def test_clear_stale_resets_flag(self):
        prop = RetractionPropagator()
        prop.record_verdict_trace("c1", "abstained_given_assertion", [("tier_u", 1)])
        prop.propagate_retraction("tier_u", 1)
        assert prop.is_stale("c1") is True
        prop.clear_stale("c1")
        assert prop.is_stale("c1") is False
        # Idempotent: clearing an already-clear claim is a no-op, not an error.
        prop.clear_stale("c1")
        assert prop.is_stale("c1") is False

    def test_is_stale_false_for_unknown_claim(self):
        prop = RetractionPropagator()
        assert prop.is_stale("never_seen") is False

    def test_unmatched_row_does_not_stale(self):
        # A retraction of a row the verdict does NOT depend on leaves it fresh.
        prop = RetractionPropagator()
        prop.record_verdict_trace("c1", "verified_given_assertion", [("tier_u", 7)])
        out = prop.propagate_retraction("tier_u", 999)
        assert out == []
        assert prop.is_stale("c1") is False

    def test_stale_marking_persists_verdict_retracted_audit_event(self, tmp_path):
        # The stale flag is carried into the verdict_retracted audit event so an
        # out-of-process reader can see which verdicts went stale.
        db = open_db(str(tmp_path / "aedos.db"))
        prop = RetractionPropagator(db=db)
        prop.record_verdict_trace("c1", "verified_given_assertion", [("tier_u", 5)])
        prop.propagate_retraction("tier_u", 5)
        import json
        row = db.execute(
            "SELECT event_data FROM audit_log WHERE event_type='verdict_retracted' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert json.loads(row["event_data"])["stale"] is True
        db.close()


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


# ---------------------------------------------------------------------------
# RetractionPropagator.replay — B4 / D6 cross-process persistence
# ---------------------------------------------------------------------------

class TestRetractionPropagatorReplay:
    """D6: replay() rehydrates the trace index from persisted verdict_recorded
    audit events, so retraction propagation survives a process restart
    (architecture 7.3 over-time soundness).

    The process boundary is simulated as a new SQLite connection to the same
    file plus a fresh RetractionPropagator with an empty index — the
    architecturally meaningful boundary for option beta (the persistence medium
    is the file; the volatile state is the propagator's dict). A single test
    process cannot fork a real OS process; concurrent multi-process writers are
    out of D6's scope."""

    def test_cross_process_retraction_via_replay(self, tmp_path):
        db_path = str(tmp_path / "aedos.db")

        # "Process 1": the aggregator records two verdicts; each emits a
        # verdict_recorded audit event (carrying source_rows) into the db file.
        conn1 = open_db(db_path)
        agg = Aggregator(retraction_propagator=RetractionPropagator(db=conn1), db=conn1)
        agg.aggregate(
            [_claim("c1"), _claim("c2")],
            [_walk_result("verified", tier_u_row_id=5),
             _walk_result("verified", tier_u_row_id=9)],
        )
        conn1.close()

        # "Process 2": a fresh connection and a fresh propagator — empty index.
        conn2 = open_db(db_path)
        prop2 = RetractionPropagator(db=conn2)

        # Discriminator (the in-test stash-and-verify): without replay() the
        # process-1 verdicts are invisible — retracting their row propagates to
        # nothing.
        assert prop2.propagate_retraction("tier_u", 5) == []

        # Startup replay rehydrates the index from the persisted events.
        assert prop2.replay() == 2

        # After replay, retraction reaches the verdict recorded in process 1.
        retracted = prop2.propagate_retraction("tier_u", 5)
        assert [r.claim_id for r in retracted] == ["c1"]
        conn2.close()

    def test_replay_reconstructs_index_faithfully(self, tmp_path):
        # Replay reproduces exactly what record_verdict_trace would have built:
        # no verdict missing, none hallucinated, verdict + source rows intact.
        db_path = str(tmp_path / "aedos.db")
        conn1 = open_db(db_path)
        agg = Aggregator(retraction_propagator=RetractionPropagator(db=conn1), db=conn1)
        agg.aggregate(
            [_claim("c1"), _claim("c2")],
            [_walk_result("verified", tier_u_row_id=5),
             _walk_result("contradicted", tier_u_row_id=9)],
        )
        conn1.close()

        conn2 = open_db(db_path)
        prop2 = RetractionPropagator(db=conn2)
        prop2.replay()
        assert set(prop2._trace_index) == {"c1", "c2"}
        assert prop2._trace_index["c1"] == [("tier_u", 5)]
        assert prop2._verdict_index["c1"] == "verified"
        assert prop2._verdict_index["c2"] == "contradicted"
        conn2.close()

    def test_replay_with_no_events_is_noop(self, tmp_path):
        conn = open_db(str(tmp_path / "aedos.db"))
        prop = RetractionPropagator(db=conn)
        assert prop.replay() == 0
        assert prop._trace_index == {}
        conn.close()

    def test_replay_performance_smoke(self, tmp_path):
        # 1000 verdicts: replay + propagate must complete well under a second —
        # a sanity check that the persistence layer is not pathological.
        import time
        db_path = str(tmp_path / "aedos.db")
        conn1 = open_db(db_path)
        agg = Aggregator(retraction_propagator=RetractionPropagator(db=conn1), db=conn1)
        agg.aggregate(
            [_claim(f"c{i}") for i in range(1000)],
            [_walk_result("verified", tier_u_row_id=i) for i in range(1000)],
        )
        conn1.close()

        conn2 = open_db(db_path)
        prop2 = RetractionPropagator(db=conn2)
        start = time.monotonic()
        n = prop2.replay()
        retracted = prop2.propagate_retraction("tier_u", 500)
        elapsed = time.monotonic() - start
        assert n == 1000
        assert len(retracted) == 1
        assert elapsed < 1.0
        conn2.close()
