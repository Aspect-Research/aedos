"""Tests for Tier U — write, lookup, temporal scope, retraction."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aedos.database import open_memory_db
from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.temporal import BEFORE_PRESENT
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer4_sources.tier_u import LookupResult, TierU, WriteResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _claim(
    subject="Asa",
    predicate="holds_role",
    object_val="President",
    polarity=1,
    asserting_party="user_test",
    valid_from=None,
    valid_until=None,
    valid_during_ref=None,
):
    return Claim(
        claim_id="c1",
        subject=subject,
        predicate=predicate,
        object=object_val,
        polarity=polarity,
        source_text="test",
        asserting_party=asserting_party,
        triage_decision=TriageDecision.VERIFY,
        valid_from=valid_from,
        valid_until=valid_until,
        valid_during_ref=valid_during_ref,
    )


def _tier_u():
    db = open_memory_db()
    return TierU(db=db), db


class _NoLLMTransport:
    """Fails loudly if invoked. The single_valued write tests seed every
    predicate_translation row, so consult() must be a pure cache hit."""

    def extract_with_tool(self, *a, **kw):
        raise AssertionError("unexpected LLM call: predicate_translation row not seeded")

    def chat(self, *a, **kw):
        return ""


def _seed_predicate(db, predicate, single_valued):
    db.execute(
        """INSERT INTO predicate_translation
           (aedos_predicate, object_type, routing_hint, single_valued, reason, created_at)
           VALUES (?, 'entity', 'user_authoritative', ?, 'seeded test row', '2026-01-01T00:00:00')""",
        (predicate, single_valued),
    )
    db.commit()


def _tier_u_with_oracle():
    """TierU wired with a predicate_translation oracle so the write path can
    consult single_valued. `born_in` is seeded functional (single_valued=1),
    `occupation` multi-valued (0) — matching the reference seed pack."""
    from aedos.layer3_substrate.predicate_translation import PredicateTranslation
    from aedos.llm.client import LLMClient

    db = open_memory_db()
    oracle = PredicateTranslation(db=db, llm_client=LLMClient(_transport=_NoLLMTransport()))
    _seed_predicate(db, "born_in", 1)
    _seed_predicate(db, "occupation", 0)
    return TierU(db=db, predicate_translation=oracle), db


_PAST = "2020-01-01T00:00:00+00:00"
_NOW_STR = datetime.now(timezone.utc).isoformat()
_FUTURE = "2099-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# TestWriteResult
# ---------------------------------------------------------------------------

class TestWriteResult:
    def test_fields_present(self):
        wr = WriteResult(row_id=1)
        assert wr.row_id == 1
        assert wr.was_idempotent is False
        assert wr.contradiction_closed is False
        assert wr.closed_row_ids == []


# ---------------------------------------------------------------------------
# TestTierUWrite
# ---------------------------------------------------------------------------

class TestTierUWrite:
    def test_write_inserts_row(self):
        tu, db = _tier_u()
        result = tu.write(_claim())
        assert result.row_id > 0
        count = db.execute("SELECT count(*) FROM tier_u").fetchone()[0]
        assert count == 1

    def test_write_returns_row_id(self):
        tu, db = _tier_u()
        result = tu.write(_claim())
        row = db.execute(
            "SELECT id FROM tier_u WHERE id=?", (result.row_id,)
        ).fetchone()
        assert row is not None

    def test_write_idempotent_second_call(self):
        tu, db = _tier_u()
        r1 = tu.write(_claim())
        r2 = tu.write(_claim())
        assert r2.was_idempotent is True
        assert r2.row_id == r1.row_id
        count = db.execute("SELECT count(*) FROM tier_u").fetchone()[0]
        assert count == 1

    def test_write_different_object_no_oracle_keeps_both(self):
        # With no predicate_translation oracle wired, TierU cannot consult
        # single_valued and defaults to multi-valued (the architecture 5.2
        # conservative default): a different-object write is a parallel
        # assertion, not a contradiction — the prior row stays open.
        tu, db = _tier_u()
        r1 = tu.write(_claim(object_val="Minister"))
        r2 = tu.write(_claim(object_val="President"))
        assert r2.contradiction_closed is False
        assert r2.closed_row_ids == []
        count = db.execute("SELECT count(*) FROM tier_u").fetchone()[0]
        assert count == 2
        row = db.execute("SELECT valid_until FROM tier_u WHERE id=?", (r1.row_id,)).fetchone()
        assert row["valid_until"] is None

    def test_write_different_polarity_closes_prior(self):
        tu, db = _tier_u()
        r1 = tu.write(_claim(polarity=1))
        r2 = tu.write(_claim(polarity=0))
        assert r2.contradiction_closed is True

    def test_write_different_predicate_no_conflict(self):
        tu, db = _tier_u()
        tu.write(_claim(predicate="holds_role"))
        result = tu.write(_claim(predicate="employed_by"))
        assert result.was_idempotent is False
        assert result.contradiction_closed is False
        count = db.execute("SELECT count(*) FROM tier_u").fetchone()[0]
        assert count == 2

    def test_write_stores_source_text(self):
        tu, db = _tier_u()
        c = _claim()
        c.source_text = "Asa is the President"
        tu.write(c)
        row = db.execute("SELECT source_text FROM tier_u LIMIT 1").fetchone()
        assert row["source_text"] == "Asa is the President"

    def test_write_stores_valid_from(self):
        tu, db = _tier_u()
        tu.write(_claim(valid_from="2020"))
        row = db.execute("SELECT valid_from FROM tier_u LIMIT 1").fetchone()
        assert row["valid_from"] == "2020"


# ---------------------------------------------------------------------------
# TestTierUWriteSingleValued — B3 / D16: the write path consults single_valued
# ---------------------------------------------------------------------------

class TestTierUWriteSingleValued:
    """The write path closes a prior row only on a genuine contradiction:
    same object + opposite polarity, or a functional predicate's different
    object at the same positive polarity. Multi-valued differences and
    contrastive corrections are parallel assertions."""

    def test_functional_object_conflict_closes_prior(self):
        # born_in is functional: a second birthplace revises the first.
        tu, db = _tier_u_with_oracle()
        r1 = tu.write(_claim(predicate="born_in", object_val="NYC"))
        r2 = tu.write(_claim(predicate="born_in", object_val="Boston"))
        assert r2.contradiction_closed is True
        assert r1.row_id in r2.closed_row_ids
        row = db.execute("SELECT valid_until FROM tier_u WHERE id=?", (r1.row_id,)).fetchone()
        assert row["valid_until"] is not None

    def test_multi_valued_object_difference_keeps_both(self):
        # occupation is multi-valued: a person may hold several — both rows
        # stay open, nothing is closed.
        tu, db = _tier_u_with_oracle()
        tu.write(_claim(predicate="occupation", object_val="teacher"))
        r2 = tu.write(_claim(predicate="occupation", object_val="lawyer"))
        assert r2.contradiction_closed is False
        assert r2.closed_row_ids == []
        open_count = db.execute(
            "SELECT count(*) FROM tier_u WHERE valid_until IS NULL AND retracted_at IS NULL"
        ).fetchone()[0]
        assert open_count == 2

    def test_functional_idempotent_write_no_new_row(self):
        # An exact re-write of a functional claim is idempotent: the
        # idempotency check short-circuits before the closure logic.
        tu, db = _tier_u_with_oracle()
        r1 = tu.write(_claim(predicate="born_in", object_val="NYC"))
        r2 = tu.write(_claim(predicate="born_in", object_val="NYC"))
        assert r2.was_idempotent is True
        assert r2.row_id == r1.row_id
        count = db.execute("SELECT count(*) FROM tier_u").fetchone()[0]
        assert count == 1

    def test_contrastive_correction_keeps_both_rows(self):
        # "Born in NYC, not Boston" extracts (born_in, NYC, 1) and
        # (born_in, Boston, 0). The negated half must not close the positive
        # half — different object at a different polarity is compatible even
        # for a functional predicate.
        tu, db = _tier_u_with_oracle()
        r1 = tu.write(_claim(predicate="born_in", object_val="NYC", polarity=1))
        r2 = tu.write(_claim(predicate="born_in", object_val="Boston", polarity=0))
        assert r2.contradiction_closed is False
        row = db.execute("SELECT valid_until FROM tier_u WHERE id=?", (r1.row_id,)).fetchone()
        assert row["valid_until"] is None

    def test_both_negative_object_difference_keeps_both(self):
        # Two negative assertions about different objects of a functional
        # predicate are consistent ("not born in NYC" and "not born in
        # Boston"). The closure rule is guarded to positive claims.
        tu, db = _tier_u_with_oracle()
        r1 = tu.write(_claim(predicate="born_in", object_val="NYC", polarity=0))
        r2 = tu.write(_claim(predicate="born_in", object_val="Boston", polarity=0))
        assert r2.contradiction_closed is False
        row = db.execute("SELECT valid_until FROM tier_u WHERE id=?", (r1.row_id,)).fetchone()
        assert row["valid_until"] is None

    def test_row_closed_emits_audit_event(self):
        from aedos.audit.log import query_events
        tu, db = _tier_u_with_oracle()
        r1 = tu.write(_claim(predicate="born_in", object_val="NYC"))
        tu.write(_claim(predicate="born_in", object_val="Boston"))
        events = query_events(db, event_type="tier_u_row_closed")
        assert len(events) == 1
        assert events[0]["event_subject"] == f"tier_u:{r1.row_id}"

    def test_parallel_assertion_emits_audit_event(self):
        from aedos.audit.log import query_events
        tu, db = _tier_u_with_oracle()
        tu.write(_claim(predicate="occupation", object_val="teacher"))
        tu.write(_claim(predicate="occupation", object_val="lawyer"))
        events = query_events(db, event_type="tier_u_parallel_assertion")
        assert len(events) == 1


# ---------------------------------------------------------------------------
# TestTierULookupStage1
# ---------------------------------------------------------------------------

class TestTierULookupStage1:
    def test_lookup_found_after_write(self):
        tu, _ = _tier_u()
        tu.write(_claim())
        result = tu.lookup(_claim(), current_time=_NOW_STR)
        assert result.found is True

    def test_lookup_miss_when_empty(self):
        tu, _ = _tier_u()
        result = tu.lookup(_claim(), current_time=_NOW_STR)
        assert result.found is False

    def test_lookup_stage_is_1(self):
        tu, _ = _tier_u()
        tu.write(_claim())
        result = tu.lookup(_claim(), current_time=_NOW_STR)
        assert result.stage == 1

    def test_lookup_row_content_correct(self):
        tu, _ = _tier_u()
        tu.write(_claim(subject="Asa", predicate="holds_role", object_val="President"))
        result = tu.lookup(_claim(), current_time=_NOW_STR)
        assert result.rows[0]["subject"] == "Asa"

    def test_lookup_different_asserting_party_miss(self):
        tu, _ = _tier_u()
        tu.write(_claim(asserting_party="user_alice"))
        result = tu.lookup(_claim(asserting_party="user_bob"), current_time=_NOW_STR)
        assert result.found is False


# ---------------------------------------------------------------------------
# TestTierUTemporalScope
# ---------------------------------------------------------------------------

class TestTierUTemporalScope:
    def test_before_present_row_not_returned_as_current(self):
        tu, db = _tier_u()
        # Write a row with valid_until=before_present (historical)
        db.execute(
            """INSERT INTO tier_u
               (asserting_party, subject, predicate, object, polarity,
                valid_until, source_text, asserted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("user_test", "Asa", "holds_role", "President", 1,
             BEFORE_PRESENT, "test", "2020-01-01"),
        )
        db.commit()
        result = tu.lookup(_claim(), current_time=_NOW_STR)
        assert result.found is False
        assert result.historical_only is True

    def test_null_valid_until_row_is_current(self):
        tu, _ = _tier_u()
        tu.write(_claim())  # valid_until is None → currently valid
        result = tu.lookup(_claim(), current_time=_NOW_STR)
        assert result.found is True

    def test_explicit_future_valid_until_is_current(self):
        tu, _ = _tier_u()
        tu.write(_claim(valid_until=_FUTURE))
        result = tu.lookup(_claim(), current_time=_NOW_STR)
        assert result.found is True

    def test_past_valid_until_row_not_returned_as_current(self):
        tu, db = _tier_u()
        db.execute(
            """INSERT INTO tier_u
               (asserting_party, subject, predicate, object, polarity,
                valid_until, source_text, asserted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("user_test", "Asa", "holds_role", "President", 1,
             _PAST, "test", "2018-01-01"),
        )
        db.commit()
        result = tu.lookup(_claim(), current_time=_NOW_STR)
        assert result.found is False


# ---------------------------------------------------------------------------
# TestTierURetraction
# ---------------------------------------------------------------------------

class TestTierURetraction:
    def test_retract_sets_retracted_at(self):
        tu, db = _tier_u()
        result = tu.write(_claim())
        tu.retract(result.row_id, "test reason")
        row = db.execute("SELECT retracted_at FROM tier_u WHERE id=?", (result.row_id,)).fetchone()
        assert row["retracted_at"] is not None

    def test_retracted_row_not_found_in_lookup(self):
        tu, _ = _tier_u()
        result = tu.write(_claim())
        tu.retract(result.row_id, "stale")
        found = tu.lookup(_claim(), current_time=_NOW_STR)
        assert found.found is False

    def test_retract_idempotent_for_nonexistent_row(self):
        tu, _ = _tier_u()
        tu.retract(9999, "nonexistent")  # should not raise


# ---------------------------------------------------------------------------
# TestTierUStage3Broadening
# ---------------------------------------------------------------------------

class TestTierUStatus:
    """Phase H Cluster 2 step 1: row status flag (asserted_unverified /
    externally_verified / contradicted_by_externally_verified)."""

    def test_default_status_is_asserted_unverified(self):
        tu, db = _tier_u()
        result = tu.write(_claim())
        row = db.execute("SELECT status FROM tier_u WHERE id=?", (result.row_id,)).fetchone()
        assert row["status"] == "asserted_unverified"

    def test_explicit_externally_verified_status(self):
        tu, db = _tier_u()
        result = tu.write(_claim(), status="externally_verified")
        row = db.execute("SELECT status FROM tier_u WHERE id=?", (result.row_id,)).fetchone()
        assert row["status"] == "externally_verified"

    def test_invalid_status_raises(self):
        tu, _ = _tier_u()
        with pytest.raises(ValueError, match="invalid tier_u status"):
            tu.write(_claim(), status="bogus")

    def test_status_persists_across_lookup(self):
        tu, _ = _tier_u()
        tu.write(_claim(), status="externally_verified")
        result = tu.lookup(_claim(), current_time=_NOW_STR)
        assert result.found is True
        assert result.rows[0]["status"] == "externally_verified"


class TestTierUMarkExternallyVerified:
    """Phase H Cluster 2 step 1: upgrade path from asserted_unverified
    to externally_verified (Q-Upgrade)."""

    def test_upgrade_asserted_unverified_row(self):
        tu, db = _tier_u()
        wr = tu.write(_claim())  # default asserted_unverified
        upgraded = tu.mark_externally_verified(wr.row_id)
        assert upgraded is True
        row = db.execute("SELECT status FROM tier_u WHERE id=?", (wr.row_id,)).fetchone()
        assert row["status"] == "externally_verified"

    def test_upgrade_idempotent_on_externally_verified(self):
        tu, _ = _tier_u()
        wr = tu.write(_claim(), status="externally_verified")
        upgraded = tu.mark_externally_verified(wr.row_id)
        assert upgraded is False  # already at target status; no-op

    def test_upgrade_skipped_for_contradicted_row(self):
        # contradicted_by_externally_verified cannot be cancelled by an
        # incoherent subsequent upgrade — the KB-wins decision holds.
        tu, db = _tier_u()
        # Seed externally_verified prior + competing user assertion to create
        # a contradicted_by_externally_verified row.
        tu.write(_claim(object_val="NYC", polarity=0), status="externally_verified")
        wr = tu.write(_claim(object_val="NYC", polarity=1))
        assert wr.was_cross_source_contradicted is True
        upgraded = tu.mark_externally_verified(wr.row_id)
        assert upgraded is False
        row = db.execute("SELECT status FROM tier_u WHERE id=?", (wr.row_id,)).fetchone()
        assert row["status"] == "contradicted_by_externally_verified"

    def test_upgrade_emits_audit_event(self):
        from aedos.audit.log import query_events
        tu, db = _tier_u()
        wr = tu.write(_claim())
        tu.mark_externally_verified(wr.row_id, grounding_chain={"kb_statement": "Q1/P31/Q5"})
        events = query_events(db, event_type="tier_u_status_upgraded")
        assert len(events) == 1
        assert events[0]["event_subject"] == f"tier_u:{wr.row_id}"
        assert events[0]["event_data"]["from_status"] == "asserted_unverified"
        assert events[0]["event_data"]["to_status"] == "externally_verified"
        assert events[0]["event_data"]["grounding_chain"] == {"kb_statement": "Q1/P31/Q5"}

    def test_upgrade_nonexistent_row(self):
        tu, _ = _tier_u()
        upgraded = tu.mark_externally_verified(9999)
        assert upgraded is False  # no-op, no raise


class TestTierUCrossSourceContradiction:
    """Phase H Cluster 2 step 1: §"KB wins" — a user assertion that
    would close an externally_verified prior via §6.1 belief revision
    is instead written with contradicted_by_externally_verified status;
    the prior stays open."""

    def test_externally_verified_negation_prior_stays_open(self):
        # Prior externally-verified negative assertion conflicts with new
        # positive assertion (direct negation case). KB-wins: prior stays
        # open; new row gets contradicted_by_externally_verified status.
        tu, db = _tier_u()
        prior = tu.write(_claim(polarity=0), status="externally_verified")
        new = tu.write(_claim(polarity=1))
        assert new.was_cross_source_contradicted is True
        assert prior.row_id in new.cross_source_conflicting_row_ids
        prior_row = db.execute(
            "SELECT valid_until, status FROM tier_u WHERE id=?", (prior.row_id,)
        ).fetchone()
        assert prior_row["valid_until"] is None  # stayed open
        assert prior_row["status"] == "externally_verified"
        new_row = db.execute(
            "SELECT status FROM tier_u WHERE id=?", (new.row_id,)
        ).fetchone()
        assert new_row["status"] == "contradicted_by_externally_verified"

    def test_externally_verified_functional_object_conflict(self):
        # Functional predicate, externally-verified prior with one object,
        # new assertion with a different object. KB-wins: prior stays.
        tu, db = _tier_u_with_oracle()
        prior = tu.write(
            _claim(predicate="born_in", object_val="NYC"),
            status="externally_verified",
        )
        new = tu.write(_claim(predicate="born_in", object_val="Boston"))
        assert new.was_cross_source_contradicted is True
        prior_row = db.execute(
            "SELECT valid_until, status FROM tier_u WHERE id=?", (prior.row_id,)
        ).fetchone()
        assert prior_row["valid_until"] is None
        assert prior_row["status"] == "externally_verified"

    def test_asserted_unverified_prior_still_closes_normally(self):
        # No KB-wins escalation for asserted_unverified priors — §6.1
        # belief revision semantics unchanged.
        tu, db = _tier_u_with_oracle()
        prior = tu.write(_claim(predicate="born_in", object_val="NYC"))  # asserted
        new = tu.write(_claim(predicate="born_in", object_val="Boston"))
        assert new.was_cross_source_contradicted is False
        assert new.contradiction_closed is True
        prior_row = db.execute(
            "SELECT valid_until FROM tier_u WHERE id=?", (prior.row_id,)
        ).fetchone()
        assert prior_row["valid_until"] is not None  # closed

    def test_cross_source_contradicted_row_skipped_on_lookup(self):
        # The new row is in the table for audit but does NOT ground future
        # lookups (behaves like a retracted row for verdict purposes).
        tu, db = _tier_u()
        tu.write(_claim(polarity=0), status="externally_verified")
        new = tu.write(_claim(polarity=1))
        # Re-lookup the positive claim: the contradicted-flagged row should
        # not satisfy the lookup (only the prior negative row would, and the
        # lookup is for positive polarity, so found is False).
        result = tu.lookup(_claim(polarity=1), current_time=_NOW_STR)
        assert result.found is False
        # But the row exists in the table.
        row = db.execute("SELECT id FROM tier_u WHERE id=?", (new.row_id,)).fetchone()
        assert row is not None

    def test_cross_source_contradicted_skipped_in_object_conflict_lookup(self):
        # Same skip rule applies to lookup_object_conflict (the walker's
        # object-conflict belief-revision path must not be triggered by a
        # KB-wins-contradicted row).
        tu, db = _tier_u_with_oracle()
        tu.write(
            _claim(predicate="born_in", object_val="NYC"),
            status="externally_verified",
        )
        new = tu.write(_claim(predicate="born_in", object_val="Boston"))
        assert new.was_cross_source_contradicted is True
        # A subsequent walker query about (Asa, born_in, Boston) running
        # lookup_object_conflict must see the externally-verified NYC row
        # (correct contradiction signal) and NOT the new Boston row (skipped).
        result = tu.lookup_object_conflict(
            _claim(predicate="born_in", object_val="Boston"),
            current_time=_NOW_STR,
        )
        # The NYC row is at the opposite object; lookup_object_conflict
        # returns positive prior rows with a different object — NYC qualifies.
        assert result.found is True
        returned_objects = {r["object"] for r in result.rows}
        assert "NYC" in returned_objects
        # The contradicted Boston row must not appear (its object matches the
        # claim; lookup_object_conflict already excludes same-object, but
        # crucially the contradicted-flag would still skip it via the new rule).

    def test_cross_source_contradiction_emits_audit_event(self):
        from aedos.audit.log import query_events
        tu, db = _tier_u()
        tu.write(_claim(polarity=0), status="externally_verified")
        new = tu.write(_claim(polarity=1))
        events = query_events(db, event_type="cross_source_contradiction")
        assert len(events) == 1
        assert events[0]["event_subject"] == f"tier_u:{new.row_id}"
        assert events[0]["event_data"]["new_row_status"] == "contradicted_by_externally_verified"


class TestTierUStage3Broadening:
    def test_stage3_finds_equivalent_predicate(self):
        from aedos.layer3_substrate.predicate_translation import PredicateTranslation
        from aedos.llm.client import LLMClient

        db = open_memory_db()

        class MockT:
            def extract_with_tool(self, *a, **kw):
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

        oracle = PredicateTranslation(db=db, llm_client=LLMClient(_transport=MockT()))
        tu = TierU(db=db, predicate_translation=oracle)

        # Write a row with predicate "holds_role"
        tu.write(_claim(predicate="holds_role"))
        oracle.consult("holds_role")  # populate oracle cache

        # Insert a neighbor predicate that maps to same kb_property (P39)
        db.execute(
            """INSERT INTO predicate_translation
               (aedos_predicate, object_type, user_subject_required, routing_hint,
                kb_namespace, kb_property, reason, created_at)
               VALUES ('serves_as', 'entity', 0, 'kb_resolvable', 'wikidata', 'P39', 'test', '2026-01-01')"""
        )
        db.commit()

        # Lookup with the equivalent predicate "serves_as" — should find the "holds_role" row via stage 3
        result = tu.lookup(_claim(predicate="serves_as"), current_time=_NOW_STR)
        assert result.found is True
        assert result.stage == 3
