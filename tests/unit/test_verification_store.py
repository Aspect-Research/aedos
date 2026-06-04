"""v0.16.2 observability: VerificationStore persist/load round-trip + durability.

Pins the durable, replay-free verification store backing GET /verification/{id}:
every per-claim field round-trips (verdict, templated abstain line, resolved QIDs,
directed-over-enumerate signals, per-claim budget, lossless trace, provenance,
premise footprint); persist is idempotent (delete-then-insert, no orphan
premises); and the record survives a process restart (a fresh connection to the
same DB file). §3.2-neutral surface — no verdict is produced or changed here.
"""
from __future__ import annotations

from aedos.database import open_memory_db, open_db
from aedos.deployment.verification_store import VerificationStore
from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer4_sources.walker import WalkResult, BudgetConsumption
from aedos.layer5_result.aggregator import VerificationResult, ClaimVerdict
from aedos.layer5_result.trace import (
    JustificationTrace, TraceNode, TraceEdge, ProvenanceTerm, ProvenanceLiteral,
)


def _claim(cid, s, p, o, pol=1):
    return Claim(claim_id=cid, subject=s, predicate=p, object=o, polarity=pol,
                 source_text=f"{s} {p} {o}", asserting_party="session:x",
                 triage_decision=TriageDecision.VERIFY)


def _abstain_walk():
    """An abstaining born_in walk: signals + resolved QIDs in walk_metadata, a
    tier_u assertion premise literal."""
    c = _claim("c1", "Obama", "born_in", "Kenya")
    t = JustificationTrace(root=TraceNode("claim", {
        "subject": "Obama", "predicate": "born_in", "object": "Kenya", "polarity": 1}))
    t.walk_metadata.update({
        "functional_entity_predicate": True, "value_known_entity": False,
        "functional_value_known": False, "resolved_subject_qid": "Q76",
        "resolved_value_qid": "Q114", "resolved_subject_cache_row_id": 42,
        "depth_reached": 1,
    })
    t.provenance.add_alternative(ProvenanceTerm.lit(ProvenanceLiteral(
        source="tier_u", table="tier_u", row_id=7,
        status="asserted_unverified", assertion=True)))
    wr = WalkResult(verdict="no_grounding_found", trace=t,
                    abstention_reason="depth_exhausted",
                    budget_consumption=BudgetConsumption(wall_clock_ms=1860.0, llm_calls=0))
    cv = ClaimVerdict(claim_id="c1", claim=c, verdict="no_grounding_found",
                      abstention_reason="depth_exhausted")
    return c, wr, cv


def _vr(claim, cv, trace):
    return VerificationResult(
        claims_extracted=[claim],
        per_claim_verdicts={cv.claim_id: cv.verdict},
        per_claim_traces={cv.claim_id: trace},
        aggregate_metadata={"claim_count": 1, "verified": 0, "contradicted": 0,
                            "abstained": 1, "source_breakdown": {}},
        audit_log_entries=[12],
        text_input={"message": "was obama born in kenya?", "draft": "No."},
        consistency_warnings=[],
        claim_verdicts=[cv],
    )


_EXTRAS = {"final_message": "No.", "intervention_type": "intervene",
           "not_assessed_claims": [], "selection_summary": "central"}


class TestRoundTrip:
    def test_full_payload_round_trips(self):
        c, wr, cv = _abstain_walk()
        store = VerificationStore(open_memory_db())
        store.persist("v1", "session:x", _vr(c, cv, wr.trace), source_kind="chat",
                      created_at="2026-06-04T00:00:00Z", walk_results=[wr],
                      chat_extras=_EXTRAS)
        out = store.load("v1")
        assert out["verification_id"] == "v1"
        assert out["asserting_party"] == "session:x"
        assert out["source_kind"] == "chat"
        assert out["final_message"] == "No."
        assert out["intervention_type"] == "intervene"
        assert out["text_input"] == {"message": "was obama born in kenya?", "draft": "No."}
        assert out["aggregate_metadata"]["abstained"] == 1
        assert out["audit_log_entries"] == [12]
        cl = out["claims"][0]
        assert cl["verdict"] == "no_grounding_found"
        assert cl["abstention_reason"] == "depth_exhausted"
        assert "exhausted" in cl["abstention_line"]
        assert cl["resolved_subject_qid"] == "Q76"
        assert cl["resolved_value_qid"] == "Q114"
        assert cl["resolved_subject_cache_row_id"] == 42
        assert cl["signals"] == {"functional_value_known": False,
                                 "value_known_entity": False,
                                 "functional_entity_predicate": True}
        assert cl["budget"] == {"wall_clock_ms": 1860.0, "llm_calls": 0}
        # lossless trace + budget folded in
        assert cl["trace"]["walk_metadata"]["depth_reached"] == 1
        assert cl["trace"]["budget_consumption"]["wall_clock_ms"] == 1860.0
        assert cl["provenance"]["op"] in ("or", "lit", "and")
        # premise footprint (the retraction reverse-index row)
        assert cl["premises"] == [{
            "source": "tier_u", "source_table": "tier_u", "source_row_id": 7,
            "premise_status": "asserted_unverified", "is_assertion": True}]

    def test_load_missing_returns_none(self):
        store = VerificationStore(open_memory_db())
        assert store.load("nope") is None

    def test_verify_source_kind_and_no_chat_extras(self):
        c, wr, cv = _abstain_walk()
        store = VerificationStore(open_memory_db())
        store.persist("v2", "session:y", _vr(c, cv, wr.trace), source_kind="verify",
                      created_at="2026-06-04T00:00:00Z", walk_results=[wr],
                      chat_extras=None)
        out = store.load("v2")
        assert out["source_kind"] == "verify"
        assert out["final_message"] is None  # /verify has no chat presentation fields


