"""Phase 6 — session-local fact lifetime invariants.

Encodes the load-bearing bookkeeping invariants for the storage
path:

  * cross-session reaffirmation: append session_id, increment
    ``affirmed_count`` exactly once per NEW session
  * same-session repetition: no count change, no session-ids
    append (principle 3 — same-session repetition is not new
    evidence)
  * session-local storage: ``is_session_local=1``, ``session_ids``
    is a single-element list (CHECK enforced at SQL)
  * marker-with-no-session: marker is ignored, fact stored as
    cross-session, event records the ignored marker
  * coexistence: a session-local + cross-session row of the same
    proposition can coexist in the same session (Q3 — separate
    storage paths, neither matches the other)

Tests cover the storage path alone (``tier_u.store_user_fact``);
lookup-side session filtering is tested in
``test_tier_u_session_model.py``. The risk profile of Phase 6 is
"bookkeeping bug ships silent" — each invariant gets a narrow
test rather than being bundled.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from src.fact_store import DEFAULT_USER_ID, Fact, FactStore
from src.layer4_lookup.tier_u import (
    StoreUserFactOutcome,
    StoreUserFactResult,
    store_user_fact,
)


# ---- fixtures ------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "phase6.db")
    yield s
    s.close()


def _claim(
    *,
    pattern: str = "spatial_temporal",
    predicate: str = "lives_in",
    polarity: int = 1,
    slots: dict | None = None,
    source_text: str = "I live in Williamstown",
) -> dict:
    """Default claim for storage tests — a cross-session-shaped
    self-attribute, same shape across most invariants."""
    if slots is None:
        slots = {"entity": "user", "location": "Williamstown"}
    return {
        "pattern": pattern,
        "predicate": predicate,
        "polarity": polarity,
        "slots": dict(slots),
        "source_text": source_text,
    }


# ============================================================================
# Section 1 — fresh insert (no prior matching fact)
# ============================================================================


def test_fresh_insert_cross_session_with_session_id(store):
    """First cross-session assertion in session A. Per principle 3,
    the assertion is the first independent external evidence event:
    affirmed_count starts at 1, session_ids=[A]. The implementation
    plan body said ``[]`` initially; clarification #7 recommendation
    #1 corrected this to ``[A]``."""
    result = store_user_fact(
        _claim(),
        store,
        current_session="A",
        key_slot_names=["entity", "location"],
    )
    assert isinstance(result, StoreUserFactResult)
    assert result.outcome is StoreUserFactOutcome.INSERTED
    assert result.is_session_local == 0
    assert result.session_ids_after == ["A"]
    assert result.affirmed_count_after == 1
    fact = store.get_fact(result.fact_id)
    assert fact is not None
    assert fact.is_session_local == 0
    assert fact.session_ids == ["A"]
    assert fact.affirmed_count == 1
    assert fact.asserted_by == "user"
    assert fact.verification_status == "user_asserted"


def test_fresh_insert_cross_session_no_active_session(store):
    """No active session (current_session=None). The first assertion
    is still independent evidence so affirmed_count=1, but
    session_ids stays []. Subsequent reaffirmations cannot increment
    because we cannot distinguish same-session from new-session
    without a session id."""
    result = store_user_fact(
        _claim(),
        store,
        current_session=None,
        key_slot_names=["entity", "location"],
    )
    assert result.outcome is StoreUserFactOutcome.INSERTED
    assert result.is_session_local == 0
    assert result.session_ids_after == []
    assert result.affirmed_count_after == 1
    fact = store.get_fact(result.fact_id)
    assert fact.session_ids == []


