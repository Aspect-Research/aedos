"""End-to-end: (text, context) → extraction → routing → walker → VerificationResult."""

from __future__ import annotations

import pytest

from src.aedos_v0_15.database import open_memory_db
from src.aedos_v0_15.layer1_extraction.extractor import Claim
from src.aedos_v0_15.layer1_extraction.triage import TriageDecision
from src.aedos_v0_15.layer3_substrate import Substrate
from src.aedos_v0_15.layer3_substrate.consistency import ConsistencyChecker
from src.aedos_v0_15.layer3_substrate.predicate_distribution import PredicateDistributionOracle
from src.aedos_v0_15.layer3_substrate.predicate_translation import PredicateTranslation
from src.aedos_v0_15.layer3_substrate.resolver import EntityResolver
from src.aedos_v0_15.layer3_substrate.subsumption import SubsumptionOracle
from src.aedos_v0_15.layer4_sources.kb_protocol import ResolutionCandidate, Statement, SubsumptionResult
from src.aedos_v0_15.layer4_sources.kb_verifier import KBVerifier
from src.aedos_v0_15.layer4_sources.python_verifier import PythonVerifier
from src.aedos_v0_15.layer4_sources.tier_u import TierU
from src.aedos_v0_15.layer4_sources.walker import VerificationContext, Walker
from src.aedos_v0_15.layer5_result.aggregator import Aggregator
from src.aedos_v0_15.layer5_result.contradiction_tracer import ContradictionTracer
from src.aedos_v0_15.layer5_result.retraction import RetractionPropagator
from src.aedos_v0_15.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockTransport:
    def extract_with_tool(self, *a, purpose=None, **kw):
        if purpose == "distribution_generation":
            return {"verdict": "neither", "reason": "test"}
        if purpose == "subsumption_generation":
            return {"verdict": "unrelated", "reason": "test"}
        if purpose == "python_code_generation":
            return {"code": "def verify(s, p, o): return True", "reasoning": "test"}
        return {
            "object_type": "entity",
            "user_subject_required": 0,
            "distinct_slots": None,
            "routing_hint": "kb_resolvable",
            "kb_namespace": "wikidata",
            "kb_property": "P39",
            "slot_to_qualifier": None,
            "reason": "test",
        }

    def chat(self, *a, **kw):
        return ""


_RESOLUTIONS = {"Obama": "Q76", "President": "Q11696"}


class MockKB:
    def __init__(self, stmts=None):
        self._stmts = stmts or []

    def resolve_entity(self, r, lc):
        qid = _RESOLUTIONS.get(r)
        return [ResolutionCandidate(qid, score=0.9)] if qid else []

    def lookup_statements(self, e, p):
        return list(self._stmts)

    def subsumption(self, a, b, rt):
        return SubsumptionResult(verdict="unrelated")


def _make_pipeline(kb_stmts=None):
    """Assemble the pipeline with the correctness mechanisms wired together
    (M1, M2): the consistency checker runs on every oracle write, and the
    aggregator records verdict traces with the retraction propagator."""
    db = open_memory_db()
    client = LLMClient(_transport=MockTransport())
    kb = MockKB(kb_stmts)
    propagator = RetractionPropagator(db=db)
    consistency = ConsistencyChecker(db=db, retraction_propagator=propagator)
    pt = PredicateTranslation(db=db, llm_client=client, consistency_checker=consistency)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb, consistency_checker=consistency)
    pd = PredicateDistributionOracle(db=db, llm_client=client, consistency_checker=consistency)
    substrate = Substrate(resolver=resolver, predicate_translation=pt, subsumption=sub, predicate_distribution=pd)
    tier_u = TierU(db=db, predicate_translation=pt)
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    py_verifier = PythonVerifier()  # no LLM: always returns no_terminal_result
    walker = Walker(tier_u=tier_u, kb_verifier=kb_verifier, python_verifier=py_verifier, substrate=substrate)
    aggregator = Aggregator(retraction_propagator=propagator, db=db)
    tracer = ContradictionTracer(db=db, retraction_propagator=propagator)
    return walker, tier_u, aggregator, consistency, propagator, tracer, db