class TestIdempotentRepersist:
    def test_repersist_replaces_children_no_orphan_premises(self):
        c, wr, cv = _abstain_walk()
        store = VerificationStore(open_memory_db())
        vr = _vr(c, cv, wr.trace)
        store.persist("v3", "session:x", vr, source_kind="chat",
                      created_at="t0", walk_results=[wr], chat_extras=_EXTRAS)
        # Re-persist the SAME id with a DIFFERENT premise set (fewer literals).
        wr.trace.provenance = ProvenanceTerm()  # drop all literals
        store.persist("v3", "session:x", vr, source_kind="chat",
                      created_at="t1", walk_results=[wr], chat_extras=_EXTRAS)
        out = store.load("v3")
        # No orphan premise rows from the first persist survive.
        assert out["claims"][0]["premises"] == []
        # Exactly one verification row, one claim row.
        conn = store._conn
        assert conn.execute("SELECT COUNT(*) FROM verification WHERE verification_id='v3'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM verification_claim WHERE verification_id='v3'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM verification_premise WHERE verification_id='v3'").fetchone()[0] == 0


class TestReviewFixes:
    """The adversarial-review must-fixes: full extraction + per-claim actions
    persisted; json.dumps can't silently drop a record on a non-serializable
    metadata value; one bad claim degrades to a partial record, not total loss."""

    def test_extracted_claims_and_per_claim_actions_round_trip(self):
        c, wr, cv = _abstain_walk()
        store = VerificationStore(open_memory_db())
        store.persist(
            "v5", "session:x", _vr(c, cv, wr.trace), source_kind="chat",
            created_at="t0", walk_results=[wr],
            chat_extras={**_EXTRAS, "per_claim_actions": [
                {"claim_id": "c1", "action_type": "abstain", "annotation": "could not verify"}]},
            extracted_claims=[
                {"claim_id": "c1", "subject": "Obama", "predicate": "born_in",
                 "object": "Kenya", "polarity": 1, "abstention_reason": None},
                {"claim_id": "c2", "subject": "x", "predicate": "is", "object": "y",
                 "polarity": 1, "abstention_reason": "not_checkworthy"}],
        )
        out = store.load("v5")
        # The extraction-abstained claim (c2) is captured even though it was never walked.
        assert len(out["extracted_claims"]) == 2
        assert out["extracted_claims"][1]["abstention_reason"] == "not_checkworthy"
        assert out["per_claim_actions"][0]["action_type"] == "abstain"
        assert out["per_claim_actions"][0]["annotation"] == "could not verify"

    def test_non_serializable_metadata_does_not_drop_record(self):
        # An open trace-metadata dict could carry a non-JSON-native value; default=str
        # must coerce it so persist never raises and silently loses the record.
        c, wr, cv = _abstain_walk()
        wr.trace.edges.append(TraceEdge(
            edge_type="premise_lookup", source=wr.trace.root,
            target=TraceNode("kb_statement", {"entity": "Q76"}),
            metadata={"weird": {1, 2, 3}}))  # a set is NOT JSON-native
        store = VerificationStore(open_memory_db())
        store.persist("v6", "session:x", _vr(c, cv, wr.trace), source_kind="chat",
                      created_at="t0", walk_results=[wr], chat_extras=_EXTRAS)
        out = store.load("v6")
        assert out is not None  # record persisted, not dropped
        # The set was stringified by default=str (lossless-enough; never raises).
        assert out["claims"][0]["trace"]["edges"][0]["metadata"]["weird"] == "{1, 2, 3}"

    def test_one_bad_claim_degrades_to_partial_record(self):
        c1, wr1, cv1 = _abstain_walk()
        # A second claim whose .claim is broken (no .subject) -> its insert raises ->
        # caught per-claim; claim 1 + the parent still persist.
        wr2 = WalkResult(verdict="verified", trace=JustificationTrace(root=TraceNode("claim", {})),
                         budget_consumption=BudgetConsumption())
        cv2 = ClaimVerdict(claim_id="c2", claim=object(), verdict="verified")
        vr = VerificationResult(
            claims_extracted=[c1], per_claim_verdicts={"c1": "no_grounding_found", "c2": "verified"},
            per_claim_traces={}, aggregate_metadata={"claim_count": 2}, audit_log_entries=[],
            text_input={}, consistency_warnings=[], claim_verdicts=[cv1, cv2])
        store = VerificationStore(open_memory_db())
        store.persist("v7", "session:x", vr, source_kind="chat", created_at="t0",
                      walk_results=[wr1, wr2], chat_extras=_EXTRAS)
        out = store.load("v7")
        assert out is not None                       # record NOT lost
        assert len(out["claims"]) == 1               # only the good claim persisted
        assert out["claims"][0]["claim_id"] == "c1"


class TestSurvivesRestart:
    def test_record_readable_from_a_fresh_connection(self, tmp_path):
        path = str(tmp_path / "deploy.db")
        c, wr, cv = _abstain_walk()
        conn1 = open_db(path)
        VerificationStore(conn1).persist("v4", "session:z", _vr(c, cv, wr.trace),
                                         source_kind="chat", created_at="t0",
                                         walk_results=[wr], chat_extras=_EXTRAS)
        conn1.close()
        # "Restart": a brand-new connection to the same file.
        conn2 = open_db(path)
        out = VerificationStore(conn2).load("v4")
        conn2.close()
        assert out is not None
        assert out["asserting_party"] == "session:z"
        assert out["claims"][0]["resolved_subject_qid"] == "Q76"
        assert out["claims"][0]["trace"]["budget_consumption"]["wall_clock_ms"] == 1860.0
