"""Shared verifier types.

VerificationOutcome and VerificationResult are used by every verifier
that returns a structural verdict (currently retrieval). The
code-generation pipeline has its own richer return type because it
carries trace artifacts; see ``code_generation.pipeline``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class VerificationOutcome(str, Enum):
    VERIFIED = "verified"
    CONTRADICTED = "contradicted"
    INCONCLUSIVE = "inconclusive"


@dataclass
class VerificationResult:
    outcome: VerificationOutcome
    actual_value: Any | None = None
    explanation: str = ""

    @property
    def verified(self) -> bool:
        return self.outcome is VerificationOutcome.VERIFIED

    @property
    def contradicted(self) -> bool:
        return self.outcome is VerificationOutcome.CONTRADICTED

    @property
    def inconclusive(self) -> bool:
        return self.outcome is VerificationOutcome.INCONCLUSIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "actual_value": self.actual_value,
            "explanation": self.explanation,
        }
