"""Routing constants: pattern-shape maps + the confidence formula.

Pulled out of router.py during the v0.7 refactor. None of these
require Router state; isolating them here lets the Router class file
focus on dispatch logic and lets other modules (CacheGate's
identity-slot anchor, Pipeline's anomaly handling) reach for the
constants without circular imports.

v0.13 — pure-frequentist confidence
===================================
Pre-v0.13 confidence had three tangled inputs: per-outcome path-prior
constants (CONF_USER_ASSERTED, CONF_PYTHON_VERIFIED, ...), an LLM
self-rating (router confidence, judge confidence, classifier
confidence), and a saturating reinforcement curve. The first two were
fundamentally subjective — the LLM's guess at how confident it should
be — and tests showed they didn't track real-world correctness.

v0.13 strips both: confidence is now a pure function of observed
counts. ``confidence_from_counts(refresh_count, contradiction_count)``
returns the Beta(1,1) Laplace-smoothed posterior estimate of P(true |
observed evidence). No per-outcome priors, no LLM emissions.
"""

from __future__ import annotations

import os
from typing import Any


def confidence_from_counts(
    refresh_count: int = 0,
    contradiction_count: int = 0,
) -> float:
    """Pure-frequentist confidence with Laplace smoothing.

    confidence = (refresh_count + 1) / (refresh_count + contradiction_count + 2)

    Properties:
      - (0, 0) → 0.5 (no evidence; uniform prior, undecided)
      - (1, 0) → 0.67 (one confirmation, no contradictions)
      - (5, 0) → 0.86
      - (10, 0) → 0.92
      - (5, 1) → 0.75 (one contradiction takes a meaningful bite)
      - (50, 5) → 0.89 (lots of evidence; rare contradictions barely register)
      - asymptotes to 1.0 as refreshes grow without contradictions
      - asymptotes to 0.0 as contradictions grow without refreshes

    The +1 / +2 is a uniform Beta(1,1) prior — minimal Bayesian
    smoothing so (0, 0) doesn't divide by zero. No per-outcome path
    priors, no LLM self-ratings; the only inputs are observed counts.
    """
    refreshes = max(0, int(refresh_count or 0))
    contras = max(0, int(contradiction_count or 0))
    return (refreshes + 1) / (refreshes + contras + 2)

# Slots that define identity for each pattern's store-lookup key.
# CacheGate also uses this map to anchor its semantic-shape lookup.
KEY_SLOTS_BY_PATTERN: dict[str, list[str]] = {
    "preference": ["agent", "object"],
    "propositional_attitude": ["agent", "proposition"],
    "spatial_temporal": ["entity", "location"],
    "categorical": ["entity", "category"],
    "role_assignment": ["agent", "role", "org"],
    "relational": ["subject", "object"],
    "quantitative": ["subject", "property"],
    "event": ["event_type", "occurred_at"],
}

# Patterns whose subject must be the user. If the extractor produced one
# of these patterns with a non-user agent, that's almost always an
# upstream slot-binding error — flag it as a routing anomaly. (v0.4 used
# a per-pattern YAML flag for this; v0.5 inlines the rule.)
USER_SUBJECT_PATTERNS: dict[str, str] = {
    "preference": "agent",
    "propositional_attitude": "agent",
}

# v0.6 PROTOTYPE — unique-value-slot detection. Opt-in via env var.
# Catches "user said X about themselves in turn N, then says Y in turn
# M" when the value-slot is biologically/definitionally unique per
# entity (one birthplace, one biological mother).
# Format: (pattern, predicate, identity_slot, value_slot) → True
UNIQUE_VALUE_SLOTS: dict[tuple[str, str, str, str], bool] = {
    ("spatial_temporal", "was_born_in", "entity", "location"): True,
}


def unique_value_slots_enabled() -> bool:
    """Reads the env var live so tests can monkeypatch."""
    return os.getenv("AEDOS_UNIQUE_VALUE_SLOTS") == "1"


def is_user(value: Any) -> bool:
    """Whether a slot value names the chatting user. Used by routing
    rules that distinguish first-party claims from third-party ones."""
    return isinstance(value, str) and value.strip().lower() in {"user", "me", "i"}


# v0.10.0 — per-claim-class user trust.
#
# The pre-v0.10 architecture trusted the user as ground truth on
# every claim regardless of subject. That makes sense for "I like
# peanut butter" — there's no external check possible. It does NOT
# make sense for "It's currently 9:56 AM in Cairo" — wrong is wrong
# whether the user or the model said it.
#
# The fix: split user claims into TWO classes by what the claim is
# about, not who said it:
#
#   * Self-attribute  — the user is the subject. The user is the
#                       only authority. Sacrosanct.
#                       Examples: preferences, beliefs/attitudes,
#                       user's own location/role/relationships.
#   * World claim     — the subject is anything else. Same
#                       verification stack as model claims.
#                       Examples: timezone offsets, populations,
#                       historical dates, who-is-the-president.
#
# This map names the slot whose value identifies the claim's
# subject for each pattern. ``is_self_attribute`` checks whether
# that slot's value is the user. Mirrors the spirit of
# USER_SUBJECT_PATTERNS but covers every pattern, not just the two
# where user-subject is mandatory.
SUBJECT_SLOT_BY_PATTERN: dict[str, str] = {
    "preference":             "agent",
    "propositional_attitude": "agent",
    "spatial_temporal":       "entity",
    "categorical":            "entity",
    "role_assignment":        "agent",
    "relational":             "subject",
    "quantitative":           "subject",
    # `event` has no single subject — `participants` is a list.
    # Self-attribute check below handles the list case.
    "event":                  "participants",
}


def is_self_attribute(claim: dict) -> bool:
    """Whether this claim's primary subject IS the user.

    True → the user is authoritative; route to the sacrosanct
    user-asserted path (no external verification, store at
    CONF_USER_ASSERTED).

    False → the claim is about the world, the user is just the one
    who said it; route through the same LLM router + verifier that
    handles model claims and store with whatever verdict comes back.

    The check looks up the pattern's subject slot and tests whether
    its value names the user. The `event` pattern carries
    `participants` as a list; we treat the claim as a self-attribute
    iff the user appears in it (so "I attended the Olympics" is
    sacrosanct on the participation aspect — but distinct claims
    like "the Olympics happened in 2020" route as world claims
    because their subject is `event_type`, not `participants`)."""
    pattern = claim.get("pattern", "")
    slot_name = SUBJECT_SLOT_BY_PATTERN.get(pattern)
    if slot_name is None:
        return False
    slots = claim.get("slots") or {}
    value = slots.get(slot_name)
    if isinstance(value, list):
        return any(is_user(v) for v in value)
    return is_user(value)
