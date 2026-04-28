"""Tier 2 verification cache (v0.6).

Per spec, this is a CACHE, not a knowledge base. Every cached verdict
is provisional — entries can be wrong, can go stale, and are subject
to eviction. The cache is a performance optimization for retrieval.

The pipeline:

  1. Scoping classifier — per claim, decides
     ``user_specific`` / ``session_specific`` / ``world_fact``.
     Only world_fact is cache-eligible. (See ``scoping_classifier.py``.)
  2. Stability classifier — for cache-eligible claims, picks one of
     ``immutable`` / ``decade_stable`` / ``years_stable`` /
     ``months_stable`` / ``days_stable`` / ``volatile`` and the
     resulting TTL. Volatile entries skip the cache. (See
     ``stability_classifier.py``.)
  3. Lookup — at route time, the router checks the cache for an
     unexpired entry under the canonical key. A hit short-circuits
     the retrieval verifier and returns a Decision with
     ``served_from_cache=True``. (See ``Router._maybe_cache_hit``.)
  4. Write — after a successful retrieval verdict, the pipeline
     writes the verdict + TTL to the cache. (See
     ``Pipeline._maybe_write_cache``.)

Always on. ``build_pipeline`` wires all three components on every
construction so the cache accumulates verdicts across sessions; that
accumulation is the whole point. Tests that want a no-cache pipeline
(for hermetic stages) construct ``Pipeline`` directly with
``scoping_classifier=None`` / ``stability_classifier=None`` /
``verification_cache=None``.

Inspect with the Cache tab in the trace UI, ``/api/cache``, or
``scripts/analyze_cache.py``.
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
from src.cache.verification_cache import (
    CachedVerdict,
    SemanticHit,
    VerificationCache,
    canonicalize_claim_key,
)
from src.cache.gate import CacheGate, CacheHit, ClaimCacheState

__all__ = [
    "classify_scope", "ScopingDecision", "SCOPING_METHODS",
    "classify_stability", "StabilityDecision",
    "STABILITY_CLASSES", "STABILITY_TTL_SECONDS",
    "VerificationCache", "CachedVerdict", "SemanticHit",
    "canonicalize_claim_key",
    "CacheGate", "CacheHit", "ClaimCacheState",
]
