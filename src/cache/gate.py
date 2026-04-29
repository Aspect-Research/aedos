"""Single owner of the v0.6 Tier 2 cache lifecycle.

Before the gate, cache logic was scattered:
  * Pipeline._run_turn_inner inlined ~70 lines of scoping/stability
    classification + write decisions.
  * Router._maybe_cache_hit owned exact + semantic lookup wiring.
  * Pipeline._maybe_write_cache owned post-verification writes.

CacheGate consolidates all of it behind three methods:

  * ``classify(claim) -> ClaimCacheState``  — scoping + stability,
    emits the cache_*_decision events, returns whether the claim is
    cache-eligible and (if so) the TTL the verdict should be written
    under.
  * ``maybe_hit(claim, identity_slots) -> CacheHit | None`` — exact
    then semantic lookup, emits the cache_lookup event.
  * ``maybe_write(decision, claim) -> None`` — gated on the prior
    classify() decision; emits cache_write event.

Pipeline + Router stop touching cache internals; they call methods
on a single typed surface. This is purely a refactor — same behavior,
same events, same wire format.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from src.cache.scoping_classifier import ScopingDecision
from src.cache.stability_classifier import StabilityDecision
from src.cache.verification_cache import (
    CachedVerdict,
    SemanticHit,
    VerificationCache,
    canonicalize_claim_key,
)


# Verdict labels we care to write to the cache. Pulled from the
# pipeline's prior inline gate so the contract is documented in one
# place. Python verifications are cheap to redo (no API cost) and
# user-authoritative is per-user; routing_anomaly is broken upstream.
_RETRIEVAL_VERDICTS_TO_CACHE = frozenset({
    "verified", "contradicted", "retrieval_inconclusive",
})


@dataclass
class ClaimCacheState:
    """Per-claim cache eligibility decided during the classify phase.
    Stashed by the gate; consumed at write time."""
    canonical_key: str
    scope: ScopingDecision
    stability: Optional[StabilityDecision] = None  # None when not classified

    @property
    def eligible_for_cache(self) -> bool:
        """True when the claim should be considered for cache reads
        and writes. Requires world_fact scope; without stability we
        don't know the TTL so writes still skip."""
        return self.scope.scope == "world_fact"

    @property
    def writable(self) -> bool:
        """True when a verified retrieval verdict should be persisted.
        Adds a stability gate (TTL must exist + not be 0/volatile)."""
        if not self.eligible_for_cache or self.stability is None:
            return False
        ttl = self.stability.ttl_seconds
        return ttl != 0  # None (immutable) and >0 are both writable


@dataclass
class CacheHit:
    """Unified hit shape for both exact and semantic matches. The
    consumer doesn't care which path served the verdict for routing
    purposes — only for trace logging."""
    verdict: CachedVerdict
    is_semantic: bool
    matched_key: Optional[str] = None  # only set on semantic hits
    score: Optional[float] = None  # only set on semantic hits


