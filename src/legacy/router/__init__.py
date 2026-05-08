"""Verification routing package.

Public API (preserves the pre-refactor ``from src.legacy.router import ...``
contract verbatim):

  * Router  — class, dispatches per-claim
  * Decision, RoutingOutcome  — result types
  * KEY_SLOTS_BY_PATTERN  — pattern-shape map (used by CacheGate)

Internal modules:
  * router.router    — Router class + dispatch logic
  * router.types     — Decision dataclass + RoutingOutcome enum
  * router.constants — confidence levels + pattern-shape maps
"""

from src.legacy.router.constants import (
    KEY_SLOTS_BY_PATTERN,
    UNIQUE_VALUE_SLOTS,
    USER_SUBJECT_PATTERNS,
    is_user,
    unique_value_slots_enabled,
)
from src.legacy.router.router import Router
from src.legacy.router.types import Decision, RoutingOutcome

# Back-compat aliases for the pre-refactor private names. Keep until
# the test suite is updated; new code should use the public exports
# from src.legacy.router.constants.
_is_user = is_user
_unique_value_slots_enabled = unique_value_slots_enabled
_USER_SUBJECT_PATTERNS = USER_SUBJECT_PATTERNS
_UNIQUE_VALUE_SLOTS = UNIQUE_VALUE_SLOTS

__all__ = [
    "Router", "Decision", "RoutingOutcome", "KEY_SLOTS_BY_PATTERN",
]
