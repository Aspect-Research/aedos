"""Tests for the Layer 2 router."""

from __future__ import annotations

import pytest

from src.aedos_v0_15.database import open_memory_db
from src.aedos_v0_15.layer1_extraction.extractor import Claim, ExtractionContext
from src.aedos_v0_15.layer1_extraction.triage import TriageDecision
from src.aedos_v0_15.layer2_routing.router import Router, RoutingDecision
from src.aedos_v0_15.layer2_routing.validator import ValidationResult, Validator
from src.aedos_v0_15.layer3_substrate.predicate_translation import (
    PredicateMetadata,
    PredicateTranslation,
    PredicateTranslationError,
)
from src.aedos_v0_15.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockTransport:
    def __init__(self, response: dict):
        self._response = response
        self.call_count = 0

    def extract_with_tool(self, system, user_message, tool, model="", purpose=None):
        self.call_count += 1
        return self._response

    def chat(self, system, messages, model="", purpose=None):
        return ""


def _meta_response(routing_hint: str, user_subject_required: int = 0,
                   distinct_slots=None, object_type: str = "entity") -> dict:
    return {
        "object_type": object_type,
        "user_subject_required": user_subject_required,
        "distinct_slots": distinct_slots,
        "routing_hint": routing_hint,
        "kb_namespace": "wikidata" if routing_hint == "kb_resolvable" else None,
        "kb_property": "P39" if routing_hint == "kb_resolvable" else None,
        "slot_to_qualifier": None,
        "reason": f"test reason for {routing_hint}",
    }


def _make_router(routing_hint: str, **meta_kwargs):
    db = open_memory_db()
    transport = MockTransport(_meta_response(routing_hint, **meta_kwargs))
    client = LLMClient(_transport=transport)
    oracle = PredicateTranslation(db=db, llm_client=client)
    validator = Validator()
    return Router(predicate_translation=oracle, validator=validator), transport


def _claim(subject="Asa", predicate="holds_role", object_val="President",
           polarity=1, asserting_party="user_test"):
    return Claim(
        claim_id="c1",
        subject=subject,
        predicate=predicate,
        object=object_val,
        polarity=polarity,
        source_text="test",
        asserting_party=asserting_party,
        triage_decision=TriageDecision.VERIFY,
    )


# ---------------------------------------------------------------------------
# TestRoutingDecisionDataclass
# ---------------------------------------------------------------------------

class TestRoutingDecisionDataclass:
    def test_fields_present(self):
        rd = RoutingDecision(route="user_authoritative")
        assert rd.route == "user_authoritative"
        assert rd.predicate_metadata is None
        assert rd.anomaly_reason is None
        assert rd.stub is False

    def test_stub_flag(self):
        rd = RoutingDecision(route="kb_resolvable", stub=True)
        assert rd.stub is True


# ---------------------------------------------------------------------------
# TestRouterFourRoutes
# ---------------------------------------------------------------------------

class TestRouterFourRoutes:
    def test_user_authoritative_route(self):
        router, _ = _make_router("user_authoritative")
        decision = router.route(_claim())
        assert decision.route == "user_authoritative"

    def test_kb_resolvable_route(self):
        router, _ = _make_router("kb_resolvable")
        decision = router.route(_claim())
        assert decision.route == "kb_resolvable"

    def test_kb_resolvable_is_stub(self):
        router, _ = _make_router("kb_resolvable")
        decision = router.route(_claim())
        assert decision.stub is True

    def test_python_route(self):
        router, _ = _make_router("python", object_type="quantity")
        decision = router.route(_claim(object_val="42"))
        assert decision.route == "python"

    def test_python_is_stub(self):
        router, _ = _make_router("python", object_type="quantity")
        decision = router.route(_claim(object_val="42"))
        assert decision.stub is True

    def test_abstain_route(self):
        router, _ = _make_router("abstain")
        decision = router.route(_claim())
        assert decision.route == "abstain"

    def test_predicate_metadata_attached(self):
        router, _ = _make_router("kb_resolvable")
        decision = router.route(_claim())
        assert decision.predicate_metadata is not None
        assert decision.predicate_metadata.routing_hint == "kb_resolvable"


# ---------------------------------------------------------------------------
# TestRouterAnomalies
# ---------------------------------------------------------------------------

class TestRouterAnomalies:
    def test_user_subject_required_violation(self):
        # predicate requires user subject but claim has a different subject
        router, _ = _make_router("user_authoritative", user_subject_required=1)
        claim = _claim(subject="Obama", asserting_party="user_test")
        decision = router.route(claim)
        assert decision.route == "anomaly"
        assert "user_subject_required" in decision.anomaly_reason

    def test_distinct_slots_violation(self):
        router, _ = _make_router("kb_resolvable", distinct_slots=["subject", "object"])
        claim = _claim(subject="France", object_val="France")
        decision = router.route(claim)
        assert decision.route == "anomaly"
        assert "distinct_slots" in decision.anomaly_reason

    def test_subject_equals_asserting_party_passes_user_subject_check(self):
        router, _ = _make_router("user_authoritative", user_subject_required=1)
        claim = _claim(subject="user_test", asserting_party="user_test")
        decision = router.route(claim)
        assert decision.route == "user_authoritative"  # not anomaly

    def test_different_subject_object_passes_distinct_slots(self):
        router, _ = _make_router("kb_resolvable", distinct_slots=["subject", "object"])
        claim = _claim(subject="France", object_val="Germany")
        decision = router.route(claim)
        assert decision.route == "kb_resolvable"


# ---------------------------------------------------------------------------
# TestRouterOnTranslationError
# ---------------------------------------------------------------------------

class TestRouterOnTranslationError:
    def test_translation_error_returns_abstain(self):
        db = open_memory_db()
        transport = MockTransport({})
        transport._raise = RuntimeError("timeout")

        class FailingTransport:
            call_count = 0
            def extract_with_tool(self, *a, **kw):
                self.call_count += 1
                raise RuntimeError("timeout")
            def chat(self, *a, **kw):
                return ""

        client = LLMClient(_transport=FailingTransport())
        oracle = PredicateTranslation(db=db, llm_client=client)
        router = Router(predicate_translation=oracle, validator=Validator())
        decision = router.route(_claim())
        assert decision.route == "abstain"
        assert "predicate_translation_failed" in decision.anomaly_reason

    def test_translation_error_no_predicate_metadata(self):
        db = open_memory_db()
        class FailingTransport:
            def extract_with_tool(self, *a, **kw):
                raise RuntimeError("error")
            def chat(self, *a, **kw):
                return ""
        oracle = PredicateTranslation(db=db, llm_client=LLMClient(_transport=FailingTransport()))
        router = Router(predicate_translation=oracle, validator=Validator())
        decision = router.route(_claim())
        assert decision.predicate_metadata is None


# ---------------------------------------------------------------------------
# TestRouterColdCache
# ---------------------------------------------------------------------------

class TestRouterColdCache:
    def test_cold_cache_triggers_oracle_call(self):
        router, transport = _make_router("kb_resolvable")
        router.route(_claim())
        assert transport.call_count == 1

    def test_warm_cache_no_oracle_call(self):
        router, transport = _make_router("kb_resolvable")
        router.route(_claim())
        router.route(_claim())
        assert transport.call_count == 1  # only one oracle call
