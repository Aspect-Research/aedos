from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from ..layer1_extraction.extractor import Claim
from ..layer3_substrate.predicate_translation import PredicateMetadata


@dataclass
class ValidationResult:
    passed: bool
    anomaly_reason: Optional[str] = None


_OBJECT_TYPE_HEURISTICS = {
    "quantity": lambda v: _looks_numeric(v),
    "time": lambda v: _looks_temporal(v),
    "entity": lambda v: True,  # any string is acceptable as an entity reference
    "proposition": lambda v: True,
    "entity_list": lambda v: True,
}


def _looks_numeric(value: str) -> bool:
    import re
    return bool(re.search(r"\d", str(value)))


def _looks_temporal(value: str) -> bool:
    import re
    return bool(re.search(r"\d{4}", str(value)))


class Validator:
    """Check structural invariants for a claim given its predicate metadata."""

    def validate(self, claim: Claim, predicate_metadata: PredicateMetadata) -> ValidationResult:
        # Check user_subject_required
        if predicate_metadata.user_subject_required:
            if claim.subject != claim.asserting_party:
                return ValidationResult(
                    passed=False,
                    anomaly_reason=(
                        f"user_subject_required: subject={claim.subject!r} "
                        f"must equal asserting_party={claim.asserting_party!r}"
                    ),
                )

        # Check distinct_slots
        if predicate_metadata.distinct_slots:
            slots = predicate_metadata.distinct_slots
            if "subject" in slots and "object" in slots:
                if claim.subject == claim.object:
                    return ValidationResult(
                        passed=False,
                        anomaly_reason=(
                            f"distinct_slots: subject and object must differ "
                            f"but both are {claim.subject!r}"
                        ),
                    )

        # Check object_type loosely
        obj_type = predicate_metadata.object_type
        if obj_type in _OBJECT_TYPE_HEURISTICS:
            check = _OBJECT_TYPE_HEURISTICS[obj_type]
            if not check(claim.object):
                return ValidationResult(
                    passed=False,
                    anomaly_reason=(
                        f"object_type mismatch: expected {obj_type!r}, "
                        f"got {claim.object!r}"
                    ),
                )

        return ValidationResult(passed=True)
