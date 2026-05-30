"""Tests for ConsistencyChecker — three inconsistency classes, retract-both, circuit breaker."""

from __future__ import annotations

import json

import pytest

from aedos.database import open_memory_db
from aedos.layer3_substrate.consistency import ConsistencyChecker, ConsistencyResult


# ---------------------------------------------------------------------------
# Helpers — inserting rows directly into DB
# ---------------------------------------------------------------------------

def _insert_pt(db, pred, ns, prop, sq="null"):
    cur = db.execute(
        "INSERT INTO predicate_translation "
        "(aedos_predicate, object_type, routing_hint, kb_namespace, kb_property, slot_to_qualifier, reason, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (pred, "entity", "kb_resolvable", ns, prop, sq, "test", "2026-01-01T00:00:00"),
    )
    db.commit()
    return cur.lastrowid


def _insert_dist(db, pred, polarity, rel, verdict):
    cur = db.execute(
        "INSERT INTO predicate_distribution "
        "(aedos_predicate, polarity, relation_type, verdict, reason, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (pred, polarity, rel, verdict, "test", "2026-01-01T00:00:00"),
    )
    db.commit()
    return cur.lastrowid


def _insert_sub(db, ea_ns, ea_id, eb_ns, eb_id, rel, verdict):
    cur = db.execute(
        "INSERT INTO subsumption "
        "(entity_a_namespace, entity_a_identifier, entity_b_namespace, entity_b_identifier, "
        "relation_type, verdict, source, reason, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (ea_ns, ea_id, eb_ns, eb_id, rel, verdict, "llm", "test", "2026-01-01T00:00:00"),
    )
    db.commit()
    return cur.lastrowid


def _make_fake_conflict(table="predicate_distribution", cls="conflicting_distribution",
                        row_a=1, row_b=2, details=None):
    return ConsistencyResult(
        status="conflict",
        inconsistency_class=cls,
        row_a_id=row_a,
        row_b_id=row_b,
        table=table,
        details=details or {"predicate": "lives_in"},
    )


# ---------------------------------------------------------------------------
# TestTransitiveEquivalenceViolation
# ---------------------------------------------------------------------------

class TestTransitiveEquivalenceViolation:
    def test_single_row_no_conflict(self):
        db = open_memory_db()
        checker = ConsistencyChecker(db)
        row_id = _insert_pt(db, "holds_role", "wikidata", "P39", '{"start": "P580"}')
        result = checker.check_on_write("predicate_translation", row_id)
        assert result.status == "pass"

    def test_different_predicates_same_property_different_sq_conflict(self):
        """Two different predicates both map to P39 but with incompatible slot_to_qualifier."""
        db = open_memory_db()
        checker = ConsistencyChecker(db)
        row_a = _insert_pt(db, "holds_role", "wikidata", "P39", '{"start": "P580"}')
        row_b = _insert_pt(db, "occupied_position", "wikidata", "P39", '{"end": "P582"}')
        result = checker.check_on_write("predicate_translation", row_b)
        assert result.status == "conflict"
        assert result.inconsistency_class == "transitive_equivalence_violation"
        assert result.table == "predicate_translation"
        assert row_a in (result.row_a_id, result.row_b_id)
        assert row_b in (result.row_a_id, result.row_b_id)

    def test_different_predicates_same_sq_no_conflict(self):
        """Two predicates mapping to same property with identical slot_to_qualifier — OK."""
        db = open_memory_db()
        checker = ConsistencyChecker(db)
        row_a = _insert_pt(db, "holds_role", "wikidata", "P39", '{"start": "P580"}')
        row_b = _insert_pt(db, "occupied_position", "wikidata", "P39", '{"start": "P580"}')
        result = checker.check_on_write("predicate_translation", row_b)
        assert result.status == "pass"

    def test_retracted_row_ignored(self):
        db = open_memory_db()
        checker = ConsistencyChecker(db)
        row_a = _insert_pt(db, "holds_role", "wikidata", "P39", '{"start": "P580"}')
        db.execute("UPDATE predicate_translation SET retracted_at='2026-01-01' WHERE id=?", (row_a,))
        db.commit()
        row_b = _insert_pt(db, "occupied_position", "wikidata", "P39", '{"end": "P582"}')
        result = checker.check_on_write("predicate_translation", row_b)
        assert result.status == "pass"

    def test_different_kb_property_no_conflict(self):
        db = open_memory_db()
        checker = ConsistencyChecker(db)
        row_a = _insert_pt(db, "holds_role", "wikidata", "P39", '{"start": "P580"}')
        row_b = _insert_pt(db, "occupied_position", "wikidata", "P131", '{"end": "P582"}')
        result = checker.check_on_write("predicate_translation", row_b)
        assert result.status == "pass"


