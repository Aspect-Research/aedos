"""Routing constants: confidence levels + pattern-shape maps.

Pulled out of router.py during the v0.7 refactor. None of these
require Router state; isolating them here lets the Router class file
focus on dispatch logic and lets other modules (CacheGate's
identity-slot anchor, Pipeline's anomaly handling) reach for the
constants without circular imports.
"""

from __future__ import annotations

import math
import os
from typing import Any

# Confidence levels — the PATH PRIOR component of a Decision's
# confidence. The full v0.7.13 formula is:
#
#     final_confidence = confidence_with_reinforcement(
#         base = path_prior * verifier_reported_confidence,
#         refresh_count, contradiction_count,
#     )
#
# Path priors capture "how much do I trust this kind of evidence" —
# code-gen sandbox > retrieval judge > store lookup. The verifier's
# own confidence (judge confidence, code-gen confidence) further
# scales it for cases where the verifier itself wasn't sure. The
# reinforcement adjustment encodes EARNED TRUST over time.
CONF_USER_ASSERTED = 0.95
CONF_PYTHON_VERIFIED = 0.99
CONF_PYTHON_CORRECTION = 0.99
CONF_RETRIEVAL_VERIFIED = 0.95
CONF_RETRIEVAL_CORRECTION = 0.95
CONF_STORE_VERIFIED = 0.95
CONF_PENDING_IMPLEMENTATION = 0.4
CONF_RETRIEVAL_INCONCLUSIVE = 0.4
CONF_RETRIEVAL_FAILED = 0.4
CONF_UNVERIFIABLE_IN_PRINCIPLE = 0.3
CONF_ROUTING_ANOMALY = 0.2

# v0.7.13: reinforcement curve tunables.
#
# Reinforcement bonus saturates: each extra confirmation buys less
# headroom toward 1.0. tau controls how fast we approach the ceiling
# (5 → ~63% of headroom after 5 reinforcements, ~86% after 10).
# Penalty per contradiction is linear and capped to keep a single
# bad event from collapsing trust entirely.
CONF_REINFORCEMENT_TAU = 5.0
CONF_PENALTY_PER_CONTRADICTION = 0.06
CONF_PENALTY_CAP = 0.40
CONF_FLOOR = 0.05  # never let confidence go below this — operator should be able to see it


def confidence_with_reinforcement(
    base: float,
    refresh_count: int = 0,
    contradiction_count: int = 0,
) -> float:
    """Adjust a base confidence using earned-trust signals.

    The user's design intent (v0.7.13 architectural pass): confidence
    should reflect HOW MANY TIMES a claim has been reinforced and
    HOW OFTEN it has flipped — not just be a per-outcome constant.

    Behavior:
      * `refresh_count = 0, contradiction_count = 0` → returns `base`.
        New verdicts retain their path prior unchanged.
      * Reinforcements (same-verdict reconfirmations) add toward 1.0
        with diminishing returns. After ~5 confirmations, ~63% of
        the headroom above `base` is gained; after ~10, ~86%.
      * Contradictions (verdict flips in this entry's history) each
        subtract a small linear penalty. Capped so one flip doesn't
        zero confidence.
      * Floor at CONF_FLOOR so a heavily-flipped entry stays
        observable (and doesn't accidentally cross the < 0.5 hedge
        threshold the moment one contradiction lands).

    `base` is clamped to [0, 1] before the curve is applied so callers
    can pass ``path_prior * verifier_confidence`` directly.
    """
    base = max(0.0, min(1.0, float(base)))
    headroom = max(0.0, 1.0 - base)
    bonus = headroom * (1.0 - math.exp(-max(0, refresh_count) / CONF_REINFORCEMENT_TAU))
    penalty = min(
        CONF_PENALTY_CAP,
        CONF_PENALTY_PER_CONTRADICTION * max(0, contradiction_count),
    )
    return max(CONF_FLOOR, min(1.0, base + bonus - penalty))

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
