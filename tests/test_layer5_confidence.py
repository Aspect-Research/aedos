"""Layer 5 confidence-formula tests (v0.14 Phase 8a).

Pin the three-factor product, the path-prior dispatch table (per
served_from_tier and per routing_method), the chain_reliability
behavior (Beta posterior over store rows; min-link from derivation),
the evidence_strength=1.0 contract, and the threshold env override.
"""

from __future__ import annotations

import json
import pytest

from src.fact_store import Fact, FactStore
from src.layer4_lookup.types import LookupOutcome, WalkerDecision
from src.layer5_decision.confidence import (
    DEFAULT_THRESHOLD,
    PATH_PRIOR_BY_VERIFIER,
    compute_decision_confidence,
    get_threshold,
)


@pytest.fixture
def store(tmp_path):
    return FactStore(str(tmp_path / "aedos_v2.db"))


# ============================================================================
# Threshold (env-driven)
# ============================================================================


def test_threshold_default_is_half(monkeypatch):
    monkeypatch.delenv("AEDOS_DECISION_THRESHOLD", raising=False)
    assert get_threshold() == DEFAULT_THRESHOLD == 0.5


def test_threshold_env_override(monkeypatch):
    monkeypatch.setenv("AEDOS_DECISION_THRESHOLD", "0.7")
    assert get_threshold() == 0.7


def test_threshold_env_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("AEDOS_DECISION_THRESHOLD", "not-a-number")
    assert get_threshold() == DEFAULT_THRESHOLD


# ============================================================================
# Path prior — by tier and routing method
# ============================================================================


def _wd(**overrides) -> WalkerDecision:
    """Minimal WalkerDecision factory for tests."""
    base = dict(
        claim={"pattern": "preference", "predicate": "likes", "polarity": 1, "slots": {}},
        served_from_tier="u",
        outcome=LookupOutcome.MATCH,
        verification_status="user_asserted",
        routing_method="user_authoritative",
    )
    base.update(overrides)
    return WalkerDecision(**base)


def test_path_prior_for_tier_u_is_one(store):
    dec = _wd(served_from_tier="u")
    cd = compute_decision_confidence(dec, store=store)
    assert cd.path_prior == 1.0


def test_path_prior_for_derivation_is_one(store):
    dec = _wd(
        served_from_tier="derivation",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        chain_reliability=0.6,
    )
    cd = compute_decision_confidence(dec, store=store)
    assert cd.path_prior == 1.0


def test_path_prior_for_routing_anomaly_is_zero(store):
    dec = _wd(
        served_from_tier="routing_anomaly",
        outcome=LookupOutcome.MISS,
        verification_status="routing_anomaly",
        routing_method=None,
    )
    cd = compute_decision_confidence(dec, store=store)
    assert cd.path_prior == 0.0
    assert cd.value == 0.0


def test_path_prior_for_fresh_python(store):
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        routing_method="python",
    )
    cd = compute_decision_confidence(dec, store=store)
    assert cd.path_prior == PATH_PRIOR_BY_VERIFIER["python"] == 0.99


def test_path_prior_for_fresh_python_with_canonical_constants(store):
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        routing_method="python_with_canonical_constants",
    )
    cd = compute_decision_confidence(dec, store=store)
    assert cd.path_prior == 0.99


def test_path_prior_for_fresh_retrieval(store):
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        routing_method="retrieval",
    )
    cd = compute_decision_confidence(dec, store=store)
    assert cd.path_prior == 0.85


def test_path_prior_for_fresh_user_authoritative_misroute(store):
    """The fresh dispatcher sees routing_method=user_authoritative and
    diagnoses a misroute (Tier U should have caught it). The verifier-kind
    path_prior is still 1.0 (we trust user_authoritative as a path); the
    intervention planner short-circuits on verification_status='routing_anomaly'
    so path_prior is moot at intervention time. Pin both: path_prior comes
    from the routing_method, NOT from the status."""
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MISS,
        verification_status="routing_anomaly",
        routing_method="user_authoritative",
    )
    cd = compute_decision_confidence(dec, store=store)
    assert cd.path_prior == 1.0