# ---------------------------------------------------------------------------
# TestInversePredicates  (N5: inverse predicates capital_of / has_capital map
# to the same KB property with subject/object swapped — a legitimate inverse
# pair, not a transitive_equivalence_violation conflict)
# ---------------------------------------------------------------------------

class TestInversePredicates:
    _CAPITAL_OF = json.dumps({"subject": "statement_value", "object": "statement_subject"})
    _HAS_CAPITAL = json.dumps({"subject": "statement_subject", "object": "statement_value"})

    def test_inverse_predicates_on_write_no_conflict(self):
        db = open_memory_db()
        checker = ConsistencyChecker(db)
        _insert_pt(db, "capital_of", "wikidata", "P36", self._CAPITAL_OF)
        row_b = _insert_pt(db, "has_capital", "wikidata", "P36", self._HAS_CAPITAL)
        result = checker.check_on_write("predicate_translation", row_b)
        assert result.status == "pass"

    def test_inverse_predicates_periodic_scan_no_conflict(self):
        # The architecturally-mandated periodic scan (§5.4) must not flag — and
        # so must not retract-both — the hand-curated capital_of / has_capital
        # seeds. Pre-N5 this returned a conflict.
        db = open_memory_db()
        checker = ConsistencyChecker(db)
        _insert_pt(db, "capital_of", "wikidata", "P36", self._CAPITAL_OF)
        _insert_pt(db, "has_capital", "wikidata", "P36", self._HAS_CAPITAL)
        conflicts = checker.check_periodic()
        assert conflicts == []

    def test_swapped_with_extra_divergence_still_conflicts(self):
        # subject/object swapped BUT a third key also diverges — not a clean
        # inverse, so the conflict still fires (direction-awareness is precise,
        # not a blanket exemption for same-property pairs).
        db = open_memory_db()
        checker = ConsistencyChecker(db)
        a = json.dumps({"subject": "statement_value", "object": "statement_subject",
                        "start": "qualifier:P580"})
        b = json.dumps({"subject": "statement_subject", "object": "statement_value",
                        "start": "qualifier:P999"})
        _insert_pt(db, "pred_a", "wikidata", "P100", a)
        row_b = _insert_pt(db, "pred_b", "wikidata", "P100", b)
        result = checker.check_on_write("predicate_translation", row_b)
        assert result.status == "conflict"
        assert result.inconsistency_class == "transitive_equivalence_violation"


# ---------------------------------------------------------------------------
# TestSubsumptionPass
# ---------------------------------------------------------------------------

class TestSubsumptionPass:
    def test_single_row_no_conflict(self):
        db = open_memory_db()
        checker = ConsistencyChecker(db)
        row_id = _insert_sub(db, "aedos", "dog", "aedos", "animal", "is_a", "a_subsumed_by_b")
        result = checker.check_on_write("subsumption", row_id)
        assert result.status == "pass"

    def test_different_entity_pairs_no_conflict(self):
        db = open_memory_db()
        checker = ConsistencyChecker(db)
        row_a = _insert_sub(db, "aedos", "dog", "aedos", "animal", "is_a", "a_subsumed_by_b")
        row_b = _insert_sub(db, "aedos", "cat", "aedos", "animal", "is_a", "a_subsumed_by_b")
        result = checker.check_on_write("subsumption", row_b)
        assert result.status == "pass"


