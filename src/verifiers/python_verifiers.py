"""Python verifiers (v0.3 — slot-based).

Each verifier reads its inputs from ``claim["slots"]`` rather than the
flat (subject, object) shape of v0.2. The dispatch table is keyed by
PREDICATE name, not function name — free-form predicates within a
pattern can land here whenever a function is registered for them.

Predicates without a python verifier fall through to the pattern's
default verification method (typically retrieval).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


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


Claim = dict[str, Any]


def _slots(claim: Claim) -> dict[str, Any]:
    s = claim.get("slots")
    return s if isinstance(s, dict) else {}


def _apply_polarity(positive_is_true: bool, polarity: int) -> VerificationOutcome:
    return (
        VerificationOutcome.VERIFIED
        if positive_is_true == bool(polarity)
        else VerificationOutcome.CONTRADICTED
    )


def _inconclusive(reason: str) -> VerificationResult:
    return VerificationResult(VerificationOutcome.INCONCLUSIVE, explanation=reason)


def _normalize_count_property(prop: str) -> str:
    """`letter_p` and `letters_p` → `p`. Bare items pass through."""
    p = str(prop).strip().lower()
    for prefix in ("letters_", "letter_", "char_", "chars_"):
        if p.startswith(prefix):
            return p[len(prefix):]
    return p


# ---- count / length / spelling --------------------------------------


def verify_has_count(claim: Claim) -> VerificationResult:
    """quantitative: subject contains N occurrences of property item.

    Slot shape: {subject: container_string, property: item, value: count_int}.
    Property may be encoded as "letter_p" or just "p"; both resolve to "p".
    """
    s = _slots(claim)
    container = s.get("subject")
    prop = s.get("property")
    value = s.get("value")
    if not isinstance(container, str) or not container:
        return _inconclusive("has_count: subject must be a non-empty string")
    if not isinstance(prop, str) or not prop:
        return _inconclusive("has_count: property must be a non-empty string")
    try:
        claimed = int(value)
    except (TypeError, ValueError):
        return _inconclusive(f"has_count: value not an int (got {value!r})")

    item = _normalize_count_property(prop)
    if not item:
        return _inconclusive("has_count: empty normalized property")
    actual = container.lower().count(item.lower())
    outcome = _apply_polarity(actual == claimed, int(claim["polarity"]))
    return VerificationResult(
        outcome,
        actual_value=actual,
        explanation=(
            f"'{container}' contains {actual} occurrence(s) of '{item}'; "
            f"claim said {claimed}"
        ),
    )


def verify_has_length(claim: Claim) -> VerificationResult:
    """quantitative: len(subject) == value."""
    s = _slots(claim)
    subject = s.get("subject")
    value = s.get("value")
    if not isinstance(subject, str):
        return _inconclusive("has_length: subject must be a string")
    try:
        claimed = int(value)
    except (TypeError, ValueError):
        return _inconclusive(f"has_length: value not an int (got {value!r})")
    actual = len(subject)
    outcome = _apply_polarity(actual == claimed, int(claim["polarity"]))
    return VerificationResult(
        outcome,
        actual_value=actual,
        explanation=f"len({subject!r}) = {actual}; claim said {claimed}",
    )


# ---- relational verifiers -------------------------------------------


def verify_contains_substring(claim: Claim) -> VerificationResult:
    """relational: subject string contains object string (case-insensitive)."""
    s = _slots(claim)
    subj = s.get("subject")
    obj = s.get("object")
    if not isinstance(subj, str) or not isinstance(obj, str):
        return _inconclusive("contains_substring: subject and object must be strings")
    if not obj:
        return _inconclusive("contains_substring: empty needle")
    positive_is_true = obj.lower() in subj.lower()
    outcome = _apply_polarity(positive_is_true, int(claim["polarity"]))
    return VerificationResult(
        outcome,
        actual_value=positive_is_true,
        explanation=f"{obj!r} in {subj!r}? {positive_is_true}",
    )


def verify_is_anagram_of(claim: Claim) -> VerificationResult:
    """relational: subject and object share the same multiset of letters."""
    s = _slots(claim)
    subj = s.get("subject")
    obj = s.get("object")
    if not isinstance(subj, str) or not isinstance(obj, str):
        return _inconclusive("is_anagram_of: subject and object must be strings")

    def norm(x: str) -> list[str]:
        return sorted(c for c in x.lower() if c.isalpha())

    a, b = norm(subj), norm(obj)
    if not a or not b:
        return _inconclusive("is_anagram_of: empty alphabetic content")
    outcome = _apply_polarity(a == b, int(claim["polarity"]))
    return VerificationResult(
        outcome,
        actual_value="".join(a),
        explanation=f"sorted letters: {''.join(a)!r} vs {''.join(b)!r}",
    )


# ---- arithmetic -----------------------------------------------------


def _parse_number_list(raw: Any) -> list[float] | None:
    """Accept JSON-string list or actual list."""
    if isinstance(raw, list):
        try:
            return [float(x) for x in raw]
        except (TypeError, ValueError):
            return None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, list):
            return None
        try:
            return [float(x) for x in parsed]
        except (TypeError, ValueError):
            return None
    return None


def _parse_number(x: Any) -> float | None:
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return None


def verify_sum_equals(claim: Claim) -> VerificationResult:
    """quantitative: sum of subject's numeric operands == value."""
    s = _slots(claim)
    operands = _parse_number_list(s.get("subject"))
    claimed = _parse_number(s.get("value"))
    if operands is None or claimed is None:
        return _inconclusive("sum_equals: bad operands")
    actual = sum(operands)
    outcome = _apply_polarity(abs(actual - claimed) < 1e-9, int(claim["polarity"]))
    return VerificationResult(
        outcome,
        actual_value=actual,
        explanation=f"sum({operands}) = {actual}; claim said {claimed}",
    )


def verify_product_equals(claim: Claim) -> VerificationResult:
    """quantitative: product of subject's numeric operands == value."""
    s = _slots(claim)
    operands = _parse_number_list(s.get("subject"))
    claimed = _parse_number(s.get("value"))
    if operands is None or claimed is None:
        return _inconclusive("product_equals: bad operands")
    actual = 1.0
    for n in operands:
        actual *= n
    outcome = _apply_polarity(abs(actual - claimed) < 1e-9, int(claim["polarity"]))
    return VerificationResult(
        outcome,
        actual_value=actual,
        explanation=f"product({operands}) = {actual}; claim said {claimed}",
    )


# ---- registry — keyed by PREDICATE name, not function name ----------


VERIFIERS: dict[str, Callable[[Claim], VerificationResult]] = {
    "has_count": verify_has_count,
    "has_length": verify_has_length,
    "contains_substring": verify_contains_substring,
    "is_anagram_of": verify_is_anagram_of,
    "sum_equals": verify_sum_equals,
    "product_equals": verify_product_equals,
}


def get_verifier(predicate: str) -> Callable[[Claim], VerificationResult] | None:
    """Look up a python verifier by predicate name. Returns None if not registered."""
    return VERIFIERS.get(predicate)
