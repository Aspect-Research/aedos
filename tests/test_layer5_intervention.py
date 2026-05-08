"""Layer 5 intervention-planner tests (v0.14 Phase 8a).

Cover every reachable cell of the decision matrix:
- 8 verification statuses
- × 3 outcomes (MATCH, CONTRADICTION, MISS) where applicable
- × 2 confidence levels (≥T, <T) where applicable

Plus replace-payload construction (Tier U user fact, Tier W cached
row, fresh-dispatch evidence), threshold-driven branching, and the
trace-side fields (notes propagation, flag_operator on routing_anomaly).
"""

from __future__ import annotations

import json
import pytest

from src.fact_store import Fact, FactStore
from src.layer4_lookup.types import LookupOutcome, WalkerDecision
from src.layer5_decision.confidence import (
    compute_decision_confidence,
)
from src.layer5_decision.intervention import plan_intervention
from src.layer5_decision.types import (
    DecisionConfidence,
    Intervention,
    InterventionType,
)


@pytest.fixture
def store(tmp_path):
    return FactStore(str(tmp_path / "aedos_v2.db"))


def _wd(**overrides) -> WalkerDecision:
    base = dict(
        claim={"pattern": "preference", "predicate": "likes", "polarity": 1, "slots": {}},
        served_from_tier="u",
        outcome=LookupOutcome.MATCH,
        verification_status="user_asserted",
        routing_method="user_authoritative",
    )
    base.update(overrides)
    return WalkerDecision(**base)


def _conf(value: float, *, path_prior: float = 1.0,
          chain_reliability: float = 1.0,
          evidence_strength: float = 1.0) -> DecisionConfidence:
    return DecisionConfidence(
        path_prior=path_prior,
        chain_reliability=chain_reliability,
        evidence_strength=evidence_strength,
        value=value,
        explanation=f"test fixture value={value}",
    )


# ============================================================================
# user_asserted
# ============================================================================


def test_user_asserted_match_is_pass_through(store):
    dec = _wd(
        served_from_tier="u",
        outcome=LookupOutcome.MATCH,
        verification_status="user_asserted",
    )
    iv = plan_intervention(dec, _conf(1.0), store=store)
    assert iv.intervention_type is InterventionType.PASS_THROUGH
    assert iv.verified_value is None
    assert iv.flag_operator is False


def test_user_asserted_contradiction_is_replace_with_user_fact(store):
    fid = store.insert_fact(Fact(
        pattern="preference", predicate="dislikes",
        slots={"agent": "user", "object": "olives"},
        polarity=1, asserted_by="user", verification_status="user_asserted",
        affirmed_count=1, contradicted_count=0,
    ))
    dec = _wd(
        served_from_tier="u",
        outcome=LookupOutcome.CONTRADICTION,
        verification_status="user_asserted",
        contradicting_fact_id=fid,
    )
    iv = plan_intervention(dec, _conf(1.0), store=store)
    assert iv.intervention_type is InterventionType.REPLACE
    vv = iv.verified_value
    assert vv is not None
    assert vv["source"] == "user_assertion"
    assert vv["pattern"] == "preference"
    assert vv["predicate"] == "dislikes"
    assert vv["slots"]["object"] == "olives"


def test_user_asserted_contradiction_no_store_returns_none_value(store):
    dec = _wd(
        served_from_tier="u",
        outcome=LookupOutcome.CONTRADICTION,
        verification_status="user_asserted",
        contradicting_fact_id=99,
    )
    iv = plan_intervention(dec, _conf(1.0), store=None)
    assert iv.intervention_type is InterventionType.REPLACE
    assert iv.verified_value is None


def test_user_asserted_miss_is_conservative_noop(store):
    dec = _wd(
        served_from_tier="u",
        outcome=LookupOutcome.MISS,
        verification_status="user_asserted",
    )
    iv = plan_intervention(dec, _conf(1.0), store=store)
    assert iv.intervention_type is InterventionType.NOOP


# ============================================================================
# verified
# ============================================================================