def _claim(claim_id: str = "c1", subject: str = "Obama", predicate: str = "holds_role", object_val: str = "President") -> Claim:
    return Claim(
        claim_id=claim_id,
        subject=subject,
        predicate=predicate,
        object=object_val,
        polarity=1,
        source_text="test",
        asserting_party="user_test",
        triage_decision=TriageDecision.VERIFY,
    )


def _ctx() -> VerificationContext:
    from datetime import datetime, timezone
    return VerificationContext(
        current_time=datetime.now(timezone.utc).isoformat(),
        asserting_party="user_test",
    )


# ---------------------------------------------------------------------------
# TestEndToEndPipeline
# ---------------------------------------------------------------------------

class TestEndToEndPipeline:
    def test_single_claim_tier_u_verified(self):
        walker, tier_u, aggregator, *_ = _make_pipeline()
        c = _claim()
        tier_u.write(c)
        walk_result = walker.walk(c, _ctx())
        vr = aggregator.aggregate([c], [walk_result])
        assert vr.per_claim_verdicts["c1"] == "verified"

    def test_single_claim_kb_verified(self):
        stmts = [Statement(value="Q11696", value_type="entity")]
        walker, _, aggregator, *_ = _make_pipeline(kb_stmts=stmts)
        c = _claim()
        walk_result = walker.walk(c, _ctx())
        vr = aggregator.aggregate([c], [walk_result])
        assert vr.per_claim_verdicts["c1"] == "verified"

    def test_single_claim_no_grounding(self):
        walker, _, aggregator, *_ = _make_pipeline()
        c = _claim()
        walk_result = walker.walk(c, _ctx())
        vr = aggregator.aggregate([c], [walk_result])
        assert vr.per_claim_verdicts["c1"] == "no_grounding_found"

    def test_multiple_claims_mixed_verdicts(self):
        walker, tier_u, aggregator, *_ = _make_pipeline()  # no KB stmts
        c1 = _claim("c1")  # will verify via Tier U
        c2 = _claim("c2", subject="UnknownEntity", predicate="unknown_pred", object_val="Q_NOPE")  # no grounding
        tier_u.write(c1)
        r1 = walker.walk(c1, _ctx())
        r2 = walker.walk(c2, _ctx())
        vr = aggregator.aggregate([c1, c2], [r1, r2])
        assert vr.per_claim_verdicts["c1"] == "verified"
        assert vr.per_claim_verdicts["c2"] == "no_grounding_found"

    def test_verification_result_has_required_fields(self):
        walker, tier_u, aggregator, *_ = _make_pipeline()
        c = _claim()
        tier_u.write(c)
        vr = aggregator.aggregate([c], [walker.walk(c, _ctx())], text_input={"text": "Obama was President"})
        assert vr.claims_extracted is not None
        assert vr.per_claim_verdicts is not None
        assert vr.per_claim_traces is not None
        assert vr.aggregate_metadata is not None
        assert vr.text_input == {"text": "Obama was President"}

    def test_aggregate_metadata_counts_correct(self):
        stmts = [Statement(value="Q11696", value_type="entity")]
        walker, tier_u, aggregator, *_ = _make_pipeline(kb_stmts=stmts)
        c1 = _claim("c1")
        c2 = _claim("c2", subject="Nobody", predicate="unknown")
        r1 = walker.walk(c1, _ctx())
        r2 = walker.walk(c2, _ctx())
        vr = aggregator.aggregate([c1, c2], [r1, r2])
        meta = vr.aggregate_metadata
        assert meta["verified"] + meta["contradicted"] + meta["abstained"] == 2


# ---------------------------------------------------------------------------
# TestConsistencyInPipeline
# ---------------------------------------------------------------------------

