"""Tests for src.fact_store under the v0.14 schema.

Schema deltas vs. v0.13 covered here:

  * ``reinforcement_count`` -> ``affirmed_count`` (rename)
  * new ``contradicted_count``
  * new ``is_session_local``
  * ``session_id`` removed
  * ``session_ids`` JSON array, with CHECK constraint enforcing
    array length <= 1 when ``is_session_local = 1``

Phase 0 ports the read/write surface; Phase 6 wires the session
filter and the contradicted-count write path. Tests below cover the
schema as shipped now.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from src.fact_store import Fact, FactStore


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
    confidence=0.5,
    asserted_by="user",
    verification_status="user_asserted",
    source_turn_id=None,
    source_text=None,
    affirmed_count=0,
    contradicted_count=0,
    is_session_local=0,
    session_ids=None,
):
    slots = slots if slots is not None else {"agent": "user", "object": "pb"}
    session_ids = list(session_ids) if session_ids is not None else []
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
        affirmed_count=affirmed_count,
        contradicted_count=contradicted_count,
        is_session_local=is_session_local,
        session_ids=session_ids,
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
    # v0.14 defaults round-trip cleanly.
    assert f.affirmed_count == 0
    assert f.contradicted_count == 0
    assert f.is_session_local == 0
    assert f.session_ids == []


def test_insert_serializes_slots_as_json(store):
    fid = store.insert_fact(
        _mk(slots={"agent": "user", "object": "pb", "intensity": "strong"})
    )
    f = store.get_fact(fid)
    assert f.slots["intensity"] == "strong"


def test_facts_flat_view_projects_subject_and_object(store):
    """The flat view picks the canonical subject/object slot for each pattern."""
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


def test_facts_flat_view_projects_mereological_part_and_whole(store):
    """v0.14 Phase 1: a mereological row's `part` slot projects as
    subject and `whole` projects as object via facts_flat."""
    fid = store.insert_fact(
        _mk(
            pattern="mereological",
            predicate="part_of",
            slots={"part": "Williamstown", "whole": "Massachusetts"},
        )
    )
    row = store._conn.execute(
        "SELECT subject, object, pattern FROM facts_flat WHERE id = ?",
        (fid,),
    ).fetchone()
    assert row["pattern"] == "mereological"
    assert row["subject"] == "Williamstown"
    assert row["object"] == "Massachusetts"


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
    assert store.find_currently_valid(
        "preference", "likes", {"agent": "user", "object": "pb"}
    ) == []


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
    """v0.14: boost_confidence increments affirmed_count and recomputes
    confidence as confidence_from_counts(affirmed, contradicted)."""
    from src.layer2_routing.constants import confidence_from_counts
    fid = store.insert_fact(_mk(confidence=confidence_from_counts(0, 0)))
    new = store.boost_confidence(fid)
    assert new == pytest.approx(confidence_from_counts(1, 0))
    new2 = store.boost_confidence(fid)
    assert new2 == pytest.approx(confidence_from_counts(2, 0))
    assert new2 > new
    # The post-boost row reflects the count.
    row = store._conn.execute(
        "SELECT affirmed_count, contradicted_count FROM facts WHERE id = ?",
        (fid,),
    ).fetchone()
    assert row["affirmed_count"] == 2
    assert row["contradicted_count"] == 0


def test_boost_confidence_uses_existing_contradicted_count(store):
    """If contradicted_count is non-zero (set out-of-band, since
    Phase 0 has no public writer), boost_confidence reads it into the
    formula. Phase 6 introduces the writer; the formula honors the
    column today."""
    fid = store.insert_fact(_mk())
    # Direct UPDATE to simulate a contradicted_count write that Phase 6
    # will introduce. Tests of the read path don't need the writer yet.
    store._conn.execute(
        "UPDATE facts SET contradicted_count = 2 WHERE id = ?", (fid,),
    )
    store._conn.commit()
    from src.layer2_routing.constants import confidence_from_counts
    new = store.boost_confidence(fid)
    assert new == pytest.approx(confidence_from_counts(1, 2))


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
    store.insert_fact(_mk())
    store.insert_fact(_mk(asserted_by="model", verification_status="verified"))
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


def test_insert_rejects_bad_is_session_local(store):
    with pytest.raises(ValueError, match="is_session_local"):
        store.insert_fact(_mk(is_session_local=2))  # type: ignore[arg-type]


def test_insert_rejects_non_list_session_ids(store):
    # Build the Fact directly to bypass _mk's list() coercion of the
    # session_ids kwarg — the assertion under test is that the
    # validator rejects a non-list.
    bad = Fact(
        pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "pb"},
        polarity=1, asserted_by="user",
        verification_status="user_asserted",
        session_ids="not a list",  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="session_ids"):
        store.insert_fact(bad)


# ---------- v0.14 statuses (unchanged from v0.3 enum) ----------


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
def test_all_statuses_accepted(store, status):
    store.insert_fact(_mk(verification_status=status))


# ---------- pipeline stages ----------


def test_pipeline_stages_accepted(store):
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


# ---------- subscriber registry ----------


def test_pipeline_event_subscriber_fires_on_insert(store):
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
    tid = store.insert_turn("user", "hi")
    other_received: list = []
    store.register_event_subscriber(
        lambda t, s, d: (_ for _ in ()).throw(RuntimeError("bad sub"))
    )
    store.register_event_subscriber(
        lambda t, s, d: other_received.append(s)
    )
    store.insert_pipeline_event(tid, "user_extraction", {})
    events = store.get_pipeline_events(tid)
    assert len(events) == 1
    assert other_received == ["user_extraction"]


def test_pipeline_event_unregister_unknown_token_is_safe(store):
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


# ---------- v0.14 schema invariants ----------


def _columns_of(store, table: str) -> set[str]:
    rows = store._conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def test_facts_has_v014_columns(store):
    cols = _columns_of(store, "facts")
    assert "affirmed_count" in cols
    assert "contradicted_count" in cols
    assert "is_session_local" in cols
    assert "session_ids" in cols


def test_facts_does_not_have_legacy_columns(store):
    """v0.14 renames reinforcement_count and removes session_id."""
    cols = _columns_of(store, "facts")
    assert "reinforcement_count" not in cols
    assert "session_id" not in cols


def test_session_ids_default_is_empty_array(store):
    """A fact inserted without explicit session_ids round-trips as []."""
    fid = store.insert_fact(_mk())
    f = store.get_fact(fid)
    assert f.session_ids == []
    # The raw row stores it as the JSON literal '[]'.
    raw = store._conn.execute(
        "SELECT session_ids FROM facts WHERE id = ?", (fid,)
    ).fetchone()
    assert raw["session_ids"] == "[]"


def test_check_constraint_rejects_session_local_with_two_ids(store):
    """is_session_local=1 + len(session_ids)=2 violates the CHECK
    constraint at the SQL layer."""
    # Bypass the Python-side validator by writing directly via the
    # connection — the assertion under test is that the DB itself
    # enforces the constraint.
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            """
            INSERT INTO facts (
                pattern, predicate, slots, polarity, confidence,
                affirmed_count, contradicted_count,
                is_session_local, session_ids,
                asserted_by, verification_status, valid_from, valid_until,
                source_turn_id, source_text, created_at, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "preference", "likes",
                '{"agent":"user","object":"pb"}',
                1, 0.5, 0, 0,
                1, '["a", "b"]',  # is_session_local=1 with len-2 array
                "user", "user_asserted",
                "2026-01-01T00:00:00+00:00", None,
                None, None,
                "2026-01-01T00:00:00+00:00", "default_user",
            ),
        )
        store._conn.commit()