# ---------------------------------------------------------------------------
# TestDistributionPass
# ---------------------------------------------------------------------------

class TestDistributionPass:
    def test_single_row_no_conflict(self):
        db = open_memory_db()
        checker = ConsistencyChecker(db)
        row_id = _insert_dist(db, "lives_in", 1, "part_of", "distributes_up")
        result = checker.check_on_write("predicate_distribution", row_id)
        assert result.status == "pass"

    def test_different_predicates_same_verdict_no_conflict(self):
        db = open_memory_db()
        checker = ConsistencyChecker(db)
        row_a = _insert_dist(db, "lives_in", 1, "part_of", "distributes_up")
        row_b = _insert_dist(db, "works_in", 1, "part_of", "distributes_up")
        result = checker.check_on_write("predicate_distribution", row_b)
        assert result.status == "pass"


# ---------------------------------------------------------------------------
# TestRetractBoth — uses synthetic ConsistencyResult
# ---------------------------------------------------------------------------

class TestRetractBoth:
    def test_resolve_conflict_retracts_both_rows(self):
        db = open_memory_db()
        checker = ConsistencyChecker(db)
        row_a = _insert_dist(db, "lives_in", 1, "part_of", "distributes_up")
        row_b = _insert_dist(db, "works_in", 1, "part_of", "distributes_up")
        conflict = ConsistencyResult(
            status="conflict",
            inconsistency_class="conflicting_distribution",
            row_a_id=row_a,
            row_b_id=row_b,
            table="predicate_distribution",
            details={"predicate_a": "lives_in", "predicate_b": "works_in"},
        )
        checker.resolve_conflict(conflict)
        rows = db.execute(
            "SELECT id, retracted_at FROM predicate_distribution WHERE id IN (?,?)",
            (row_a, row_b),
        ).fetchall()
        assert all(r["retracted_at"] is not None for r in rows)

    def test_retraction_reason_contains_class(self):
        db = open_memory_db()
        checker = ConsistencyChecker(db)
        row_a = _insert_sub(db, "aedos", "dog", "aedos", "animal", "is_a", "a_subsumed_by_b")
        row_b = _insert_sub(db, "aedos", "cat", "aedos", "animal", "is_a", "a_subsumed_by_b")
        conflict = ConsistencyResult(
            status="conflict",
            inconsistency_class="contradicting_subsumption",
            row_a_id=row_a,
            row_b_id=row_b,
            table="subsumption",
            details={"entity_a": "aedos:dog"},
        )
        checker.resolve_conflict(conflict)
        rows = db.execute(
            "SELECT retraction_reason FROM subsumption WHERE id IN (?,?)", (row_a, row_b)
        ).fetchall()
        for r in rows:
            assert r["retraction_reason"] is not None
            assert "consistency_check" in r["retraction_reason"]

    def test_resolve_pass_status_is_noop(self):
        db = open_memory_db()
        checker = ConsistencyChecker(db)
        conflict = ConsistencyResult(status="pass")
        checker.resolve_conflict(conflict)  # must not raise

    def test_periodic_scan_finds_pt_conflict(self):
        db = open_memory_db()
        checker = ConsistencyChecker(db)
        _insert_pt(db, "holds_role", "wikidata", "P39", '{"start": "P580"}')
        _insert_pt(db, "occupied_position", "wikidata", "P39", '{"end": "P582"}')
        conflicts = checker.check_periodic()
        assert len(conflicts) >= 1
        assert all(c.status == "conflict" for c in conflicts)


