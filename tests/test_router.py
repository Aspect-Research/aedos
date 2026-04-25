"""Tests for src.router."""

from __future__ import annotations

import json

import pytest

from src.fact_store import Fact, FactStore
from src.predicate_registry import load_default_registry, reset_cache
from src.router import (
    Decision,
    Router,
    RoutingOutcome,
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


def _claim(predicate, subject="user", object="pb", object_type="entity", polarity=1):
    return {
        "subject": subject,
        "predicate": predicate,
        "object": object,
        "object_type": object_type,
        "polarity": polarity,
        "source_text": "<source>",
    }


# ---------- user routing ----------


def test_user_new_fact_is_stored(router, store):
    d = router.route(_claim("likes"), origin="user", source_turn_id=1)
    assert d.outcome is RoutingOutcome.USER_STORED
    assert d.stored_fact_id is not None

    f = store.get_fact(d.stored_fact_id)
    assert f.asserted_by == "user"
    assert f.verification_status == "user_asserted"


def test_user_duplicate_assertion_is_boosted(router, store):
    router.route(_claim("likes"), origin="user", source_turn_id=1)
    d2 = router.route(_claim("likes"), origin="user", source_turn_id=2)
    assert d2.outcome is RoutingOutcome.USER_DUPLICATE
    assert d2.stored_fact_id is None
    assert d2.boosted_fact_id is not None


def test_user_contradicting_prior_closes_old_and_stores_new(router, store):
    d1 = router.route(
        _claim("likes", polarity=1), origin="user", source_turn_id=1
    )
    d2 = router.route(
        _claim("likes", polarity=0), origin="user", source_turn_id=2
    )
    assert d2.outcome is RoutingOutcome.USER_CONTRADICTED_PRIOR
    assert d1.stored_fact_id in d2.closed_fact_ids
    assert d2.stored_fact_id is not None

    old = store.get_fact(d1.stored_fact_id)
    assert old.valid_until is not None
    new = store.get_fact(d2.stored_fact_id)
    assert new.valid_until is None
    assert new.polarity == 0


# ---------- python verification ----------


def test_model_python_verified(router, store):
    claim = _claim(
        "has_count",
        subject="strawberry",
        object=json.dumps({"item": "r", "count": 3}),
        object_type="count",
    )
    d = router.route(claim, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.VERIFIED
    f = store.get_fact(d.stored_fact_id)
    assert f.verification_status == "verified"
    assert f.confidence >= 0.95


def test_model_python_contradicted_stores_correction(router, store):
    claim = _claim(
        "has_count",
        subject="strawberry",
        object=json.dumps({"item": "p", "count": 3}),
        object_type="count",
    )
    d = router.route(claim, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.CONTRADICTED
    assert d.correction is not None
    assert d.correction["original_object"] == claim["object"]
    # The correction should be JSON-encoded count with item=p, count=0
    corrected = json.loads(d.correction["corrected_object"])
    assert corrected["item"] == "p"
    assert corrected["count"] == 0

    stored = store.get_fact(d.stored_fact_id)
    assert stored.asserted_by == "python_verifier"
    assert stored.verification_status == "verified"


def test_model_python_inconclusive_stored_unverified(router, store):
    claim = _claim(
        "has_count",
        subject="strawberry",
        object="not-json",
        object_type="count",
    )
    d = router.route(claim, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIED
    f = store.get_fact(d.stored_fact_id)
    assert f.confidence == pytest.approx(0.5)
    assert f.verification_status == "unverified"


def test_model_python_verifier_crash_is_caught(router, store, monkeypatch):
    import src.router as router_mod

    def bad_verifier(_claim):
        raise RuntimeError("boom")

    monkeypatch.setattr(router_mod, "get_verifier", lambda _n: bad_verifier)
    claim = _claim(
        "has_count",
        subject="x",
        object=json.dumps({"item": "x", "count": 1}),
        object_type="count",
    )
    d = router.route(claim, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIED
    assert any("boom" in n for n in d.notes)


# ---------- store lookup (user_authoritative) ----------


def test_model_user_authoritative_match_boosts_existing(router, store):
    # user first asserts
    d_user = router.route(_claim("likes"), origin="user", source_turn_id=1)
    original_conf = store.get_fact(d_user.stored_fact_id).confidence

    # model later restates the same
    d_model = router.route(_claim("likes"), origin="model", source_turn_id=2)
    assert d_model.outcome is RoutingOutcome.VERIFIED
    assert d_model.matching_fact_id == d_user.stored_fact_id
    assert d_model.stored_fact_id is None  # nothing new inserted
    new_conf = store.get_fact(d_user.stored_fact_id).confidence
    assert new_conf > original_conf


def test_model_user_authoritative_contradiction_flags_correction(router, store):
    router.route(_claim("likes", polarity=1), origin="user", source_turn_id=1)
    d = router.route(_claim("likes", polarity=0), origin="model", source_turn_id=2)
    assert d.outcome is RoutingOutcome.CONTRADICTED
    assert d.contradicting_fact_id is not None
    assert d.correction is not None


def test_model_user_authoritative_miss_stored_unverified(router, store):
    d = router.route(_claim("likes"), origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIED
    f = store.get_fact(d.stored_fact_id)
    assert f.confidence == pytest.approx(0.5)


# ---------- retrieval + unverifiable ----------


def test_retrieval_predicate_stored_low_confidence(router, store):
    claim = _claim(
        "capital_of", subject="Berlin", object="Germany", object_type="entity"
    )
    d = router.route(claim, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.RETRIEVAL_STUB
    f = store.get_fact(d.stored_fact_id)
    assert f.confidence == pytest.approx(0.4)


def test_unverifiable_predicate_flagged(router, store):
    claim = _claim(
        "will_happen",
        subject="weather",
        object="rain tomorrow",
        object_type="string",
    )
    d = router.route(claim, origin="model", source_turn_id=1)
    assert d.outcome is RoutingOutcome.UNVERIFIABLE_FLAGGED
    f = store.get_fact(d.stored_fact_id)
    assert f.confidence == pytest.approx(0.3)


# ---------- guardrails ----------


def test_unknown_predicate_raises(router):
    bad = _claim("fabricated_predicate")
    with pytest.raises(ValueError, match="unknown predicate"):
        router.route(bad, origin="user", source_turn_id=1)


def test_invalid_origin_raises(router):
    with pytest.raises(ValueError, match="origin"):
        router.route(_claim("likes"), origin="assistant", source_turn_id=1)
