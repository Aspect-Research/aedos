"""Tests for src.layer2_routing.router (Phase 2 orchestrator).

Phase 2 router is classification only — no verifier dispatch. Tests
exercise the orchestrator's flow:

  1. validator anomaly → ROUTING_ANOMALY, no LLM call, no memo write
  2. memo hit → CLASSIFIED + memo_hit=True, no LLM call, last_consulted_at bumped
  3. memo miss → CLASSIFIED + memo_hit=False, LLM called once, memo row written
  4. five method classifications happy-path (using stub routing_fn)
  5. four anomaly classes route through validator (one per invariant)
  6. pipeline events emitted (routing_decision, routing_memo_hit/write,
     routing_validation_failed, routing_anomaly_detected)
  7. guardrails: missing routing_fn on miss raises; unknown pattern
     reaches validator anomaly path

Live-LLM end-to-end tests for the same five methods live in
tests/v2/test_routing_memo_calibration.py (RUN_API_TESTS gated).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from src.fact_store import FactStore
from src.layer1_extraction.pattern_registry import (
    load_default_registry,
    reset_cache,
)
from src.layer2_routing.llm_router import RoutingDecision
from src.layer2_routing.router import Router
from src.layer2_routing.routing_memo import RoutingMemo
from src.layer2_routing.types import RoutingOutcome


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_cache()
    yield
    reset_cache()


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "router.db")
    yield s
    s.close()


@dataclass
class StubRoutingFn:
    """Queueable routing function. Pop one decision per call.

    Tracks ``calls`` so tests can assert the LLM router was (or was
    not) invoked. The orchestrator must NOT call this on memo hit
    or routing-anomaly paths — those branches are tested via call
    count.
    """

    decisions: list[RoutingDecision] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def __call__(self, claim):
        self.calls.append(claim)
        if not self.decisions:
            raise RuntimeError("StubRoutingFn has no queued decision")
        return self.decisions.pop(0)


def _make_router(store, *, decisions=None, memo=None):
    fn = StubRoutingFn(decisions=list(decisions or []))
    r = Router(
        store, load_default_registry(),
        routing_fn=fn,
        memo=memo,
    )
    return r, fn


def _claim(pattern, predicate, slots, polarity=1, source_text="<src>"):
    return {
        "pattern": pattern, "predicate": predicate, "slots": slots,
        "polarity": polarity, "source_text": source_text,
    }


def _python_decision():
    return RoutingDecision(
        method="python", reason="pure",
        python_inputs_self_contained=True,
    )


def _retrieval_decision(query="x"):
    return RoutingDecision(
        method="retrieval", reason="external",
        retrieval_query_hint=query,
    )


def _user_auth_decision():
    return RoutingDecision(method="user_authoritative", reason="about user")


def _unverifiable_decision():
    return RoutingDecision(method="unverifiable", reason="judgment")


def _ccc_decision():
    return RoutingDecision(
        method="python_with_canonical_constants",
        reason="needs canon",
        python_inputs_self_contained=False,
        canonical_constants_needed=["list of US states"],
    )


# ---- five method classifications (stub routing_fn) ----


def test_classifies_python(store):
    router, fn = _make_router(store, decisions=[_python_decision()])
    claim = _claim(
        "quantitative", "has_count",
        {"subject": "strawberry", "property": "letter_r", "value": 3},
    )
    d = router.classify(claim, source_turn_id=1)
    assert d.outcome is RoutingOutcome.CLASSIFIED
    assert d.method == "python"
    assert d.memo_hit is False
    assert len(fn.calls) == 1


def test_classifies_python_with_canonical_constants(store):
    router, fn = _make_router(store, decisions=[_ccc_decision()])
    claim = _claim(
        "quantitative", "us_states_starting_with_letter",
        {"subject": "US states", "property": "starting_with_A", "value": 4},
    )
    d = router.classify(claim, source_turn_id=1)
    assert d.method == "python_with_canonical_constants"
    assert d.routing_decision["canonical_constants_needed"] == ["list of US states"]


def test_classifies_retrieval(store):
    router, fn = _make_router(store, decisions=[_retrieval_decision("Tokyo population")])
    claim = _claim(
        "quantitative", "population_of",
        {"subject": "Tokyo", "property": "population", "value": 14000000},
    )
    d = router.classify(claim, source_turn_id=1)
    assert d.method == "retrieval"
    assert d.routing_decision["retrieval_query_hint"] == "Tokyo population"


def test_classifies_user_authoritative(store):
    router, fn = _make_router(store, decisions=[_user_auth_decision()])
    claim = _claim(
        "preference", "likes",
        {"agent": "user", "object": "peanut butter"},
    )
    d = router.classify(claim, source_turn_id=1)
    assert d.method == "user_authoritative"


def test_classifies_unverifiable(store):
    router, fn = _make_router(store, decisions=[_unverifiable_decision()])
    claim = _claim(
        "event", "will_happen",
        {"event_type": "Fed rate cut", "participants": ["Fed"],
         "occurred_at": "2026-05"},
    )
    d = router.classify(claim, source_turn_id=1)
    assert d.method == "unverifiable"


# ---- routing-anomaly: validator runs first, LLM never called ----


def test_anomaly_preference_non_user_agent(store):
    """preference + non-user agent → routing anomaly. The LLM router
    is NOT consulted; no memo row is written."""
    router, fn = _make_router(store, decisions=[_python_decision()])
    claim = _claim("preference", "likes",
                   {"agent": "Donald Trump", "object": "pb"})
    d = router.classify(claim, source_turn_id=1)
    assert d.outcome is RoutingOutcome.ROUTING_ANOMALY
    assert d.method is None
    assert d.validation.ok is False
    assert d.validation.invariant == "user_subject_required"
    assert fn.calls == []
    # No memo row should have been written.
    assert RoutingMemo(store).list_all() == []


def test_anomaly_propositional_attitude_third_party(store):
    router, fn = _make_router(store, decisions=[_python_decision()])
    claim = _claim(
        "propositional_attitude", "feels",
        {"agent": "a critic", "attitude": "feels",
         "proposition": "the novel is elegant"},
    )
    d = router.classify(claim, source_turn_id=1)
    assert d.outcome is RoutingOutcome.ROUTING_ANOMALY
    assert fn.calls == []


def test_anomaly_mereological_self_parthood(store):
    router, fn = _make_router(store, decisions=[_python_decision()])
    claim = _claim("mereological", "part_of",
                   {"part": "Tokyo", "whole": "Tokyo"})
    d = router.classify(claim, source_turn_id=1)
    assert d.outcome is RoutingOutcome.ROUTING_ANOMALY
    assert d.validation.invariant == "mereological_self_parthood"
    assert fn.calls == []


def test_anomaly_event_no_participants(store):
    router, fn = _make_router(store, decisions=[_python_decision()])
    # Use a non-list value so the universal "required slot present"
    # check passes but the event invariant fires.
    claim = _claim(
        "event", "was_inaugurated",
        {"event_type": "inauguration", "participants": "Donald Trump",
         "occurred_at": "2025-01-20"},
    )
    d = router.classify(claim, source_turn_id=1)
    assert d.outcome is RoutingOutcome.ROUTING_ANOMALY
    assert d.validation.invariant == "event_no_participants"
    assert fn.calls == []


def test_anomaly_required_slot_missing(store):
    router, fn = _make_router(store, decisions=[_python_decision()])
    claim = _claim("preference", "likes", {"agent": "user"})
    d = router.classify(claim, source_turn_id=1)
    assert d.outcome is RoutingOutcome.ROUTING_ANOMALY
    assert d.validation.invariant == "required_slot_missing"
    assert d.validation.slot == "object"
    assert fn.calls == []


# ---- memo write + hit flow ----


def test_first_call_writes_memo_and_calls_llm(store):
    """Memo miss path: LLM router runs, memo row written."""
    router, fn = _make_router(store, decisions=[_retrieval_decision()])
    claim = _claim(
        "mereological", "part_of",
        {"part": "Williamstown", "whole": "Massachusetts"},
    )
    d = router.classify(claim, source_turn_id=1)
    assert d.outcome is RoutingOutcome.CLASSIFIED
    assert d.method == "retrieval"
    assert d.memo_hit is False
    assert len(fn.calls) == 1
    rows = RoutingMemo(store).list_all()
    assert len(rows) == 1
    assert (rows[0].pattern, rows[0].predicate) == ("mereological", "part_of")
    assert rows[0].method == "retrieval"


def test_second_call_hits_memo_and_skips_llm(store):
    """Memo hit path: same (pattern, predicate) → no LLM call,
    memo_hit=True, last_consulted_at bumped."""
    router, fn = _make_router(store, decisions=[_retrieval_decision()])
    claim_a = _claim(
        "mereological", "part_of",
        {"part": "Williamstown", "whole": "Massachusetts"},
    )
    claim_b = _claim(
        "mereological", "part_of",
        {"part": "Berkshire County", "whole": "Massachusetts"},
    )
    d_a = router.classify(claim_a, source_turn_id=1)
    d_b = router.classify(claim_b, source_turn_id=2)

    assert d_a.memo_hit is False
    assert d_b.memo_hit is True
    assert d_b.method == "retrieval"
    # LLM was called exactly once — for the first claim.
    assert len(fn.calls) == 1


def test_memo_hit_does_not_change_counts(store):
    """Counts stay 0 even after many hits. Pins principle 3 at the
    orchestrator level (the unit test in test_routing_memo also
    pins it at the table level)."""
    router, fn = _make_router(store, decisions=[_retrieval_decision()])
    claim = _claim(
        "mereological", "part_of",
        {"part": "Williamstown", "whole": "Massachusetts"},
    )
    router.classify(claim, source_turn_id=1)
    for i in range(20):
        router.classify(claim, source_turn_id=2 + i)
    entry = RoutingMemo(store).lookup("mereological", "part_of")
    assert entry.affirmed_count == 0
    assert entry.contradicted_count == 0


# ---- pipeline event emission ----


def test_routing_decision_event_logged_on_classification(store):
    router, fn = _make_router(store, decisions=[_python_decision()])
    turn = store.insert_turn("user", "test")
    claim = _claim(
        "quantitative", "has_count",
        {"subject": "x", "property": "y", "value": 3},
    )
    router.classify(claim, source_turn_id=turn)
    events = store.get_pipeline_events(turn)
    routing = [e for e in events if e["stage"] == "routing_decision"]
    assert len(routing) == 1
    payload = routing[0]["data"]
    assert payload["method"] == "python"
    assert payload["outcome"] == "classified"
    assert payload["memo_hit"] is False


def test_routing_memo_write_event_logged_on_miss(store):
    router, fn = _make_router(store, decisions=[_retrieval_decision()])
    turn = store.insert_turn("user", "test")
    claim = _claim(
        "mereological", "part_of",
        {"part": "Williamstown", "whole": "Massachusetts"},
    )
    router.classify(claim, source_turn_id=turn)
    events = store.get_pipeline_events(turn)
    writes = [e for e in events if e["stage"] == "routing_memo_write"]
    assert len(writes) == 1
    assert writes[0]["data"]["method"] == "retrieval"


def test_routing_memo_hit_event_logged_on_hit(store):
    router, fn = _make_router(store, decisions=[_retrieval_decision()])
    claim_a = _claim(
        "mereological", "part_of",
        {"part": "Williamstown", "whole": "Massachusetts"},
    )
    claim_b = _claim(
        "mereological", "part_of",
        {"part": "Berkshire County", "whole": "Massachusetts"},
    )
    turn1 = store.insert_turn("user", "first")
    turn2 = store.insert_turn("user", "second")
    router.classify(claim_a, source_turn_id=turn1)
    router.classify(claim_b, source_turn_id=turn2)

    events_2 = store.get_pipeline_events(turn2)
    hits = [e for e in events_2 if e["stage"] == "routing_memo_hit"]
    assert len(hits) == 1
    assert hits[0]["data"]["method"] == "retrieval"
    assert hits[0]["data"]["affirmed_count"] == 0


def test_routing_validation_failed_event_logged_on_anomaly(store):
    router, fn = _make_router(store, decisions=[_python_decision()])
    turn = store.insert_turn("user", "test")
    claim = _claim("preference", "likes",
                   {"agent": "Donald Trump", "object": "pb"})
    router.classify(claim, source_turn_id=turn)
    events = store.get_pipeline_events(turn)
    failed = [e for e in events if e["stage"] == "routing_validation_failed"]
    assert len(failed) == 1
    assert failed[0]["data"]["validation"]["invariant"] == "user_subject_required"
    # Parity event for v1's anomaly stream consumers:
    anomaly = [e for e in events if e["stage"] == "routing_anomaly_detected"]
    assert len(anomaly) == 1


# ---- guardrails ----


def test_router_without_routing_fn_raises_on_memo_miss(store):
    """A router with neither llm nor routing_fn fails loudly on the
    first memo miss. Memo hits would still work without an LLM —
    that's acceptable; a fully memoized stack is a valid mode."""
    router = Router(store, load_default_registry())
    claim = _claim(
        "quantitative", "has_count",
        {"subject": "x", "property": "y", "value": 3},
    )
    with pytest.raises(RuntimeError, match="routing_fn"):
        router.classify(claim, source_turn_id=1)


