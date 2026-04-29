"""Public router types: Decision dataclass + RoutingOutcome enum.

Pulled out of the monolithic router.py during the v0.7 refactor so
consumers (Pipeline, Corrector, tests) can import these without
loading the full Router machinery — clearer dependency graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from src.verifiers.retrieval_verifier import RetrievalResult


class RoutingOutcome(str, Enum):
    USER_STORED = "user_stored"
    USER_DUPLICATE = "user_duplicate"
    USER_CONTRADICTED_PRIOR = "user_contradicted_prior"
    USER_CONTRADICTED_SELF = "user_contradicted_self"  # v0.6 prototype
    VERIFIED = "verified"
    CONTRADICTED = "contradicted"
    UNVERIFIED = "unverified"
    UNVERIFIABLE_IN_PRINCIPLE = "unverifiable_in_principle"
    ROUTING_ANOMALY = "routing_anomaly"


# Map from internal verification_status (8 distinct states for routing
# logic) to a 4-bucket display_status the UI can render uniformly.
# The internal statuses stay because the corrector + cache + tests
# depend on the fine-grained distinction (e.g. retrieval_failed vs
# retrieval_inconclusive flips the corrector between hedge and silence).
# But the UI doesn't need 8 colors / 8 badges / 8 explanations — 4
# buckets cover the operator's mental model:
#
#   verified        — claim was confirmed (any path)
#   contradicted    — claim was disproven (any path)
#   inconclusive    — verifier ran but couldn't decide
#   not_applicable  — verification doesn't apply (user-asserted,
#                     unverifiable_in_principle, routing_anomaly,
#                     verifier failed entirely)
#
# Routing logic still keys off verification_status; the UI keys off
# display_status. Adding a status to the internal enum just needs an
# entry in this map.
DISPLAY_STATUS_BY_VERIFICATION_STATUS: dict[str, str] = {
    "verified": "verified",
    "user_asserted": "not_applicable",
    "contradicted": "contradicted",
    "retrieval_inconclusive": "inconclusive",
    "retrieval_failed": "not_applicable",  # not evidence of uncertainty
    "unverifiable_in_principle": "not_applicable",
    "unverifiable_pending_implementation": "inconclusive",
    "routing_anomaly": "not_applicable",
}


def display_status_for(verification_status: str) -> str:
    """Map an internal verification_status to its UI bucket. Unknown
    statuses fall back to ``inconclusive`` rather than crashing — the
    operator can spot something they don't recognize and the UI keeps
    rendering."""
    return DISPLAY_STATUS_BY_VERIFICATION_STATUS.get(
        verification_status, "inconclusive",
    )


@dataclass
class Decision:
    claim: dict
    outcome: RoutingOutcome
    verification_status: str = ""
    confidence: float = 0.0
    stored_fact_id: Optional[int] = None
    boosted_fact_id: Optional[int] = None
    closed_fact_ids: list[int] = field(default_factory=list)
    contradicting_fact_id: Optional[int] = None
    matching_fact_id: Optional[int] = None
    code_gen_result: Optional[dict] = None
    retrieval_result: Optional[RetrievalResult] = None
    correction: Optional[dict] = None
    notes: list[str] = field(default_factory=list)
    anomaly_slot: Optional[dict] = None  # {slot, expected, actual} for routing anomalies
    # v0.5: routing decision payload from the LLM router. Surfaces in the
    # trace UI as the leading section of every model-origin Decision.
    routing_decision: Optional[dict] = None
    # v0.6: True when the verdict came from the Tier 2 verification
    # cache (short-circuited the retrieval verifier). Lets the UI mark
    # cached claims distinctly without having to grep notes for the
    # "served from cache" string.
    served_from_cache: bool = False

    @property
    def display_status(self) -> str:
        """4-bucket UI-facing status. Computed from verification_status
        via DISPLAY_STATUS_BY_VERIFICATION_STATUS — see that map."""
        return display_status_for(self.verification_status)

    def to_dict(self) -> dict:
        return {
            "claim": self.claim,
            "outcome": self.outcome.value,
            "verification_status": self.verification_status,
            "display_status": self.display_status,
            "confidence": self.confidence,
            "stored_fact_id": self.stored_fact_id,
            "boosted_fact_id": self.boosted_fact_id,
            "closed_fact_ids": self.closed_fact_ids,
            "contradicting_fact_id": self.contradicting_fact_id,
            "matching_fact_id": self.matching_fact_id,
            "code_gen_result": self.code_gen_result,
            "retrieval_result": (
                # Cache-as-evidence (v0.7.10): the cache-hit path
                # passes a dict directly. The fresh-retrieval path
                # passes a RetrievalResult object with .to_dict().
                self.retrieval_result.to_dict()
                if hasattr(self.retrieval_result, "to_dict")
                else self.retrieval_result
            ),
            "correction": self.correction,
            "notes": self.notes,
            "anomaly_slot": self.anomaly_slot,
            "routing_decision": self.routing_decision,
            "served_from_cache": self.served_from_cache,
        }
