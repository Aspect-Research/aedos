"""Tests for SubsumptionOracle — KB-mediated, substrate-row, cold-cache, retraction."""

from __future__ import annotations

import pytest

from aedos.database import open_memory_db
from aedos.layer3_substrate.subsumption import (
    EntityRef,
    SubsumptionOracle,
    SubsumptionOracleError,
    SubsumptionVerdictType,
)
from aedos.layer4_sources.kb_protocol import SubsumptionResult
from aedos.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockKB:
    def __init__(self, verdict="a_subsumed_by_b", chain=None):
        self._verdict = verdict
        self._chain = chain or ["Q43229"]
        self.call_count = 0

    def subsumption(self, entity_a, entity_b, relation_type):
        self.call_count += 1
        return SubsumptionResult(
            verdict=self._verdict,
            establishing_property="P31",
            traversal_chain=self._chain,
        )


class MockTransport:
    def __init__(self, verdict="a_subsumed_by_b"):
        self._verdict = verdict
        self.call_count = 0

    def extract_with_tool(self, *a, **kw):
        self.call_count += 1
        return {"verdict": self._verdict, "reason": f"test: {self._verdict}"}

    def chat(self, *a, **kw):
        return ""


def _oracle(kb=None, transport_verdict="a_subsumed_by_b"):
    db = open_memory_db()
    transport = MockTransport(verdict=transport_verdict)
    client = LLMClient(_transport=transport)
    kb_instance = kb or MockKB()
    return SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb_instance), transport, db


def _wikidata(q_id: str) -> EntityRef:
    return EntityRef(namespace="wikidata", identifier=q_id)


def _aedos(identifier: str) -> EntityRef:
    return EntityRef(namespace="aedos", identifier=identifier)


# ---------------------------------------------------------------------------
# TestEntityRef
# ---------------------------------------------------------------------------

class TestEntityRef:
    def test_fields_present(self):
        ref = EntityRef(namespace="wikidata", identifier="Q76")
        assert ref.namespace == "wikidata"
        assert ref.identifier == "Q76"


# ---------------------------------------------------------------------------
# TestSubsumptionOracleKBMediated
# ---------------------------------------------------------------------------

class TestSubsumptionOracleKBMediated:
    def test_kb_called_when_both_wikidata(self):
        oracle, _, _ = _oracle()
        kb = MockKB()
        oracle._kb = kb
        oracle.consult(_wikidata("Q76"), _wikidata("Q5"), "is_a")
        assert kb.call_count == 1

    def test_kb_mediated_returns_correct_verdict(self):
        oracle, _, _ = _oracle(kb=MockKB(verdict="a_subsumed_by_b"))
        result = oracle.consult(_wikidata("Q76"), _wikidata("Q5"), "is_a")
        assert result.verdict == SubsumptionVerdictType.A_SUBSUMED_BY_B

    def test_kb_mediated_source_is_kb(self):
        oracle, _, _ = _oracle()
        result = oracle.consult(_wikidata("Q76"), _wikidata("Q5"), "is_a")
        assert result.source == "kb"

    def test_kb_mediated_no_row_written(self):
        oracle, _, db = _oracle()
        oracle.consult(_wikidata("Q76"), _wikidata("Q5"), "is_a")
        count = db.execute("SELECT count(*) FROM subsumption").fetchone()[0]
        assert count == 0  # KB-mediated results are not cached as substrate rows

    def test_kb_mediated_has_traversal_chain(self):
        oracle, _, _ = _oracle(kb=MockKB(chain=["Q43229", "Q4830453"]))
        result = oracle.consult(_wikidata("Q76"), _wikidata("Q5"), "is_a")
        assert len(result.traversal_chain) == 2


# ---------------------------------------------------------------------------
# TestSubsumptionOracleSubstrateRow
# ---------------------------------------------------------------------------