def test_verified_match_above_threshold_is_pass_through(store):
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        routing_method="python",
    )
    iv = plan_intervention(dec, _conf(0.9), store=store, threshold=0.5)
    assert iv.intervention_type is InterventionType.PASS_THROUGH


def test_verified_match_below_threshold_is_hedge(store):
    dec = _wd(
        served_from_tier="derivation",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        chain_reliability=0.4,
    )
    iv = plan_intervention(dec, _conf(0.4), store=store, threshold=0.5)
    assert iv.intervention_type is InterventionType.HEDGE
    assert "0.400" in iv.reason
    assert "0.500" in iv.reason


def test_verified_match_at_threshold_is_pass_through(store):
    """Boundary: conf == T → pass_through (≥, not >)."""
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        routing_method="retrieval",
    )
    iv = plan_intervention(dec, _conf(0.5), store=store, threshold=0.5)
    assert iv.intervention_type is InterventionType.PASS_THROUGH


def test_verified_contradiction_above_threshold_is_replace(store):
    cur = store._conn.execute(
        """
        INSERT INTO verification_cache (
            canonical_key, pattern, predicate, verdict, evidence,
            stability_class, cached_at, expires_at, hit_count, created_at,
            evidence_hash, source_urls,
            last_refreshed_at, refresh_count, contradiction_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
        """,
        (
            "k1", "spatial_temporal", "located_in", "verified",
            json.dumps({"trace": "x", "actual_value": "Japan"}),
            "decade_stable", "2026-01-01T00:00:00+00:00", None,
            "2026-01-01T00:00:00+00:00", None, "[]",
            "2026-01-01T00:00:00+00:00", 1, 0,
        ),
    )
    store._conn.commit()
    row_id = int(cur.lastrowid)
    dec = _wd(
        served_from_tier="w",
        outcome=LookupOutcome.CONTRADICTION,
        verification_status="verified",
        routing_method="retrieval",
        contradicting_w_row_id=row_id,
    )
    iv = plan_intervention(dec, _conf(0.85), store=store, threshold=0.5)
    assert iv.intervention_type is InterventionType.REPLACE
    vv = iv.verified_value
    assert vv["source"] == "verification_cache"
    assert vv["row_id"] == row_id
    assert vv["verdict"] == "verified"


def test_verified_contradiction_below_threshold_is_hedge(store):
    dec = _wd(
        served_from_tier="w",
        outcome=LookupOutcome.CONTRADICTION,
        verification_status="verified",
        routing_method="retrieval",
        contradicting_w_row_id=99,
    )
    iv = plan_intervention(dec, _conf(0.3), store=None, threshold=0.5)
    assert iv.intervention_type is InterventionType.HEDGE


def test_verified_miss_is_noop_with_breadcrumb(store):
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MISS,
        verification_status="verified",
        routing_method="retrieval",
    )
    iv = plan_intervention(dec, _conf(0.85), store=store)
    assert iv.intervention_type is InterventionType.NOOP
    assert "no stored fact engaged" in iv.reason


# ============================================================================
# contradicted
# ============================================================================


def test_contradicted_above_threshold_is_replace_uses_evidence(store):
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.CONTRADICTION,
        verification_status="contradicted",
        routing_method="python",
        evidence={"trace": "computed", "actual_value": 42},
    )
    iv = plan_intervention(dec, _conf(0.99), store=store, threshold=0.5)
    assert iv.intervention_type is InterventionType.REPLACE
    # Fresh-dispatch path: verified_value falls back to evidence dict
    assert iv.verified_value == {"trace": "computed", "actual_value": 42}


def test_contradicted_below_threshold_is_hedge(store):
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.CONTRADICTION,
        verification_status="contradicted",
        routing_method="retrieval",
        evidence={"trace": "x"},
    )
    iv = plan_intervention(dec, _conf(0.3), store=store, threshold=0.5)
    assert iv.intervention_type is InterventionType.HEDGE


