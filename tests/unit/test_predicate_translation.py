"""Tests for the predicate translation oracle (Phase 2)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest

from aedos.database import open_memory_db
from aedos.llm.client import LLMClient
from aedos.layer3_substrate.predicate_translation import (
    PREDICATE_METADATA_TOOL,
    PredicateMetadata,
    PredicateTranslation,
    PredicateTranslationError,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

class MockTransport:
    """Minimal transport for predicate translation tests."""

    def __init__(self, response: dict | None = None, raise_on_call: Exception | None = None):
        self._response = response or _default_metadata_response()
        self._raise = raise_on_call
        self.call_count = 0

    def extract_with_tool(self, system, user_message, tool, model="", purpose=None):
        self.call_count += 1
        if self._raise is not None:
            raise self._raise
        return self._response

    def chat(self, system, messages, model="", purpose=None):
        return ""


def _default_metadata_response(**overrides) -> dict[str, Any]:
    base = {
        "object_type": "entity",
        "user_subject_required": 0,
        "distinct_slots": None,
        "routing_hint": "kb_resolvable",
        "kb_namespace": "wikidata",
        "kb_property": "P39",
        "slot_to_qualifier": None,
        "reason": "holds_role maps to P39 (position held) in Wikidata.",
    }
    return {**base, **overrides}


def _make_oracle(response: dict | None = None, raise_on_call: Exception | None = None):
    db = open_memory_db()
    transport = MockTransport(response=response, raise_on_call=raise_on_call)
    client = LLMClient(_transport=transport)
    oracle = PredicateTranslation(db=db, llm_client=client)
    return oracle, db, transport


# ---------------------------------------------------------------------------
# TestPredicateMetadataDataclass
# ---------------------------------------------------------------------------

class TestPredicateMetadataDataclass:
    def test_all_fields_present(self):
        m = PredicateMetadata(
            id=1,
            aedos_predicate="holds_role",
            object_type="entity",
            user_subject_required=False,
            distinct_slots=None,
            routing_hint="kb_resolvable",
            kb_namespace="wikidata",
            kb_property="P39",
            slot_to_qualifier=None,
            reason="maps to position held",
            created_at="2026-01-01T00:00:00+00:00",
        )
        assert m.id == 1
        assert m.aedos_predicate == "holds_role"

    def test_optional_fields_default(self):
        m = PredicateMetadata(
            id=1,
            aedos_predicate="p",
            object_type="entity",
            user_subject_required=False,
            distinct_slots=None,
            routing_hint="abstain",
            kb_namespace=None,
            kb_property=None,
            slot_to_qualifier=None,
            reason="reason",
            created_at="2026-01-01T00:00:00+00:00",
        )
        assert m.last_consulted_at is None
        assert m.used_count == 0
        assert m.retracted_at is None
        assert m.retraction_reason is None

    def test_user_subject_required_is_bool(self):
        m = PredicateMetadata(
            id=1, aedos_predicate="p", object_type="entity",
            user_subject_required=True, distinct_slots=None,
            routing_hint="user_authoritative", kb_namespace=None, kb_property=None,
            slot_to_qualifier=None, reason="r", created_at="t",
        )
        assert m.user_subject_required is True


# ---------------------------------------------------------------------------
# TestConsultColdCache
# ---------------------------------------------------------------------------

class TestConsultColdCache:
    def test_cold_cache_triggers_llm_call(self):
        oracle, _, transport = _make_oracle()
        oracle.consult("holds_role")
        assert transport.call_count == 1

    def test_cold_cache_returns_metadata(self):
        oracle, _, _ = _make_oracle()
        meta = oracle.consult("holds_role")
        assert isinstance(meta, PredicateMetadata)
        assert meta.aedos_predicate == "holds_role"

    def test_cold_cache_stores_row_in_db(self):
        oracle, db, _ = _make_oracle()
        oracle.consult("holds_role")
        row = db.execute(
            "SELECT * FROM predicate_translation WHERE aedos_predicate='holds_role'"
        ).fetchone()
        assert row is not None

    def test_routing_hint_stored(self):
        oracle, db, _ = _make_oracle()
        oracle.consult("holds_role")
        row = db.execute(
            "SELECT routing_hint FROM predicate_translation WHERE aedos_predicate='holds_role'"
        ).fetchone()
        assert row["routing_hint"] == "kb_resolvable"

    def test_kb_property_stored(self):
        oracle, db, _ = _make_oracle()
        oracle.consult("holds_role")
        row = db.execute(
            "SELECT kb_property FROM predicate_translation WHERE aedos_predicate='holds_role'"
        ).fetchone()
        assert row["kb_property"] == "P39"

    def test_created_at_populated(self):
        oracle, db, _ = _make_oracle()
        oracle.consult("holds_role")
        row = db.execute(
            "SELECT created_at FROM predicate_translation WHERE aedos_predicate='holds_role'"
        ).fetchone()
        assert row["created_at"] is not None

    def test_user_authoritative_routing(self):
        resp = _default_metadata_response(
            routing_hint="user_authoritative", kb_property=None, kb_namespace=None
        )
        oracle, _, _ = _make_oracle(response=resp)
        meta = oracle.consult("prefers")
        assert meta.routing_hint == "user_authoritative"

    def test_python_routing(self):
        resp = _default_metadata_response(
            routing_hint="python", object_type="quantity",
            kb_property=None, kb_namespace=None
        )
        oracle, _, _ = _make_oracle(response=resp)
        meta = oracle.consult("is_greater_than")
        assert meta.routing_hint == "python"


# ---------------------------------------------------------------------------
# TestConsultWarmCache
# ---------------------------------------------------------------------------

class TestConsultWarmCache:
    def test_warm_cache_no_llm_call(self):
        oracle, _, transport = _make_oracle()
        oracle.consult("holds_role")
        oracle.consult("holds_role")
        assert transport.call_count == 1  # only one LLM call

    def test_warm_cache_returns_same_predicate(self):
        oracle, _, _ = _make_oracle()
        first = oracle.consult("holds_role")
        second = oracle.consult("holds_role")
        assert first.aedos_predicate == second.aedos_predicate

    def test_warm_cache_returns_same_id(self):
        oracle, _, _ = _make_oracle()
        first = oracle.consult("holds_role")
        second = oracle.consult("holds_role")
        assert first.id == second.id

    def test_warm_cache_increments_used_count(self):
        oracle, db, _ = _make_oracle()
        oracle.consult("holds_role")
        oracle.consult("holds_role")
        row = db.execute(
            "SELECT used_count FROM predicate_translation WHERE aedos_predicate='holds_role'"
        ).fetchone()
        assert row["used_count"] >= 1

    def test_different_predicates_both_stored(self):
        oracle, db, transport = _make_oracle()
        oracle.consult("holds_role")
        oracle.consult("born_in")
        assert transport.call_count == 2
        count = db.execute(
            "SELECT count(*) FROM predicate_translation"
        ).fetchone()[0]
        assert count == 2


# ---------------------------------------------------------------------------
# TestRetraction
# ---------------------------------------------------------------------------

class TestRetraction:
    def test_retract_sets_retracted_at(self):
        oracle, db, _ = _make_oracle()
        meta = oracle.consult("holds_role")
        oracle.retract(meta.id, "test retraction")
        row = db.execute(
            "SELECT retracted_at FROM predicate_translation WHERE id=?", (meta.id,)
        ).fetchone()
        assert row["retracted_at"] is not None

    def test_retract_sets_retraction_reason(self):
        oracle, db, _ = _make_oracle()
        meta = oracle.consult("holds_role")
        oracle.retract(meta.id, "test reason")
        row = db.execute(
            "SELECT retraction_reason FROM predicate_translation WHERE id=?", (meta.id,)
        ).fetchone()
        assert row["retraction_reason"] == "test reason"

    def test_retracted_row_excluded_from_consult(self):
        oracle, _, transport = _make_oracle()
        meta = oracle.consult("holds_role")
        oracle.retract(meta.id, "stale")
        # Second consult should trigger a new LLM call (retracted row not usable)
        oracle.consult("holds_role")
        assert transport.call_count == 2

    def test_retracted_row_not_deleted(self):
        oracle, db, _ = _make_oracle()
        meta = oracle.consult("holds_role")
        oracle.retract(meta.id, "stale")
        row = db.execute(
            "SELECT id FROM predicate_translation WHERE id=?", (meta.id,)
        ).fetchone()
        assert row is not None  # row still exists

    def test_retract_nonexistent_row_does_not_raise(self):
        oracle, _, _ = _make_oracle()
        oracle.retract(9999, "nonexistent")  # should not raise

    def test_used_count_updated_before_retraction(self):
        oracle, db, _ = _make_oracle()
        meta = oracle.consult("holds_role")
        oracle.consult("holds_role")
        oracle.retract(meta.id, "done")
        row = db.execute(
            "SELECT used_count FROM predicate_translation WHERE id=?", (meta.id,)
        ).fetchone()
        assert row["used_count"] >= 1


# ---------------------------------------------------------------------------
# TestQueryNeighbors
# ---------------------------------------------------------------------------

class TestQueryNeighbors:
    def test_no_neighbors_when_alone(self):
        oracle, _, _ = _make_oracle()
        oracle.consult("holds_role")
        neighbors = oracle.query_neighbors("holds_role")
        assert neighbors == []

    def test_neighbor_with_same_kb_property(self):
        oracle, _, _ = _make_oracle()
        oracle.consult("holds_role")
        # Directly insert a conflicting row with same kb_property
        oracle._db.execute(
            """INSERT INTO predicate_translation
               (aedos_predicate, object_type, user_subject_required, routing_hint,
                kb_namespace, kb_property, reason, created_at)
               VALUES ('serves_as', 'entity', 0, 'kb_resolvable', 'wikidata', 'P39',
                       'also maps to position held', '2026-01-01')"""
        )
        oracle._db.commit()
        neighbors = oracle.query_neighbors("holds_role")
        assert len(neighbors) == 1
        assert neighbors[0].aedos_predicate == "serves_as"

    def test_no_neighbors_when_kb_property_null(self):
        resp = _default_metadata_response(
            routing_hint="user_authoritative", kb_property=None, kb_namespace=None
        )
        oracle, _, _ = _make_oracle(response=resp)
        oracle.consult("prefers")
        neighbors = oracle.query_neighbors("prefers")
        assert neighbors == []


# ---------------------------------------------------------------------------
# TestAuditLog
# ---------------------------------------------------------------------------

class TestAuditLog:
    def test_creation_event_logged(self):
        db = open_memory_db()
        transport = MockTransport()
        client = LLMClient(_transport=transport)
        oracle = PredicateTranslation(db=db, llm_client=client)
        oracle.consult("holds_role")
        events = db.execute(
            "SELECT * FROM audit_log WHERE event_type='row_created'"
        ).fetchall()
        assert len(events) == 1

    def test_retraction_event_logged(self):
        db = open_memory_db()
        transport = MockTransport()
        client = LLMClient(_transport=transport)
        oracle = PredicateTranslation(db=db, llm_client=client)
        meta = oracle.consult("holds_role")
        oracle.retract(meta.id, "test")
        events = db.execute(
            "SELECT * FROM audit_log WHERE event_type='row_retracted'"
        ).fetchall()
        assert len(events) == 1

    def test_creation_event_contains_predicate(self):
        db = open_memory_db()
        transport = MockTransport()
        client = LLMClient(_transport=transport)
        oracle = PredicateTranslation(db=db, llm_client=client)
        oracle.consult("holds_role")
        event = db.execute(
            "SELECT event_data FROM audit_log WHERE event_type='row_created'"
        ).fetchone()
        data = json.loads(event["event_data"])
        assert data["aedos_predicate"] == "holds_role"


# ---------------------------------------------------------------------------
# TestErrorHandling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_llm_exception_raises_predicate_translation_error(self):
        oracle, _, _ = _make_oracle(raise_on_call=RuntimeError("timeout"))
        with pytest.raises(PredicateTranslationError) as exc_info:
            oracle.consult("holds_role")
        assert exc_info.value.cause == "llm_call_failed"

    def test_missing_object_type_raises(self):
        resp = _default_metadata_response()
        del resp["object_type"]
        oracle, _, _ = _make_oracle(response=resp)
        with pytest.raises(PredicateTranslationError) as exc_info:
            oracle.consult("holds_role")
        assert exc_info.value.cause == "malformed_response"

    def test_missing_routing_hint_raises(self):
        resp = _default_metadata_response()
        del resp["routing_hint"]
        oracle, _, _ = _make_oracle(response=resp)
        with pytest.raises(PredicateTranslationError):
            oracle.consult("holds_role")

    def test_missing_reason_raises(self):
        resp = _default_metadata_response()
        del resp["reason"]
        oracle, _, _ = _make_oracle(response=resp)
        with pytest.raises(PredicateTranslationError):
            oracle.consult("holds_role")

    def test_error_logged(self):
        db = open_memory_db()
        transport = MockTransport(raise_on_call=RuntimeError("error"))
        client = LLMClient(_transport=transport)
        oracle = PredicateTranslation(db=db, llm_client=client)
        with pytest.raises(PredicateTranslationError):
            oracle.consult("holds_role")
        events = db.execute(
            "SELECT * FROM audit_log WHERE event_type='row_generation_failed'"
        ).fetchall()
        assert len(events) == 1

    def test_no_partial_row_stored_on_error(self):
        oracle, db, _ = _make_oracle(raise_on_call=RuntimeError("error"))
        with pytest.raises(PredicateTranslationError):
            oracle.consult("holds_role")
        count = db.execute(
            "SELECT count(*) FROM predicate_translation"
        ).fetchone()[0]
        assert count == 0

    def test_empty_string_reason_raises(self):
        resp = _default_metadata_response(reason="")
        oracle, _, _ = _make_oracle(response=resp)
        with pytest.raises(PredicateTranslationError):
            oracle.consult("holds_role")

    def test_error_predicate_attribute(self):
        oracle, _, _ = _make_oracle(raise_on_call=ValueError("bad"))
        with pytest.raises(PredicateTranslationError) as exc_info:
            oracle.consult("born_in")
        assert exc_info.value.predicate == "born_in"


# ---------------------------------------------------------------------------
# TestToolSchema
# ---------------------------------------------------------------------------

class TestToolSchema:
    def test_tool_name(self):
        assert PREDICATE_METADATA_TOOL["name"] == "generate_predicate_metadata"

    def test_routing_hint_enum(self):
        props = PREDICATE_METADATA_TOOL["input_schema"]["properties"]
        enum_vals = props["routing_hint"]["enum"]
        assert "user_authoritative" in enum_vals
        assert "python" in enum_vals
        assert "kb_resolvable" in enum_vals
        assert "abstain" in enum_vals

    def test_object_type_enum(self):
        props = PREDICATE_METADATA_TOOL["input_schema"]["properties"]
        enum_vals = props["object_type"]["enum"]
        assert "entity" in enum_vals
        assert "quantity" in enum_vals
        assert "time" in enum_vals
        assert "proposition" in enum_vals
        assert "entity_list" in enum_vals
