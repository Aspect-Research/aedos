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

    def to_dict(self) -> dict:
        return {
            "claim": self.claim,
            "outcome": self.outcome.value,
            "verification_status": self.verification_status,
            "confidence": self.confidence,
            "stored_fact_id": self.stored_fact_id,
            "boosted_fact_id": self.boosted_fact_id,
            "closed_fact_ids": self.closed_fact_ids,
            "contradicting_fact_id": self.contradicting_fact_id,
            "matching_fact_id": self.matching_fact_id,
            "code_gen_result": self.code_gen_result,
            "retrieval_result": (
                self.retrieval_result.to_dict() if self.retrieval_result else None
            ),
            "correction": self.correction,
            "notes": self.notes,
            "anomaly_slot": self.anomaly_slot,
            "routing_decision": self.routing_decision,
            "served_from_cache": self.served_from_cache,
        }