def test_path_prior_zero_only_for_layer2_short_circuit(store):
    """Path_prior=0.0 is reserved for the served_from_tier='routing_anomaly'
    case (Layer 2 validator short-circuit, no verifier ran). The
    served_from_tier='fresh' + status='routing_anomaly' case (fresh
    dispatcher diagnosed misroute) is NOT path_prior=0 — a verifier path
    was attempted."""
    dec_layer2 = _wd(
        served_from_tier="routing_anomaly",
        outcome=LookupOutcome.MISS,
        verification_status="routing_anomaly",
        routing_method=None,
    )
    assert compute_decision_confidence(dec_layer2, store=store).path_prior == 0.0

    dec_fresh = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MISS,
        verification_status="routing_anomaly",
        routing_method="user_authoritative",
    )
    assert compute_decision_confidence(dec_fresh, store=store).path_prior == 1.0


def test_path_prior_for_fresh_unknown_method_falls_back(store):
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MISS,
        verification_status="unverifiable_pending_implementation",
        routing_method="weird_unknown_method",
    )
    cd = compute_decision_confidence(dec, store=store)
    assert cd.path_prior == 0.85


# ============================================================================
# Path prior — Tier W stability_class heuristic
# ============================================================================


def _insert_w_row(
    store: FactStore,
    *,
    canonical_key: str = "preference|likes|p=1|t=present|agent=user&object=tea",
    pattern: str = "preference",
    predicate: str = "likes",
    verdict: str = "verified",
    stability_class: str = "decade_stable",
    refresh_count: int = 0,
    contradiction_count: int = 0,
) -> int:
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
            canonical_key, pattern, predicate, verdict,
            json.dumps({"trace": "x"}), stability_class,
            "2026-01-01T00:00:00+00:00", None, "2026-01-01T00:00:00+00:00",
            None, "[]",
            "2026-01-01T00:00:00+00:00", refresh_count, contradiction_count,
        ),
    )
    store._conn.commit()
    return int(cur.lastrowid)


def test_path_prior_for_tier_w_immutable_is_python(store):
    row_id = _insert_w_row(store, stability_class="immutable")
    dec = _wd(
        served_from_tier="w",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        routing_method="python",
        matching_w_row_id=row_id,
    )
    cd = compute_decision_confidence(dec, store=store)
    assert cd.path_prior == 0.99


def test_path_prior_for_tier_w_decade_stable_is_retrieval(store):
    row_id = _insert_w_row(store, stability_class="decade_stable")
    dec = _wd(
        served_from_tier="w",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        routing_method="retrieval",
        matching_w_row_id=row_id,
    )
    cd = compute_decision_confidence(dec, store=store)
    assert cd.path_prior == 0.85


def test_path_prior_for_tier_w_years_stable_is_retrieval(store):
    row_id = _insert_w_row(store, stability_class="years_stable")
    dec = _wd(
        served_from_tier="w",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        routing_method="retrieval",
        matching_w_row_id=row_id,
    )
    cd = compute_decision_confidence(dec, store=store)
    assert cd.path_prior == 0.85


def test_path_prior_for_tier_w_no_store_falls_back(store):
    dec = _wd(
        served_from_tier="w",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        routing_method="retrieval",
        matching_w_row_id=42,
    )
    cd = compute_decision_confidence(dec, store=None)
    assert cd.path_prior == 0.85


def test_path_prior_for_tier_w_contradiction_uses_contradicting_row(store):
    row_id = _insert_w_row(store, stability_class="immutable")
    dec = _wd(
        served_from_tier="w",
        outcome=LookupOutcome.CONTRADICTION,
        verification_status="verified",
        routing_method="python",
        contradicting_w_row_id=row_id,
    )
    cd = compute_decision_confidence(dec, store=store)
    assert cd.path_prior == 0.99


# ============================================================================
# Chain reliability
# ============================================================================


