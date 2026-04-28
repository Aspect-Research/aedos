"""Routing constants: confidence levels + pattern-shape maps.

Pulled out of router.py during the v0.7 refactor. None of these
require Router state; isolating them here lets the Router class file
focus on dispatch logic and lets other modules (CacheGate's
identity-slot anchor, Pipeline's anomaly handling) reach for the
constants without circular imports.
"""

from __future__ import annotations

import os
from typing import Any

# Confidence levels — assigned to Decisions per verification path.
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
