"""Tests for Tier U — write, lookup, temporal scope, retraction."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.aedos_v0_15.database import open_memory_db
from src.aedos_v0_15.layer1_extraction.extractor import Claim
from src.aedos_v0_15.layer1_extraction.temporal import BEFORE_PRESENT
from src.aedos_v0_15.layer1_extraction.triage import TriageDecision
from src.aedos_v0_15.layer4_sources.tier_u import LookupResult, TierU, WriteResult


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
        assert wr.closed_row_id is None


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

    def test_write_different_object_closes_prior(self):
        tu, db = _tier_u()
        r1 = tu.write(_claim(object_val="Minister"))
        r2 = tu.write(_claim(object_val="President"))
        assert r2.contradiction_closed is True
        assert r2.closed_row_id == r1.row_id
        # Prior row now has valid_until set
        row = db.execute("SELECT valid_until FROM tier_u WHERE id=?", (r1.row_id,)).fetchone()
        assert row["valid_until"] is not None

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

class TestTierUStage3Broadening:
    def test_stage3_finds_equivalent_predicate(self):
        from src.aedos_v0_15.layer3_substrate.predicate_translation import PredicateTranslation
        from src.aedos_v0_15.llm.client import LLMClient

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
