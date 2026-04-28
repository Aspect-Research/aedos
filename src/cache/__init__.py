"""Tier 2 verification cache (v0.6).

Per spec, this is a CACHE, not a knowledge base. Every cached verdict
is provisional — entries can be wrong, can go stale, and are subject
to eviction. The cache is a performance optimization for retrieval.

Implementation order (each its own commit):

  1. ✓ Schema (verification_cache table + 4 pipeline event stages)
  2. → Scoping classifier in OBSERVATION MODE (this module).
       Decides per claim: user_specific / session_specific / world_fact.
       Only world_fact is cache-eligible. In observation mode, the
       classifier runs and logs its decision but does NOT gate caching.
       Two sessions of observation logs first, then move to step 3.
  3. Stability classifier in observation mode. Decides TTL class
     for cache-eligible claims.
  4. Cache lookup wired into retrieval verifier (read-only at first).
  5. Cache write wired in. Now the cache is live.
  6. Cache inspector tab in trace UI.

Steps 4 and 5 are deliberately two commits — read-only-first lets us
measure the hit rate against actual retrieval results before risking
serving cached verdicts.
"""

from src.cache.scoping_classifier import (
    SCOPING_METHODS,
    ScopingDecision,
    classify_scope,
)

__all__ = ["classify_scope", "ScopingDecision", "SCOPING_METHODS"]
