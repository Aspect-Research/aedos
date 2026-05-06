"""Tests for src.router (v0.5 — LLM-routed dispatch)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from src.fact_store import FactStore
from src.llm_router import RoutingDecision
from src.pattern_registry import load_default_registry, reset_cache
from src.router import (
    KEY_SLOTS_BY_PATTERN,
    Decision,
    Router,
    RoutingOutcome,
    _is_user,
)
from src.verifiers.code_generation.pipeline import CodeGenVerificationResult


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_cache()
    yield
    reset_cache()


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "t.db")
    yield s
    s.close()


@dataclass
class StubRoutingFn:
    """A queueable routing function. Pop one decision per call."""

    decisions: list[RoutingDecision] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def __call__(self, claim):
        self.calls.append(claim)
        if not self.decisions:
            raise RuntimeError("StubRoutingFn has no queued decision")
        return self.decisions.pop(0)


@dataclass
class StubCodeGenVerifier:
    results: list[CodeGenVerificationResult] = field(default_factory=list)
    cross_check_results: list[CodeGenVerificationResult] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    cross_check_calls: list[dict] = field(default_factory=list)

    def verify(self, claim, *, source_turn_id=None):
        self.calls.append({"claim": claim, "source_turn_id": source_turn_id})
        if not self.results:
            raise RuntimeError("StubCodeGenVerifier has no queued result")
        return self.results.pop(0)

    def verify_with_cross_check(self, claim, *, source_turn_id=None):
        self.cross_check_calls.append(
            {"claim": claim, "source_turn_id": source_turn_id}
        )
        if not self.cross_check_results:
            raise RuntimeError("StubCodeGenVerifier has no queued cross-check result")
        return self.cross_check_results.pop(0)


def _router(store, *, decisions=None, code_results=None, cross_check_results=None):
    routing_fn = StubRoutingFn(decisions=list(decisions or []))
    cg = StubCodeGenVerifier(
        results=list(code_results or []),
        cross_check_results=list(cross_check_results or []),
    )
    r = Router(
        store, load_default_registry(),
        routing_fn=routing_fn,
        code_gen_verifier=cg,
    )
    return r, routing_fn, cg


def _f(pattern, predicate, slots, polarity=1, source_text="<src>"):
    return {
        "pattern": pattern, "predicate": predicate, "slots": slots,
        "polarity": polarity, "source_text": source_text,
    }


def _python_decision():
    return RoutingDecision(method="python", reason="pure",
                           python_inputs_self_contained=True)


def _retrieval_decision(query="x"):
    return RoutingDecision(method="retrieval", reason="external",
                           retrieval_query_hint=query)


def _user_auth_decision():
    return RoutingDecision(method="user_authoritative", reason="about user")


def _unverifiable_decision():
    return RoutingDecision(method="unverifiable", reason="judgment")


def _ccc_decision():
    return RoutingDecision(method="python_with_canonical_constants",
                           reason="needs canon",
                           python_inputs_self_contained=False,
                           canonical_constants_needed=["list of US states"])


# ---------- user origin (LLM router does not run) ----------


def test_user_pref_stored(store):
    router, _, _ = _router(store)
    d = router.route(
        _f("preference", "likes", {"agent": "user", "object": "pb"}),
        origin="user", source_turn_id=1,
    )
    assert d.outcome is RoutingOutcome.USER_STORED
    assert d.verification_status == "user_asserted"


def test_user_duplicate_boosts(store):
    router, _, _ = _router(store)
    fact = _f("preference", "likes", {"agent": "user", "object": "pb"})
    router.route(fact, origin="user", source_turn_id=1)
    d2 = router.route(fact, origin="user", source_turn_id=2)
    assert d2.outcome is RoutingOutcome.USER_DUPLICATE


def test_user_polarity_flip_closes_old_and_stores_new(store):
    router, _, _ = _router(store)
    pos = _f("preference", "likes", {"agent": "user", "object": "pb"}, polarity=1)
    neg = _f("preference", "likes", {"agent": "user", "object": "pb"}, polarity=0)
    d1 = router.route(pos, origin="user", source_turn_id=1)
    d2 = router.route(neg, origin="user", source_turn_id=2)
    assert d2.outcome is RoutingOutcome.USER_CONTRADICTED_PRIOR
    assert d1.stored_fact_id in d2.closed_fact_ids


# ---------- python (code generation) ----------


def test_python_verified_routes_through_code_gen(store):
    cg_result = CodeGenVerificationResult(
        status="verified", actual_value=3, trace={},
    )
    router, routing_fn, cg = _router(
        store, decisions=[_python_decision()], code_results=[cg_result],
    )
    fact = _f("quantitative", "has_count",
              {"subject": "strawberry", "property": "letter_r", "value": 3})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.VERIFIED
    assert d.verification_status == "verified"
    assert d.code_gen_result is not None
    assert d.routing_decision is not None
    assert d.routing_decision["method"] == "python"
    assert len(routing_fn.calls) == 1
    assert len(cg.calls) == 1


def test_python_contradicted_stores_correction(store):
    cg_result = CodeGenVerificationResult(
        status="contradicted", actual_value=0, trace={},
    )
    router, _, _ = _router(
        store, decisions=[_python_decision()], code_results=[cg_result],
    )
    fact = _f("quantitative", "has_count",
              {"subject": "strawberry", "property": "letter_p", "value": 3})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.CONTRADICTED
    assert d.correction is not None
    assert d.correction["corrected_object"] == 0
    corrected = store.get_fact(d.stored_fact_id)
    assert corrected.slots["value"] == 0


def test_python_execution_failed_marks_pending(store):
    cg_result = CodeGenVerificationResult(
        status="code_execution_failed", explanation="timed out", trace={},
    )
    router, _, _ = _router(
        store, decisions=[_python_decision()], code_results=[cg_result],
    )
    fact = _f("quantitative", "has_count",
              {"subject": "x", "property": "y", "value": 3})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIED
    assert d.verification_status == "unverifiable_pending_implementation"


def test_python_comparison_error_marks_pending(store):
    cg_result = CodeGenVerificationResult(
        status="comparison_error", explanation="parse fail", trace={},
    )
    router, _, _ = _router(
        store, decisions=[_python_decision()], code_results=[cg_result],
    )
    fact = _f("quantitative", "has_count",
              {"subject": "x", "property": "y", "value": 3})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIED
    assert d.verification_status == "unverifiable_pending_implementation"


# ---------- python_with_canonical_constants ----------


def test_canonical_constants_routes_through_cross_check(store):
    cg_result = CodeGenVerificationResult(
        status="verified", actual_value=4, trace={},
    )
    router, _, cg = _router(
        store, decisions=[_ccc_decision()],
        cross_check_results=[cg_result],
    )
    fact = _f("quantitative", "us_states_starting_with_letter",
              {"subject": "US states", "property": "starting_with_A", "value": 4})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.VERIFIED
    assert len(cg.cross_check_calls) == 1
    assert len(cg.calls) == 0  # the regular path was not used


def test_canonical_constants_disagreement_marks_pending(store):
    """Cross-check returns a disagreement → router treats as pending."""
    cg_result = CodeGenVerificationResult(
        status="canonical_constants_disagreement",
        explanation="a=4, b=5", trace={},
    )
    router, _, _ = _router(
        store, decisions=[_ccc_decision()],
        cross_check_results=[cg_result],
    )
    fact = _f("quantitative", "us_states_starting_with_letter",
              {"subject": "x", "property": "y", "value": 4})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIED
    assert d.verification_status == "unverifiable_pending_implementation"


# ---------- routing anomaly (preserved from v0.4 — predates LLM router) ----------


def test_preference_with_non_user_agent_flags_anomaly(store):
    """Pattern flag still wins over the LLM router for preference / attitude
    patterns whose extractor produced a non-user agent — that's an
    upstream extractor error.
    """
    router, routing_fn, _ = _router(store)
    fact = _f("preference", "likes",
              {"agent": "Donald Trump", "object": "peanut butter"})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.ROUTING_ANOMALY
    assert d.anomaly_slot is not None
    assert d.anomaly_slot["slot"] == "agent"
    # The LLM router was NOT consulted — anomaly check ran first.
    assert routing_fn.calls == []


def test_propositional_attitude_with_non_user_agent_flags_anomaly(store):
    router, _, _ = _router(store)
    fact = _f("propositional_attitude", "believes",
              {"agent": "Donald Trump", "attitude": "thinks", "proposition": "X"})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.ROUTING_ANOMALY


def test_anomaly_subject_normalization():
    assert _is_user("user")
    assert _is_user("User")
    assert _is_user(" me ")
    assert _is_user("I")
    assert not _is_user("Donald Trump")
    assert not _is_user("")


# ---------- store_lookup (model claim of user-authoritative pattern) ----------


def test_model_user_authoritative_match_boosts(store):
    router, _, _ = _router(
        store,
        decisions=[_user_auth_decision()],
    )
    user = _f("preference", "likes", {"agent": "user", "object": "pb"})
    d_user = router.route(user, origin="user", source_turn_id=1)

    model_same = _f("preference", "likes", {"agent": "user", "object": "pb"})
    d_model = router.route(model_same, origin="model", source_turn_id=2)
    assert d_model.outcome is RoutingOutcome.VERIFIED
    assert d_model.boosted_fact_id == d_user.stored_fact_id


def test_model_user_authoritative_contradiction_provides_correction(store):
    router, _, _ = _router(
        store,
        decisions=[_user_auth_decision()],
    )
    user = _f("preference", "likes", {"agent": "user", "object": "pb"}, polarity=1)
    router.route(user, origin="user", source_turn_id=1)
    model_neg = _f("preference", "likes", {"agent": "user", "object": "pb"}, polarity=0)
    d = router.route(model_neg, origin="model", source_turn_id=2)
    assert d.outcome is RoutingOutcome.CONTRADICTED
    assert d.correction is not None


def test_model_user_authoritative_miss_marks_pending(store):
    router, _, _ = _router(store, decisions=[_user_auth_decision()])
    fact = _f("preference", "likes", {"agent": "user", "object": "pb"})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIED
    assert d.verification_status == "unverifiable_pending_implementation"


# ---------- retrieval (no verifier configured) ----------


def test_retrieval_routed_with_no_verifier_marks_failed(store):
    router, _, _ = _router(store, decisions=[_retrieval_decision()])
    fact = _f("categorical", "is_a",
              {"entity": "Marie Curie", "category": "physicist"})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIED
    assert d.verification_status == "retrieval_failed"


def test_retrieval_query_hint_logged_on_routing_decision(store):
    router, _, _ = _router(
        store, decisions=[_retrieval_decision(query="Marie Curie physicist")],
    )
    fact = _f("categorical", "is_a", {"entity": "Marie Curie", "category": "physicist"})
    turn_id = store.insert_turn("user", "ctx")
    d = router.route(fact, origin="model", source_turn_id=turn_id)
    assert d.routing_decision["retrieval_query_hint"] == "Marie Curie physicist"
    events = store.get_pipeline_events(turn_id)
    routing_events = [e for e in events if e["stage"] == "routing_decision"]
    assert routing_events, "routing_decision was not logged"
    payload = routing_events[0]["data"]
    assert payload["decision"]["retrieval_query_hint"] == "Marie Curie physicist"


# ---------- unverifiable ----------


def test_unverifiable_decision_routes_to_unverifiable_in_principle(store):
    router, _, _ = _router(store, decisions=[_unverifiable_decision()])
    fact = _f("propositional_attitude", "believes",
              {"agent": "user", "attitude": "thinks", "proposition": "X"})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIABLE_IN_PRINCIPLE
    assert d.verification_status == "unverifiable_in_principle"


# ---------- python without a code-gen verifier configured ----------


def test_python_method_without_verifier_marks_pending(store):
    """Router gets python decision but no CodeGen verifier — pending fallback."""
    routing_fn = StubRoutingFn(decisions=[_python_decision()])
    router = Router(
        store, load_default_registry(), routing_fn=routing_fn,
    )
    fact = _f(
        "quantitative", "has_count",
        {"subject": "strawberry", "property": "letter_r", "value": 3},
    )
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIED
    assert d.verification_status == "unverifiable_pending_implementation"
    assert any("CodeGenerationVerifier" in n for n in d.notes)


# ---------- routing decision is logged on every model claim ----------


def test_routing_decision_logged(store):
    cg = CodeGenVerificationResult(status="verified", actual_value=3, trace={})
    router, _, _ = _router(
        store, decisions=[_python_decision()], code_results=[cg],
    )
    turn = store.insert_turn("user", "test")
    fact = _f("quantitative", "has_count",
              {"subject": "x", "property": "y", "value": 3})
    router.route(fact, origin="model", source_turn_id=turn)
    events = store.get_pipeline_events(turn)
    routing = [e for e in events if e["stage"] == "routing_decision"]
    assert len(routing) == 1
    payload = routing[0]["data"]
    assert payload["decision"]["method"] == "python"
    assert payload["decision"]["reason"]
    # v0.13: confidence is no longer a routing-decision field.
    assert "confidence" not in payload["decision"]


# ---------- guardrails ----------


def test_unknown_pattern_raises(store):
    router, _, _ = _router(store)
    bad = _f("invented_pattern", "x", {"y": 1})
    with pytest.raises(ValueError, match="unknown pattern"):
        router.route(bad, origin="user", source_turn_id=1)


def test_invalid_origin_raises(store):
    router, _, _ = _router(store)
    with pytest.raises(ValueError, match="origin"):
        router.route(_f("preference", "likes", {"agent": "user", "object": "x"}),
                     origin="assistant", source_turn_id=1)


def test_model_routing_without_routing_fn_raises(store):
    """If neither routing_fn nor llm is provided, model claims fail loudly."""
    router = Router(store, load_default_registry())
    fact = _f("quantitative", "has_count",
              {"subject": "x", "property": "y", "value": 3})
    with pytest.raises(RuntimeError, match="routing_fn"):
        router.route(fact, origin="model", source_turn_id=1)


# ---------- temporal scope is lifted to fact columns ----------


def test_role_assignment_temporal_scope_lifted_to_columns(store):
    router, _, _ = _router(
        store, decisions=[_retrieval_decision()],
    )
    fact = _f(
        "role_assignment", "served_as",
        {
            "agent": "Donald Trump",
            "role": "45th President",
            "org": "United States",
            "valid_from": "2017-01-20",
            "valid_until": "2021-01-20",
        },
    )
    d = router.route(fact, origin="model", source_turn_id=1)
    f = store.get_fact(d.stored_fact_id)
    assert f.valid_from == "2017-01-20"
    assert f.valid_until == "2021-01-20"


# ---------- key slots map covers all patterns ----------


def test_key_slots_defined_for_every_pattern():
    reg = load_default_registry()
    for name in reg.names():
        assert name in KEY_SLOTS_BY_PATTERN, f"no key slots for {name!r}"


# ---- display_status mapping (4-bucket UI projection) ----


def test_display_status_buckets_for_every_internal_status():
    """The 8 internal verification statuses each map to one of 4 UI
    buckets. Pins the contract so adding a new status forces an
    explicit decision about which bucket it shows in."""
    from src.router.types import (
        DISPLAY_STATUS_BY_VERIFICATION_STATUS, display_status_for,
    )
    expected = {
        "verified": "verified",
        "user_asserted": "not_applicable",
        "contradicted": "contradicted",
        "retrieval_inconclusive": "inconclusive",
        "retrieval_failed": "not_applicable",
        "unverifiable_in_principle": "not_applicable",
        "unverifiable_pending_implementation": "inconclusive",
        "routing_anomaly": "not_applicable",
    }
    assert DISPLAY_STATUS_BY_VERIFICATION_STATUS == expected
    for status, bucket in expected.items():
        assert display_status_for(status) == bucket


def test_display_status_unknown_falls_back_to_inconclusive():
    """Unknown statuses don't crash the UI — they show as
    'inconclusive' so the operator notices something unfamiliar
    without losing the rest of the trace."""
    from src.router.types import display_status_for
    assert display_status_for("totally_made_up_status") == "inconclusive"
    assert display_status_for("") == "inconclusive"


def test_decision_to_dict_includes_display_status():
    """Decision.to_dict carries display_status so the UI can render
    without re-running the mapping client-side."""
    from src.router.types import Decision, RoutingOutcome
    d = Decision(
        claim={}, outcome=RoutingOutcome.VERIFIED,
        verification_status="verified",
    )
    assert d.to_dict()["display_status"] == "verified"
    d2 = Decision(
        claim={}, outcome=RoutingOutcome.UNVERIFIED,
        verification_status="retrieval_failed",
    )
    assert d2.to_dict()["display_status"] == "not_applicable"
