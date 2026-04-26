"""Tests for src.router (v0.4 — code-generated python dispatch)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from src.fact_store import FactStore
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
class StubCodeGenVerifier:
    """Test double for CodeGenerationVerifier.

    Returns a queued CodeGenVerificationResult per .verify() call.
    """

    results: list[CodeGenVerificationResult] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def verify(self, claim, *, source_turn_id=None):
        self.calls.append({"claim": claim, "source_turn_id": source_turn_id})
        if not self.results:
            raise RuntimeError("StubCodeGenVerifier has no queued result")
        return self.results.pop(0)


def _router(store, *, code_results=None):
    stub = StubCodeGenVerifier(results=list(code_results or []))
    return Router(store, load_default_registry(), code_gen_verifier=stub), stub


def _f(pattern, predicate, slots, polarity=1, source_text="<src>"):
    return {
        "pattern": pattern,
        "predicate": predicate,
        "slots": slots,
        "polarity": polarity,
        "source_text": source_text,
    }


# ---------- user origin ----------


def test_user_pref_stored(store):
    router, _ = _router(store)
    d = router.route(
        _f("preference", "likes", {"agent": "user", "object": "pb"}),
        origin="user", source_turn_id=1,
    )
    assert d.outcome is RoutingOutcome.USER_STORED
    assert d.verification_status == "user_asserted"
    f = store.get_fact(d.stored_fact_id)
    assert f.pattern == "preference"
    assert f.slots == {"agent": "user", "object": "pb"}


def test_user_duplicate_boosts(store):
    router, _ = _router(store)
    fact = _f("preference", "likes", {"agent": "user", "object": "pb"})
    router.route(fact, origin="user", source_turn_id=1)
    d2 = router.route(fact, origin="user", source_turn_id=2)
    assert d2.outcome is RoutingOutcome.USER_DUPLICATE
    assert d2.boosted_fact_id is not None


def test_user_polarity_flip_closes_old_and_stores_new(store):
    router, _ = _router(store)
    pos = _f("preference", "likes", {"agent": "user", "object": "pb"}, polarity=1)
    neg = _f("preference", "likes", {"agent": "user", "object": "pb"}, polarity=0)
    d1 = router.route(pos, origin="user", source_turn_id=1)
    d2 = router.route(neg, origin="user", source_turn_id=2)
    assert d2.outcome is RoutingOutcome.USER_CONTRADICTED_PRIOR
    assert d1.stored_fact_id in d2.closed_fact_ids


# ---------- python (code generation) ----------


def test_quantitative_python_verified_routes_through_code_gen(store):
    """Triage says verifiable, comparator returns verified."""
    result = CodeGenVerificationResult(
        status="verified",
        confidence=0.99,
        explanation="claimed 3; computed 3; equal",
        actual_value=3,
        trace={"comparison": {"verdict": "verified"}},
    )
    router, stub = _router(store, code_results=[result])
    fact = _f(
        "quantitative", "has_count",
        {"subject": "strawberry", "property": "letter_r", "value": 3},
    )
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.VERIFIED
    assert d.verification_status == "verified"
    assert d.code_gen_result is not None
    assert d.code_gen_result["status"] == "verified"
    assert len(stub.calls) == 1


def test_quantitative_python_contradicted_stores_correction(store):
    result = CodeGenVerificationResult(
        status="contradicted",
        confidence=0.99,
        explanation="claimed 3; computed 0; not equal",
        actual_value=0,
        trace={"comparison": {"verdict": "contradicted"}},
    )
    router, _ = _router(store, code_results=[result])
    fact = _f(
        "quantitative", "has_count",
        {"subject": "strawberry", "property": "letter_p", "value": 3},
    )
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.CONTRADICTED
    assert d.correction is not None
    assert d.correction["corrected_object"] == 0
    # Correction was projected into the value slot.
    corrected = store.get_fact(d.stored_fact_id)
    assert corrected.slots["value"] == 0


def test_python_not_verifiable_falls_back_to_retrieval(store):
    """Triage says not verifiable; quantitative pattern's next rule is retrieval.

    No retrieval verifier wired, so the fall-through ends up retrieval_failed.
    """
    result = CodeGenVerificationResult(
        status="not_python_verifiable",
        explanation="requires biographical data",
        trace={"triage": {"verifiable": False, "reason": "external"}},
    )
    router, _ = _router(store, code_results=[result])
    fact = _f(
        "quantitative", "born_in_year",
        {"subject": "Einstein", "property": "birth_year", "value": 1879},
    )
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIED
    assert d.verification_status == "retrieval_failed"
    # The code-gen trace is preserved so the UI can show the triage
    # decision even though the final verdict came from retrieval.
    assert d.code_gen_result is not None
    assert d.code_gen_result["status"] == "not_python_verifiable"


def test_python_execution_failed_marks_pending(store):
    result = CodeGenVerificationResult(
        status="code_execution_failed",
        explanation="timed out after 5s",
        trace={"execution": {"timed_out": True}},
    )
    router, _ = _router(store, code_results=[result])
    fact = _f(
        "quantitative", "has_count",
        {"subject": "strawberry", "property": "letter_p", "value": 3},
    )
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIED
    assert d.verification_status == "unverifiable_pending_implementation"


def test_python_comparison_error_marks_pending(store):
    result = CodeGenVerificationResult(
        status="comparison_error",
        explanation="could not parse stdout as int",
    )
    router, _ = _router(store, code_results=[result])
    fact = _f(
        "quantitative", "has_count",
        {"subject": "strawberry", "property": "letter_p", "value": 3},
    )
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIED
    assert d.verification_status == "unverifiable_pending_implementation"


# ---------- predicate_overrides on relational ----------


def test_relational_reverse_of_routes_to_python_via_override(store):
    """relational defaults to retrieval but reverse_of is overridden to python."""
    result = CodeGenVerificationResult(
        status="verified",
        confidence=0.99,
        explanation="reverse(egalitarian) == nairatilage",
        actual_value="nairatilage",
        trace={},
    )
    router, stub = _router(store, code_results=[result])
    fact = _f(
        "relational", "reverse_of",
        {"subject": "nairatilage", "object": "egalitarian"},
    )
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.VERIFIED
    assert len(stub.calls) == 1, "predicate override should route through code gen"


def test_relational_nonoverride_routes_to_retrieval(store):
    """Predicates not in the override map still go through the pattern's rules."""
    router, stub = _router(store)  # no code-gen results queued
    fact = _f("relational", "married_to",
              {"subject": "Marie Curie", "object": "Pierre Curie"})
    d = router.route(fact, origin="model", source_turn_id=1)
    # No retrieval verifier wired in; ends up retrieval_failed without
    # touching the code-gen stub.
    assert d.verification_status == "retrieval_failed"
    assert stub.calls == []


