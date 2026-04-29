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