class TestSubsumptionOracleSubstrateRow:
    def test_substrate_row_found_no_llm_call(self):
        oracle, transport, db = _oracle()
        db.execute(
            """INSERT INTO subsumption
               (entity_a_namespace, entity_a_identifier, entity_b_namespace, entity_b_identifier,
                relation_type, verdict, source, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("aedos", "Asa", "aedos", "person", "is_a", "a_subsumed_by_b", "substrate", "test", "2026-01-01"),
        )
        db.commit()
        result = oracle.consult(_aedos("Asa"), _aedos("person"), "is_a")
        assert result.source == "substrate"
        assert transport.call_count == 0

    def test_substrate_row_increments_used_count(self):
        oracle, _, db = _oracle()
        db.execute(
            """INSERT INTO subsumption
               (entity_a_namespace, entity_a_identifier, entity_b_namespace, entity_b_identifier,
                relation_type, verdict, source, reason, created_at, used_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            ("aedos", "Asa", "aedos", "person", "is_a", "a_subsumed_by_b", "substrate", "test", "2026-01-01"),
        )
        db.commit()
        oracle.consult(_aedos("Asa"), _aedos("person"), "is_a")
        row = db.execute("SELECT used_count FROM subsumption LIMIT 1").fetchone()
        assert row["used_count"] == 1


# ---------------------------------------------------------------------------
# TestSubsumptionOracleColdCache
# ---------------------------------------------------------------------------

class TestSubsumptionOracleColdCache:
    def test_cold_cache_calls_llm(self):
        oracle, transport, _ = _oracle()
        oracle.consult(_aedos("Asa"), _aedos("person"), "is_a")
        assert transport.call_count == 1

    def test_cold_cache_writes_row(self):
        oracle, _, db = _oracle()
        oracle.consult(_aedos("Asa"), _aedos("person"), "is_a")
        count = db.execute("SELECT count(*) FROM subsumption").fetchone()[0]
        assert count == 1

    def test_cold_cache_warm_on_second_call(self):
        oracle, transport, _ = _oracle()
        oracle.consult(_aedos("Asa"), _aedos("person"), "is_a")
        oracle.consult(_aedos("Asa"), _aedos("person"), "is_a")
        assert transport.call_count == 1  # second call hits substrate

    def test_cold_cache_verdict_stored(self):
        oracle, _, db = _oracle(transport_verdict="unrelated")
        oracle.consult(_aedos("Asa"), _aedos("person"), "is_a")
        row = db.execute("SELECT verdict FROM subsumption LIMIT 1").fetchone()
        assert row["verdict"] == "unrelated"

    def test_llm_error_raises_oracle_error(self):
        db = open_memory_db()
        class FailTransport:
            def extract_with_tool(self, *a, **kw): raise RuntimeError("timeout")
            def chat(self, *a, **kw): return ""
        oracle = SubsumptionOracle(db=db, llm_client=LLMClient(_transport=FailTransport()))
        with pytest.raises(SubsumptionOracleError):
            oracle.consult(_aedos("Asa"), _aedos("person"), "is_a")


# ---------------------------------------------------------------------------
# TestSubsumptionOracleRetraction
# ---------------------------------------------------------------------------

class TestSubsumptionOracleRetraction:
    def test_retract_sets_retracted_at(self):
        oracle, _, db = _oracle()
        result = oracle.consult(_aedos("Asa"), _aedos("person"), "is_a")
        oracle.retract(result.row_id, "test")
        row = db.execute(
            "SELECT retracted_at FROM subsumption WHERE id=?", (result.row_id,)
        ).fetchone()
        assert row["retracted_at"] is not None

    def test_retracted_row_excluded_from_lookup(self):
        oracle, transport, _ = _oracle()
        result = oracle.consult(_aedos("Asa"), _aedos("person"), "is_a")
        oracle.retract(result.row_id, "stale")
        oracle.consult(_aedos("Asa"), _aedos("person"), "is_a")
        assert transport.call_count == 2  # had to re-generate


# ---------------------------------------------------------------------------
# TestSubsumptionOracleQueryNeighbors
# ---------------------------------------------------------------------------

class TestSubsumptionOracleQueryNeighbors:
    def test_query_neighbors_returns_rows(self):
        oracle, _, db = _oracle()
        db.execute(
            """INSERT INTO subsumption
               (entity_a_namespace, entity_a_identifier, entity_b_namespace, entity_b_identifier,
                relation_type, verdict, source, reason, created_at)
               VALUES ('aedos', 'Asa', 'aedos', 'human', 'is_a', 'a_subsumed_by_b', 'substrate', 'test', '2026-01-01')"""
        )
        db.commit()
        results = oracle.query_neighbors(_aedos("Asa"), "is_a")
        assert len(results) == 1

    def test_query_neighbors_excludes_retracted(self):
        oracle, _, db = _oracle()
        db.execute(
            """INSERT INTO subsumption
               (entity_a_namespace, entity_a_identifier, entity_b_namespace, entity_b_identifier,
                relation_type, verdict, source, reason, created_at, retracted_at)
               VALUES ('aedos', 'Asa', 'aedos', 'human', 'is_a', 'a_subsumed_by_b', 'substrate', 'test', '2026-01-01', '2026-01-02')"""
        )
        db.commit()
        results = oracle.query_neighbors(_aedos("Asa"), "is_a")
        assert len(results) == 0


# ---------------------------------------------------------------------------
# TestSubsumptionOracleFindNeighbors  (C2 fix-up: walker taxonomy traversal)
# ---------------------------------------------------------------------------

def _insert_subsumption(db, a, b, relation_type, verdict, retracted=False):
    db.execute(
        """INSERT INTO subsumption
           (entity_a_namespace, entity_a_identifier, entity_b_namespace, entity_b_identifier,
            relation_type, verdict, source, reason, created_at, retracted_at)
           VALUES ('aedos', ?, 'aedos', ?, ?, ?, 'substrate', 'test', '2026-01-01', ?)""",
        (a, b, relation_type, verdict, "2026-01-02" if retracted else None),
    )
    db.commit()


class TestSubsumptionOracleFindNeighbors:
    def test_a_subsumed_by_b_yields_parent_for_a(self):
        oracle, _, db = _oracle()
        _insert_subsumption(db, "Williamstown", "Massachusetts", "part_of", "a_subsumed_by_b")
        neighbors = oracle.find_neighbors(_aedos("Williamstown"), "part_of")
        assert len(neighbors) == 1
        assert neighbors[0].entity.identifier == "Massachusetts"
        assert neighbors[0].direction == "parent"

    def test_a_subsumed_by_b_yields_child_for_b(self):
        oracle, _, db = _oracle()
        _insert_subsumption(db, "Williamstown", "Massachusetts", "part_of", "a_subsumed_by_b")
        neighbors = oracle.find_neighbors(_aedos("Massachusetts"), "part_of")
        assert len(neighbors) == 1
        assert neighbors[0].entity.identifier == "Williamstown"
        assert neighbors[0].direction == "child"

    def test_b_subsumed_by_a_directions(self):
        oracle, _, db = _oracle()
        _insert_subsumption(db, "dog", "poodle", "is_a", "b_subsumed_by_a")
        # poodle is_a dog: querying dog finds poodle as a child
        dog_neighbors = oracle.find_neighbors(_aedos("dog"), "is_a")
        assert [(n.entity.identifier, n.direction) for n in dog_neighbors] == [("poodle", "child")]
        # querying poodle finds dog as a parent
        poodle_neighbors = oracle.find_neighbors(_aedos("poodle"), "is_a")
        assert [(n.entity.identifier, n.direction) for n in poodle_neighbors] == [("dog", "parent")]

    def test_find_neighbors_excludes_retracted(self):
        oracle, _, db = _oracle()
        _insert_subsumption(db, "Williamstown", "Massachusetts", "part_of", "a_subsumed_by_b", retracted=True)
        assert oracle.find_neighbors(_aedos("Williamstown"), "part_of") == []

    def test_find_neighbors_skips_equivalent_and_unrelated(self):
        oracle, _, db = _oracle()
        _insert_subsumption(db, "NYC", "New York City", "is_a", "equivalent")
        _insert_subsumption(db, "NYC", "banana", "is_a", "unrelated")
        assert oracle.find_neighbors(_aedos("NYC"), "is_a") == []

    def test_find_neighbors_filters_relation_type(self):
        oracle, _, db = _oracle()
        _insert_subsumption(db, "Williamstown", "Massachusetts", "part_of", "a_subsumed_by_b")
        assert oracle.find_neighbors(_aedos("Williamstown"), "is_a") == []

    def test_find_neighbors_empty_when_no_rows(self):
        oracle, _, db = _oracle()
        assert oracle.find_neighbors(_aedos("Nowhere"), "part_of") == []
