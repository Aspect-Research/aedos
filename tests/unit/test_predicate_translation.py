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
    PredicateBinding,
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

    def test_entity_types_default_none(self):
        # Phase G D33: new fields default to None (no filter for either slot).
        m = PredicateMetadata(
            id=1, aedos_predicate="p", object_type="entity",
            user_subject_required=False, distinct_slots=None,
            routing_hint="kb_resolvable", kb_namespace="wikidata", kb_property="P39",
            slot_to_qualifier=None, reason="r", created_at="t",
        )
        assert m.subject_entity_types is None
        assert m.object_entity_types is None

    def test_entity_types_populate(self):
        m = PredicateMetadata(
            id=1, aedos_predicate="p", object_type="entity",
            user_subject_required=False, distinct_slots=None,
            routing_hint="kb_resolvable", kb_namespace="wikidata", kb_property="P39",
            slot_to_qualifier=None, reason="r", created_at="t",
            subject_entity_types=["Q5"], object_entity_types=["Q43229"],
        )
        assert m.subject_entity_types == ["Q5"]
        assert m.object_entity_types == ["Q43229"]


# ---------------------------------------------------------------------------
# TestPredicateMetadataBindings (v0.16 WS1)
# ---------------------------------------------------------------------------

class TestPredicateMetadataBindings:
    """v0.16 WS1: PredicateMetadata gains a `bindings` list. A legacy scalar
    construction synthesizes exactly one PredicateBinding mirroring the scalar
    fields; an explicit bindings list round-trips and mirrors bindings[0] back
    onto the scalar accessors."""

    def test_legacy_scalar_synthesizes_one_binding(self):
        # Construct with only the scalar fields (no `bindings=`). __post_init__
        # must synthesize exactly one binding mirroring the scalars.
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
            single_valued=True,
            subject_entity_types=["Q5"],
            object_entity_types=["Q4164871"],
        )
        assert m.bindings  # non-empty
        assert len(m.bindings) == 1
        b = m.bindings[0]
        assert isinstance(b, PredicateBinding)
        assert b.kb_property == m.kb_property == "P39"
        assert b.kb_namespace == "wikidata"
        assert b.single_valued is True
        assert b.subject_entity_types == ["Q5"]
        assert b.object_entity_types == ["Q4164871"]
        assert b.source == "legacy_scalar"

    def test_explicit_bindings_round_trip_and_mirror_scalars(self):
        # Construct with an explicit bindings list; bindings round-trips and
        # bindings[0]'s values mirror back onto the scalar accessors so the
        # ~18 existing scalar readers stay correct.
        primary = PredicateBinding(
            kb_namespace="wikidata",
            kb_property="P106",
            slot_to_qualifier={"start": "P580"},
            single_valued=False,
            subject_entity_types=["Q5"],
            object_entity_types=["Q28640"],
            source="ontology_p2302",
            rank=0.9,
        )
        secondary = PredicateBinding(
            kb_namespace="wikidata",
            kb_property="P39",
            source="oracle",
            rank=0.4,
        )
        m = PredicateMetadata(
            id=2,
            aedos_predicate="works_as",
            object_type="entity",
            user_subject_required=False,
            distinct_slots=None,
            routing_hint="kb_resolvable",
            kb_namespace=None,
            kb_property=None,
            slot_to_qualifier=None,
            reason="r",
            created_at="t",
            bindings=[primary, secondary],
        )
        # bindings round-trips intact (order + count preserved).
        assert m.bindings == [primary, secondary]
        # bindings[0] mirrored onto the scalar accessors.
        assert m.kb_property == "P106"
        assert m.kb_namespace == "wikidata"
        assert m.slot_to_qualifier == {"start": "P580"}
        assert m.subject_entity_types == ["Q5"]
        assert m.object_entity_types == ["Q28640"]


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

    def test_entity_type_fields_in_tool_schema(self):
        # Phase G D33: the oracle's tool schema must accept entity-type
        # fields so cold-start generation can emit them.
        props = PREDICATE_METADATA_TOOL["input_schema"]["properties"]
        assert "subject_entity_types" in props
        assert "object_entity_types" in props

    def test_prompt_guides_entity_type_emission(self):
        # Phase G D33: the system prompt must instruct the LLM on when to
        # emit entity types (and when to leave them null). Regression guard
        # so the prompt narrative doesn't silently lose this guidance.
        from aedos.layer3_substrate.predicate_translation import (
            _GENERATION_SYSTEM_PROMPT,
        )
        assert "subject_entity_types" in _GENERATION_SYSTEM_PROMPT
        assert "object_entity_types" in _GENERATION_SYSTEM_PROMPT
        # Must mention the null/open-type path so the LLM doesn't always
        # guess a list when the slot is open.
        assert "null" in _GENERATION_SYSTEM_PROMPT.lower()