# ---------- routing anomaly ----------


def test_preference_with_non_user_agent_flags_anomaly(store):
    router, _ = _router(store)
    fact = _f("preference", "likes",
              {"agent": "Donald Trump", "object": "peanut butter"})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.ROUTING_ANOMALY
    assert d.verification_status == "routing_anomaly"
    assert d.anomaly_slot is not None
    assert d.anomaly_slot["slot"] == "agent"
    assert d.anomaly_slot["actual"] == "Donald Trump"


def test_propositional_attitude_with_non_user_agent_flags_anomaly(store):
    router, _ = _router(store)
    fact = _f("propositional_attitude", "believes",
              {"agent": "Donald Trump", "attitude": "thinks", "proposition": "X"})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.ROUTING_ANOMALY


def test_spatial_temporal_with_non_user_entity_is_NOT_anomaly(store):
    router, _ = _router(store)
    fact = _f("spatial_temporal", "lives_in",
              {"entity": "Marie Curie", "location": "Paris",
               "relation_kind": "residence"})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is not RoutingOutcome.ROUTING_ANOMALY
    assert d.verification_status == "retrieval_failed"


def test_anomaly_subject_normalization():
    assert _is_user("user")
    assert _is_user("User")
    assert _is_user(" me ")
    assert _is_user("I")
    assert not _is_user("Donald Trump")
    assert not _is_user("")


