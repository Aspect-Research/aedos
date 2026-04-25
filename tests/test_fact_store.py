"""Tests for src.fact_store."""

from __future__ import annotations

import time

import pytest

from src.fact_store import Fact, FactStore


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "t.db")
    yield s
    s.close()


def _mk(
    subject="user",
    predicate="likes",
    object="pb",
    object_type="entity",
    polarity=1,
    confidence=0.95,
    asserted_by="user",
    verification_status="user_asserted",
    source_turn_id=None,
    source_text=None,
):
    return Fact(
        subject=subject,
        predicate=predicate,
        object=object,
        object_type=object_type,
        polarity=polarity,
        confidence=confidence,
        asserted_by=asserted_by,
        verification_status=verification_status,
        source_turn_id=source_turn_id,
        source_text=source_text,
    )


def test_insert_and_get(store):
    fid = store.insert_fact(_mk())
    f = store.get_fact(fid)
    assert f is not None
    assert f.id == fid
    assert f.subject == "user"
    assert f.predicate == "likes"
    assert f.object == "pb"
    assert f.valid_from is not None, "valid_from should default to now"
    assert f.valid_until is None, "new facts are open-ended"


def test_find_currently_valid_case_insensitive(store):
    store.insert_fact(_mk(subject="User", object="Peanut Butter"))
    found = store.find_currently_valid("user", "likes", "peanut butter")
    assert len(found) == 1
    assert found[0].subject == "User"


def test_find_contradictions_polarity_flip(store):
    store.insert_fact(_mk(polarity=1))
    neg = store.find_contradictions("user", "likes", "pb", polarity=0)
    assert len(neg) == 1, "a positive fact contradicts a negative claim"
    pos = store.find_contradictions("user", "likes", "pb", polarity=1)
    assert pos == [], "a positive fact does not contradict itself"


def test_close_fact_sets_valid_until_and_lowers_confidence(store):
    fid = store.insert_fact(_mk(confidence=0.9))
    store.close_fact(fid)
    f = store.get_fact(fid)
    assert f.valid_until is not None
    assert f.confidence <= 0.5


def test_close_then_find_currently_valid_excludes_closed(store):
    fid = store.insert_fact(_mk())
    store.close_fact(fid)
    assert store.find_currently_valid("user", "likes", "pb") == []


def test_close_and_reopen_with_opposite_polarity(store):
    """Temporal workflow: user asserts X, later asserts not-X."""
    fid1 = store.insert_fact(_mk(polarity=1))
    time.sleep(0.001)

    # contradiction workflow:
    old = store.find_contradictions("user", "likes", "pb", polarity=0)
    assert len(old) == 1 and old[0].id == fid1
    store.close_fact(fid1)

    fid2 = store.insert_fact(_mk(polarity=0))
    valid = store.find_currently_valid("user", "likes", "pb")
    assert len(valid) == 1
    assert valid[0].id == fid2
    assert valid[0].polarity == 0


def test_boost_confidence_caps_at_0_99(store):
    fid = store.insert_fact(_mk(confidence=0.98))
    new = store.boost_confidence(fid, amount=0.5)
    assert new == pytest.approx(0.99)


def test_boost_unknown_fact_raises(store):
    with pytest.raises(LookupError):
        store.boost_confidence(999)


def test_query_facts_filters(store):
    store.insert_fact(_mk(subject="user", predicate="likes", object="pb"))
    store.insert_fact(_mk(subject="user", predicate="dislikes", object="olives"))
    store.insert_fact(
        _mk(
            subject="strawberry",
            predicate="has_count",
            object='{"item":"p","count":3}',
            object_type="count",
            polarity=1,
            confidence=0.99,
            asserted_by="model",
            verification_status="contradicted",
        )
    )

    assert len(store.query_facts()) == 3
    assert len(store.query_facts(subject="user")) == 2
    assert len(store.query_facts(predicate="likes")) == 1
    assert len(store.query_facts(asserted_by="model")) == 1
    assert len(store.query_facts(verification_status="contradicted")) == 1


def test_all_user_facts_excludes_closed(store):
    fid = store.insert_fact(_mk())
    assert len(store.all_user_facts()) == 1
    store.close_fact(fid)
    assert store.all_user_facts() == []


def test_insert_rejects_bad_object_type(store):
    bad = _mk(object_type="banana")
    with pytest.raises(ValueError, match="object_type"):
        store.insert_fact(bad)


def test_insert_rejects_bad_polarity(store):
    bad = _mk(polarity=2)
    with pytest.raises(ValueError, match="polarity"):
        store.insert_fact(bad)


def test_insert_rejects_bad_confidence(store):
    bad = _mk(confidence=1.5)
    with pytest.raises(ValueError, match="confidence"):
        store.insert_fact(bad)


# ---------------- turns ----------------


def test_insert_and_get_turn(store):
    tid = store.insert_turn("user", "hello")
    t = store.get_turn(tid)
    assert t is not None
    assert t["role"] == "user"
    assert t["content"] == "hello"
    assert t["original_content"] is None


def test_update_turn_content_preserves_original(store):
    tid = store.insert_turn("assistant", "strawberry has 2 p's")
    store.update_turn_content(tid, "strawberry has 0 p's", "strawberry has 2 p's")
    t = store.get_turn(tid)
    assert t["content"] == "strawberry has 0 p's"
    assert t["original_content"] == "strawberry has 2 p's"


def test_insert_turn_rejects_bad_role(store):
    with pytest.raises(ValueError, match="role"):
        store.insert_turn("system", "nope")


def test_list_turns_in_order(store):
    a = store.insert_turn("user", "a")
    b = store.insert_turn("assistant", "b")
    c = store.insert_turn("user", "c")
    ids = [t["id"] for t in store.list_turns()]
    assert ids == [a, b, c]


# ---------------- pipeline events ----------------


def test_pipeline_event_roundtrip(store):
    tid = store.insert_turn("user", "hi")
    payload = {"claims": [{"subject": "user", "predicate": "likes", "object": "pb"}]}
    eid = store.insert_pipeline_event(tid, "user_extraction", payload)
    events = store.get_pipeline_events(tid)
    assert len(events) == 1
    assert events[0]["id"] == eid
    assert events[0]["stage"] == "user_extraction"
    assert events[0]["data"] == payload


def test_pipeline_event_rejects_unknown_stage(store):
    tid = store.insert_turn("user", "hi")
    with pytest.raises(ValueError, match="stage"):
        store.insert_pipeline_event(tid, "unknown_stage", {})


def test_pipeline_events_ordered_by_id(store):
    tid = store.insert_turn("user", "hi")
    a = store.insert_pipeline_event(tid, "user_extraction", {"n": 1})
    b = store.insert_pipeline_event(tid, "assistant_draft", {"n": 2})
    c = store.insert_pipeline_event(tid, "verification", {"n": 3})
    events = store.get_pipeline_events(tid)
    assert [e["id"] for e in events] == [a, b, c]
    assert [e["data"]["n"] for e in events] == [1, 2, 3]


def test_reset_clears_everything(store):
    store.insert_fact(_mk())
    tid = store.insert_turn("user", "hi")
    store.insert_pipeline_event(tid, "user_extraction", {})
    store.reset()
    assert store.query_facts() == []
    assert store.list_turns() == []
    assert store.get_pipeline_events(tid) == []