# ---------------------------------------------------------------------------
# TestEntityTypesRoundTrip (Phase G D33)
# ---------------------------------------------------------------------------

class TestEntityTypesRoundTrip:
    """Phase G D33: oracle accepts entity-type fields from the LLM, persists
    them, and surfaces them through consult()."""

    def test_cold_start_persists_entity_types(self):
        resp = _default_metadata_response(
            subject_entity_types=["Q5"],
            object_entity_types=["Q43229"],
        )
        oracle, db, _ = _make_oracle(response=resp)
        meta = oracle.consult("holds_role")
        assert meta.subject_entity_types == ["Q5"]
        assert meta.object_entity_types == ["Q43229"]
        # Verify it's actually in the DB
        row = db.execute(
            "SELECT subject_entity_types, object_entity_types "
            "FROM predicate_translation WHERE aedos_predicate='holds_role'"
        ).fetchone()
        assert json.loads(row["subject_entity_types"]) == ["Q5"]
        assert json.loads(row["object_entity_types"]) == ["Q43229"]

    def test_warm_cache_returns_entity_types(self):
        resp = _default_metadata_response(
            subject_entity_types=["Q5"],
            object_entity_types=["Q43229"],
        )
        oracle, _, _ = _make_oracle(response=resp)
        oracle.consult("holds_role")
        # Second call goes through _fetch / _row_to_metadata
        meta = oracle.consult("holds_role")
        assert meta.subject_entity_types == ["Q5"]
        assert meta.object_entity_types == ["Q43229"]

    def test_absent_entity_types_default_to_none(self):
        # Cold-start LLM that doesn't return the fields → metadata has None
        resp = _default_metadata_response()
        oracle, _, _ = _make_oracle(response=resp)
        meta = oracle.consult("holds_role")
        assert meta.subject_entity_types is None
        assert meta.object_entity_types is None

    def test_null_entity_types_persist_as_null(self):
        # Explicit None in LLM response → stored as NULL, returned as None
        resp = _default_metadata_response(
            subject_entity_types=None,
            object_entity_types=None,
        )
        oracle, db, _ = _make_oracle(response=resp)
        oracle.consult("holds_role")
        row = db.execute(
            "SELECT subject_entity_types, object_entity_types "
            "FROM predicate_translation WHERE aedos_predicate='holds_role'"
        ).fetchone()
        assert row["subject_entity_types"] is None
        assert row["object_entity_types"] is None


# ---------------------------------------------------------------------------
# v0.16 WS1 binding discovery (PropertyRelations + SLING)
# ---------------------------------------------------------------------------

from aedos.layer3_substrate.property_relations import PropertyOntology
from aedos.layer3_substrate.predicate_translation import PredicateBinding


class _StubPropertyRelations:
    """Test double for PropertyRelations. Returns a pre-canned PropertyOntology
    per kb_property; an unknown property yields an empty ontology (the
    fall-open case). Records which properties were fetched."""

    def __init__(self, by_property: dict[str, PropertyOntology]):
        self._by_property = by_property
        self.fetched: list[str] = []

    def fetch(self, kb_property, kb_namespace="wikidata") -> PropertyOntology:
        self.fetched.append(kb_property)
        return self._by_property.get(kb_property, PropertyOntology())


class _StubSling:
    """Test double for SlingFallback. Returns a pre-canned list of bindings;
    records calls. propose_bindings is the only method discovery calls."""

    def __init__(self, bindings: list[PredicateBinding] | None = None):
        self._bindings = bindings or []
        self.calls = 0

    def propose_bindings(self, predicate, oracle_raw) -> list[PredicateBinding]:
        self.calls += 1
        return list(self._bindings)


def _make_discovery_oracle(response, property_relations=None, sling=None):
    db = open_memory_db()
    transport = MockTransport(response=response)
    client = LLMClient(_transport=transport)
    oracle = PredicateTranslation(
        db=db,
        llm_client=client,
        property_relations=property_relations,
        sling=sling,
    )
    return oracle, db, transport


