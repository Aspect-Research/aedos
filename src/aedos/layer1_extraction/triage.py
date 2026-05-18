from __future__ import annotations

import re
from enum import Enum
from typing import Optional


class TriageDecision(Enum):
    VERIFY = "verify"
    INERT_PROSE = "inert_prose"


_ALWAYS_VERIFY: frozenset[str] = frozenset([
    "born_in", "died_in", "has_nationality", "graduated_from", "employed_by",
    "located_in", "part_of", "instance_of", "founded_by", "holds_role",
    "has_age", "has_population", "has_height", "has_area", "has_length",
    "received_award", "authored", "has_parent", "has_spouse", "has_child",
    "educated_at", "member_of", "affiliated_with", "co_founded", "has_capital",
    "has_official_language", "has_currency", "is_capital_of", "is_president_of",
    "is_ceo_of", "co_founder_of", "founded", "has_headquarters",
])

_COMPARATIVE: frozenset[str] = frozenset([
    "is_greater_than", "is_less_than", "is_larger_than", "is_smaller_than",
    "is_older_than", "is_younger_than", "is_taller_than", "is_faster_than",
    "has_more_than", "has_fewer_than", "is_heavier_than", "is_longer_than",
    "is_higher_than", "is_lower_than",
])

_TEMPORAL: frozenset[str] = frozenset([
    "occurred_on", "started_on", "ended_on", "founded_in", "dissolved_in",
    "published_on", "released_on", "occurred_in", "established_in",
    "happened_on", "happened_in",
])

_NUMERIC = re.compile(r"\b\d+[,.]?\d*\b")


def _has_named_entity(value: str) -> bool:
    """Heuristic: True if value contains a word beginning with a capital letter."""
    return bool(re.search(r"\b[A-Z][a-zA-Z]", value))


def triage(
    predicate: str,
    subject: str,
    object_value: str,
    valid_from: Optional[str] = None,
    valid_until: Optional[str] = None,
    valid_during_ref: Optional[str] = None,
) -> TriageDecision:
    """Decide whether a claim is worth routing (VERIFY) or is inert prose."""
    if predicate in _ALWAYS_VERIFY:
        return TriageDecision.VERIFY
    if predicate in _COMPARATIVE:
        return TriageDecision.VERIFY
    if predicate in _TEMPORAL:
        return TriageDecision.VERIFY
    if _NUMERIC.search(object_value):
        return TriageDecision.VERIFY
    if _has_named_entity(subject) or _has_named_entity(object_value):
        return TriageDecision.VERIFY
    if valid_from or valid_until or valid_during_ref:
        return TriageDecision.VERIFY
    return TriageDecision.INERT_PROSE