# ---------------------------------------------------------------------------
# TestCircuitBreaker
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    def _run_resolve_n(self, checker, conflict, n):
        for _ in range(n):
            checker.resolve_conflict(conflict)

    def test_circuit_breaker_increments(self):
        db = open_memory_db()
        checker = ConsistencyChecker(db, config={"circuit_breaker_threshold": 3})
        row_a = _insert_dist(db, "lives_in", 1, "part_of", "distributes_up")
        row_b = _insert_dist(db, "works_in", 1, "part_of", "distributes_up")
        conflict = _make_fake_conflict("predicate_distribution", "conflicting_distribution", row_a, row_b)
        self._run_resolve_n(checker, conflict, 1)
        row = db.execute("SELECT cycle_count, unresolvable FROM consistency_circuit_breaker").fetchone()
        assert row["cycle_count"] == 1
        assert row["unresolvable"] == 0

    def test_circuit_breaker_triggers_at_threshold(self):
        db = open_memory_db()
        checker = ConsistencyChecker(db, config={"circuit_breaker_threshold": 3})
        row_a = _insert_dist(db, "lives_in", 1, "part_of", "distributes_up")
        row_b = _insert_dist(db, "works_in", 1, "part_of", "distributes_up")
        conflict = _make_fake_conflict("predicate_distribution", "conflicting_distribution", row_a, row_b)
        self._run_resolve_n(checker, conflict, 3)
        row = db.execute("SELECT cycle_count, unresolvable FROM consistency_circuit_breaker").fetchone()
        assert row["cycle_count"] == 3
        assert row["unresolvable"] == 1

    def test_circuit_breaker_not_triggered_below_threshold(self):
        db = open_memory_db()
        checker = ConsistencyChecker(db, config={"circuit_breaker_threshold": 3})
        row_a = _insert_dist(db, "lives_in", 1, "part_of", "distributes_up")
        row_b = _insert_dist(db, "works_in", 1, "part_of", "distributes_up")
        conflict = _make_fake_conflict("predicate_distribution", "conflicting_distribution", row_a, row_b)
        self._run_resolve_n(checker, conflict, 2)
        row = db.execute("SELECT unresolvable FROM consistency_circuit_breaker").fetchone()
        assert row["unresolvable"] == 0

    def test_is_unresolvable_returns_true_after_threshold(self):
        db = open_memory_db()
        checker = ConsistencyChecker(db, config={"circuit_breaker_threshold": 2})
        row_a = _insert_dist(db, "lives_in", 1, "part_of", "distributes_up")
        row_b = _insert_dist(db, "works_in", 1, "part_of", "distributes_up")
        conflict = _make_fake_conflict("predicate_distribution", "conflicting_distribution", row_a, row_b)
        self._run_resolve_n(checker, conflict, 2)
        assert checker.is_unresolvable(conflict) is True

    def test_is_unresolvable_returns_false_before_threshold(self):
        db = open_memory_db()
        checker = ConsistencyChecker(db, config={"circuit_breaker_threshold": 5})
        row_a = _insert_dist(db, "lives_in", 1, "part_of", "distributes_up")
        row_b = _insert_dist(db, "works_in", 1, "part_of", "distributes_up")
        conflict = _make_fake_conflict("predicate_distribution", "conflicting_distribution", row_a, row_b)
        self._run_resolve_n(checker, conflict, 4)
        assert checker.is_unresolvable(conflict) is False

    def test_circuit_breaker_custom_threshold(self):
        db = open_memory_db()
        checker = ConsistencyChecker(db, config={"circuit_breaker_threshold": 5})
        row_a = _insert_dist(db, "lives_in", 1, "part_of", "distributes_up")
        row_b = _insert_dist(db, "works_in", 1, "part_of", "distributes_up")
        conflict = _make_fake_conflict("predicate_distribution", "conflicting_distribution", row_a, row_b)
        self._run_resolve_n(checker, conflict, 5)
        row = db.execute("SELECT unresolvable FROM consistency_circuit_breaker").fetchone()
        assert row["unresolvable"] == 1