class CacheGate:
    """Single owner of cache classification + lookup + write for one
    pipeline turn. Stateful by design — ``classify`` stashes per-claim
    decisions for the later ``maybe_write`` step.

    Construction:
      * ``cache``: VerificationCache or None. None disables every
        method (returns no-ops). Lets tests construct a no-op gate
        without classifier wiring.
      * ``scoping_fn``: callable(claim) -> ScopingDecision; usually
        ``classify_scope.__get__(llm)``-style closure built by
        build_pipeline.
      * ``stability_fn``: callable(claim) -> StabilityDecision.
      * ``store``: FactStore for emitting cache_* events. Required when
        cache is not None.
    """

    def __init__(
        self,
        cache: Optional[VerificationCache] = None,
        scoping_fn: Optional[Callable[[dict], ScopingDecision]] = None,
        stability_fn: Optional[Callable[[dict], StabilityDecision]] = None,
        store: Any = None,
    ):
        self._cache = cache
        self._scoping_fn = scoping_fn
        self._stability_fn = stability_fn
        self._store = store
        # Per-turn state. Reset by reset_for_turn().
        self._states: dict[str, ClaimCacheState] = {}

    @property
    def enabled(self) -> bool:
        """True when at least scoping is wired. The gate's other
        methods short-circuit when disabled."""
        return self._scoping_fn is not None

    @property
    def cache(self) -> Optional[VerificationCache]:
        """The underlying VerificationCache, or None when not wired.
        Exposed only for legacy callers; new code goes through the
        gate."""
        return self._cache

    @property
    def eligible_keys(self) -> set[str]:
        """The set of canonical keys the current turn's classify pass
        marked as cache-eligible. The router consults this to gate
        cache lookups."""
        return {k for k, st in self._states.items() if st.eligible_for_cache}

    def reset_for_turn(self) -> None:
        """Clear per-claim decisions. Pipeline calls at the start of
        each turn so prior classifications don't leak."""
        self._states.clear()

    # ---- classify (was Pipeline stage 5b) ------------------------------

    def classify(self, claim: dict, *, turn_id: int) -> Optional[ClaimCacheState]:
        """Run scoping → stability for one claim. Logs both decision
        events on the given turn_id. Returns the resulting
        ClaimCacheState, or None when the gate is disabled.

        Failures in either classifier are logged but never raised —
        this is observability + cache-eligibility, not the verdict
        path. A classifier exception just leaves the claim
        cache-ineligible.
        """
        if not self.enabled:
            return None
        claim_summary = {
            "pattern": claim.get("pattern"),
            "predicate": claim.get("predicate"),
            "slots": claim.get("slots"),
            "polarity": claim.get("polarity"),
        }
        # Scoping.
        try:
            scope = self._scoping_fn(claim)
            self._emit("cache_scoping_decision", turn_id, {
                "claim": claim_summary, "decision": scope.to_dict(),
            })
        except Exception as exc:  # noqa: BLE001
            self._emit("cache_scoping_decision", turn_id, {
                "claim": claim_summary,
                "error": f"{type(exc).__name__}: {exc}",
            })
            return None

        if scope.scope != "world_fact":
            # Not eligible — record state but skip stability.
            state = ClaimCacheState(
                canonical_key=canonicalize_claim_key(claim), scope=scope,
            )
            self._states[state.canonical_key] = state
            return state

        key = canonicalize_claim_key(claim)
        state = ClaimCacheState(canonical_key=key, scope=scope)

        # Stability (only for world_fact + when classifier is wired).
        if self._stability_fn is not None:
            try:
                stab = self._stability_fn(claim)
                self._emit("cache_stability_decision", turn_id, {
                    "claim": claim_summary, "decision": stab.to_dict(),
                })
                state.stability = stab
            except Exception as exc:  # noqa: BLE001
                self._emit("cache_stability_decision", turn_id, {
                    "claim": claim_summary,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                # state.stability stays None; writable will be False.

        self._states[key] = state
        return state

    # ---- maybe_hit (was Router._maybe_cache_hit) -----------------------

    def maybe_hit(
        self, claim: dict, identity_slot_names: list[str], *, turn_id: int,
    ) -> Optional[CacheHit]:
        """Try exact lookup, then semantic lookup. Emits the
        cache_lookup event in either branch (hit / semantic_hit /
        miss / error) so the trace UI sees a uniform record.

        Returns None if the gate is disabled, the claim isn't
        eligible, the cache is unwired, or both lookups miss.
        """
        if self._cache is None or not self.enabled:
            return None
        key = canonicalize_claim_key(claim)
        if key not in self.eligible_keys:
            return None
        # Exact lookup.
        try:
            cached = self._cache.lookup(key)
        except Exception as exc:  # noqa: BLE001
            self._emit("cache_lookup", turn_id, {
                "canonical_key": key,
                "error": f"{type(exc).__name__}: {exc}",
            })
            return None
        if cached is not None:
            self._emit("cache_lookup", turn_id, {
                "canonical_key": key, "result": "hit",
                "verdict": cached.verdict,
                "stability_class": cached.stability_class,
                "hit_count": cached.hit_count,
                "cached_at": cached.cached_at,
                "expires_at": cached.expires_at,
            })
            return CacheHit(verdict=cached, is_semantic=False)

        # Exact miss → semantic lookup.
        semantic: Optional[SemanticHit] = None
        try:
            semantic = self._cache.semantic_lookup(claim, identity_slot_names)
        except Exception as exc:  # noqa: BLE001
            self._emit("cache_lookup", turn_id, {
                "canonical_key": key,
                "error": (
                    f"semantic_lookup raised: {type(exc).__name__}: {exc}"
                ),
            })
            return None
        if semantic is None:
            self._emit("cache_lookup", turn_id, {
                "canonical_key": key, "result": "miss",
            })
            return None

        self._emit("cache_lookup", turn_id, {
            "canonical_key": key, "result": "semantic_hit",
            "matched_key": semantic.matched_key,
            "score": round(semantic.score, 3),
            "verdict": semantic.verdict.verdict,
            "stability_class": semantic.verdict.stability_class,
            "hit_count": semantic.verdict.hit_count,
            "cached_at": semantic.verdict.cached_at,
            "expires_at": semantic.verdict.expires_at,
        })
        return CacheHit(
            verdict=semantic.verdict, is_semantic=True,
            matched_key=semantic.matched_key, score=semantic.score,
        )

    # ---- maybe_write (was Pipeline._maybe_write_cache) -----------------

    def maybe_write(
        self, decision: Any, claim: dict, *, turn_id: int,
    ) -> None:
        """Write the verification decision to the cache when:
          * the gate is enabled
          * the claim was classified writable (world_fact + non-volatile)
          * the verdict came from the retrieval path (not python / store)
          * the verdict is in the cacheable set

        Decision is the Router's Decision dataclass; we duck-type on
        ``verification_status``, ``code_gen_result`` (None for
        retrieval), and ``retrieval_result``.
        """
        if self._cache is None:
            return
        key = canonicalize_claim_key(claim)
        state = self._states.get(key)
        if state is None or not state.writable:
            return
        verdict = getattr(decision, "verification_status", None)
        if verdict not in _RETRIEVAL_VERDICTS_TO_CACHE:
            return
        # Skip python verifications (free to redo, would shadow stale).
        if getattr(decision, "code_gen_result", None) is not None:
            return
        # v0.7.10: a Decision that was served from the cache should not
        # then write itself back. The hit_count was already bumped on
        # lookup; rewriting would reset cached_at and pollute refresh
        # bookkeeping (every hit would look like a fresh confirmation).
        if getattr(decision, "served_from_cache", False):
            return
        evidence = None
        retrieval_result = getattr(decision, "retrieval_result", None)
        if retrieval_result is not None:
            # Tolerate both RetrievalResult objects (fresh path) and
            # dicts (cache-as-evidence path, defensive — shouldn't
            # actually reach here because we skip cache hits above).
            evidence = (
                retrieval_result.to_dict()
                if hasattr(retrieval_result, "to_dict")
                else retrieval_result
            )
        try:
            outcome = self._cache.write(
                canonical_key=key,
                pattern=claim.get("pattern", ""),
                predicate=claim.get("predicate", ""),
                verdict=verdict,
                stability_class=state.stability.stability_class,
                ttl_seconds=state.stability.ttl_seconds,
                evidence=evidence,
            )
            self._emit("cache_write", turn_id, {
                "canonical_key": key,
                "verdict": verdict,
                "stability_class": state.stability.stability_class,
                "ttl_seconds": state.stability.ttl_seconds,
                "action": outcome.action,
            })
            # Surface verdict reversals as their own pipeline event so
            # the operator can see when a previously-cached verdict got
            # overwritten with a different one. This is the load-bearing
            # contradiction signal — same key, opposite verdict, often
            # caused by source drift or a flaky earlier verification.
            if outcome.action == "contradicted_and_replaced":
                self._emit("cache_contradiction_replaced", turn_id, {
                    "canonical_key": key,
                    "claim": claim,
                    "prior_verdict": outcome.prior_verdict,
                    "new_verdict": verdict,
                    "stability_class": state.stability.stability_class,
                })
                # v0.7.11 causal cascade: 1-hop semantic neighbors get
                # flagged_for_review. Lookup will treat them as miss
                # until the next verification confirms (or contradicts
                # again, which would re-flag THEIR neighbors). The
                # cascade is bounded — neighbors-of-neighbors are NOT
                # touched here, only on their own contradiction.
                from src.router.constants import KEY_SLOTS_BY_PATTERN
                identity_slots = KEY_SLOTS_BY_PATTERN.get(
                    claim.get("pattern", ""), []
                )
                try:
                    flagged = self._cache.flag_neighbors_for_review(
                        primary_canonical_key=key,
                        claim=claim,
                        identity_slot_names=identity_slots,
                    )
                    if flagged:
                        self._emit("cache_drift_cascade", turn_id, {
                            "primary_key": key,
                            "flagged_keys": flagged,
                            "flagged_count": len(flagged),
                        })
                except Exception as exc:  # noqa: BLE001
                    self._emit("cache_drift_cascade", turn_id, {
                        "primary_key": key,
                        "error": f"{type(exc).__name__}: {exc}",
                    })
        except Exception as exc:  # noqa: BLE001
            self._emit("cache_write", turn_id, {
                "canonical_key": key,
                "error": f"{type(exc).__name__}: {exc}",
            })

    # ---- internal ------------------------------------------------------

    def _emit(self, stage: str, turn_id: int, data: dict) -> None:
        if self._store is None:
            return
        try:
            self._store.insert_pipeline_event(turn_id, stage, data)
        except Exception:
            pass  # observability never breaks a turn