def test_contradicted_miss_is_noop(store):
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MISS,
        verification_status="contradicted",
        routing_method="python",
    )
    iv = plan_intervention(dec, _conf(0.99), store=store)
    assert iv.intervention_type is InterventionType.NOOP


# ============================================================================
# unverifiable_in_principle
# ============================================================================


def test_unverifiable_in_principle_is_soften_regardless_of_outcome(store):
    for outcome in (LookupOutcome.MATCH, LookupOutcome.MISS, LookupOutcome.CONTRADICTION):
        dec = _wd(
            served_from_tier="fresh",
            outcome=outcome,
            verification_status="unverifiable_in_principle",
            routing_method="unverifiable",
        )
        iv = plan_intervention(dec, _conf(1.0), store=store)
        assert iv.intervention_type is InterventionType.SOFTEN, (
            f"expected SOFTEN for outcome={outcome}, got {iv.intervention_type}"
        )


def test_unverifiable_in_principle_is_soften_below_threshold_too(store):
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MISS,
        verification_status="unverifiable_in_principle",
        routing_method="unverifiable",
    )
    iv = plan_intervention(dec, _conf(0.1), store=store)
    assert iv.intervention_type is InterventionType.SOFTEN


# ============================================================================
# retrieval_inconclusive / retrieval_failed
# ============================================================================


def test_retrieval_inconclusive_is_hedge_regardless_of_outcome(store):
    for outcome in (LookupOutcome.MATCH, LookupOutcome.MISS):
        dec = _wd(
            served_from_tier="fresh" if outcome is LookupOutcome.MISS else "w",
            outcome=outcome,
            verification_status="retrieval_inconclusive",
            routing_method="retrieval",
        )
        iv = plan_intervention(dec, _conf(0.85), store=store)
        assert iv.intervention_type is InterventionType.HEDGE


def test_retrieval_failed_is_noop_no_hedge(store):
    """Critical contract: retrieval_failed does NOT hedge — adding 'I think'
    to a possibly-true claim is worse than leaving it. The pipeline logs
    a verifier failure event separately."""
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MISS,
        verification_status="retrieval_failed",
        routing_method="retrieval",
    )
    iv = plan_intervention(dec, _conf(0.85), store=store)
    assert iv.intervention_type is InterventionType.NOOP
    assert "absence of evidence" in iv.reason


# ============================================================================
# unverifiable_pending_implementation
# ============================================================================


def test_unverifiable_pending_implementation_is_hedge_with_impl_flag(store):
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MISS,
        verification_status="unverifiable_pending_implementation",
        routing_method="python",
    )
    iv = plan_intervention(dec, _conf(0.99), store=store)
    assert iv.intervention_type is InterventionType.HEDGE
    assert "implementation" in iv.reason.lower()


# ============================================================================
# routing_anomaly
# ============================================================================


def test_routing_anomaly_is_noop_with_flag_operator(store):
    dec = _wd(
        served_from_tier="routing_anomaly",
        outcome=LookupOutcome.MISS,
        verification_status="routing_anomaly",
        routing_method=None,
    )
    iv = plan_intervention(dec, _conf(0.0), store=store)
    assert iv.intervention_type is InterventionType.NOOP
    assert iv.flag_operator is True


def test_routing_anomaly_high_conf_still_noop(store):
    """Routing anomaly noops regardless of confidence — outcome doesn't gate."""
    dec = _wd(
        served_from_tier="routing_anomaly",
        outcome=LookupOutcome.MATCH,
        verification_status="routing_anomaly",
        routing_method=None,
    )
    iv = plan_intervention(dec, _conf(0.99), store=store)
    assert iv.intervention_type is InterventionType.NOOP
    assert iv.flag_operator is True


# ============================================================================
# Unknown / future statuses
# ============================================================================


def test_unknown_status_is_conservative_noop(store):
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MATCH,
        # Bypass enum validation by using a string the matrix doesn't know.
        # FactStore validates at insert; WalkerDecision is a frozen dataclass
        # without status validation.
        verification_status="some_future_status",
        routing_method="python",
    )
    iv = plan_intervention(dec, _conf(0.99), store=store)
    assert iv.intervention_type is InterventionType.NOOP
    assert "unknown" in iv.reason.lower()