def test_fresh_insert_session_local_with_marker(store):
    """Marker phrase + active session → session-local. session_ids
    is a single-element list; is_session_local=1; affirmed_count=1
    (the assertion is itself independent evidence)."""
    from src.session_markers import SESSION_SCOPE_MARKERS
    claim = _claim(
        source_text="let's say for this conversation I live in Berlin",
        slots={"entity": "user", "location": "Berlin"},
    )
    result = store_user_fact(
        claim,
        store,
        current_session="A",
        key_slot_names=["entity", "location"],
    )
    assert result.outcome is StoreUserFactOutcome.INSERTED
    assert result.is_session_local == 1
    assert result.session_ids_after == ["A"]
    assert result.affirmed_count_after == 1
    # The regex returns the leftmost match. The text has both
    # "let's say" (leftmost) and "for this conversation" — the
    # captured phrase MUST be one of the two, but precisely which
    # one is regex-internal. Don't pin the phrase; pin that it's a
    # valid marker.
    assert result.marker_detected_phrase is not None
    assert SESSION_SCOPE_MARKERS.fullmatch(
        result.marker_detected_phrase,
    ) is not None


def test_marker_with_no_active_session_falls_through_to_cross_session(store):
    """Marker present but current_session=None: cannot create a
    session-local without a session to scope it to. Fall through
    to cross-session storage. The pipeline event records that the
    marker was detected and ignored — operator can see this in the
    trace UI."""
    claim = _claim(
        source_text="hypothetically, I live in Berlin",
        slots={"entity": "user", "location": "Berlin"},
    )
    result = store_user_fact(
        claim,
        store,
        current_session=None,
        key_slot_names=["entity", "location"],
        source_turn_id=_insert_turn(store),
    )
    assert result.outcome is StoreUserFactOutcome.INSERTED
    assert result.is_session_local == 0  # cross-session despite marker
    assert result.session_ids_after == []
    # Event should record the ignored marker.
    events = _storage_events(store)
    assert len(events) == 1
    payload = events[0]["data"]
    assert payload.get("marker_detected_phrase") is not None
    assert payload.get("marker_ignored_no_session") is True


# ============================================================================
# Section 2 — cross-session reaffirmation
# ============================================================================


def test_reaffirm_in_new_session_appends_and_increments(store):
    """User asserts in A, then again in B (no marker). The matching
    cross-session fact's session_ids becomes [A, B]; affirmed_count
    becomes 2."""
    first = store_user_fact(
        _claim(),
        store,
        current_session="A",
        key_slot_names=["entity", "location"],
    )
    second = store_user_fact(
        _claim(),
        store,
        current_session="B",
        key_slot_names=["entity", "location"],
    )
    assert second.outcome is StoreUserFactOutcome.REAFFIRMED
    assert second.fact_id == first.fact_id
    assert second.session_ids_after == ["A", "B"]
    assert second.affirmed_count_after == 2
    fact = store.get_fact(second.fact_id)
    assert fact.session_ids == ["A", "B"]
    assert fact.affirmed_count == 2


def test_reaffirm_in_same_session_is_noop(store):
    """User asserts in A, then again in A. Same-session repetition
    is not new evidence (principle 3): no append, no count change."""
    first = store_user_fact(
        _claim(),
        store,
        current_session="A",
        key_slot_names=["entity", "location"],
    )
    second = store_user_fact(
        _claim(),
        store,
        current_session="A",
        key_slot_names=["entity", "location"],
    )
    assert second.outcome is StoreUserFactOutcome.NOOP
    assert second.fact_id == first.fact_id
    assert second.session_ids_after == ["A"]
    assert second.affirmed_count_after == 1
    fact = store.get_fact(second.fact_id)
    assert fact.session_ids == ["A"]
    assert fact.affirmed_count == 1


def test_reaffirm_across_three_sessions(store):
    """Three independent sessions all reaffirm: count grows monotonic-
    ally, session_ids preserves insertion order."""
    first = store_user_fact(
        _claim(), store,
        current_session="A", key_slot_names=["entity", "location"],
    )
    store_user_fact(
        _claim(), store,
        current_session="B", key_slot_names=["entity", "location"],
    )
    third = store_user_fact(
        _claim(), store,
        current_session="C", key_slot_names=["entity", "location"],
    )
    assert third.outcome is StoreUserFactOutcome.REAFFIRMED
    assert third.fact_id == first.fact_id
    assert third.session_ids_after == ["A", "B", "C"]
    assert third.affirmed_count_after == 3


