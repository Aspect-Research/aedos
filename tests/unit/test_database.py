"""Tests for v0.15 database schema."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from aedos.database import (
    TABLE_NAMES,
    create_schema,
    open_db,
    open_memory_db,
)

_SEEDS_FILE = Path(__file__).resolve().parents[2] / "seeds" / "predicate_translation.json"


@pytest.fixture
def conn():
    c = open_memory_db()
    yield c
    c.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


class TestTableExistence:
    def test_tier_u_exists(self, conn):
        assert _table_exists(conn, "tier_u")

    def test_predicate_translation_exists(self, conn):
        assert _table_exists(conn, "predicate_translation")

    def test_subsumption_exists(self, conn):
        assert _table_exists(conn, "subsumption")

    def test_predicate_distribution_exists(self, conn):
        assert _table_exists(conn, "predicate_distribution")

    def test_audit_log_exists(self, conn):
        assert _table_exists(conn, "audit_log")

    def test_consistency_circuit_breaker_exists(self, conn):
        assert _table_exists(conn, "consistency_circuit_breaker")

    def test_entity_resolution_cache_exists(self, conn):
        assert _table_exists(conn, "entity_resolution_cache")

    def test_all_table_names_present(self, conn):
        for name in TABLE_NAMES:
            assert _table_exists(conn, name), f"missing table: {name}"


class TestTierUSchema:
    def test_required_columns(self, conn):
        cols = _column_names(conn, "tier_u")
        required = {
            "id", "asserting_party", "subject", "predicate", "object",
            "polarity", "resolved_subject_id", "resolved_object_id",
            "valid_from", "valid_until", "valid_during_ref",
            "source_text", "source_context", "asserted_at",
            "retracted_at", "retraction_reason",
        }
        assert required.issubset(cols)

    def test_polarity_check_constraint(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tier_u (asserting_party, subject, predicate, object, polarity, source_text, asserted_at) "
                "VALUES ('u', 's', 'p', 'o', 2, 'x', '2026-01-01')"
            )

    def test_not_null_constraints(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tier_u (asserting_party, subject, predicate, object, polarity) "
                "VALUES ('u', 's', 'p', 'o', 1)"
            )


class TestPredicateTranslationSchema:
    def test_required_columns(self, conn):
        cols = _column_names(conn, "predicate_translation")
        required = {
            "id", "aedos_predicate", "object_type", "user_subject_required",
            "distinct_slots", "routing_hint", "kb_namespace", "kb_property",
            "slot_to_qualifier", "single_valued", "reason", "created_at",
            "last_consulted_at", "used_count", "retracted_at", "retraction_reason",
        }
        assert required.issubset(cols)

    def test_bindings_column_present(self, conn):
        # v0.16 M1: predicate_translation gains a `bindings` TEXT (JSON) column
        # holding the multi-property ranked PredicateBinding set. Legacy scalar
        # columns are retained alongside it (read-synthesis when bindings IS
        # NULL), so this is purely additive.
        cols = _column_names(conn, "predicate_translation")
        assert "bindings" in cols

    def test_bindings_column_accepts_json_and_null(self, conn):
        # bindings is nullable (legacy rows synthesize from scalars) and stores
        # JSON text when present.
        conn.execute(
            "INSERT INTO predicate_translation "
            "(aedos_predicate, object_type, routing_hint, reason, created_at, "
            " kb_namespace, kb_property, bindings) "
            "VALUES ('held_position', 'entity', 'kb_resolvable', 'seed', "
            "'2026-01-01', 'wikidata', 'P39', ?)",
            (json.dumps([{"kb_namespace": "wikidata", "kb_property": "P39"}]),),
        )
        conn.execute(
            "INSERT INTO predicate_translation "
            "(aedos_predicate, object_type, routing_hint, reason, created_at, kb_namespace) "
            "VALUES ('legacy_pred', 'entity', 'kb_resolvable', 'seed', '2026-01-01', 'wikidata')"
        )
        conn.commit()
        with_bindings = conn.execute(
            "SELECT bindings FROM predicate_translation WHERE aedos_predicate='held_position'"
        ).fetchone()
        assert json.loads(with_bindings["bindings"])[0]["kb_property"] == "P39"
        legacy = conn.execute(
            "SELECT bindings FROM predicate_translation WHERE aedos_predicate='legacy_pred'"
        ).fetchone()
        assert legacy["bindings"] is None

    def test_unique_predicate_namespace(self, conn):
        conn.execute(
            "INSERT INTO predicate_translation "
            "(aedos_predicate, object_type, routing_hint, reason, created_at, kb_namespace) "
            "VALUES ('holds_role', 'entity', 'kb_resolvable', 'seed', '2026-01-01', 'wikidata')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO predicate_translation "
                "(aedos_predicate, object_type, routing_hint, reason, created_at, kb_namespace) "
                "VALUES ('holds_role', 'entity', 'kb_resolvable', 'dup', '2026-01-01', 'wikidata')"
            )


class TestSubsumptionSchema:
    def test_required_columns(self, conn):
        cols = _column_names(conn, "subsumption")
        required = {
            "id", "entity_a_namespace", "entity_a_identifier",
            "entity_b_namespace", "entity_b_identifier",
            "relation_type", "verdict", "source", "reason", "created_at",
            "last_consulted_at", "used_count", "retracted_at", "retraction_reason",
        }
        assert required.issubset(cols)


class TestPredicateDistributionSchema:
    def test_required_columns(self, conn):
        cols = _column_names(conn, "predicate_distribution")
        required = {
            "id", "aedos_predicate", "polarity", "relation_type", "verdict",
            "reason", "created_at", "last_consulted_at", "used_count",
            "retracted_at", "retraction_reason",
        }
        assert required.issubset(cols)

    def test_polarity_check_constraint(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO predicate_distribution "
                "(aedos_predicate, polarity, relation_type, verdict, reason, created_at) "
                "VALUES ('lives_in', 2, 'part_of', 'distributes_up', 'test', '2026-01-01')"
            )


class TestAuditLogSchema:
    def test_required_columns(self, conn):
        cols = _column_names(conn, "audit_log")
        required = {"id", "event_type", "event_subject", "event_data", "occurred_at", "verification_context"}
        assert required.issubset(cols)


class TestCircuitBreakerSchema:
    def test_required_columns(self, conn):
        cols = _column_names(conn, "consistency_circuit_breaker")
        required = {"id", "question_signature", "cycle_count", "last_triggered_at", "unresolvable", "unresolvable_reason"}
        assert required.issubset(cols)

    def test_unique_signature(self, conn):
        conn.execute(
            "INSERT INTO consistency_circuit_breaker (question_signature, cycle_count, last_triggered_at) "
            "VALUES ('sig1', 0, '2026-01-01')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO consistency_circuit_breaker (question_signature, cycle_count, last_triggered_at) "
                "VALUES ('sig1', 1, '2026-01-02')"
            )


class TestEntityResolutionCacheSchema:
    def test_required_columns(self, conn):
        cols = _column_names(conn, "entity_resolution_cache")
        required = {
            "id", "reference", "local_context_signature",
            "resolved_kb_namespace", "resolved_kb_identifier",
            "provenance", "created_at", "last_used_at", "used_count",
            "retracted_at", "retraction_reason",
        }
        assert required.issubset(cols)


# ---------------------------------------------------------------------------
# v0.16 M2: property_relations — cached Wikidata property ontology used to
# discover candidate PredicateBindings. WS1-canonical schema (§0.4 #2).
# ---------------------------------------------------------------------------

class TestPropertyRelationsSchema:
    def test_table_exists(self, conn):
        assert _table_exists(conn, "property_relations")

    def test_required_columns(self, conn):
        cols = _column_names(conn, "property_relations")
        required = {
            "id", "kb_namespace", "kb_property", "relation_type",
            "related_value", "source", "created_at", "last_consulted_at",
            "used_count", "retracted_at", "retraction_reason",
        }
        assert required.issubset(cols)

    def test_unique_constraint(self, conn):
        conn.execute(
            "INSERT INTO property_relations "
            "(kb_namespace, kb_property, relation_type, related_value, source, created_at) "
            "VALUES ('wikidata', 'P39', 'value_type_constraint', 'Q4164871', "
            "'wikidata_p2302', '2026-01-01')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO property_relations "
                "(kb_namespace, kb_property, relation_type, related_value, source, created_at) "
                "VALUES ('wikidata', 'P39', 'value_type_constraint', 'Q4164871', "
                "'wikidata_p2302', '2026-01-02')"
            )


# ---------------------------------------------------------------------------
# v0.16 M3: substrate_exceptions — bounded nogood/exception cache. WS3-
# canonical schema (§0.3 / §0.4 #1).
# ---------------------------------------------------------------------------

class TestSubstrateExceptionsSchema:
    def test_table_exists(self, conn):
        assert _table_exists(conn, "substrate_exceptions")

    def test_required_columns(self, conn):
        cols = _column_names(conn, "substrate_exceptions")
        required = {
            "id", "exception_kind", "relation_type", "property_path",
            "source_identifier", "target_identifier", "reason", "created_at",
            "last_consulted_at", "used_count", "retracted_at", "retraction_reason",
        }
        assert required.issubset(cols)

    def test_unique_constraint(self, conn):
        conn.execute(
            "INSERT INTO substrate_exceptions "
            "(exception_kind, relation_type, property_path, source_identifier, "
            " target_identifier, reason, created_at) "
            "VALUES ('subsumption', 'part_of', 'P131|P30|P17', 'Q270', 'Q183', "
            "'leak_guard', '2026-01-01')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO substrate_exceptions "
                "(exception_kind, relation_type, property_path, source_identifier, "
                " target_identifier, reason, created_at) "
                "VALUES ('subsumption', 'part_of', 'P131|P30|P17', 'Q270', 'Q183', "
                "'ask_false', '2026-01-02')"
            )


# ---------------------------------------------------------------------------
# TestSingleValuedMigration  (N6: create_schema migrates a pre-fixup DB that
# lacks the single_valued column on predicate_translation)
# ---------------------------------------------------------------------------

class TestSingleValuedMigration:
    def test_alter_table_adds_missing_column(self):
        from aedos.database import create_schema

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # Simulate a pre-fixup database: predicate_translation WITHOUT the
        # single_valued column, holding one existing row.
        conn.execute(
            "CREATE TABLE predicate_translation ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, aedos_predicate TEXT NOT NULL, "
            "object_type TEXT NOT NULL, routing_hint TEXT NOT NULL, "
            "reason TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO predicate_translation "
            "(aedos_predicate, object_type, routing_hint, reason, created_at) "
            "VALUES ('born_in', 'entity', 'kb_resolvable', 'pre-fixup row', '2026-01-01')"
        )
        conn.commit()
        assert "single_valued" not in _column_names(conn, "predicate_translation")

        create_schema(conn)  # the migration guard runs here

        assert "single_valued" in _column_names(conn, "predicate_translation")
        row = conn.execute(
            "SELECT single_valued FROM predicate_translation WHERE aedos_predicate='born_in'"
        ).fetchone()
        assert row["single_valued"] == 0  # the existing row gets the safe default
        conn.close()

    def test_create_schema_idempotent_on_fresh_db(self):
        # On a fresh DB the column already exists from CREATE TABLE; the ALTER
        # raises OperationalError and is swallowed. A second create_schema call
        # must not raise either.
        from aedos.database import create_schema

        conn = open_memory_db()  # already ran create_schema once
        create_schema(conn)      # second call — must not raise
        assert "single_valued" in _column_names(conn, "predicate_translation")
        conn.close()


# ---------------------------------------------------------------------------
# TestTierUStatusMigration (Phase H Cluster 2 step 1): a pre-Cluster-2 DB
# whose tier_u table lacks the `status` column gets the column added; any
# pre-existing rows migrate to `externally_verified` (they pre-date the
# promotion path, so they represent established external knowledge, not
# in-session user assertions). Same idempotent ALTER pattern as the
# single_valued migration.
# ---------------------------------------------------------------------------

class TestTierUStatusMigration:
    def test_fresh_db_has_status_column(self, conn):
        cols = _column_names(conn, "tier_u")
        assert "status" in cols

    def test_status_default_is_asserted_unverified_on_fresh_db(self, conn):
        # CREATE TABLE's DEFAULT applies to new rows that don't specify status.
        conn.execute(
            "INSERT INTO tier_u (asserting_party, subject, predicate, object, polarity, source_text, asserted_at) "
            "VALUES ('u', 's', 'p', 'o', 1, 'x', '2026-01-01')"
        )
        conn.commit()
        row = conn.execute("SELECT status FROM tier_u").fetchone()
        assert row["status"] == "asserted_unverified"

    def test_alter_adds_status_and_migrates_existing_rows(self):
        # Simulate a pre-Cluster-2 database: tier_u without the status
        # column, holding one existing row (representing an operator-
        # seeded or test-fixture-seeded external fact).
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE tier_u ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, asserting_party TEXT NOT NULL, "
            "subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL, "
            "polarity INTEGER NOT NULL CHECK(polarity IN (0,1)), "
            "source_text TEXT NOT NULL, asserted_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO tier_u (asserting_party, subject, predicate, object, polarity, source_text, asserted_at) "
            "VALUES ('operator', 'Asa', 'lives_in', 'Williamstown', 1, 'pre-c2', '2026-01-01')"
        )
        conn.commit()
        assert "status" not in _column_names(conn, "tier_u")

        create_schema(conn)  # migration runs here

        assert "status" in _column_names(conn, "tier_u")
        row = conn.execute(
            "SELECT status FROM tier_u WHERE subject='Asa'"
        ).fetchone()
        # Pre-existing rows migrate to externally_verified (they pre-date
        # the promotion path).
        assert row["status"] == "externally_verified"
        conn.close()

    def test_check_constraint_rejects_invalid_status_on_fresh_db(self, conn):
        # The CREATE TABLE CHECK constraint enforces the three-value enum.
        # (Migrated DBs lose the CHECK because ALTER can't add it; the
        # Python code in TierU.write enforces validity in that case.)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tier_u (asserting_party, subject, predicate, object, polarity, source_text, asserted_at, status) "
                "VALUES ('u', 's', 'p', 'o', 1, 'x', '2026-01-01', 'bogus_value')"
            )


# ---------------------------------------------------------------------------
# Phase H Cluster 3 (2026-05-26): seed loading at DB-open with opt-out.
# ---------------------------------------------------------------------------

def _seed_count() -> int:
    return len(json.loads(_SEEDS_FILE.read_text(encoding="utf-8")))


class TestSeedLoadingOnOpen:
    """The seed-loading discipline introduced by Cluster 3 step 1.

    - `open_db(path)` defaults to `load_seeds=True` — production deployments
      get the seed pack on first open. Re-opening is a no-op for the seed
      table (empty-table gate preserves operator state).
    - `open_memory_db()` defaults to `load_seeds=False` — the test-fixture
      convention is a minimal schema-only DB.
    - Both honor explicit overrides.
    - A successful auto-load emits a `seeds_loaded` audit event with the
      seed file path and entry count, useful for trace reconstruction.
    """

    def test_open_memory_db_default_loads_no_seeds(self):
        conn = open_memory_db()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM predicate_translation"
            ).fetchone()[0]
            assert count == 0
        finally:
            conn.close()

    def test_open_memory_db_with_load_seeds_true_loads_pack(self):
        conn = open_memory_db(load_seeds=True)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM predicate_translation"
            ).fetchone()[0]
            assert count == _seed_count()
        finally:
            conn.close()

    def test_open_db_default_loads_seeds(self, tmp_path):
        db_file = tmp_path / "default.db"
        conn = open_db(str(db_file))
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM predicate_translation"
            ).fetchone()[0]
            assert count == _seed_count()
        finally:
            conn.close()

    def test_open_db_with_load_seeds_false_leaves_empty(self, tmp_path):
        db_file = tmp_path / "cold.db"
        conn = open_db(str(db_file), load_seeds=False)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM predicate_translation"
            ).fetchone()[0]
            assert count == 0
        finally:
            conn.close()

    def test_seeded_db_carries_expected_rows(self):
        conn = open_memory_db(load_seeds=True)
        try:
            row = conn.execute(
                "SELECT routing_hint, kb_property FROM predicate_translation "
                "WHERE aedos_predicate='holds_role'"
            ).fetchone()
            assert row is not None
            assert row["routing_hint"] == "kb_resolvable"
            assert row["kb_property"] == "P39"
        finally:
            conn.close()

    def test_reopen_does_not_clobber_operator_state(self, tmp_path):
        # First open loads the seeds. Operator retracts one row in-place.
        # Re-opening must not undo the retraction — the empty-table gate
        # in _maybe_load_seeds skips loading when rows already exist.
        db_file = tmp_path / "persist.db"
        conn = open_db(str(db_file))
        conn.execute(
            "UPDATE predicate_translation SET retracted_at='2026-01-01' "
            "WHERE aedos_predicate='holds_role'"
        )
        conn.commit()
        conn.close()

        conn2 = open_db(str(db_file))
        try:
            row = conn2.execute(
                "SELECT retracted_at FROM predicate_translation "
                "WHERE aedos_predicate='holds_role'"
            ).fetchone()
            assert row["retracted_at"] == "2026-01-01"
        finally:
            conn2.close()

    def test_seeds_loaded_audit_event_emitted(self):
        conn = open_memory_db(load_seeds=True)
        try:
            rows = conn.execute(
                "SELECT event_type, event_data FROM audit_log "
                "WHERE event_type='seeds_loaded'"
            ).fetchall()
            assert len(rows) == 1
            data = json.loads(rows[0]["event_data"])
            assert data["entries_loaded"] == _seed_count()
            assert "predicate_translation.json" in data["seed_file"]
        finally:
            conn.close()

    def test_no_audit_event_when_not_loading(self):
        conn = open_memory_db()  # default load_seeds=False
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE event_type='seeds_loaded'"
            ).fetchone()[0]
            assert rows == 0
        finally:
            conn.close()
