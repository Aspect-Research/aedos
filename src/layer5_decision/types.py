"""Layer 5 dataclasses (v0.14 Phase 8).

The decision-and-response layer consumes Layer 4's ``WalkerDecision``
and produces an ``Intervention`` per claim. Layer 5 is rule-based; the
corrector's LLM call is the only place an LLM enters this layer, and
that's downstream of intervention planning.

Shapes
======

  * **InterventionType** — the five-action vocabulary the planner
    chooses from. ``pass_through`` and ``noop`` make the previously-
    implicit "no edit" branch explicit so the trace UI sees every
    claim's resolution. The corrector filters ``pass_through`` /
    ``noop`` out of its rewrite prompt; the trace UI keeps them.

  * **DecisionConfidence** — three-factor product
    (path_prior × chain_reliability × evidence_strength) plus the
    final value and a human-readable explanation. Surfaced as a
    standalone object so the trace UI can render the breakdown.

  * **Intervention** — the unified planner output. Carries the claim,
    the chosen action, the verification_status and decision_confidence
    that produced the choice, the reason string, and (for ``replace``)
    the verified_value the corrector should use. ``flag_operator`` is
    the routing-anomaly signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class InterventionType(str, Enum):
    """The five Layer-5 actions on a model claim.

    The matrix in ``intervention.plan_intervention`` keys on
    (verification_status, outcome, decision_confidence vs T) to pick
    one of these. Every claim gets exactly one type — there are no
    None / "skip" values.
    """

    PASS_THROUGH = "pass_through"
    REPLACE = "replace"
    HEDGE = "hedge"
    SOFTEN = "soften"
    NOOP = "noop"


@dataclass(frozen=True)
class DecisionConfidence:
    """Three-factor decision confidence breakdown.

    ``value = path_prior × chain_reliability × evidence_strength``.

    The intervention planner reads ``value`` against the threshold T
    to discriminate the 'verified high-conf' from 'verified low-conf'
    cells of the matrix. The three factors are stored separately so the
    trace UI renders the breakdown rather than just the product.
    """

    path_prior: float
    chain_reliability: float
    evidence_strength: float
    value: float
    explanation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path_prior": self.path_prior,
            "chain_reliability": self.chain_reliability,
            "evidence_strength": self.evidence_strength,
            "value": self.value,
            "explanation": self.explanation,
        }


@dataclass(frozen=True)
class Intervention:
    """One Layer-5 action on a model claim.

    ``intervention_type`` is one of the five enum values. ``claim`` is
    the original Layer-1 claim shape. ``verification_status`` and
    ``decision_confidence`` are the inputs that produced the choice;
    the trace UI reads them to explain the planner's reasoning.

    ``verified_value`` is set on ``REPLACE`` and otherwise None. Its
    shape varies by source:

      * Tier U contradiction: ``{"source": "user_assertion",
        "pattern", "predicate", "polarity", "slots", "asserted_at"}``
      * Tier W contradiction: ``{"source": "verification_cache",
        "row_id", "canonical_key", "predicate", "verdict", "evidence"}``
      * Fresh dispatch contradiction: the verifier's evidence dict
        as-is (carries trace + actual_value for python; full
        RetrievalResult for retrieval).

    ``flag_operator`` is the routing-anomaly signal; the pipeline emits
    a separate ``routing_anomaly_detected`` event when this is True.
    """

    intervention_type: InterventionType
    claim: dict[str, Any]
    verification_status: str
    decision_confidence: DecisionConfidence
    reason: str
    verified_value: Optional[Any] = None
    flag_operator: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intervention_type": self.intervention_type.value,
            "claim": dict(self.claim),
            "verification_status": self.verification_status,
            "decision_confidence": self.decision_confidence.to_dict(),
            "reason": self.reason,
            "verified_value": self.verified_value,
            "flag_operator": self.flag_operator,
            "notes": list(self.notes),
        }