def test_reaffirm_revisit_known_session_is_noop(store):
    """Order: session A, then B, then back to A. The third call
    finds A already in session_ids → no change."""
    store_user_fact(
        _claim(), store,
        current_session="A", key_slot_names=["entity", "location"],
    )
    store_user_fact(
        _claim(), store,
        current_session="B", key_slot_names=["entity", "location"],
    )
    third = store_user_fact(
        _claim(), store,
        current_session="A", key_slot_names=["entity", "location"],
    )
    assert third.outcome is StoreUserFactOutcome.NOOP
    assert third.session_ids_after == ["A", "B"]  # order preserved
    assert third.affirmed_count_after == 2


def test_reaffirm_with_no_active_session_is_noop(store):
    """Existing fact in session A; reasserted with current_session=None.
    No session to record — cannot distinguish same-session from
    new-session, so no increment. Conservative principle-3 reading."""
    first = store_user_fact(
        _claim(), store,
        current_session="A", key_slot_names=["entity", "location"],
    )
    second = store_user_fact(
        _claim(), store,
        current_session=None, key_slot_names=["entity", "location"],
    )
    assert second.outcome is StoreUserFactOutcome.NOOP
    assert second.fact_id == first.fact_id
    assert second.session_ids_after == ["A"]  # unchanged
    assert second.affirmed_count_after == 1   # unchanged


# ============================================================================
# Section 3 — session-local same-session repetition
# ============================================================================


def test_session_local_same_session_repeat_is_noop(store):
    """User says "let's say I live in Berlin" twice in session A.
    The session-local fact exists; the second assertion is same-
    session repetition (principle 3) — no count change."""
    claim = _claim(
        source_text="let's say I live in Berlin",
        slots={"entity": "user", "location": "Berlin"},
    )
    first = store_user_fact(
        claim, store,
        current_session="A", key_slot_names=["entity", "location"],
    )
    second = store_user_fact(
        claim, store,
        current_session="A", key_slot_names=["entity", "location"],
    )
    assert first.is_session_local == 1
    assert second.outcome is StoreUserFactOutcome.NOOP
    assert second.fact_id == first.fact_id
    assert second.session_ids_after == ["A"]
    assert second.affirmed_count_after == 1


# ============================================================================
# Section 4 — coexistence (Q3): session-local + cross-session in same session
# ============================================================================


def test_coexistence_session_local_and_cross_session_same_content(store):
    """User says "let's say I live in Berlin" in session A
    (session-local), then later in session A says "I live in Berlin"
    without a marker (cross-session). Both rows coexist; the storage
    path's "match existing" is scoped (cross-session storage only
    matches against cross-session candidates; session-local matches
    against session-locals in current session). The two paths are
    independent."""
    session_local = store_user_fact(
        _claim(
            source_text="let's say I live in Berlin",
            slots={"entity": "user", "location": "Berlin"},
        ),
        store, current_session="A",
        key_slot_names=["entity", "location"],
    )
    cross_session = store_user_fact(
        _claim(
            source_text="I live in Berlin",
            slots={"entity": "user", "location": "Berlin"},
        ),
        store, current_session="A",
        key_slot_names=["entity", "location"],
    )
    assert session_local.outcome is StoreUserFactOutcome.INSERTED
    assert cross_session.outcome is StoreUserFactOutcome.INSERTED
    assert session_local.fact_id != cross_session.fact_id
    assert session_local.is_session_local == 1
    assert cross_session.is_session_local == 0


# ============================================================================
# Section 5 — CHECK constraint and validation
# ============================================================================