# ============================================================================
# Threshold env override
# ============================================================================


def test_threshold_env_override_changes_branching(store, monkeypatch):
    """A claim that was pass_through under T=0.5 hedges under T=0.95."""
    monkeypatch.setenv("AEDOS_DECISION_THRESHOLD", "0.95")
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        routing_method="retrieval",
    )
    # path_prior=0.85 × chain=1.0 × evidence=1.0 = 0.85, which is < 0.95
    cd = compute_decision_confidence(dec, store=store)
    assert cd.value == pytest.approx(0.85)
    iv = plan_intervention(dec, cd, store=store)
    assert iv.intervention_type is InterventionType.HEDGE


def test_explicit_threshold_arg_overrides_env(store, monkeypatch):
    monkeypatch.setenv("AEDOS_DECISION_THRESHOLD", "0.95")
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        routing_method="retrieval",
    )
    cd = compute_decision_confidence(dec, store=store)
    iv = plan_intervention(dec, cd, store=store, threshold=0.5)
    assert iv.intervention_type is InterventionType.PASS_THROUGH


# ============================================================================
# Notes / metadata propagation
# ============================================================================


def test_intervention_carries_walker_notes(store):
    dec = _wd(
        served_from_tier="u",
        outcome=LookupOutcome.MATCH,
        verification_status="user_asserted",
        notes=["user reaffirmed in session foo", "literal match"],
    )
    iv = plan_intervention(dec, _conf(1.0), store=store)
    assert iv.notes == ["user reaffirmed in session foo", "literal match"]


def test_intervention_to_dict_round_trip(store):
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.CONTRADICTION,
        verification_status="contradicted",
        routing_method="python",
        evidence={"trace": "computed", "actual_value": 42},
    )
    iv = plan_intervention(dec, _conf(0.99), store=store, threshold=0.5)
    d = iv.to_dict()
    assert d["intervention_type"] == "replace"
    assert d["verification_status"] == "contradicted"
    assert d["verified_value"] == {"trace": "computed", "actual_value": 42}
    assert d["flag_operator"] is False
    assert "decision_confidence" in d
    assert d["decision_confidence"]["value"] == 0.99


# ============================================================================
# Integration: confidence + intervention end-to-end
# ============================================================================


def test_end_to_end_tier_u_match_with_low_count_fact(store):
    """Reaffirmed fact has low Beta posterior; user_asserted match
    still pass_through (no confidence gating on user_asserted)."""
    fid = store.insert_fact(Fact(
        pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "tea"},
        polarity=1, asserted_by="user", verification_status="user_asserted",
        affirmed_count=0, contradicted_count=0,
    ))
    dec = _wd(
        served_from_tier="u",
        outcome=LookupOutcome.MATCH,
        verification_status="user_asserted",
        matching_fact_id=fid,
    )
    cd = compute_decision_confidence(dec, store=store)
    # Beta(1,1) on (0,0) = 0.5
    assert cd.chain_reliability == pytest.approx(0.5)
    assert cd.value == pytest.approx(0.5)
    iv = plan_intervention(dec, cd, store=store)
    # Even though value == threshold, user_asserted bypasses confidence gating
    assert iv.intervention_type is InterventionType.PASS_THROUGH


def test_end_to_end_derivation_chain_below_floor_hedges(store):
    """A derivation walk produced MATCH but chain_reliability=0.45 yields
    decision_confidence=0.45 < T=0.5 → hedge."""
    dec = _wd(
        served_from_tier="derivation",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        chain_reliability=0.45,
    )
    cd = compute_decision_confidence(dec, store=store)
    assert cd.value == pytest.approx(0.45)
    iv = plan_intervention(dec, cd, store=store, threshold=0.5)
    assert iv.intervention_type is InterventionType.HEDGE
