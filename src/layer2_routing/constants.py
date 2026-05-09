"""Layer 2 routing constants for the v0.14 stack.

Phase 0 shipped only ``confidence_from_counts``. Phase 1 added the
pattern-shape maps (``SUBJECT_SLOT_BY_PATTERN`` and
``KEY_SLOTS_BY_PATTERN``) seeded with all nine patterns. Phase 2 adds
``USER_SUBJECT_PATTERNS`` (the validator invariant for preference and
propositional_attitude), ``UNIQUE_VALUE_SLOTS`` (a v0.6 prototype the
Phase 6 Tier U rewrite will consume), and two helpers: ``is_user``
(lexical user check, used by the Phase 2 validator) and
``is_self_attribute`` (subject-slot user check, used by Phase 4+'s
walker for tier-U-vs-tier-W routing).

The function signature changes from v0.13's
``(refresh_count, contradiction_count)`` to v0.14's
``(affirmed_count, contradicted_count)`` so call sites in v2 can pass
the schema's own column names without translation. The math is
unchanged: a Beta(1,1) Laplace-smoothed posterior of P(true | evidence).
"""

from __future__ import annotations

import os
from typing import Any


def confidence_from_counts(
    affirmed_count: int = 0,
    contradicted_count: int = 0,
) -> float:
    """Beta(1,1)-smoothed posterior estimate of P(true | observed counts).

    confidence = (affirmed_count + 1) / (affirmed_count + contradicted_count + 2)

    Properties (identical to v0.13):
      - (0, 0)   -> 0.5  (no evidence; uniform prior)
      - (1, 0)   -> 0.67
      - (5, 0)   -> 0.86
      - (10, 0)  -> 0.92
      - (5, 1)   -> 0.75 (one contradiction takes a meaningful bite)
      - (50, 5)  -> 0.89
      - asymptotes to 1.0 as affirmations grow without contradictions
      - asymptotes to 0.0 as contradictions grow without affirmations

    The +1 / +2 is a uniform Beta(1,1) prior — minimal smoothing so
    (0, 0) doesn't divide by zero. Per architecture principle 3,
    counts only increment on independent external evidence; this
    function is the consumer of those counts.
    """
    affirmed = max(0, int(affirmed_count or 0))
    contras = max(0, int(contradicted_count or 0))
    return (affirmed + 1) / (affirmed + contras + 2)


# v0.14 Phase 1 — pattern-shape maps consumed by Phase 2's validator.
#
# SUBJECT_SLOT_BY_PATTERN names the slot whose value is the
# canonical "subject" of a claim under each pattern. Phase 2 uses
# this for routing-anomaly detection (preference / propositional_
# attitude must have agent ∈ {user, me, i}; mereological's part
# must not equal whole; etc.) and for the user-vs-world claim split
# at routing time.
#
# Mereological's subject is `part`. Locational containment lives in
# spatial_temporal, NOT here — see patterns.yaml for the
# constitutive/locational boundary.
SUBJECT_SLOT_BY_PATTERN: dict[str, str] = {
    "preference":             "agent",
    "propositional_attitude": "agent",
    "spatial_temporal":       "entity",
    "categorical":            "entity",
    "role_assignment":        "agent",
    "relational":             "subject",
    "quantitative":           "subject",
    # `event` has no single subject — `participants` is a list.
    # Phase 2's helpers handle the list case.
    "event":                  "participants",
    "mereological":           "part",
}


# KEY_SLOTS_BY_PATTERN names the slots that together identify a fact
# under each pattern. Used by store-lookup keys and (in v1, ported to
# Phase 2) the routing memo's identity check. The ordering matters:
# slots are concatenated in the listed order to form the lookup key.
#
# Mereological's identity is (part, whole) — the same fact carries
# the same part/whole pair regardless of which constitutive
# predicate label (part_of, member_of, composed_of, …) was used.
KEY_SLOTS_BY_PATTERN: dict[str, list[str]] = {
    "preference":             ["agent", "object"],
    "propositional_attitude": ["agent", "proposition"],
    "spatial_temporal":       ["entity", "location"],
    "categorical":            ["entity", "category"],
    "role_assignment":        ["agent", "role", "org"],
    "relational":             ["subject", "object"],
    "quantitative":           ["subject", "property"],
    "event":                  ["event_type", "occurred_at"],
    "mereological":           ["part", "whole"],
}


# DEPRECATED v0.14.3 — the source of truth for "patterns whose subject
# slot must name the user" is now the per-pattern ``agent_constraint``
# field in ``patterns.yaml``. The validator reads from the registry
# directly. This constant remains as a back-compat alias for any
# external test or downstream module still importing it; its contents
# mirror what ``patterns.yaml`` declares but are no longer
# load-bearing.
USER_SUBJECT_PATTERNS: dict[str, str] = {
    "preference":             "agent",
    "propositional_attitude": "agent",
}


# v0.6 prototype — unique-value-slot detection. Catches the case where
# the user asserts X about themselves in turn N, then asserts Y in
# turn M, on a slot whose value is unique by definition (one
# birthplace per person, one biological mother). Opt-in via env var.
#
# Format: ``(pattern, predicate, identity_slot, value_slot) -> True``.
# Phase 2 ports the constant per the original Phase-2-promise in this
# module's docstring; the first consumer is Phase 6's Tier U rewrite,
# which has the session model needed to distinguish "user updated
# themselves" from "user is contradicting an immutable fact".
UNIQUE_VALUE_SLOTS: dict[tuple[str, str, str, str], bool] = {
    ("spatial_temporal", "was_born_in", "entity", "location"): True,
}


def unique_value_slots_enabled() -> bool:
    """Reads the env var live so tests can monkeypatch."""
    return os.getenv("AEDOS_UNIQUE_VALUE_SLOTS") == "1"


def is_user(value: Any) -> bool:
    """Lexical user check: does ``value`` name the chatting user?

    Used by the Phase 2 validator for the ``USER_SUBJECT_PATTERNS``
    invariant. The check is intentionally narrow — it lower-cases
    and strips whitespace, then compares against a fixed set
    ``{"user", "me", "i"}``. The extractor is responsible for
    canonicalizing first-person references into one of those three
    tokens; if it emits ``"the user"`` or anything else, the
    validator treats that as a non-user agent (which is almost
    always an extractor bug).
    """
    return isinstance(value, str) and value.strip().lower() in {"user", "me", "i"}


def is_self_attribute(claim: dict) -> bool:
    """Whether this claim's primary subject IS the user.

    Used by Phase 4+'s walker to decide tier U vs tier W routing —
    NOT by the Phase 2 validator. A self-attribute claim is sacrosanct
    on its truth value (the user is ground truth about themselves);
    a world claim with the user as speaker still gets verified
    through the same stack as model claims.

    Looks up the pattern's subject slot via ``SUBJECT_SLOT_BY_PATTERN``
    and tests whether the slot's value names the user. The ``event``
    pattern carries ``participants`` as a list; the claim is treated
    as a self-attribute iff the user appears anywhere in it (so "I
    attended the Olympics" is sacrosanct on the participation aspect,
    while "the Olympics happened in 2020" — whose subject is
    ``event_type``, not ``participants`` — routes as a world claim).
    """
    pattern = claim.get("pattern", "")
    slot_name = SUBJECT_SLOT_BY_PATTERN.get(pattern)
    if slot_name is None:
        return False
    slots = claim.get("slots") or {}
    value = slots.get(slot_name)
    if isinstance(value, list):
        return any(is_user(v) for v in value)
    return is_user(value)