class TestConsistencyInPipeline:
    def test_consistency_checker_detects_pt_conflict(self):
        """Two predicates both mapping to P39 with different slot_to_qualifier — detectable conflict."""
        _, _, _, checker, *_ = _make_pipeline()
        db = checker._db
        r_a = db.execute(
            "INSERT INTO predicate_translation "
            "(aedos_predicate, object_type, routing_hint, kb_namespace, kb_property, slot_to_qualifier, reason, created_at) "
            "VALUES ('holds_role', 'entity', 'kb_resolvable', 'wikidata', 'P39', '{\"start\": \"P580\"}', 'test', '2026-01-01T00:00:00')"
        ).lastrowid
        db.commit()
        r_b = db.execute(
            "INSERT INTO predicate_translation "
            "(aedos_predicate, object_type, routing_hint, kb_namespace, kb_property, slot_to_qualifier, reason, created_at) "
            "VALUES ('occupied_position', 'entity', 'kb_resolvable', 'wikidata', 'P39', '{\"end\": \"P582\"}', 'test', '2026-01-01T00:00:00')"
        ).lastrowid
        db.commit()
        result = checker.check_on_write("predicate_translation", r_b)
        assert result.status == "conflict"
        assert result.inconsistency_class == "transitive_equivalence_violation"

    def test_retract_both_after_conflict(self):
        _, _, _, checker, *_ = _make_pipeline()
        db = checker._db
        r_a = db.execute(
            "INSERT INTO predicate_translation "
            "(aedos_predicate, object_type, routing_hint, kb_namespace, kb_property, slot_to_qualifier, reason, created_at) "
            "VALUES ('holds_role2', 'entity', 'kb_resolvable', 'wikidata', 'P100', '{\"a\": \"1\"}', 'test', '2026-01-01T00:00:00')"
        ).lastrowid
        db.commit()
        r_b = db.execute(
            "INSERT INTO predicate_translation "
            "(aedos_predicate, object_type, routing_hint, kb_namespace, kb_property, slot_to_qualifier, reason, created_at) "
            "VALUES ('occupied2', 'entity', 'kb_resolvable', 'wikidata', 'P100', '{\"b\": \"2\"}', 'test', '2026-01-01T00:00:00')"
        ).lastrowid
        db.commit()
        conflict = checker.check_on_write("predicate_translation", r_b)
        assert conflict.status == "conflict"
        checker.resolve_conflict(conflict)
        rows = db.execute(
            "SELECT retracted_at FROM predicate_translation WHERE id IN (?,?)", (r_a, r_b)
        ).fetchall()
        assert all(r["retracted_at"] is not None for r in rows)


# ---------------------------------------------------------------------------
# TestRetractionPropagationInPipeline
# ---------------------------------------------------------------------------

class TestRetractionPropagation:
    def test_registered_trace_retracts_on_row_retraction(self):
        *_, propagator, _, __ = _make_pipeline()
        propagator.record_verdict_trace("c1", "verified", [("tier_u", 100)])
        retractions = propagator.propagate_retraction("tier_u", 100)
        assert len(retractions) == 1
        assert retractions[0].claim_id == "c1"

    def test_contradiction_tracer_retracts_via_propagator(self):
        *_, propagator, tracer, __ = _make_pipeline()
        propagator.record_verdict_trace("c1", "verified", [("tier_u", 200)])
        result = tracer.trace_contradiction("c1", {"source": "user_correction"})
        assert any(r.claim_id == "c1" for r in result)


# ---------------------------------------------------------------------------
# Fix-up (M1, M2, m6): the correctness mechanisms are wired into the pipeline.
# ---------------------------------------------------------------------------

class _ConflictMetadataTransport:
    """Two predicates get conflicting slot_to_qualifier mappings to the same
    kb_property — a transitive_equivalence_violation the on-write consistency
    check must catch."""

    def extract_with_tool(self, *a, purpose=None, **kw):
        user_message = a[1] if len(a) > 1 else kw.get("user_message", "")
        sq = {"start": "P580"} if "alpha" in user_message else {"end": "P582"}
        return {
            "object_type": "entity", "user_subject_required": 0, "distinct_slots": None,
            "routing_hint": "kb_resolvable", "kb_namespace": "wikidata", "kb_property": "P39",
            "slot_to_qualifier": sq, "single_valued": 0, "reason": "test",
        }

    def chat(self, *a, **kw):
        return ""


class _SpyPropagator:
    def __init__(self):
        self.calls = []

    def propagate_retraction(self, table, row_id):
        self.calls.append((table, row_id))
        return []