# ---------- store_lookup (model claim of user-authoritative pattern) ----------


def test_model_user_authoritative_match_boosts(store):
    router, _ = _router(store)
    user = _f("preference", "likes", {"agent": "user", "object": "pb"})
    d_user = router.route(user, origin="user", source_turn_id=1)

    model_same = _f("preference", "likes", {"agent": "user", "object": "pb"})
    d_model = router.route(model_same, origin="model", source_turn_id=2)
    assert d_model.outcome is RoutingOutcome.VERIFIED
    assert d_model.boosted_fact_id == d_user.stored_fact_id


def test_model_user_authoritative_contradiction_provides_correction(store):
    router, _ = _router(store)
    user = _f("preference", "likes", {"agent": "user", "object": "pb"}, polarity=1)
    router.route(user, origin="user", source_turn_id=1)
    model_neg = _f("preference", "likes", {"agent": "user", "object": "pb"}, polarity=0)
    d = router.route(model_neg, origin="model", source_turn_id=2)
    assert d.outcome is RoutingOutcome.CONTRADICTED
    assert d.correction is not None


def test_model_user_authoritative_miss_marks_pending(store):
    router, _ = _router(store)
    fact = _f("preference", "likes", {"agent": "user", "object": "pb"})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIED
    assert d.verification_status == "unverifiable_pending_implementation"


# ---------- retrieval (no verifier configured) ----------


def test_retrieval_pattern_with_no_verifier_marks_failed(store):
    router, _ = _router(store)
    fact = _f("categorical", "is_a",
              {"entity": "Marie Curie", "category": "physicist"})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIED
    assert d.verification_status == "retrieval_failed"


def test_decision_carries_status_and_confidence_for_every_path(store):
    """Mix of patterns hit different paths; each Decision is well-formed."""
    verified_result = CodeGenVerificationResult(status="verified", actual_value=3)
    router, _ = _router(store, code_results=[verified_result])
    cases = [
        _f("preference", "likes", {"agent": "user", "object": "x"}),  # store_lookup miss
        _f("quantitative", "has_count",
           {"subject": "strawberry", "property": "letter_r", "value": 3}),  # python verified
        _f("categorical", "is_a", {"entity": "x", "category": "y"}),  # retrieval failed
        _f("preference", "likes", {"agent": "Donald Trump", "object": "x"}),  # anomaly
    ]
    for fact in cases:
        d = router.route(fact, origin="model", source_turn_id=1)
        assert d.verification_status, f"missing status for {fact}"
        assert d.confidence > 0, f"missing confidence for {fact}"


# ---------- guardrails ----------


def test_unknown_pattern_raises(store):
    router, _ = _router(store)
    bad = _f("invented_pattern", "x", {"y": 1})
    with pytest.raises(ValueError, match="unknown pattern"):
        router.route(bad, origin="user", source_turn_id=1)


def test_invalid_origin_raises(store):
    router, _ = _router(store)
    with pytest.raises(ValueError, match="origin"):
        router.route(_f("preference", "likes", {"agent": "user", "object": "x"}),
                     origin="assistant", source_turn_id=1)


# ---------- key slots map covers all patterns ----------


def test_key_slots_defined_for_every_pattern():
    reg = load_default_registry()
    for name in reg.names():
        assert name in KEY_SLOTS_BY_PATTERN, f"no key slots for {name!r}"


# ---------- temporal scope is lifted to fact columns ----------


def test_role_assignment_temporal_scope_lifted_to_columns(store):
    router, _ = _router(store)
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


# ---------- python without a code-gen verifier configured ----------


def test_python_method_without_verifier_marks_pending(store):
    router = Router(store, load_default_registry())  # no code_gen_verifier
    fact = _f(
        "quantitative", "has_count",
        {"subject": "strawberry", "property": "letter_r", "value": 3},
    )
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIED
    assert d.verification_status == "unverifiable_pending_implementation"
    assert any("CodeGenerationVerifier" in n for n in d.notes)
