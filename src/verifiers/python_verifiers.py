"""Deterministic python verifiers for claims where a simple function suffices.

Each verifier takes a ``claim`` dict with keys (subject, predicate, object,
object_type, polarity, source_text) and returns a ``VerificationResult``.

Design notes:
- Verifiers are deliberately narrow. When input shape doesn't match, return
  INCONCLUSIVE rather than guessing. The router treats INCONCLUSIVE as
  "unverified, low confidence".
- Polarity is factored in uniformly via ``_apply_polarity``: compute whether
  the positive-polarity form of the claim is true, then XOR with the claim's
  actual polarity.
- Several verifiers accept JSON-encoded structure in the object or subject
  (documented per-predicate in predicates.yaml) so the extractor can pass
  along the operands the verifier needs without each one re-parsing prose.
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
    actual_value: Any | None = None  # what the verifier found; used to build corrections
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


def _apply_polarity(positive_is_true: bool, polarity: int) -> VerificationOutcome:
    """If the claim's polarity aligns with reality, VERIFIED; otherwise CONTRADICTED."""
    return (
        VerificationOutcome.VERIFIED
        if positive_is_true == bool(polarity)
        else VerificationOutcome.CONTRADICTED
    )


def _inconclusive(reason: str) -> VerificationResult:
    return VerificationResult(VerificationOutcome.INCONCLUSIVE, explanation=reason)


# ---- individual verifiers ------------------------------------------------


def verify_has_count(claim: Claim) -> VerificationResult:
    """Subject contains N occurrences of item. object is JSON: {item, count}."""
    try:
        data = json.loads(claim["object"])
        item = str(data["item"])
        claimed = int(data["count"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
        return _inconclusive(f"has_count: could not parse object JSON ({e})")

    if not item:
        return _inconclusive("has_count: empty 'item'")

    container = str(claim["subject"])
    actual = container.lower().count(item.lower())
    outcome = _apply_polarity(actual == claimed, int(claim["polarity"]))
    return VerificationResult(
        outcome,
        actual_value=json.dumps({"item": item, "count": actual}),
        explanation=(
            f"'{container}' contains {actual} occurrence(s) of '{item}'; "
            f"claim said {claimed}"
        ),
    )


def verify_spelled_as(claim: Claim) -> VerificationResult:
    """Subject word is spelled as object (letters, optionally hyphen/space-separated)."""
    subject = str(claim["subject"]).strip().lower()
    raw = str(claim["object"]).strip().lower()
    # Accept "s-t-r-a-w-b-e-r-r-y", "s t r a w b e r r y", "strawberry"
    spelling = "".join(ch for ch in raw if ch.isalnum())
    if not subject or not spelling:
        return _inconclusive("spelled_as: empty subject or object")
    outcome = _apply_polarity(subject == spelling, int(claim["polarity"]))
    return VerificationResult(
        outcome,
        actual_value="-".join(subject),
        explanation=f"'{subject}' is spelled {'-'.join(subject)}; claim gave {raw!r}",
    )


def verify_has_length(claim: Claim) -> VerificationResult:
    """len(subject) == object."""
    try:
        claimed = int(claim["object"])
    except (TypeError, ValueError) as e:
        return _inconclusive(f"has_length: object not an int ({e})")
    subject = str(claim["subject"])
    actual = len(subject)
    outcome = _apply_polarity(actual == claimed, int(claim["polarity"]))
    return VerificationResult(
        outcome,
        actual_value=actual,
        explanation=f"len({subject!r}) = {actual}; claim said {claimed}",
    )


def verify_equals(claim: Claim) -> VerificationResult:
    """Case-insensitive, whitespace-trimmed string equality."""
    s = str(claim["subject"]).strip().lower()
    o = str(claim["object"]).strip().lower()
    outcome = _apply_polarity(s == o, int(claim["polarity"]))
    return VerificationResult(
        outcome,
        actual_value=s,
        explanation=f"{s!r} == {o!r}? {s == o}",
    )


def _parse_number(x: Any) -> float | None:
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return None


def verify_greater_than(claim: Claim) -> VerificationResult:
    s = _parse_number(claim["subject"])
    o = _parse_number(claim["object"])
    if s is None or o is None:
        return _inconclusive("greater_than: non-numeric operand")
    outcome = _apply_polarity(s > o, int(claim["polarity"]))
    return VerificationResult(
        outcome,
        actual_value=s,
        explanation=f"{s} > {o}? {s > o}",
    )


def verify_less_than(claim: Claim) -> VerificationResult:
    s = _parse_number(claim["subject"])
    o = _parse_number(claim["object"])
    if s is None or o is None:
        return _inconclusive("less_than: non-numeric operand")
    outcome = _apply_polarity(s < o, int(claim["polarity"]))
    return VerificationResult(
        outcome,
        actual_value=s,
        explanation=f"{s} < {o}? {s < o}",
    )


def verify_contains_substring(claim: Claim) -> VerificationResult:
    """Case-insensitive substring check."""
    s = str(claim["subject"]).lower()
    o = str(claim["object"]).lower()
    if not o:
        return _inconclusive("contains_substring: empty needle")
    outcome = _apply_polarity(o in s, int(claim["polarity"]))
    return VerificationResult(
        outcome,
        actual_value=o in s,
        explanation=f"{o!r} in {s!r}? {o in s}",
    )


def verify_is_anagram_of(claim: Claim) -> VerificationResult:
    """Same multiset of alphabetic characters (case-insensitive)."""

    def norm(x: str) -> list[str]:
        return sorted(c for c in x.lower() if c.isalpha())

    s_norm = norm(str(claim["subject"]))
    o_norm = norm(str(claim["object"]))
    if not s_norm or not o_norm:
        return _inconclusive("is_anagram_of: empty alphabetic content")
    outcome = _apply_polarity(s_norm == o_norm, int(claim["polarity"]))
    return VerificationResult(
        outcome,
        actual_value="".join(s_norm),
        explanation=(
            f"sorted letters of subject={''.join(s_norm)!r}, "
            f"object={''.join(o_norm)!r}"
        ),
    )


def _parse_number_list(raw: Any) -> list[float] | None:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    try:
        return [float(x) for x in parsed]
    except (TypeError, ValueError):
        return None


def verify_sum_equals(claim: Claim) -> VerificationResult:
    """Sum of JSON-list operands in subject equals object."""
    operands = _parse_number_list(claim["subject"])
    claimed = _parse_number(claim["object"])
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
    """Product of JSON-list operands in subject equals object."""
    operands = _parse_number_list(claim["subject"])
    claimed = _parse_number(claim["object"])
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


VERIFIERS: dict[str, Callable[[Claim], VerificationResult]] = {
    "verify_has_count": verify_has_count,
    "verify_spelled_as": verify_spelled_as,
    "verify_has_length": verify_has_length,
    "verify_equals": verify_equals,
    "verify_greater_than": verify_greater_than,
    "verify_less_than": verify_less_than,
    "verify_contains_substring": verify_contains_substring,
    "verify_is_anagram_of": verify_is_anagram_of,
    "verify_sum_equals": verify_sum_equals,
    "verify_product_equals": verify_product_equals,
}


def get_verifier(name: str) -> Callable[[Claim], VerificationResult]:
    if name not in VERIFIERS:
        raise KeyError(f"no python verifier named {name!r}")
    return VERIFIERS[name]