def test_chain_reliability_for_tier_u_uses_fact_counts(store):
    fid = store.insert_fact(Fact(
        pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "tea"},
        polarity=1, asserted_by="user", verification_status="user_asserted",
        affirmed_count=4, contradicted_count=0,
    ))
    dec = _wd(
        served_from_tier="u",
        outcome=LookupOutcome.MATCH,
        verification_status="user_asserted",
        matching_fact_id=fid,
    )
    cd = compute_decision_confidence(dec, store=store)
    # Beta(1,1) on (4, 0) = 5/6 ≈ 0.833
    assert cd.chain_reliability == pytest.approx(5 / 6)


def test_chain_reliability_for_tier_u_no_store_uses_walker_value(store):
    dec = _wd(
        served_from_tier="u",
        outcome=LookupOutcome.MATCH,
        verification_status="user_asserted",
        matching_fact_id=99,
        chain_reliability=0.42,
    )
    cd = compute_decision_confidence(dec, store=None)
    assert cd.chain_reliability == 0.42


def test_chain_reliability_for_tier_w_uses_row_counts(store):
    row_id = _insert_w_row(
        store, stability_class="decade_stable",
        refresh_count=2, contradiction_count=1,
    )
    dec = _wd(
        served_from_tier="w",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        routing_method="retrieval",
        matching_w_row_id=row_id,
    )
    cd = compute_decision_confidence(dec, store=store)
    # Beta(1,1) on (2, 1) = 3/5 = 0.6
    assert cd.chain_reliability == pytest.approx(0.6)


def test_chain_reliability_for_derivation_uses_walker_value(store):
    dec = _wd(
        served_from_tier="derivation",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        chain_reliability=0.5,
    )
    cd = compute_decision_confidence(dec, store=store)
    assert cd.chain_reliability == 0.5


def test_chain_reliability_for_fresh_is_one(store):
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        routing_method="python",
        chain_reliability=0.001,  # walker can't downgrade fresh chain
    )
    cd = compute_decision_confidence(dec, store=store)
    assert cd.chain_reliability == 1.0


def test_chain_reliability_for_routing_anomaly_is_one(store):
    dec = _wd(
        served_from_tier="routing_anomaly",
        outcome=LookupOutcome.MISS,
        verification_status="routing_anomaly",
        routing_method=None,
    )
    cd = compute_decision_confidence(dec, store=store)
    # path_prior is 0.0 so the product is still 0; chain_reliability
    # is the no-chain default.
    assert cd.chain_reliability == 1.0


# ============================================================================
# Evidence strength (Phase 8 contract: 1.0 in all paths)
# ============================================================================


def test_evidence_strength_is_one_for_all_paths(store):
    fresh_dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        routing_method="retrieval",
    )
    assert compute_decision_confidence(fresh_dec, store=store).evidence_strength == 1.0

    deriv_dec = _wd(
        served_from_tier="derivation",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        chain_reliability=0.5,
    )
    assert compute_decision_confidence(deriv_dec, store=store).evidence_strength == 1.0


# ============================================================================
# Final value + explanation
# ============================================================================


def test_decision_confidence_value_is_product(store):
    dec = _wd(
        served_from_tier="derivation",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        chain_reliability=0.6,
    )
    cd = compute_decision_confidence(dec, store=store)
    # 1.0 (deriv) × 0.6 (chain) × 1.0 (evidence) = 0.6
    assert cd.value == pytest.approx(0.6)


def test_decision_confidence_explanation_includes_factors(store):
    dec = _wd(
        served_from_tier="fresh",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        routing_method="python",
    )
    cd = compute_decision_confidence(dec, store=store)
    assert "path_prior=" in cd.explanation
    assert "chain_reliability=" in cd.explanation
    assert "evidence_strength=" in cd.explanation
    assert "tier='fresh'" in cd.explanation


def test_decision_confidence_to_dict_round_trip(store):
    dec = _wd(
        served_from_tier="u",
        outcome=LookupOutcome.MATCH,
        verification_status="user_asserted",
    )
    cd = compute_decision_confidence(dec, store=store)
    d = cd.to_dict()
    assert d["path_prior"] == 1.0
    assert d["chain_reliability"] == 1.0
    assert d["evidence_strength"] == 1.0
    assert d["value"] == 1.0
    assert "explanation" in d


