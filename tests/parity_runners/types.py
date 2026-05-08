"""Shared dataclasses for the Phase 9 parity audit.

The audit's job is bucket assignment, not pass/fail comparison: per
the architecture conversation in 9a's planning, both stacks may
*both* fail an entry (legitimate pre-existing limitation), neither
may apply (substrate_direct entries have no v1 equivalent), or one
may legitimately exceed the other (the architectural improvements
shipped in v0.14). The bucket captures the joint state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Bucket(str, Enum):
    """Five-bucket assignment per entry.

    The cutover gate is ``UNEXPECTED_DIVERGENCE`` count == 0. The
    other four are informational; the report shows counts in each.
    """

    V2_ONLY_BY_DESIGN = "v2_only_by_design"
    EXPECTED_DIVERGENCE = "expected_divergence"
    UNEXPECTED_DIVERGENCE = "unexpected_divergence"
    BOTH_PASS = "both_pass"
    BOTH_FAIL = "both_fail"


class StackResult(str, Enum):
    """Per-stack outcome for a single entry."""

    PASS = "pass"            # stack produced the expected behavior
    FAIL = "fail"            # stack ran but produced wrong behavior
    NOT_APPLICABLE = "not_applicable"  # stack has no equivalent path
    ERROR = "error"          # stack raised an unexpected exception


@dataclass(frozen=True)
class StackVerdict:
    """One stack's outcome on one entry."""

    result: StackResult
    detail: str = ""           # one-line human-readable summary
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.result is StackResult.PASS

    def to_dict(self) -> dict:
        return {
            "result": self.result.value,
            "detail": self.detail,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


@dataclass(frozen=True)
class EntryOutcome:
    """Joint per-entry audit outcome."""

    entry_id: str
    shape: str                  # smoke entry shape value, e.g. "assistant_lookup"
    expected_divergence_kind: Optional[str]  # registry tag, or None
    v1: StackVerdict
    v2: StackVerdict
    bucket: Bucket
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "shape": self.shape,
            "expected_divergence_kind": self.expected_divergence_kind,
            "v1": self.v1.to_dict(),
            "v2": self.v2.to_dict(),
            "bucket": self.bucket.value,
            "notes": list(self.notes),
        }
