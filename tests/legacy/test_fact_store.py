"""Tests for src.fact_store under the v0.3 pattern/slots schema."""

from __future__ import annotations

import json
import time

import pytest

from src.legacy.fact_store import Fact, FactStore


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "t.db")
    yield s
    s.close()


def _mk(
    pattern="preference",
    predicate="likes",
    slots=None,
    polarity=1,
    confidence=0.5,  # Beta(1,1) prior at zero counts
    asserted_by="user",
    verification_status="user_asserted",
    source_turn_id=None,
    source_text=None,
):
    slots = slots if slots is not None else {"agent": "user", "object": "pb"}
    return Fact(
        pattern=pattern,
        predicate=predicate,
        slots=slots,
        polarity=polarity,
        confidence=confidence,
        asserted_by=asserted_by,
        verification_status=verification_status,
        source_turn_id=source_turn_id,
        source_text=source_text,
    )


# ---------- insert / get ----------


def test_insert_and_get(store):
    fid = store.insert_fact(_mk())
    f = store.get_fact(fid)
    assert f is not None
    assert f.id == fid
    assert f.pattern == "preference"
    assert f.predicate == "likes"
    assert f.slots == {"agent": "user", "object": "pb"}
    assert f.valid_from is not None
    assert f.valid_until is None


def test_insert_serializes_slots_as_json(store):
    fid = store.insert_fact(
        _mk(slots={"agent": "user", "object": "pb", "intensity": "strong"})
    )
    f = store.get_fact(fid)
    assert f.slots["intensity"] == "strong"


