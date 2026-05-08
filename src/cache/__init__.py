"""Cache-classifier helpers (scoping + stability + combined).

Used by Layer 4's fresh-tier dispatcher to decide whether a retrieval
verdict is worth writing to Tier W. The fresh dispatcher consults
``classify_for_cache`` (the single-call combined classifier) per
claim; only world-fact claims with non-volatile stability get cached.

Cache *storage* is Tier W (``src.layer4_lookup.tier_w``), not this
module. This module is purely the scope/stability classification logic
the dispatcher uses to gate writes.
"""

from src.cache.scoping_classifier import (
    SCOPING_METHODS,
    ScopingDecision,
    classify_scope,
)
from src.cache.stability_classifier import (
    STABILITY_CLASSES,
    STABILITY_TTL_SECONDS,
    StabilityDecision,
    classify_stability,
)

__all__ = [
    "classify_scope", "ScopingDecision", "SCOPING_METHODS",
    "classify_stability", "StabilityDecision",
    "STABILITY_CLASSES", "STABILITY_TTL_SECONDS",
]
