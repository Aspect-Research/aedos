"""Tests for src.router (v0.3 — pattern dispatch)."""

from __future__ import annotations

import json

import pytest
# v0.3: tests rewritten in Section 4.

from src.fact_store import FactStore
from src.pattern_registry import load_default_registry, reset_cache
from src.router import (
    KEY_SLOTS_BY_PATTERN,
    Decision,
    Router,
    RoutingOutcome,
    _is_user,
)


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


@pytest.fixture
def router(store):
    return Router(store, load_default_registry())


def _f(pattern, predicate, slots, polarity=1, source_text="<src>"):
    return {
        "pattern": pattern,
        "predicate": predicate,
        "slots": slots,
        "polarity": polarity,
        "source_text": source_text,
    }


# ---------- user origin ----------


def test_user_pref_stored(router, store):
    d = router.route(
        _f("preference", "likes", {"agent": "user", "object": "pb"}),
        origin="user", source_turn_id=1,
    )
    assert d.outcome is RoutingOutcome.USER_STORED
    assert d.verification_status == "user_asserted"
    f = store.get_fact(d.stored_fact_id)
    assert f.pattern == "preference"
    assert f.slots == {"agent": "user", "object": "pb"}


def test_user_duplicate_boosts(router, store):
    fact = _f("preference", "likes", {"agent": "user", "object": "pb"})
    router.route(fact, origin="user", source_turn_id=1)
    d2 = router.route(fact, origin="user", source_turn_id=2)
    assert d2.outcome is RoutingOutcome.USER_DUPLICATE
    assert d2.boosted_fact_id is not None


def test_user_polarity_flip_closes_old_and_stores_new(router, store):
    pos = _f("preference", "likes", {"agent": "user", "object": "pb"}, polarity=1)
    neg = _f("preference", "likes", {"agent": "user", "object": "pb"}, polarity=0)
    d1 = router.route(pos, origin="user", source_turn_id=1)
    d2 = router.route(neg, origin="user", source_turn_id=2)
    assert d2.outcome is RoutingOutcome.USER_CONTRADICTED_PRIOR
    assert d1.stored_fact_id in d2.closed_fact_ids


# ---------- python verification (quantitative + has_count) ----------


def test_quantitative_with_python_verifier_routes_to_python_and_verifies(router, store):
    """has_count has a python verifier so quantitative routes there."""
    fact = _f(
        "quantitative", "has_count",
        {"subject": "strawberry", "property": "letter_r", "value": 3},
    )
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.VERIFIED
    assert d.verification_status == "verified"
    assert d.verifier_result is not None


def test_quantitative_with_python_verifier_contradicted_stores_correction(router, store):
    fact = _f(
        "quantitative", "has_count",
        {"subject": "strawberry", "property": "letter_p", "value": 3},
    )
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.CONTRADICTED
    assert d.correction is not None
    assert d.correction["corrected_object"] == 0


def test_quantitative_without_python_verifier_falls_through_to_retrieval(router, store):
    """`weighs` has no python verifier — must fall through to retrieval (no
    verifier configured here, so it ends up retrieval_failed)."""
    fact = _f("quantitative", "weighs",
              {"subject": "blue whale", "property": "weight", "value": 150, "unit": "tons"})
    d = router.route(fact, origin="model", source_turn_id=1)
    # No retrieval verifier wired in; status is retrieval_failed.
    assert d.outcome is RoutingOutcome.UNVERIFIED
    assert d.verification_status == "retrieval_failed"


# ---------- routing anomaly ----------


def test_preference_with_non_user_agent_flags_anomaly(router, store):
    """preference's flag_non_user_as_anomaly is on; agent != user → anomaly."""
    fact = _f("preference", "likes",
              {"agent": "Donald Trump", "object": "peanut butter"})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.ROUTING_ANOMALY
    assert d.verification_status == "routing_anomaly"
    assert d.anomaly_slot is not None
    assert d.anomaly_slot["slot"] == "agent"
    assert d.anomaly_slot["actual"] == "Donald Trump"