class TestConsistencyCheckWiring:
    """M1: the consistency check runs on every oracle row write."""

    def test_oracle_write_triggers_consistency_check_and_retracts(self):
        db = open_memory_db()
        client = LLMClient(_transport=_ConflictMetadataTransport())
        checker = ConsistencyChecker(db=db)
        pt = PredicateTranslation(db=db, llm_client=client, consistency_checker=checker)
        # First predicate: no conflict yet. Second: conflicts on slot_to_qualifier.
        pt.consult("alpha_predicate")
        pt.consult("beta_predicate")
        rows = db.execute("SELECT retracted_at FROM predicate_translation").fetchall()
        assert len(rows) == 2
        # The on-write check on the second row detects the conflict and the
        # retract-both policy retracts both predicate_translation rows.
        assert all(r["retracted_at"] is not None for r in rows)

    def test_resolve_conflict_propagates_retraction(self):
        # M1: ConsistencyChecker.resolve_conflict drives the retraction
        # propagator (architecture 5.4 step 2).
        db = open_memory_db()
        spy = _SpyPropagator()
        checker = ConsistencyChecker(db=db, retraction_propagator=spy)
        for pred, sq in (("alpha", '{"start": "P580"}'), ("beta", '{"end": "P582"}')):
            db.execute(
                "INSERT INTO predicate_translation "
                "(aedos_predicate, object_type, routing_hint, kb_namespace, kb_property, "
                "slot_to_qualifier, reason, created_at) "
                "VALUES (?, 'entity', 'kb_resolvable', 'wikidata', 'P39', ?, 't', '2026-01-01')",
                (pred, sq),
            )
        db.commit()
        rows = db.execute("SELECT id FROM predicate_translation ORDER BY id").fetchall()
        conflict = checker.check_on_write("predicate_translation", rows[1]["id"])
        assert conflict.status == "conflict"
        checker.resolve_conflict(conflict)
        assert len(spy.calls) == 2  # both retracted rows propagated


class TestRetractionWiring:
    """M2: the aggregator records verdict traces; ContradictionTracer retracts."""

    def test_aggregator_records_verdict_trace_with_source_rows(self):
        walker, tier_u, aggregator, _, propagator, _, _ = _make_pipeline()
        claim = _claim()
        tier_u.write(claim)
        result = walker.walk(claim, _ctx())
        aggregator.aggregate([claim], [result])
        # The aggregator registered the verdict's trace, and the source rows
        # were extracted from the trace (the walker now carries row ids).
        assert claim.claim_id in propagator._trace_index
        recorded = propagator._trace_index[claim.claim_id]
        assert any(table == "tier_u" for table, _ in recorded)

    def test_retracting_a_recorded_row_propagates_to_the_verdict(self):
        walker, tier_u, aggregator, _, propagator, _, _ = _make_pipeline()
        claim = _claim()
        write = tier_u.write(claim)
        result = walker.walk(claim, _ctx())
        aggregator.aggregate([claim], [result])
        retractions = propagator.propagate_retraction("tier_u", write.row_id)
        assert any(r.claim_id == claim.claim_id for r in retractions)

    def test_contradiction_tracer_issues_retracted_at_update(self):
        walker, tier_u, aggregator, _, propagator, tracer, db = _make_pipeline()
        claim = _claim()
        write = tier_u.write(claim)
        result = walker.walk(claim, _ctx())
        aggregator.aggregate([claim], [result])
        tracer.trace_contradiction(claim.claim_id, {"source": "user_correction"})
        row = db.execute(
            "SELECT retracted_at FROM tier_u WHERE id=?", (write.row_id,)
        ).fetchone()
        assert row["retracted_at"] is not None

    def test_verification_result_audit_log_entries_populated(self):
        # m6: audit_log_entries is no longer a hardcoded [].
        walker, tier_u, aggregator, _, _, _, _ = _make_pipeline()
        claim = _claim()
        tier_u.write(claim)
        result = walker.walk(claim, _ctx())
        vr = aggregator.aggregate([claim], [result])
        assert len(vr.audit_log_entries) == 1