def test_check_constraint_rejects_session_local_with_two_sessions(store):
    """Defense-in-depth: a synthetic UPDATE that violates the CHECK
    constraint must be rejected at the SQL layer. This case cannot
    arise from the storage path under correct visibility semantics
    (clarification #7 recommendation #3) — it's tested here so a
    future implementation bug surfaces immediately."""
    fact_id = store.insert_fact(Fact(
        pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "Berlin"},
        polarity=1,
        asserted_by="user", verification_status="user_asserted",
        is_session_local=1, session_ids=["A"],
    ))
    # Try to UPDATE session_ids to length 2 while is_session_local=1.
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "UPDATE facts SET session_ids = ? WHERE id = ?",
            (json.dumps(["A", "B"]), fact_id),
        )
        store._conn.commit()


def test_python_validate_fact_rejects_session_local_with_two_sessions(store):
    """The fact_store's pre-insert validation catches the CHECK
    case before the round trip — same defense, different layer."""
    with pytest.raises(ValueError, match="session_ids may have at most"):
        store.insert_fact(Fact(
            pattern="preference", predicate="likes",
            slots={"agent": "user", "object": "Berlin"},
            polarity=1,
            asserted_by="user", verification_status="user_asserted",
            is_session_local=1, session_ids=["A", "B"],
        ))


# ============================================================================
# Section 6 — confidence recomputation
# ============================================================================


def test_confidence_recomputed_after_reaffirmation(store):
    """confidence = (affirmed+1)/(affirmed+contradicted+2) per
    confidence_from_counts. After a single reaffirmation in a new
    session, confidence should reflect the updated count."""
    first = store_user_fact(
        _claim(), store,
        current_session="A", key_slot_names=["entity", "location"],
    )
    fact_after_first = store.get_fact(first.fact_id)
    # affirmed=1, contradicted=0 → (1+1)/(1+0+2) = 2/3 ≈ 0.6667
    assert fact_after_first.confidence == pytest.approx(2 / 3, abs=1e-9)
    second = store_user_fact(
        _claim(), store,
        current_session="B", key_slot_names=["entity", "location"],
    )
    fact_after_second = store.get_fact(second.fact_id)
    # affirmed=2, contradicted=0 → (2+1)/(2+0+2) = 3/4 = 0.75
    assert fact_after_second.confidence == pytest.approx(0.75, abs=1e-9)
    assert second.confidence_after == pytest.approx(0.75, abs=1e-9)


# ============================================================================
# Section 7 — pipeline events
# ============================================================================


def test_storage_emits_pipeline_event_with_full_payload(store):
    """The storage path emits a tier_u_storage event whose payload
    carries the outcome, fact_id, session_ids_after, count delta,
    is_session_local flag, current_session, and the marker phrase
    (if any). This is the trace-UI's source of truth for storage
    decisions."""
    turn_id = _insert_turn(store)
    result = store_user_fact(
        _claim(
            source_text="let's say I live in Berlin",
            slots={"entity": "user", "location": "Berlin"},
        ),
        store, current_session="A",
        key_slot_names=["entity", "location"],
        source_turn_id=turn_id,
    )
    events = _storage_events(store, turn_id=turn_id)
    assert len(events) == 1
    payload = events[0]["data"]
    assert payload["outcome"] == "inserted"
    assert payload["fact_id"] == result.fact_id
    assert payload["is_session_local"] == 1
    assert payload["session_ids_after"] == ["A"]
    assert payload["affirmed_count_after"] == 1
    assert payload["current_session"] == "A"
    assert payload["marker_detected_phrase"] is not None
    assert "this conversation" in payload["marker_detected_phrase"].lower() \
        or "let's" in payload["marker_detected_phrase"].lower()


def test_reaffirm_emits_event_with_reaffirmed_outcome(store):
    turn_id_a = _insert_turn(store)
    store_user_fact(
        _claim(), store,
        current_session="A", key_slot_names=["entity", "location"],
        source_turn_id=turn_id_a,
    )
    turn_id_b = _insert_turn(store)
    store_user_fact(
        _claim(), store,
        current_session="B", key_slot_names=["entity", "location"],
        source_turn_id=turn_id_b,
    )
    events = _storage_events(store, turn_id=turn_id_b)
    assert len(events) == 1
    payload = events[0]["data"]
    assert payload["outcome"] == "reaffirmed"
    assert payload["session_ids_after"] == ["A", "B"]
    assert payload["affirmed_count_after"] == 2