def test_facts_flat_view_projects_subject_and_object(store):
    """The flat view should pick the canonical subject/object slot for each pattern."""
    store.insert_fact(_mk(slots={"agent": "user", "object": "peanut butter"}))
    store.insert_fact(
        _mk(
            pattern="categorical",
            predicate="is_a",
            slots={"entity": "Marie Curie", "category": "physicist"},
        )
    )
    rows = store._conn.execute(
        "SELECT subject, object, pattern FROM facts_flat ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["subject"] == "user"
    assert rows[0]["object"] == "peanut butter"
    assert rows[1]["subject"] == "Marie Curie"
    assert rows[1]["object"] == "physicist"


# ---------- find / contradictions ----------


def test_find_currently_valid_matches_slots(store):
    store.insert_fact(_mk(slots={"agent": "user", "object": "pb"}))
    found = store.find_currently_valid(
        "preference", predicate="likes", slot_match={"agent": "user", "object": "pb"}
    )
    assert len(found) == 1


def test_find_contradictions_polarity_flip(store):
    store.insert_fact(_mk(polarity=1))
    neg = store.find_contradictions(
        "preference", "likes", {"agent": "user", "object": "pb"}, polarity=0
    )
    assert len(neg) == 1
    pos = store.find_contradictions(
        "preference", "likes", {"agent": "user", "object": "pb"}, polarity=1
    )
    assert pos == []


def test_close_fact_excludes_from_currently_valid(store):
    fid = store.insert_fact(_mk())
    store.close_fact(fid)
    assert store.find_currently_valid("preference", "likes",
                                      {"agent": "user", "object": "pb"}) == []


def test_close_and_reopen_with_opposite_polarity(store):
    fid1 = store.insert_fact(_mk(polarity=1))
    time.sleep(0.001)
    old = store.find_contradictions(
        "preference", "likes", {"agent": "user", "object": "pb"}, polarity=0
    )
    assert len(old) == 1 and old[0].id == fid1
    store.close_fact(fid1)
    fid2 = store.insert_fact(_mk(polarity=0))
    valid = store.find_currently_valid(
        "preference", predicate="likes", slot_match={"agent": "user", "object": "pb"}
    )
    assert len(valid) == 1 and valid[0].id == fid2


def test_boost_confidence_recomputes_from_counts(store):
    """v0.13: boost_confidence increments reinforcement_count and
    recomputes confidence as confidence_from_counts(R, 0). The old
    flat-step + cap behavior is gone."""
    from src.legacy.router.constants import confidence_from_counts
    fid = store.insert_fact(_mk(confidence=confidence_from_counts(0, 0)))
    new = store.boost_confidence(fid)
    # First boost: reinforcement_count becomes 1.
    assert new == pytest.approx(confidence_from_counts(1, 0))
    new2 = store.boost_confidence(fid)
    assert new2 == pytest.approx(confidence_from_counts(2, 0))
    assert new2 > new


def test_query_facts_filters(store):
    store.insert_fact(_mk(pattern="preference", predicate="likes",
                          slots={"agent": "user", "object": "pb"}))
    store.insert_fact(
        _mk(
            pattern="categorical",
            predicate="is_a",
            slots={"entity": "Marie Curie", "category": "physicist"},
            asserted_by="model",
            verification_status="verified",
        )
    )
    assert len(store.query_facts()) == 2
    assert len(store.query_facts(pattern="preference")) == 1
    assert len(store.query_facts(predicate="is_a")) == 1
    assert len(store.query_facts(asserted_by="model")) == 1
    assert len(store.query_facts(verification_status="verified")) == 1


def test_all_user_facts_only_returns_user_asserted_valid_rows(store):
    store.insert_fact(_mk())  # user_asserted, valid
    store.insert_fact(
        _mk(asserted_by="model", verification_status="verified")
    )
    user_facts = store.all_user_facts()
    assert len(user_facts) == 1
    assert user_facts[0].asserted_by == "user"


# ---------- validation ----------


def test_insert_rejects_missing_pattern(store):
    with pytest.raises(ValueError, match="pattern"):
        store.insert_fact(_mk(pattern=""))


def test_insert_rejects_non_dict_slots(store):
    with pytest.raises(ValueError, match="slots"):
        store.insert_fact(_mk(slots="not a dict"))  # type: ignore[arg-type]


def test_insert_rejects_unknown_verification_status(store):
    with pytest.raises(ValueError, match="verification_status"):
        store.insert_fact(_mk(verification_status="unknown_state"))


def test_insert_rejects_bad_polarity(store):
    with pytest.raises(ValueError, match="polarity"):
        store.insert_fact(_mk(polarity=2))


def test_insert_rejects_bad_confidence(store):
    with pytest.raises(ValueError, match="confidence"):
        store.insert_fact(_mk(confidence=1.5))


# ---------- new statuses (Section 6 split) ----------


@pytest.mark.parametrize(
    "status",
    [
        "verified",
        "contradicted",
        "user_asserted",
        "unverifiable_in_principle",
        "retrieval_inconclusive",
        "retrieval_failed",
        "unverifiable_pending_implementation",
        "routing_anomaly",
    ],
)
def test_all_v03_statuses_accepted(store, status):
    store.insert_fact(_mk(verification_status=status))


# ---------- new pipeline stages ----------


def test_v03_pipeline_stages_accepted(store):
    tid = store.insert_turn("user", "hi")
    for stage in (
        "user_extraction", "user_storage", "assistant_draft",
        "assistant_extraction", "verification", "correction", "final",
        "routing_anomaly_detected", "retrieval_query_attempt", "verifier_failure",
    ):
        store.insert_pipeline_event(tid, stage, {})
    events = store.get_pipeline_events(tid)
    assert len(events) == 10


def test_pipeline_event_rejects_unknown_stage(store):
    tid = store.insert_turn("user", "hi")
    with pytest.raises(ValueError, match="stage"):
        store.insert_pipeline_event(tid, "unknown_stage", {})


# ---------- subscriber registry (live SSE flow view) ----------


def test_pipeline_event_subscriber_fires_on_insert(store):
    """A registered subscriber receives every successful insert with
    (turn_id, stage, data). This is what /api/chat/stream uses to
    push events to the SSE consumer in real time."""
    tid = store.insert_turn("user", "hi")
    received: list[tuple] = []
    token = store.register_event_subscriber(
        lambda t, s, d: received.append((t, s, d))
    )
    store.insert_pipeline_event(tid, "user_extraction", {"x": 1})
    store.insert_pipeline_event(tid, "verification", {"decisions": []})
    assert received == [
        (tid, "user_extraction", {"x": 1}),
        (tid, "verification", {"decisions": []}),
    ]
    store.unregister_event_subscriber(token)


def test_pipeline_event_subscriber_unregister_stops_callbacks(store):
    tid = store.insert_turn("user", "hi")
    received: list = []
    token = store.register_event_subscriber(lambda t, s, d: received.append(s))
    store.insert_pipeline_event(tid, "user_extraction", {})
    store.unregister_event_subscriber(token)
    store.insert_pipeline_event(tid, "verification", {})
    assert received == ["user_extraction"]


def test_pipeline_event_subscriber_exception_does_not_break_insert(store):
    """A buggy subscriber must not crash the turn — insert still
    succeeds, the row is durable, other subscribers still fire."""
    tid = store.insert_turn("user", "hi")
    other_received: list = []
    store.register_event_subscriber(
        lambda t, s, d: (_ for _ in ()).throw(RuntimeError("bad sub"))
    )
    store.register_event_subscriber(
        lambda t, s, d: other_received.append(s)
    )
    # Should not raise.
    store.insert_pipeline_event(tid, "user_extraction", {})
    # Row is persisted.
    events = store.get_pipeline_events(tid)
    assert len(events) == 1
    # Healthy subscriber still got the call.
    assert other_received == ["user_extraction"]


def test_pipeline_event_unregister_unknown_token_is_safe(store):
    # Idempotent — no error if the token isn't currently registered.
    store.unregister_event_subscriber(lambda: None)


# ---------- turns ----------


def test_insert_and_get_turn(store):
    tid = store.insert_turn("user", "hello")
    t = store.get_turn(tid)
    assert t["role"] == "user"
    assert t["content"] == "hello"
    assert t["original_content"] is None


def test_update_turn_content_preserves_original(store):
    tid = store.insert_turn("assistant", "wrong")
    store.update_turn_content(tid, "right", "wrong")
    t = store.get_turn(tid)
    assert t["content"] == "right"
    assert t["original_content"] == "wrong"


def test_list_turns_in_order(store):
    a = store.insert_turn("user", "a")
    b = store.insert_turn("assistant", "b")
    c = store.insert_turn("user", "c")
    ids = [t["id"] for t in store.list_turns()]
    assert ids == [a, b, c]


# ---------- retrieval cache ----------


def test_retrieval_cache_roundtrip(store):
    snippets = [{"title": "t", "snippet": "s", "url": "u"}]
    store.cache_retrieval("a query", snippets)
    cached = store.get_cached_retrieval("a query", ttl_seconds=3600)
    assert cached == snippets


def test_retrieval_cache_expiry(store):
    store.cache_retrieval("q", [{"x": 1}])
    assert store.get_cached_retrieval("q", ttl_seconds=0) is None


def test_reset_clears_everything(store):
    store.insert_fact(_mk())
    tid = store.insert_turn("user", "hi")
    store.insert_pipeline_event(tid, "user_extraction", {})
    store.cache_retrieval("q", [{}])
    store.reset()
    assert store.query_facts() == []
    assert store.list_turns() == []
    assert store.get_pipeline_events(tid) == []
    assert store.get_cached_retrieval("q", 3600) is None