class TestBindingDiscoveryFallsOpen:
    """Discovery is ADDITIVE ENRICHMENT. With no collaborators wired (the
    mock/cold/default case), the result is a SINGLE legacy_scalar binding
    mirroring the oracle's primary property = pre-v0.16 behavior."""

    def test_no_collaborators_yields_single_legacy_binding(self):
        oracle, _, _ = _make_oracle()  # no property_relations / sling
        meta = oracle.consult("holds_role")
        assert len(meta.bindings) == 1
        b = meta.bindings[0]
        assert b.source == "legacy_scalar"
        assert b.kb_property == "P39"
        # Scalar mirror preserved.
        assert meta.kb_property == "P39"

    def test_empty_ontology_falls_open_to_oracle_binding(self):
        # PropertyRelations wired but returns an EMPTY ontology for the
        # property → discovery falls open to the oracle's single binding.
        pr = _StubPropertyRelations({})  # every fetch → empty ontology
        oracle, _, _ = _make_discovery_oracle(
            _default_metadata_response(), property_relations=pr
        )
        meta = oracle.consult("holds_role")
        assert pr.fetched == ["P39"]  # the primary was consulted
        assert len(meta.bindings) == 1
        # The single binding is the oracle binding (empty ontology adds nothing).
        assert meta.bindings[0].kb_property == "P39"

    def test_non_kb_resolvable_never_invokes_discovery(self):
        # A user_authoritative predicate carries no KB binding; discovery must
        # not touch PropertyRelations and the single binding is legacy_scalar.
        pr = _StubPropertyRelations({})
        resp = _default_metadata_response(
            routing_hint="user_authoritative", kb_property=None, kb_namespace=None
        )
        oracle, _, _ = _make_discovery_oracle(resp, property_relations=pr)
        meta = oracle.consult("prefers")
        assert pr.fetched == []  # never consulted for a non-KB predicate
        assert len(meta.bindings) == 1
        assert meta.bindings[0].source == "legacy_scalar"

    def test_discovery_error_degrades_to_oracle_binding(self):
        # A PropertyRelations.fetch that raises must not break the write —
        # discovery is enrichment; it degrades to the oracle binding.
        class _RaisingPR:
            def fetch(self, *a, **kw):
                raise RuntimeError("ontology backend down")

        oracle, db, _ = _make_discovery_oracle(
            _default_metadata_response(), property_relations=_RaisingPR()
        )
        meta = oracle.consult("holds_role")  # must not raise
        assert len(meta.bindings) == 1
        assert meta.bindings[0].kb_property == "P39"
        # The degradation is logged for observability.
        events = db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE event_type='binding_discovery_failed'"
        ).fetchone()[0]
        assert events == 1


class TestBindingDiscoveryFromOntology:
    """When the Wikidata property ontology (PropertyRelations) constrains a
    candidate, discovery builds an ontology_p2302 binding ranked ABOVE the
    plain oracle binding, carrying the ontology's value/subject types and
    single-value flag."""

    def test_ontology_typed_binding_supersedes_oracle_for_primary(self):
        # The primary property's ontology constrains it → the ontology binding
        # for P39 dedupes out the plain oracle binding for the same P-id.
        # The ontology supplies TYPES; the ORACLE supplies single_valued. Here
        # the oracle says single_valued=1, so the ontology binding mirrors it.
        pr = _StubPropertyRelations({
            "P39": PropertyOntology(
                subject_type_qids=["Q5"],
                value_type_qids=["Q4164871"],
                single_valued=True,
            )
        })
        oracle, _, _ = _make_discovery_oracle(
            _default_metadata_response(single_valued=1), property_relations=pr
        )
        meta = oracle.consult("holds_role")
        assert len(meta.bindings) == 1  # deduped by (namespace, P-id)
        b = meta.bindings[0]
        assert b.source == "ontology_p2302"
        assert b.object_entity_types == ["Q4164871"]
        assert b.subject_entity_types == ["Q5"]
        # single_valued comes from the ORACLE (authoritative), not the ontology.
        assert b.single_valued is True
        # bindings[0] mirrors onto the scalar accessors (back-compat).
        assert meta.single_valued is True
        assert meta.object_entity_types == ["Q4164871"]

    def test_oracle_authoritative_for_single_valued_not_ontology(self):
        # PATCH-A fix (4) / §3.2 never-false-contradict: single_valued is the
        # ONLY flag that licenses a CONTRADICTED verdict, so the conservative
        # oracle stays authoritative for it. An ontology that asserts
        # single_valued=True must NOT OR-promote the flag past the oracle's
        # deliberate 0 — the primary binding's single_valued stays False, so
        # this binding alone cannot license a false contradiction.
        pr = _StubPropertyRelations({
            "P39": PropertyOntology(
                subject_type_qids=["Q5"],
                value_type_qids=["Q4164871"],
                single_valued=True,  # ontology says functional...
            )
        })
        oracle, _, _ = _make_discovery_oracle(
            _default_metadata_response(),  # ...but the oracle is silent (=> 0)
            property_relations=pr,
        )
        meta = oracle.consult("holds_role")
        assert len(meta.bindings) == 1
        b = meta.bindings[0]
        assert b.source == "ontology_p2302"
        # Types still flow from the ontology (enrichment is intact)...
        assert b.object_entity_types == ["Q4164871"]
        assert b.subject_entity_types == ["Q5"]
        # ...but single_valued does NOT: the oracle's 0 is authoritative.
        assert b.single_valued is False
        assert meta.single_valued is False

    def test_candidate_property_adds_second_binding_ranked_above_oracle(self):
        # The copula case: oracle primary P31, candidate P106. P106's ontology
        # constrains it → an ontology_p2302 binding for P106 is added. P31's
        # ontology is empty → the plain oracle binding for P31 stays. Ranking:
        # ontology-typed (P106) first, then oracle-primary (P31).
        pr = _StubPropertyRelations({
            "P106": PropertyOntology(value_type_qids=["Q28640"]),  # occupation
        })
        resp = _default_metadata_response(
            kb_property="P31",
            candidate_kb_properties=["P106"],
        )
        oracle, _, _ = _make_discovery_oracle(resp, property_relations=pr)
        meta = oracle.consult("is_a_physicist")
        props = [b.kb_property for b in meta.bindings]
        assert props == ["P106", "P31"], props
        assert meta.bindings[0].source == "ontology_p2302"
        assert meta.bindings[0].object_entity_types == ["Q28640"]
        assert meta.bindings[1].source == "oracle"
        # Scalar mirror flips to bindings[0] (intended per spec — scalar mirrors
        # the evidence-arbitrated winner).
        assert meta.kb_property == "P106"
        # Persisted JSON round-trips: re-fetch through _row_to_metadata.
        warm = oracle.consult("is_a_physicist")
        assert [b.kb_property for b in warm.bindings] == ["P106", "P31"]
        assert warm.bindings[0].source == "ontology_p2302"

    def test_candidate_kb_properties_tolerated_when_absent(self):
        # candidate_kb_properties is optional. A response WITHOUT it (the common
        # case) discovers exactly the primary binding — no crash, no extra rows.
        pr = _StubPropertyRelations({})
        resp = _default_metadata_response()  # no candidate_kb_properties key
        oracle, _, _ = _make_discovery_oracle(resp, property_relations=pr)
        meta = oracle.consult("holds_role")
        assert [b.kb_property for b in meta.bindings] == ["P39"]

    def test_candidate_kb_properties_null_tolerated(self):
        # Explicit null candidate_kb_properties behaves identically to absent.
        pr = _StubPropertyRelations({})
        resp = _default_metadata_response(candidate_kb_properties=None)
        oracle, _, _ = _make_discovery_oracle(resp, property_relations=pr)
        meta = oracle.consult("holds_role")
        assert [b.kb_property for b in meta.bindings] == ["P39"]


