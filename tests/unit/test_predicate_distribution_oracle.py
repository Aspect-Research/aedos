"""Tests for PredicateDistributionOracle — cold/warm cache, verdicts, retraction."""

from __future__ import annotations

import pytest

from aedos.database import open_memory_db
from aedos.layer3_substrate.predicate_distribution import (
    DistributionVerdictType,
    PredicateDistributionError,
    PredicateDistributionOracle,
)
from aedos.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockTransport:
    def __init__(self, verdict="neither"):
        self._verdict = verdict
        self.call_count = 0

    def extract_with_tool(self, *a, **kw):
        self.call_count += 1
        return {"verdict": self._verdict, "reason": f"test: {self._verdict}"}

    def chat(self, *a, **kw):
        return ""


def _oracle(verdict="neither"):
    db = open_memory_db()
    transport = MockTransport(verdict=verdict)
    client = LLMClient(_transport=transport)
    return PredicateDistributionOracle(db=db, llm_client=client), transport, db


# ---------------------------------------------------------------------------
# TestPredicateDistributionColdCache
# ---------------------------------------------------------------------------

class TestPredicateDistributionColdCache:
    def test_cold_cache_calls_llm(self):
        oracle, transport, _ = _oracle()
        oracle.consult("lives_in", 1, "part_of")
        assert transport.call_count == 1

    def test_cold_cache_writes_row(self):
        oracle, _, db = _oracle()
        oracle.consult("lives_in", 1, "part_of")
        count = db.execute("SELECT count(*) FROM predicate_distribution").fetchone()[0]
        assert count == 1

    def test_warm_cache_no_llm_call(self):
        oracle, transport, _ = _oracle()
        oracle.consult("lives_in", 1, "part_of")
        oracle.consult("lives_in", 1, "part_of")
        assert transport.call_count == 1

    def test_warm_cache_returns_was_cached_true(self):
        oracle, _, _ = _oracle()
        oracle.consult("lives_in", 1, "part_of")
        result = oracle.consult("lives_in", 1, "part_of")
        assert result.was_cached is True

    def test_cold_cache_returns_was_cached_false(self):
        oracle, _, _ = _oracle()
        result = oracle.consult("lives_in", 1, "part_of")
        assert result.was_cached is False

    def test_cold_cache_row_id_set(self):
        oracle, _, _ = _oracle()
        result = oracle.consult("lives_in", 1, "part_of")
        assert result.row_id is not None
        assert result.row_id > 0

    def test_different_predicates_separate_rows(self):
        oracle, transport, db = _oracle()
        oracle.consult("lives_in", 1, "part_of")
        oracle.consult("employed_by", 1, "part_of")
        assert transport.call_count == 2
        count = db.execute("SELECT count(*) FROM predicate_distribution").fetchone()[0]
        assert count == 2

    def test_different_relation_type_separate_rows(self):
        oracle, transport, _ = _oracle()
        oracle.consult("lives_in", 1, "part_of")
        oracle.consult("lives_in", 1, "is_a")
        assert transport.call_count == 2


# ---------------------------------------------------------------------------
# TestPredicateDistributionFourVerdicts
# ---------------------------------------------------------------------------

class TestPredicateDistributionFourVerdicts:
    def test_distributes_up(self):
        oracle, _, _ = _oracle(verdict="distributes_up")
        result = oracle.consult("lives_in", 1, "part_of")
        assert result.verdict == DistributionVerdictType.DISTRIBUTES_UP

    def test_distributes_down(self):
        oracle, _, _ = _oracle(verdict="distributes_down")
        result = oracle.consult("mortal", 1, "is_a")
        assert result.verdict == DistributionVerdictType.DISTRIBUTES_DOWN

    def test_both(self):
        oracle, _, _ = _oracle(verdict="both")
        result = oracle.consult("member_of", 1, "is_a")
        assert result.verdict == DistributionVerdictType.BOTH

    def test_neither(self):
        oracle, _, _ = _oracle(verdict="neither")
        result = oracle.consult("prefers", 1, "is_a")
        assert result.verdict == DistributionVerdictType.NEITHER


# ---------------------------------------------------------------------------
# TestPredicateDistributionRetraction
# ---------------------------------------------------------------------------

class TestPredicateDistributionRetraction:
    def test_retract_sets_retracted_at(self):
        oracle, _, db = _oracle()
        result = oracle.consult("lives_in", 1, "part_of")
        oracle.retract(result.row_id, "test")
        row = db.execute(
            "SELECT retracted_at FROM predicate_distribution WHERE id=?", (result.row_id,)
        ).fetchone()
        assert row["retracted_at"] is not None

    def test_retracted_row_triggers_regeneration(self):
        oracle, transport, _ = _oracle()
        result = oracle.consult("lives_in", 1, "part_of")
        oracle.retract(result.row_id, "stale")
        oracle.consult("lives_in", 1, "part_of")
        assert transport.call_count == 2

    def test_llm_error_raises_distribution_error(self):
        db = open_memory_db()
        class FailTransport:
            def extract_with_tool(self, *a, **kw): raise RuntimeError("fail")
            def chat(self, *a, **kw): return ""
        oracle = PredicateDistributionOracle(db=db, llm_client=LLMClient(_transport=FailTransport()))
        with pytest.raises(PredicateDistributionError):
            oracle.consult("lives_in", 1, "part_of")


# ---------------------------------------------------------------------------
# TestPredicateDistributionQueryNeighbors
# ---------------------------------------------------------------------------

class TestPredicateDistributionQueryNeighbors:
    def test_query_neighbors_returns_both_polarities(self):
        oracle, _, db = _oracle()
        db.execute(
            """INSERT INTO predicate_distribution
               (aedos_predicate, polarity, relation_type, verdict, reason, created_at)
               VALUES ('lives_in', 1, 'part_of', 'distributes_up', 'test', '2026-01-01')"""
        )
        db.execute(
            """INSERT INTO predicate_distribution
               (aedos_predicate, polarity, relation_type, verdict, reason, created_at)
               VALUES ('lives_in', 0, 'part_of', 'neither', 'test', '2026-01-01')"""
        )
        db.commit()
        results = oracle.query_neighbors("lives_in", "part_of")
        assert len(results) == 2

    def test_query_neighbors_excludes_retracted(self):
        oracle, _, db = _oracle()
        db.execute(
            """INSERT INTO predicate_distribution
               (aedos_predicate, polarity, relation_type, verdict, reason, created_at, retracted_at)
               VALUES ('lives_in', 1, 'part_of', 'distributes_up', 'test', '2026-01-01', '2026-01-02')"""
        )
        db.commit()
        results = oracle.query_neighbors("lives_in", "part_of")
        assert len(results) == 0