def test_router_serves_memo_hits_without_llm(store):
    """If the memo is pre-populated (e.g. from an earlier session),
    a router with no LLM can still classify. Validates that the
    LLM is on the cold path only."""
    memo = RoutingMemo(store)
    memo.record(
        "mereological", "part_of", "retrieval", "constitutive parthood",
    )
    router = Router(store, load_default_registry(), memo=memo)
    claim = _claim(
        "mereological", "part_of",
        {"part": "Williamstown", "whole": "Massachusetts"},
    )
    d = router.classify(claim, source_turn_id=1)
    assert d.memo_hit is True
    assert d.method == "retrieval"


def test_unknown_pattern_reaches_validator_as_anomaly(store):
    """Unknown patterns should NOT crash the orchestrator. They route
    through the validator's defensive required_slot_missing branch."""
    router, fn = _make_router(store, decisions=[_python_decision()])
    bad = _claim("invented_pattern", "x", {"y": 1})
    d = router.classify(bad, source_turn_id=1)
    assert d.outcome is RoutingOutcome.ROUTING_ANOMALY
    assert d.validation.slot == "pattern"
    assert fn.calls == []


# ---- decision serialization ----


def test_decision_to_dict_is_self_describing(store):
    router, fn = _make_router(store, decisions=[_python_decision()])
    claim = _claim(
        "quantitative", "has_count",
        {"subject": "x", "property": "y", "value": 3},
    )
    d = router.classify(claim, source_turn_id=1)
    payload = d.to_dict()
    assert payload["outcome"] == "classified"
    assert payload["method"] == "python"
    assert payload["memo_hit"] is False
    assert payload["validation"]["ok"] is True
    assert payload["routing_decision"]["method"] == "python"


def test_decision_to_dict_for_anomaly_carries_validation_payload(store):
    router, fn = _make_router(store, decisions=[_python_decision()])
    claim = _claim("preference", "likes",
                   {"agent": "Donald Trump", "object": "pb"})
    d = router.classify(claim, source_turn_id=1)
    payload = d.to_dict()
    assert payload["outcome"] == "routing_anomaly"
    assert payload["method"] is None
    assert payload["validation"]["ok"] is False
    assert payload["validation"]["invariant"] == "user_subject_required"
    assert payload["validation"]["slot"] == "agent"
    assert payload["validation"]["actual"] == "Donald Trump"
