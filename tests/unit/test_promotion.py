"""Tests for the assertion-promotion module (Phase H Cluster 2 step 2)."""

from __future__ import annotations

from aedos.audit.log import query_events
from aedos.database import open_memory_db
from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer4_sources.promotion import PromotionResult, promote_assertions
from aedos.layer4_sources.tier_u import TierU


def _claim(
    claim_id="c1",
    subject="Asa",
    predicate="lives_in",
    object_val="Williamstown",
    polarity=1,
    asserting_party="user_test",
):
    return Claim(
        claim_id=claim_id,
        subject=subject,
        predicate=predicate,
        object=object_val,
        polarity=polarity,
        source_text="test",
        asserting_party=asserting_party,
        triage_decision=TriageDecision.VERIFY,
    )


def _tier_u():
    db = open_memory_db()
    return TierU(db=db), db


class TestPromoteAssertionsBasic:
    def test_single_claim_writes_asserted_unverified_row(self):
        tu, db = _tier_u()
        results = promote_assertions([_claim()], tu)
        assert len(results) == 1
        assert results[0].pre_verdict is None  # no contradiction → walker decides
        row = db.execute(
            "SELECT status FROM tier_u WHERE id=?", (results[0].tier_u_row_id,)
        ).fetchone()
        assert row["status"] == "asserted_unverified"

    def test_returns_one_result_per_claim_in_order(self):
        tu, _ = _tier_u()
        claims = [
            _claim(claim_id="c1", subject="Asa", predicate="lives_in", object_val="Williamstown"),
            _claim(claim_id="c2", subject="Asa", predicate="works_at", object_val="Acme"),
            _claim(claim_id="c3", subject="Asa", predicate="holds_role", object_val="student"),
        ]
        results = promote_assertions(claims, tu)
        assert len(results) == 3
        assert [r.claim.claim_id for r in results] == ["c1", "c2", "c3"]

    def test_idempotent_claim_returns_existing_row_id(self):
        tu, db = _tier_u()
        # First promotion writes the row.
        first = promote_assertions([_claim()], tu)
        assert first[0].write_result.was_idempotent is False
        # Second promotion of the same claim is idempotent — same row id,
        # no new row, no pre_verdict.
        second = promote_assertions([_claim()], tu)
        assert second[0].write_result.was_idempotent is True
        assert second[0].tier_u_row_id == first[0].tier_u_row_id
        assert second[0].pre_verdict is None
        count = db.execute("SELECT count(*) FROM tier_u").fetchone()[0]
        assert count == 1

    def test_empty_input_returns_empty_list(self):
        tu, _ = _tier_u()
        results = promote_assertions([], tu)
        assert results == []


class TestPromoteAssertionsMultiClaim:
    """Q-MultiClaim: promote-all then walk-all. After
    promote_assertions returns, every input claim is reflected in
    Tier U as a queryable premise."""

    def test_all_rows_written_before_return(self):
        tu, db = _tier_u()
        claims = [
            _claim(claim_id="c1", subject="Asa", predicate="lives_in", object_val="Williamstown"),
            _claim(claim_id="c2", subject="Asa", predicate="works_at", object_val="Acme"),
            _claim(claim_id="c3", subject="Asa", predicate="holds_role", object_val="student"),
        ]
        promote_assertions(claims, tu)
        # Every claim is queryable in Tier U.
        count = db.execute("SELECT count(*) FROM tier_u").fetchone()[0]
        assert count == 3

    def test_batch_with_self_redundant_claims_dedupes(self):
        # The same claim appearing twice in a batch writes one row;
        # the second is idempotent.
        tu, db = _tier_u()
        claims = [_claim(claim_id="c1"), _claim(claim_id="c2")]  # same content
        results = promote_assertions(claims, tu)
        assert results[0].tier_u_row_id == results[1].tier_u_row_id
        assert results[0].write_result.was_idempotent is False
        assert results[1].write_result.was_idempotent is True
        count = db.execute("SELECT count(*) FROM tier_u").fetchone()[0]
        assert count == 1


class TestPromoteAssertionsCrossSourceContradiction:
    """§"KB wins": a promotion that conflicts with an
    externally_verified prior gets `pre_verdict='contradicted'` (plain
    contradicted, NOT contradicted_given_assertion — the
    contradiction is externally grounded)."""

    def test_contradiction_with_externally_verified_prior(self):
        tu, db = _tier_u()
        # Seed an externally-verified negative prior.
        tu.write(_claim(polarity=0), status="externally_verified")
        # Promote the contradicting positive assertion.
        results = promote_assertions([_claim(polarity=1)], tu)
        # §"KB wins": pre_verdict is `contradicted`, externally grounded.
        assert results[0].pre_verdict == "contradicted"
        assert results[0].write_result.was_cross_source_contradicted is True
        # The new row is in Tier U but flagged so it cannot ground future verdicts.
        row = db.execute(
            "SELECT status FROM tier_u WHERE id=?", (results[0].tier_u_row_id,)
        ).fetchone()
        assert row["status"] == "contradicted_by_externally_verified"

    def test_contradiction_emits_audit_event(self):
        tu, db = _tier_u()
        tu.write(_claim(polarity=0), status="externally_verified")
        promote_assertions([_claim(polarity=1)], tu)
        # `cross_source_contradiction` audit event is emitted by TierU.write
        # itself; promotion does not re-emit duplicates.
        events = query_events(db, event_type="cross_source_contradiction")
        assert len(events) == 1

    def test_no_pre_verdict_when_no_contradiction(self):
        tu, _ = _tier_u()
        # No prior. Promotion is a clean write.
        results = promote_assertions([_claim()], tu)
        assert results[0].pre_verdict is None
        assert results[0].write_result.was_cross_source_contradicted is False

    def test_batch_with_one_contradicted_and_one_clean(self):
        # Mixed batch: one claim contradicts an externally_verified
        # prior, another doesn't. Per-claim pre_verdicts reflect that.
        tu, _ = _tier_u()
        tu.write(
            _claim(subject="Asa", predicate="lives_in", object_val="Williamstown", polarity=0),
            status="externally_verified",
        )
        claims = [
            _claim(claim_id="c1", subject="Asa", predicate="lives_in", object_val="Williamstown", polarity=1),
            _claim(claim_id="c2", subject="Asa", predicate="works_at", object_val="Acme"),
        ]
        results = promote_assertions(claims, tu)
        assert results[0].pre_verdict == "contradicted"  # KB-wins on lives_in
        assert results[1].pre_verdict is None  # works_at has no conflict


class TestPromotionResultDataclass:
    def test_fields_present(self):
        c = _claim()
        pr = PromotionResult(claim=c, tier_u_row_id=1)
        assert pr.claim is c
        assert pr.tier_u_row_id == 1
        assert pr.pre_verdict is None
        assert pr.write_result is None
