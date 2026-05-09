"""Layer 5 intervention planner (v0.14 Phase 8).

The 5-action decision matrix. Maps each ``WalkerDecision`` (plus its
``DecisionConfidence``) to exactly one ``Intervention``.

The matrix
==========

The primary key is ``verification_status``. ``outcome`` and the
confidence-vs-threshold comparison are secondary discriminators only
for the verified / contradicted / user_asserted statuses (the rest
have a single action regardless of outcome).

  user_asserted MATCH                  → pass_through
  user_asserted CONTRADICTION          → replace (user said different)
  user_asserted MISS                   → noop (conservative; shouldn't happen)

  verified MATCH conf≥T                → pass_through
  verified MATCH conf<T                → hedge (verifier confirmed but conf low)
  verified CONTRADICTION conf≥T        → replace (cache verified opposite-polarity)
  verified CONTRADICTION conf<T        → hedge
  verified MISS                        → noop (no fact engaged this claim)

  contradicted MATCH/CONTRADICTION ≥T  → replace
  contradicted MATCH/CONTRADICTION <T  → hedge
  contradicted MISS                    → noop

  unverifiable_in_principle (any)      → soften
  retrieval_inconclusive (any)         → hedge
  retrieval_failed (any)               → noop (no evidence; don't pretend)
  unverifiable_pending_implementation  → hedge (impl-flag in reason)
  routing_anomaly (any)                → noop with flag_operator=True

Auditability (principle 6)
==========================

Every claim gets one Intervention — there is no None / skip return.
``pass_through`` and ``noop`` are explicit so the trace UI sees every
claim's resolution. The corrector filters those out before constructing
the rewrite prompt; the trace UI keeps them.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from src.fact_store import FactStore
from src.layer4_lookup.types import LookupOutcome, WalkerDecision
from src.layer5_decision.confidence import get_threshold
from src.layer5_decision.types import (
    DecisionConfidence,
    Intervention,
    InterventionType,
)


def plan_intervention(
    walker_decision: WalkerDecision,
    decision_confidence: DecisionConfidence,
    *,
    store: Optional[FactStore] = None,
    threshold: Optional[float] = None,
    triage_skipped: bool = False,
) -> Intervention:
    """Plan exactly one intervention for a model claim.

    ``store`` is optional; when provided, the planner can resolve
    ``verified_value`` payloads from the matched/contradicting fact
    or cached row. Without it, the planner falls back to whatever
    evidence the walker already attached.

    ``threshold`` defaults to ``get_threshold()`` (env-driven).

    ``triage_skipped`` (v0.14.3): True iff the verifiability triage
    gate decided PASS_THROUGH for this claim (fresh dispatch was
    suppressed). When True, an ``unverifiable_pending_implementation``
    or ``retrieval_failed`` walker outcome is the EXPECTED no-op
    consequence of triage — the planner returns ``pass_through``
    instead of ``hedge`` because hedging implies "we tried and were
    uncertain", whereas triage-skipped means "we didn't try by
    design, no information either way." The trace UI hides skipped
    claims behind a drop-down so they don't clutter the chat-side
    view.
    """
    if threshold is None:
        threshold = get_threshold()

    status = walker_decision.verification_status
    outcome = walker_decision.outcome

    # Routing anomaly: noop, flag operator. Outcome / confidence don't matter.
    if status == "routing_anomaly":
        return _build(
            InterventionType.NOOP, walker_decision, decision_confidence,
            reason="Layer 2 validator rejected the claim; flagged for operator",
            flag_operator=True,
        )

    # v0.14.3 — triage-skipped short-circuit. When the triage gate
    # explicitly suppressed fresh dispatch and the cheap walker tiers
    # also missed, this is "we chose not to verify, no information"
    # not "we tried and failed". Pass through unchanged; no hedge.
    if (triage_skipped
        and status in ("unverifiable_pending_implementation",
                       "retrieval_failed")):
        return _build(
            InterventionType.PASS_THROUGH, walker_decision, decision_confidence,
            reason=(
                "verifiability triage skipped this claim (no falsifiability "
                "signal); leaving the model's text unchanged — no hedge, "
                "no rewrite. The triage gate already decided this isn't "
                "worth verifying."
            ),
        )

    # User-asserted: outcome decides. Confidence isn't gating
    # (the user is ground truth on user-authoritative claim classes).
    if status == "user_asserted":
        if outcome is LookupOutcome.MATCH:
            return _build(
                InterventionType.PASS_THROUGH, walker_decision,
                decision_confidence,
                reason="user previously asserted this claim; pass through",
            )
        if outcome is LookupOutcome.CONTRADICTION:
            verified_value = _build_verified_value_from_user_fact(
                walker_decision, store=store,
            )
            return _build(
                InterventionType.REPLACE, walker_decision,
                decision_confidence,
                reason="user has asserted something different from the model claim",
                verified_value=verified_value,
            )
        return _build(
            InterventionType.NOOP, walker_decision, decision_confidence,
            reason=(
                "user_asserted with outcome=MISS; no stored user fact "
                "engaged this claim — conservative noop"
            ),
        )

    if status == "verified":
        if outcome is LookupOutcome.MISS:
            return _miss_noop(walker_decision, decision_confidence, status)
        above = decision_confidence.value >= threshold
        if outcome is LookupOutcome.MATCH:
            if above:
                return _build(
                    InterventionType.PASS_THROUGH, walker_decision,
                    decision_confidence,
                    reason="verifier confirmed; high decision confidence",
                )
            return _build(
                InterventionType.HEDGE, walker_decision, decision_confidence,
                reason=(
                    f"verifier confirmed but decision_confidence "
                    f"{decision_confidence.value:.3f} < threshold "
                    f"{threshold:.3f}"
                ),
            )
        # CONTRADICTION
        if above:
            verified_value = _build_verified_value_from_w_row(
                walker_decision, store=store,
            )
            return _build(
                InterventionType.REPLACE, walker_decision,
                decision_confidence,
                reason=(
                    "cached verifier verdict contradicts claim; "
                    "high decision confidence"
                ),
                verified_value=verified_value,
            )
        return _build(
            InterventionType.HEDGE, walker_decision, decision_confidence,
            reason=(
                f"cached contradiction but decision_confidence "
                f"{decision_confidence.value:.3f} < threshold "
                f"{threshold:.3f}"
            ),
        )

    if status == "contradicted":
        if outcome is LookupOutcome.MISS:
            return _miss_noop(walker_decision, decision_confidence, status)
        above = decision_confidence.value >= threshold
        if above:
            verified_value = _build_verified_value_from_w_row(
                walker_decision, store=store,
            )
            return _build(
                InterventionType.REPLACE, walker_decision,
                decision_confidence,
                reason="verifier contradicted claim; high decision confidence",
                verified_value=verified_value,
            )
        return _build(
            InterventionType.HEDGE, walker_decision, decision_confidence,
            reason=(
                f"verifier contradicted but decision_confidence "
                f"{decision_confidence.value:.3f} < threshold "
                f"{threshold:.3f}"
            ),
        )

    if status == "unverifiable_in_principle":
        return _build(
            InterventionType.SOFTEN, walker_decision, decision_confidence,
            reason="claim is unverifiable by design; soften any definite framing",
        )

    if status == "retrieval_inconclusive":
        return _build(
            InterventionType.HEDGE, walker_decision, decision_confidence,
            reason="retrieval found evidence but judge said insufficient",
        )

    if status == "retrieval_failed":
        return _build(
            InterventionType.NOOP, walker_decision, decision_confidence,
            reason=(
                "verifier broke (network, parse, judge error); "
                "absence of evidence is not evidence of weakness"
            ),
        )

    if status == "unverifiable_pending_implementation":
        return _build(
            InterventionType.HEDGE, walker_decision, decision_confidence,
            reason=(
                "verifier returned no conclusive result "
                "(implementation pending or transient); hedge with impl flag"
            ),
        )

    # Unknown status — conservative noop.
    return _build(
        InterventionType.NOOP, walker_decision, decision_confidence,
        reason=f"unknown verification_status {status!r}; conservative noop",
    )


# ============================================================================
# Helpers
# ============================================================================


def _build(
    intervention_type: InterventionType,
    walker_decision: WalkerDecision,
    decision_confidence: DecisionConfidence,
    *,
    reason: str,
    verified_value: Optional[Any] = None,
    flag_operator: bool = False,
) -> Intervention:
    return Intervention(
        intervention_type=intervention_type,
        claim=dict(walker_decision.claim),
        verification_status=walker_decision.verification_status,
        decision_confidence=decision_confidence,
        reason=reason,
        verified_value=verified_value,
        flag_operator=flag_operator,
        notes=list(walker_decision.notes),
    )


def _miss_noop(
    walker_decision: WalkerDecision,
    decision_confidence: DecisionConfidence,
    status: str,
) -> Intervention:
    """Build a NOOP for outcome=MISS with a non-special status.

    Architectural note (Phase 8): the matrix entry for
    "outcome=MISS, status in {verified, contradicted}" is NOOP with a
    trace breadcrumb saying no stored fact engaged the claim. The
    corrector doesn't act on these claims; the trace UI's per-claim
    breakdown records the MISS so there's no silent gap (principle 6:
    auditability by construction).
    """
    return _build(
        InterventionType.NOOP, walker_decision, decision_confidence,
        reason=(
            f"outcome=MISS with verification_status={status!r}: no stored "
            f"fact engaged this claim; verifier output was the source"
        ),
    )


def _build_verified_value_from_user_fact(
    walker_decision: WalkerDecision,
    *,
    store: Optional[FactStore] = None,
) -> Optional[dict[str, Any]]:
    """For a Tier-U CONTRADICTION, return the user's stored assertion
    in a shape suitable for the corrector's rewrite prompt.

    Returns None when the store isn't available or the fact has been
    deleted between Layer 4 resolution and Layer 5 planning.
    """
    if store is None:
        return None
    fact_id = walker_decision.contradicting_fact_id
    if fact_id is None:
        return None
    fact = store.get_fact(fact_id)
    if fact is None:
        return None
    return {
        "source": "user_assertion",
        "fact_id": fact.id,
        "pattern": fact.pattern,
        "predicate": fact.predicate,
        "polarity": fact.polarity,
        "slots": dict(fact.slots),
        "asserted_at": fact.created_at,
    }


def _build_verified_value_from_w_row(
    walker_decision: WalkerDecision,
    *,
    store: Optional[FactStore] = None,
) -> Optional[Any]:
    """For a contradicted/verified-CONTRADICTION outcome, return the
    cached W row's payload (or the fresh dispatcher's evidence).

    Tier W contradictions carry ``contradicting_w_row_id``; verified-
    CONTRADICTION on Tier W also lands in the same field. Fresh-
    dispatch contradicted verdicts carry the verifier's output in
    ``walker_decision.evidence`` directly.
    """
    row_id = (
        walker_decision.contradicting_w_row_id
        or walker_decision.matching_w_row_id
    )
    if row_id is None:
        # Fresh-dispatch path: evidence is the verifier's output dict.
        return walker_decision.evidence
    if store is None:
        return walker_decision.evidence
    row = store._conn.execute(
        "SELECT canonical_key, predicate, verdict, evidence "
        "FROM verification_cache WHERE id = ?",
        (row_id,),
    ).fetchone()
    if row is None:
        return walker_decision.evidence
    evidence: Optional[Any] = None
    raw = row["evidence"]
    if raw:
        try:
            evidence = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            evidence = None
    return {
        "source": "verification_cache",
        "row_id": row_id,
        "canonical_key": row["canonical_key"],
        "predicate": row["predicate"],
        "verdict": row["verdict"],
        "evidence": evidence,
    }
