"""Public types for Layer 2 routing.

Three exports:

  * ``ValidationResult`` — tagged result from the rule-based validator
    (Phase 2's first step). ``ok=True`` is a Pass; ``ok=False`` carries
    the invariant name plus the specific slot / expected / actual that
    failed, sufficient for the trace UI to render the anomaly.
  * ``RoutingOutcome`` — the two outcomes Phase 2's classifier produces.
    ``CLASSIFIED`` means the claim got a routing method (from memo or
    LLM). ``ROUTING_ANOMALY`` means validation failed and no router ran.
  * ``Decision`` — the classification record. Phase 2 keeps this narrow:
    it names the dispatch destination but does NOT carry verifier-side
    state (stored_fact_id, retrieval_result, served_from_cache, etc.).
    Those fields accrete in Phases 3-7 as verifiers come online.

The narrow Decision is intentional. v1's Decision dataclass packs
classification, store-match outcome, retrieval result, and cache state
into one shape because v1 has a single Router.route() entry point that
does all of those things at once. The v0.14 architecture splits routing
(Layer 2) from verification dispatch (later layers); the Decision shape
matches that split. Phase 8 may consolidate display_status mapping
across the layers when verifier outcomes are populated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class RoutingOutcome(str, Enum):
    """Phase 2 outcome enumeration.

    ``CLASSIFIED`` covers every successful routing classification —
    whether served from memo or fresh from the LLM router. The memo
    state is carried separately on ``Decision.memo_hit`` because both
    paths are equally valid classifications; the operator just wants to
    know which one ran.

    ``ROUTING_ANOMALY`` means the validator's rule-based step rejected
    the claim before the LLM router could see it. The LLM router was
    NOT consulted; no memo row is written.
    """

    CLASSIFIED = "classified"
    ROUTING_ANOMALY = "routing_anomaly"


@dataclass(frozen=True)
class ValidationResult:
    """Tagged result of the rule-based validator.

    The discriminator is ``ok``. When ``ok=True`` the other fields are
    all None. When ``ok=False`` they describe the invariant that failed
    and the specific slot / expected / actual values, so the trace UI
    can render the anomaly without re-running validation.

    The validator short-circuits on the first failure — see
    ``validator.py``'s docstring for the rationale (operators reason
    about one root cause, not a list).

    Construct via the ``passed()`` and ``anomaly()`` classmethods rather
    than positional args; that keeps the discriminator coherent with
    its associated fields and matches how the validator emits results.
    """

    ok: bool
    invariant: Optional[str] = None
    slot: Optional[str] = None
    expected: Optional[str] = None
    actual: Optional[Any] = None

    @classmethod
    def passed(cls) -> "ValidationResult":
        return cls(ok=True)

    @classmethod
    def anomaly(
        cls,
        *,
        invariant: str,
        slot: str,
        expected: str,
        actual: Any,
    ) -> "ValidationResult":
        return cls(
            ok=False,
            invariant=invariant,
            slot=slot,
            expected=expected,
            actual=actual,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "invariant": self.invariant,
            "slot": self.slot,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass
class Decision:
    """A Phase 2 routing classification.

    Carries the chosen verification method (one of the 5 routing
    methods, or None for routing-anomaly outcomes), a short reason,
    whether the classification came from the memo, the validator's
    result, and the raw ``RoutingDecision`` payload from the LLM
    router (None on memo hit and on anomaly).

    Phase 3+ wraps this Decision when verifier dispatch runs; the
    dispatch-level result is a separate, richer record produced
    downstream of Layer 2.
    """

    claim: dict
    outcome: RoutingOutcome
    method: Optional[str] = None
    reason: Optional[str] = None
    memo_hit: bool = False
    validation: Optional[ValidationResult] = None
    routing_decision: Optional[dict] = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "outcome": self.outcome.value,
            "method": self.method,
            "reason": self.reason,
            "memo_hit": self.memo_hit,
            "validation": self.validation.to_dict() if self.validation else None,
            "routing_decision": self.routing_decision,
            "notes": list(self.notes),
        }
