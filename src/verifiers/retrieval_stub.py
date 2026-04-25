"""Retrieval-based verification — stub for v1.

Predicates like ``capital_of`` or ``born_in_year`` need an external knowledge
source to verify. We don't have one wired up yet, so this stub always returns
``inconclusive`` with an explanation that makes it clear *why* the claim
wasn't verified (and therefore what the user would need to add in v2).

Keeping it a separate module (and a separate verification method in the
registry) means adding a real retrieval layer later is a drop-in change —
we'd replace this function's body, not touch the router.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RetrievalStubResult:
    explanation: str = (
        "retrieval verification is not implemented in v1; "
        "the claim is stored as unverified with low confidence"
    )

    def to_dict(self) -> dict:
        return {"outcome": "inconclusive_stub", "explanation": self.explanation}


def retrieval_verify(claim: dict) -> RetrievalStubResult:
    return RetrievalStubResult()