# ============================================================================
# v0.14.6 — freshly-cached Tier W rows clear the decision threshold
# ============================================================================
#
# Pin the bug fix from v0.14.6: Tier W's write_verifier_result seeds the
# initial insert with refresh_count=1 (the first verifier verdict is one
# independent evidence event under principle 3, mirroring Tier U's
# affirmed_count=1 on initial storage). Without this seed,
# chain_reliability is Beta(1,1)=0.5 on the first cache hit and the
# product with path_prior (0.85 retrieval / 0.99 immutable) lands at
# 0.425 / 0.495 — both under the default 0.5 threshold — so a freshly
# verified fact gets hedged on the very next turn.


def test_freshly_cached_retrieval_row_clears_threshold(store, tmp_path):
    """End-to-end: write a verified retrieval verdict to Tier W via the
    public writer, then compute the decision_confidence for a Tier W
    MATCH against that row. Must be ≥ 0.5 (default threshold) so the
    intervention planner does NOT hedge."""
    from src.layer1_extraction.pattern_registry import (
        load_default_registry, reset_cache,
    )
    from src.layer4_lookup import tier_w
    reset_cache()
    registry = load_default_registry()
    claim = {
        "pattern": "quantitative", "predicate": "completion_year",
        "polarity": 1,
        "slots": {"subject": "Brooklyn Bridge",
                  "property": "completion_year", "value": 1883},
        "source_text": "Brooklyn Bridge was completed in 1883",
    }
    outcome = tier_w.write_verifier_result(
        claim, store,
        verification_status="verified",
        registry=registry,
        stability_class="decade_stable",
        ttl_seconds=3600,
    )
    assert outcome.action == "inserted"
    assert outcome.row_id is not None

    dec = _wd(
        served_from_tier="w",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        routing_method="retrieval",
        matching_w_row_id=outcome.row_id,
    )
    cd = compute_decision_confidence(dec, store=store)
    # path_prior=0.85 (decade_stable → retrieval) × chain=2/3 × evidence=1.0
    # = 0.567, comfortably above 0.5.
    assert cd.path_prior == pytest.approx(0.85)
    assert cd.chain_reliability == pytest.approx(2 / 3)
    assert cd.value >= get_threshold(), (
        f"freshly verified retrieval fact must clear threshold, "
        f"got value={cd.value:.3f}"
    )
    reset_cache()


def test_freshly_cached_immutable_row_clears_threshold(store, tmp_path):
    """Same regression for an immutable (e.g. historical date) row.
    The Brooklyn-Bridge case from the bug report was specifically an
    immutable row: path_prior=0.99, chain=0.5 → value=0.495 < 0.5 →
    hedge. After the fix: chain=2/3 → value=0.66 → no hedge."""
    from src.layer1_extraction.pattern_registry import (
        load_default_registry, reset_cache,
    )
    from src.layer4_lookup import tier_w
    reset_cache()
    registry = load_default_registry()
    claim = {
        "pattern": "quantitative", "predicate": "born_in_year",
        "polarity": 1,
        "slots": {"subject": "Marie Curie",
                  "property": "birth_year", "value": 1867},
        "source_text": "Marie Curie was born in 1867",
    }
    outcome = tier_w.write_verifier_result(
        claim, store,
        verification_status="verified",
        registry=registry,
        stability_class="immutable",
        ttl_seconds=None,
    )
    assert outcome.action == "inserted"
    assert outcome.row_id is not None

    dec = _wd(
        served_from_tier="w",
        outcome=LookupOutcome.MATCH,
        verification_status="verified",
        routing_method="retrieval",
        matching_w_row_id=outcome.row_id,
    )
    cd = compute_decision_confidence(dec, store=store)
    assert cd.path_prior == pytest.approx(0.99)
    assert cd.chain_reliability == pytest.approx(2 / 3)
    assert cd.value >= get_threshold(), (
        f"freshly verified immutable fact must clear threshold, "
        f"got value={cd.value:.3f}"
    )
    reset_cache()
