"""Layer 5 decision-confidence formula (v0.14 Phase 8).

decision_confidence = path_prior × chain_reliability × evidence_strength

Each factor reflects a distinct kind of uncertainty:

  * **path_prior** — structural prior on the verifier kind. Python
    paths score ~0.99 (deterministic execution + comparator); retrieval
    scores ~0.85 (judge-mediated synthesis); user-authoritative scores
    1.0 (the user is ground truth on user-authoritative claim classes).
    For Tier W matches the prior is derived from the cached row's
    stability_class — see ``_path_prior_for_w_row``.

    NOTE (revisit in v0.15+): the stability_class → verifier mapping
    is a v0.14 heuristic. Currently only Python and retrieval write
    Tier W rows and they map cleanly (immutable → python → 0.99;
    everything else → retrieval → 0.85). When v0.15+ adds new verifier
    types (e.g., a structured-data lookup), the right fix is a
    ``verifier_method`` column on ``verification_cache`` so path_prior
    can be looked up directly. Phase 8 deliberately does NOT add the
    column — that's a v0.15 schema change.

  * **chain_reliability** — minimum-link Beta posterior across all
    oracle rows consulted in the lookup or derivation. Tier U / Tier W
    direct matches (no oracle consultation) get the matched row's own
    Beta posterior; oracle-mediated or derivation paths inherit the
    walker's chain_reliability (already min-link from the BFS).

  * **evidence_strength** — how strong the actual evidence is, separate
    from how reliable the verifier process is in general. Phase 8
    contract (Option A): 1.0 in all current paths. The
    intervention matrix keys on verification_status (not on
    evidence_strength) for retrieval_inconclusive / retrieval_failed,
    so a graded factor wouldn't change Phase 8 behavior. Graded
    evidence_strength is deferred to v0.15+ where it will key on
    explicit per-claim verifier scores.

The threshold T (env ``AEDOS_DECISION_THRESHOLD``, default 0.5) splits
'hard verdict' (decision_confidence ≥ T) from 'soft verdict' (< T).
The intervention planner reads the comparison for the verified and
contradicted cells of the matrix.
"""

from __future__ import annotations

import os
from typing import Optional

from src.fact_store import FactStore
from src.layer2_routing.constants import confidence_from_counts
from src.layer4_lookup.types import LookupOutcome, WalkerDecision
from src.layer5_decision.types import DecisionConfidence


DEFAULT_THRESHOLD = 0.5


PATH_PRIOR_BY_VERIFIER: dict[str, float] = {
    "python": 0.99,
    "python_with_canonical_constants": 0.99,
    "retrieval": 0.85,
    "user_authoritative": 1.0,
    "unverifiable": 1.0,
}


def get_threshold() -> float:
    """Read ``AEDOS_DECISION_THRESHOLD`` live so tests can monkeypatch.

    Falls back to ``DEFAULT_THRESHOLD`` when unset or unparseable.
    Read-each-call (rather than module-level constant) so the test
    suite can override per-test without import-order acrobatics.
    """
    raw = os.getenv("AEDOS_DECISION_THRESHOLD")
    if raw is None:
        return DEFAULT_THRESHOLD
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_THRESHOLD


def compute_decision_confidence(
    walker_decision: WalkerDecision,
    *,
    store: Optional[FactStore] = None,
) -> DecisionConfidence:
    """Compute the three-factor decision confidence for a Layer-4 verdict.

    ``store`` is optional but recommended: with the store we can read
    the matched fact's / cached row's counts to derive a precise
    chain_reliability (and the W row's stability_class for path_prior).
    Without the store we fall back to the walker's own
    ``chain_reliability`` field and a conservative path_prior heuristic.
    """
    path_prior = _path_prior_for_walker_decision(walker_decision, store=store)
    chain_reliability = _chain_reliability_for_walker_decision(
        walker_decision, store=store,
    )
    evidence_strength = _evidence_strength_for_walker_decision(walker_decision)
    value = path_prior * chain_reliability * evidence_strength
    explanation = (
        f"path_prior={path_prior:.3f} × "
        f"chain_reliability={chain_reliability:.3f} × "
        f"evidence_strength={evidence_strength:.3f} "
        f"= {value:.3f} (tier={walker_decision.served_from_tier!r})"
    )
    return DecisionConfidence(
        path_prior=path_prior,
        chain_reliability=chain_reliability,
        evidence_strength=evidence_strength,
        value=value,
        explanation=explanation,
    )


# ============================================================================
# Path prior
# ============================================================================


