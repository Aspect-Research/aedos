"""Tests for the /v2/api/dispatch-one endpoint (v0.14 Phase 8.5).

The endpoint runs Layer 2 routing → walker (U/W/derivation/fresh) →
Layer 5 (decision_confidence + intervention) for a single structured
claim. The trace UI in Phase 8.5 calls this so it has a WalkerDecision
and Intervention to render — /v2/api/chat is a Phase 9 deliverable.

These tests pin the JSON shape contract that the trace UI relies on.
A future backend change that drops one of the documented fields breaks
the UI silently otherwise.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.app import _set_store, app
from src.fact_store import Fact, FactStore
from src.layer2_routing.routing_memo import RoutingMemo


@pytest.fixture
def isolated_store(tmp_path):
    s = FactStore(tmp_path / "dispatch_one.db")
    _set_store(s)
    yield s
    s.close()
    _set_store(None)


@pytest.fixture
def client():
    return TestClient(app)


def _seed_memo(store: FactStore, pattern: str, predicate: str, method: str) -> None:
    """Pre-populate the routing memo so dispatch-one doesn't need an LLM."""
    memo = RoutingMemo(store)
    memo.record(pattern, predicate, method, "test seed")


# ============================================================================
# Tier U match
# ============================================================================


def test_tier_u_match_returns_user_asserted_pass_through(client, isolated_store):
    """User asserted 'I love sushi'; the same claim resolves at Tier U
    with verification_status='user_asserted', intervention=pass_through,
    decision_confidence carrying all three factors."""
    _seed_memo(isolated_store, "preference", "loves", "user_authoritative")
    isolated_store.insert_fact(Fact(
        pattern="preference",
        predicate="loves",
        slots={"agent": "user", "object": "sushi"},
        polarity=1,
        asserted_by="user",
        verification_status="user_asserted",
        affirmed_count=1,
        contradicted_count=0,
    ))

    resp = client.post(
        "/api/dispatch-one",
        json={
            "claim": {
                "pattern": "preference",
                "predicate": "loves",
                "polarity": 1,
                "slots": {"agent": "user", "object": "sushi"},
                "source_text": "user loves sushi",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Top-level shape contract — every key the trace UI reads.
    assert set(body.keys()) >= {
        "turn_id", "threshold", "layer2_decision",
        "walker_decision", "decision_confidence",
        "intervention", "events",
    }

    assert body["walker_decision"]["served_from_tier"] == "u"
    assert body["walker_decision"]["outcome"] == "match"
    assert body["walker_decision"]["verification_status"] == "user_asserted"
    assert body["walker_decision"]["routing_method"] == "user_authoritative"

    dc = body["decision_confidence"]
    assert set(dc.keys()) == {
        "path_prior", "chain_reliability",
        "evidence_strength", "value", "explanation",
    }
    assert dc["path_prior"] == 1.0
    # value = path_prior × chain_reliability × evidence_strength
    expected = dc["path_prior"] * dc["chain_reliability"] * dc["evidence_strength"]
    assert abs(dc["value"] - expected) < 1e-9

    iv = body["intervention"]
    assert iv["intervention_type"] == "pass_through"
    assert iv["verification_status"] == "user_asserted"
    # decision_confidence is duplicated inside intervention; both copies
    # must agree (the trace UI reads from either).
    assert iv["decision_confidence"]["value"] == dc["value"]


# ============================================================================
# Tier U miss → no fresh: walker terminates at the placeholder verdict
# ============================================================================


def test_tier_u_miss_without_fresh_returns_pending(client, isolated_store):
    """No matching fact, no derivation chain, run_fresh=false: the
    walker emits the 'no fresh dispatcher provided' placeholder
    decision. The UI distinguishes this from a real miss via the
    notes."""
    _seed_memo(isolated_store, "relational", "founded_by", "retrieval")

    resp = client.post(
        "/api/dispatch-one",
        json={
            "claim": {
                "pattern": "relational",
                "predicate": "founded_by",
                "polarity": 1,
                "slots": {"subject": "Apple", "object": "Steve Jobs"},
            },
            "run_fresh": False,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["walker_decision"]["served_from_tier"] == "fresh"
    assert (
        body["walker_decision"]["verification_status"]
        == "unverifiable_pending_implementation"
    )
    assert body["walker_decision"]["routing_method"] == "retrieval"


# ============================================================================
# Routing anomaly short-circuits walker
# ============================================================================


def test_routing_anomaly_short_circuits(client, isolated_store):
    """A preference claim with a non-user agent triggers Layer 2's
    validator. The walker short-circuits at served_from_tier=
    'routing_anomaly', verification_status='routing_anomaly'. The
    intervention is noop with flag_operator=True."""
    resp = client.post(
        "/api/dispatch-one",
        json={
            "claim": {
                "pattern": "preference",
                "predicate": "likes",
                "polarity": 1,
                "slots": {"agent": "Donald Trump", "object": "peanut butter"},
                "source_text": "Donald Trump likes peanut butter",
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    wd = body["walker_decision"]
    assert wd["served_from_tier"] == "routing_anomaly"
    assert wd["verification_status"] == "routing_anomaly"
    assert wd["outcome"] == "miss"
    assert wd["routing_method"] is None

    iv = body["intervention"]
    assert iv["intervention_type"] == "noop"
    assert iv["flag_operator"] is True


# ============================================================================
# Pipeline events are written and surfaced
# ============================================================================


def test_dispatch_one_writes_pipeline_events(client, isolated_store):
    _seed_memo(isolated_store, "preference", "loves", "user_authoritative")
    isolated_store.insert_fact(Fact(
        pattern="preference",
        predicate="loves",
        slots={"agent": "user", "object": "ramen"},
        polarity=1,
        asserted_by="user",
        verification_status="user_asserted",
    ))

    resp = client.post(
        "/api/dispatch-one",
        json={
            "claim": {
                "pattern": "preference",
                "predicate": "loves",
                "polarity": 1,
                "slots": {"agent": "user", "object": "ramen"},
            },
        },
    )
    body = resp.json()
    events = body["events"]
    stages = {e["stage"] for e in events}
    # walker_decision is the load-bearing event for the trace UI.
    assert "walker_decision" in stages
    # routing_memo_hit fires because the memo was pre-seeded.
    assert "routing_memo_hit" in stages

    # And /api/trace/{turn_id} returns the same events as a flat list.
    trace = client.get(f"/api/trace/{body['turn_id']}").json()
    assert isinstance(trace, list)
    assert len(trace) == len(events)


# ============================================================================
# Malformed input rejects with 422 (FastAPI's default for pydantic errors)
# ============================================================================


def test_malformed_claim_rejected(client, isolated_store):
    # Missing required `pattern` and `predicate` fields.
    resp = client.post(
        "/api/dispatch-one",
        json={"claim": {"polarity": 1, "slots": {}}},
    )
    assert resp.status_code == 422


def test_polarity_zero_runs_through(client, isolated_store):
    """Polarity 0 is valid for preference claims; the dispatcher
    propagates it through routing → walker → Layer 5 like any other
    claim. Memo is pre-seeded so the test doesn't need an LLM."""
    _seed_memo(isolated_store, "preference", "loves", "user_authoritative")
    resp = client.post(
        "/api/dispatch-one",
        json={
            "claim": {
                "pattern": "preference",
                "predicate": "loves",
                "polarity": 0,
                "slots": {"agent": "user", "object": "raisins"},
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["walker_decision"]["outcome"] == "miss"
    # No matching fact in U, no derivation chain, no fresh dispatcher.
    assert body["walker_decision"]["served_from_tier"] == "fresh"