def test_propositional_attitude_with_non_user_agent_flags_anomaly(router, store):
    fact = _f("propositional_attitude", "believes",
              {"agent": "Donald Trump", "attitude": "thinks", "proposition": "X"})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.ROUTING_ANOMALY


def test_spatial_temporal_with_non_user_entity_is_NOT_anomaly(router, store):
    """spatial_temporal opts out — non-user entities are normal there."""
    fact = _f("spatial_temporal", "lives_in",
              {"entity": "Marie Curie", "location": "Paris",
               "relation_kind": "residence"})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is not RoutingOutcome.ROUTING_ANOMALY
    # No retrieval verifier configured, so it's retrieval_failed.
    assert d.verification_status == "retrieval_failed"


def test_anomaly_subject_normalization():
    assert _is_user("user")
    assert _is_user("User")
    assert _is_user(" me ")
    assert _is_user("I")
    assert not _is_user("Donald Trump")
    assert not _is_user("")


# ---------- store_lookup (model claim of user-authoritative pattern) ----------


def test_model_user_authoritative_match_boosts(router, store):
    """Model says 'user likes pb' after user said it: store-lookup MATCH."""
    user = _f("preference", "likes", {"agent": "user", "object": "pb"})
    d_user = router.route(user, origin="user", source_turn_id=1)

    model_same = _f("preference", "likes", {"agent": "user", "object": "pb"})
    d_model = router.route(model_same, origin="model", source_turn_id=2)
    assert d_model.outcome is RoutingOutcome.VERIFIED
    assert d_model.boosted_fact_id == d_user.stored_fact_id


def test_model_user_authoritative_contradiction_provides_correction(router, store):
    user = _f("preference", "likes", {"agent": "user", "object": "pb"}, polarity=1)
    router.route(user, origin="user", source_turn_id=1)
    model_neg = _f("preference", "likes", {"agent": "user", "object": "pb"}, polarity=0)
    d = router.route(model_neg, origin="model", source_turn_id=2)
    assert d.outcome is RoutingOutcome.CONTRADICTED
    assert d.correction is not None


def test_model_user_authoritative_miss_marks_pending(router, store):
    """Model asserts user-auth claim user never said: stored as pending."""
    fact = _f("preference", "likes", {"agent": "user", "object": "pb"})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIED
    assert d.verification_status == "unverifiable_pending_implementation"


# ---------- retrieval (no verifier configured) ----------


def test_retrieval_pattern_with_no_verifier_marks_failed(router, store):
    """Without a retrieval verifier wired, retrieval patterns are retrieval_failed."""
    fact = _f("categorical", "is_a",
              {"entity": "Marie Curie", "category": "physicist"})
    d = router.route(fact, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIED
    assert d.verification_status == "retrieval_failed"


# ---------- unverifiable (preference with non-user, propositional) ---------


# Note: preference + non-user goes to ROUTING_ANOMALY first, never reaching the
# unverifiable branch directly. To exercise unverifiable, use a pattern whose
# default rule is unverifiable WITHOUT triggering anomaly. None of the v0.3
# patterns have that exact shape after Section 1 — so we test the unverifiable
# path at the integration level once the patterns evolve.


def test_decision_carries_status_and_confidence_for_every_path(router, store):
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


def test_unknown_pattern_raises(router):
    bad = _f("invented_pattern", "x", {"y": 1})
    with pytest.raises(ValueError, match="unknown pattern"):
        router.route(bad, origin="user", source_turn_id=1)


def test_invalid_origin_raises(router):
    with pytest.raises(ValueError, match="origin"):
        router.route(_f("preference", "likes", {"agent": "user", "object": "x"}),
                     origin="assistant", source_turn_id=1)


# ---------- key slots map covers all patterns ----------


def test_key_slots_defined_for_every_pattern():
    reg = load_default_registry()
    for name in reg.names():
        assert name in KEY_SLOTS_BY_PATTERN, f"no key slots for {name!r}"


# ---------- temporal scope is lifted to fact columns ----------


def test_role_assignment_temporal_scope_lifted_to_columns(router, store):
    """A claim with valid_from/valid_until in slots should populate the
    fact's valid_from/valid_until columns so temporal queries work."""
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