def test_check_constraint_allows_session_local_with_one_id(store):
    """is_session_local=1 + len(session_ids)=1 is the canonical
    session-local fact shape."""
    fid = store.insert_fact(
        _mk(is_session_local=1, session_ids=["sess_a"])
    )
    f = store.get_fact(fid)
    assert f.is_session_local == 1
    assert f.session_ids == ["sess_a"]


def test_check_constraint_allows_session_local_with_zero_ids(store):
    """A session-local fact with an empty session_ids list is
    permitted by the CHECK (0 <= 1). Production callers will always
    populate it, but the constraint is upper-bound only."""
    fid = store.insert_fact(_mk(is_session_local=1, session_ids=[]))
    f = store.get_fact(fid)
    assert f.is_session_local == 1
    assert f.session_ids == []


def test_check_constraint_allows_cross_session_with_many_ids(store):
    """is_session_local=0 has no length constraint on session_ids:
    cross-session facts accumulate the set of sessions that
    reaffirmed them."""
    fid = store.insert_fact(
        _mk(is_session_local=0, session_ids=["s1", "s2", "s3", "s4"])
    )
    f = store.get_fact(fid)
    assert f.is_session_local == 0
    assert f.session_ids == ["s1", "s2", "s3", "s4"]


def test_python_validator_rejects_session_local_with_two_ids(store):
    """Belt-and-suspenders: the Python validator catches the same
    condition before the SQL CHECK does, with a clearer error."""
    with pytest.raises(ValueError, match="session_ids"):
        store.insert_fact(
            _mk(is_session_local=1, session_ids=["a", "b"])
        )


# ---------- Phase 2: routing_memo schema ----------


def test_routing_memo_table_exists(store):
    """Phase 2 adds the routing_memo table. Pin its column shape so
    schema drift surfaces here before it breaks the RoutingMemo
    wrapper."""
    cols = _columns_of(store, "routing_memo")
    assert cols == {
        "pattern", "predicate", "method", "reason",
        "affirmed_count", "contradicted_count",
        "created_at", "last_consulted_at",
    }


def test_reset_drops_routing_memo(store):
    """reset() must wipe the routing_memo table along with everything
    else — otherwise the dev-loop reset button would leave stale memo
    rows around."""
    store._conn.execute(
        "INSERT INTO routing_memo (pattern, predicate, method, reason, "
        "created_at) VALUES (?, ?, ?, ?, ?)",
        ("preference", "likes", "user_authoritative", "x", "2026-01-01"),
    )
    store._conn.commit()
    before = store._conn.execute(
        "SELECT COUNT(*) AS n FROM routing_memo"
    ).fetchone()
    assert before["n"] == 1
    store.reset()
    after = store._conn.execute(
        "SELECT COUNT(*) AS n FROM routing_memo"
    ).fetchone()
    assert after["n"] == 0


def test_routing_memo_pipeline_stages_accepted(store):
    """The three Phase 2 events must be on the accepted-stages list."""
    tid = store.insert_turn("user", "test")
    store.insert_pipeline_event(tid, "routing_validation_failed", {})
    store.insert_pipeline_event(tid, "routing_memo_hit", {})
    store.insert_pipeline_event(tid, "routing_memo_write", {})
    events = store.get_pipeline_events(tid)
    stages = {e["stage"] for e in events}
    assert {
        "routing_validation_failed",
        "routing_memo_hit",
        "routing_memo_write",
    } <= stages