# ============================================================================
# Section 8 — multi-user isolation
# ============================================================================


def test_different_user_ids_do_not_cross_pollinate(store):
    """User X's session A has nothing to do with user Y's session A.
    The storage path is scoped by user_id; user Y's first assertion
    is INSERTED, not REAFFIRMED, even when user X has the matching
    proposition."""
    store_user_fact(
        _claim(), store,
        current_session="A", key_slot_names=["entity", "location"],
        user_id="user_x",
    )
    result = store_user_fact(
        _claim(), store,
        current_session="A", key_slot_names=["entity", "location"],
        user_id="user_y",
    )
    assert result.outcome is StoreUserFactOutcome.INSERTED
    assert result.affirmed_count_after == 1


# ============================================================================
# Section 9 — closed prior fact ignored
# ============================================================================


def test_closed_prior_fact_does_not_block_new_insert(store):
    """A fact with valid_until set is closed; the storage path should
    treat the proposition as fresh (a new INSERT, count=1) rather than
    REAFFIRMING the closed row. Mirrors lookup semantics — closed
    facts don't participate in current state."""
    first = store_user_fact(
        _claim(), store,
        current_session="A", key_slot_names=["entity", "location"],
    )
    store.close_fact(first.fact_id)
    second = store_user_fact(
        _claim(), store,
        current_session="B", key_slot_names=["entity", "location"],
    )
    assert second.outcome is StoreUserFactOutcome.INSERTED
    assert second.fact_id != first.fact_id
    assert second.affirmed_count_after == 1
    assert second.session_ids_after == ["B"]


# ============================================================================
# Section 10 — boost_confidence vs reaffirm_cross_session distinction
# ============================================================================


def test_boost_confidence_only_increments_count_does_not_touch_session_ids(store):
    """boost_confidence is the operator-action / generic helper —
    increments affirmed_count only, leaves session_ids untouched.
    The storage path uses reaffirm_cross_session for the atomic
    append-and-increment. Both functions exist; they serve
    different callers."""
    first = store_user_fact(
        _claim(), store,
        current_session="A", key_slot_names=["entity", "location"],
    )
    new_conf = store.boost_confidence(first.fact_id)
    fact = store.get_fact(first.fact_id)
    assert fact.affirmed_count == 2
    assert fact.session_ids == ["A"]  # untouched
    assert new_conf == pytest.approx(0.75, abs=1e-9)


def test_reaffirm_cross_session_appends_and_increments_atomically(store):
    """The fact_store helper that the storage path uses. Verifies
    the atomic update: both columns change in one statement."""
    first = store_user_fact(
        _claim(), store,
        current_session="A", key_slot_names=["entity", "location"],
    )
    new_count, new_sessions, new_conf = store.reaffirm_cross_session(
        first.fact_id, "B",
    )
    assert new_count == 2
    assert new_sessions == ["A", "B"]
    assert new_conf == pytest.approx(0.75, abs=1e-9)
    fact = store.get_fact(first.fact_id)
    assert fact.affirmed_count == 2
    assert fact.session_ids == ["A", "B"]


def test_reaffirm_cross_session_idempotent_when_session_already_present(store):
    """Calling reaffirm_cross_session with a session id already in
    the list is a no-op (no append, no increment). The storage
    path uses this to encode same-session repetition cheaply."""
    first = store_user_fact(
        _claim(), store,
        current_session="A", key_slot_names=["entity", "location"],
    )
    new_count, new_sessions, _ = store.reaffirm_cross_session(
        first.fact_id, "A",
    )
    assert new_count == 1  # unchanged
    assert new_sessions == ["A"]


# ============================================================================
# helpers
# ============================================================================


def _insert_turn(store: FactStore) -> int:
    return store.insert_turn("user", "test content")