class TestBindingDiscoverySlingFallback:
    """SLING is consulted ONLY when the ontology couldn't constrain a candidate
    (any candidate had an empty ontology). SLING bindings rank LAST and never
    license a contradiction (single_valued is forced False at the SLING layer)."""

    def test_sling_fires_when_ontology_empty(self):
        pr = _StubPropertyRelations({})  # ontology empty for the primary
        sling = _StubSling([
            PredicateBinding(
                kb_namespace="wikidata", kb_property="P800",
                source="sling", rank=0.1,
            )
        ])
        oracle, _, _ = _make_discovery_oracle(
            _default_metadata_response(), property_relations=pr, sling=sling
        )
        meta = oracle.consult("holds_role")
        assert sling.calls == 1
        props = [b.kb_property for b in meta.bindings]
        # Oracle primary first (its ontology was empty so it stays as `oracle`),
        # SLING candidate last.
        assert props == ["P39", "P800"], props
        assert meta.bindings[-1].source == "sling"

    def test_sling_not_consulted_when_ontology_constrains_every_candidate(self):
        # If every candidate's ontology is non-empty, there is no long-tail gap
        # for SLING to fill → propose_bindings is never called.
        pr = _StubPropertyRelations({
            "P39": PropertyOntology(value_type_qids=["Q4164871"]),
        })
        sling = _StubSling([
            PredicateBinding(kb_namespace="wikidata", kb_property="P800", source="sling")
        ])
        oracle, _, _ = _make_discovery_oracle(
            _default_metadata_response(), property_relations=pr, sling=sling
        )
        meta = oracle.consult("holds_role")
        assert sling.calls == 0
        assert [b.kb_property for b in meta.bindings] == ["P39"]

    def test_sling_empty_proposal_leaves_oracle_binding(self):
        # SLING fails open: an empty proposal list leaves just the oracle binding.
        pr = _StubPropertyRelations({})
        sling = _StubSling([])  # no signal
        oracle, _, _ = _make_discovery_oracle(
            _default_metadata_response(), property_relations=pr, sling=sling
        )
        meta = oracle.consult("holds_role")
        assert sling.calls == 1
        assert [b.kb_property for b in meta.bindings] == ["P39"]