def _path_prior_for_walker_decision(
    decision: WalkerDecision,
    *,
    store: Optional[FactStore] = None,
) -> float:
    """Return path_prior for the walker decision.

    Tier dispatch:
      * 'routing_anomaly' → 0.0 (claim is structurally invalid)
      * 'u' → 1.0 (user is authoritative)
      * 'derivation' → 1.0 (sound by principle 4; chain_reliability
        carries the soft signal)
      * 'fresh' → method-dispatched via PATH_PRIOR_BY_VERIFIER
      * 'w' → derived from cached row's stability_class (heuristic;
        see module docstring NOTE for the v0.15 fix)
    """
    tier = decision.served_from_tier

    if tier == "routing_anomaly":
        return 0.0
    if tier == "u":
        return 1.0
    if tier == "derivation":
        return 1.0
    if tier == "fresh":
        method = decision.routing_method or ""
        return PATH_PRIOR_BY_VERIFIER.get(method, 0.85)
    if tier == "w":
        return _path_prior_for_w_decision(decision, store=store)
    return 0.5


def _path_prior_for_w_decision(
    decision: WalkerDecision,
    *,
    store: Optional[FactStore] = None,
) -> float:
    """Derive path_prior from a Tier W cached row's stability_class.

    immutable → python source → 0.99
    everything else → retrieval source → 0.85

    Falls back to 0.85 (retrieval prior) when no store is available
    or when the row id can't be resolved — retrieval is the more
    common Tier W writer, so the conservative default biases toward
    the more common case.
    """
    if store is None:
        return 0.85
    row_id = decision.matching_w_row_id or decision.contradicting_w_row_id
    if row_id is None:
        return 0.85
    stability = _stability_class_for_w_row(store, row_id)
    if stability == "immutable":
        return 0.99
    return 0.85


def _stability_class_for_w_row(
    store: FactStore, row_id: int,
) -> Optional[str]:
    row = store._conn.execute(
        "SELECT stability_class FROM verification_cache WHERE id = ?",
        (row_id,),
    ).fetchone()
    if row is None:
        return None
    return row["stability_class"]


# ============================================================================
# Chain reliability
# ============================================================================


def _chain_reliability_for_walker_decision(
    decision: WalkerDecision,
    *,
    store: Optional[FactStore] = None,
) -> float:
    """Return chain_reliability for the walker decision.

      * 'fresh' / 'routing_anomaly' → 1.0 (no chain)
      * 'derivation' → walker's chain_reliability (already min-link
        from the BFS)
      * 'u' → matched fact's Beta posterior (when store is available);
        else walker's chain_reliability
      * 'w' → cached row's Beta posterior (when store is available);
        else walker's chain_reliability
    """
    tier = decision.served_from_tier

    if tier in ("fresh", "routing_anomaly"):
        return 1.0
    if tier == "derivation":
        return decision.chain_reliability
    if tier == "u":
        if store is not None:
            fact_id = (
                decision.matching_fact_id or decision.contradicting_fact_id
            )
            if fact_id is not None:
                fact = store.get_fact(fact_id)
                if fact is not None:
                    return confidence_from_counts(
                        fact.affirmed_count, fact.contradicted_count,
                    )
        return decision.chain_reliability
    if tier == "w":
        if store is not None:
            row_id = (
                decision.matching_w_row_id or decision.contradicting_w_row_id
            )
            if row_id is not None:
                counts = _counts_for_w_row(store, row_id)
                if counts is not None:
                    return confidence_from_counts(*counts)
        return decision.chain_reliability
    return decision.chain_reliability


def _counts_for_w_row(
    store: FactStore, row_id: int,
) -> Optional[tuple[int, int]]:
    row = store._conn.execute(
        "SELECT refresh_count, contradiction_count "
        "FROM verification_cache WHERE id = ?",
        (row_id,),
    ).fetchone()
    if row is None:
        return None
    return (
        int(row["refresh_count"] or 0),
        int(row["contradiction_count"] or 0),
    )


# ============================================================================
# Evidence strength (Phase 8 Option A: 1.0 in all current paths)
# ============================================================================


def _evidence_strength_for_walker_decision(
    decision: WalkerDecision,
) -> float:
    """Phase 8 contract: 1.0 in all current paths.

    Per Option A, evidence_strength is a multiplicative factor reserved
    for graded per-claim verifier scoring (post-v0.14 work). The Phase 8
    intervention matrix discriminates retrieval_inconclusive,
    retrieval_failed, and unverifiable_pending_implementation by status
    rather than by a graded evidence_strength, so the constant 1.0 is
    architecturally honest in v0.14.
    """
    return 1.0