def _storage_events(
    store: FactStore, turn_id: int | None = None,
) -> list[dict]:
    """All tier_u_storage events; filtered to a turn if given."""
    if turn_id is None:
        rows = store._conn.execute(
            "SELECT * FROM pipeline_events WHERE stage = ? ORDER BY id",
            ("tier_u_storage",),
        ).fetchall()
    else:
        rows = store._conn.execute(
            "SELECT * FROM pipeline_events "
            "WHERE turn_id = ? AND stage = ? ORDER BY id",
            (turn_id, "tier_u_storage"),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "turn_id": r["turn_id"],
            "stage": r["stage"],
            "data": json.loads(r["data"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


# ============================================================================
# Phase 8.6 — raw_text overrides source_text for marker detection
# ============================================================================


def test_raw_text_with_marker_makes_session_local_when_source_text_is_stripped(store):
    """**Phase 8.6 Bug 2 fix.** Real chat extractors strip session-
    marker phrases from ``source_text`` ("Let's say for this
    conversation I live in Williamsburg" → source_text "I live in
    Williamsburg"). Pre-fix, the marker check ran on source_text and
    missed; the fact got stored cross-session even though the user
    explicitly bounded it.

    Phase 8.6 threads the raw turn text through ``store_user_fact``'s
    ``raw_text`` parameter; the marker check uses raw_text when
    provided. Verify: source_text without the marker, raw_text WITH
    the marker, current_session active → fact lands as session-local."""
    claim = _claim(source_text="I live in Williamsburg")  # marker stripped
    result = store_user_fact(
        claim, store,
        current_session="session-1",
        key_slot_names=["entity", "location"],
        raw_text="Let's say for this conversation I live in Williamsburg",
    )
    assert result.outcome is StoreUserFactOutcome.INSERTED
    assert result.is_session_local == 1
    assert result.session_ids_after == ["session-1"]
    # The regex's "let's say" alternative matches first (leftmost) over
    # "for this conversation"; either match satisfies the marker check.
    assert result.marker_detected_phrase is not None
    assert (result.marker_detected_phrase or "").lower() in (
        "let's say", "for this conversation",
    ) or "let's say" in (result.marker_detected_phrase or "").lower()


def test_raw_text_without_marker_preserves_cross_session(store):
    """When neither raw_text nor source_text contains a marker, the
    fact is cross-session — Phase 8.6 doesn't change the no-marker
    default behavior."""
    claim = _claim(source_text="I live in Williamsburg")
    result = store_user_fact(
        claim, store,
        current_session="session-1",
        key_slot_names=["entity", "location"],
        raw_text="I live in Williamsburg",
    )
    assert result.outcome is StoreUserFactOutcome.INSERTED
    assert result.is_session_local == 0


def test_source_text_marker_still_works_when_raw_text_omitted(store):
    """Backwards compatibility: when ``raw_text`` is None, the marker
    check falls back to ``source_text``. Pre-Phase-8.6 callers (tests,
    direct invocations) keep working without modification."""
    claim = _claim(
        source_text="Let's say for this conversation I live in Williamsburg"
    )
    result = store_user_fact(
        claim, store,
        current_session="session-1",
        key_slot_names=["entity", "location"],
        # raw_text=None (default) — falls back to source_text
    )
    assert result.outcome is StoreUserFactOutcome.INSERTED
    assert result.is_session_local == 1


def test_raw_text_takes_precedence_over_source_text(store):
    """If both raw_text and source_text supply marker context, raw_text
    wins. The intent is that raw_text is the authoritative source —
    extractor projection must not override the original utterance."""
    # raw_text has NO marker; source_text DOES — under Phase 8.6,
    # raw_text wins, so the fact is cross-session (no marker recognized).
    claim = _claim(
        source_text="Let's say for this conversation I live in Williamsburg",
    )
    result = store_user_fact(
        claim, store,
        current_session="session-1",
        key_slot_names=["entity", "location"],
        raw_text="I live in Williamsburg",  # no marker — wins
    )
    assert result.outcome is StoreUserFactOutcome.INSERTED
    assert result.is_session_local == 0, (
        "raw_text without marker must override source_text with marker"
    )
