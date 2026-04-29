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

# v0.7.12: don't cache low-conviction verdicts. Caching an entry the
# verifier wasn't sure about pollutes future lookups with weak signal —
# better to re-run retrieval next time. The router assigns confidence
# per outcome (CONF_RETRIEVAL_INCONCLUSIVE = 0.4 by default), so this
# floor naturally filters out inconclusive retrievals while keeping
# high-conviction verified / contradicted verdicts.
_MIN_CONFIDENCE_TO_CACHE = 0.5

# v0.7.12: rough per-hit cost estimate. Each cache hit avoids one
# retrieval-judge LLM call (the most expensive part of the retrieval
# path). Real cost varies by model + snippet length; this is an
# order-of-magnitude estimate so the operator can see "the cache is
# saving ~$X per turn". Override via AEDOS_CACHE_AVG_HIT_SAVINGS_USD.
import os as _os
try:
    _CACHE_HIT_AVG_SAVINGS_USD = float(
        _os.getenv("AEDOS_CACHE_AVG_HIT_SAVINGS_USD", "0.001")
    )
except (TypeError, ValueError):
    _CACHE_HIT_AVG_SAVINGS_USD = 0.001


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
        combined_fn: Optional[Callable[[dict], Any]] = None,
    ):
        self._cache = cache
        self._scoping_fn = scoping_fn
        self._stability_fn = stability_fn
        # v0.7.16: when set, ONE LLM call returns both scoping AND
        # stability (CombinedDecision from src.cache.classify_combined).
        # Halves cache-classifier LLM cost. classify() prefers this when
        # available and falls back to the legacy two-call path otherwise.
        self._combined_fn = combined_fn
        self._store = store
        # Per-turn state. Reset by reset_for_turn().
        self._states: dict[str, ClaimCacheState] = {}
        # v0.7.12: per-turn cache-savings tally. Bumped on every hit;
        # Pipeline reads at end-of-turn for the cache_savings event.
        self._turn_hits: int = 0
        self._turn_savings_usd: float = 0.0
        # v0.7.14: canonical_keys we've already looked up this turn
        # AND that missed. Prevents the Pipeline-level tier-3 short-
        # circuit and the router-level _maybe_cache_hit from
        # double-emitting cache_lookup events for the same claim.
        # Hits aren't tracked because they short-circuit and the
        # router never reaches _maybe_cache_hit on hit.
        self._missed_keys_this_turn: set[str] = set()

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
        """Clear per-claim decisions + per-turn savings tally +
        already-looked-up tracking. Pipeline calls at the start of
        each turn so prior classifications don't leak."""
        self._states.clear()
        self._turn_hits = 0
        self._turn_savings_usd = 0.0
        self._missed_keys_this_turn.clear()

    def turn_savings(self) -> dict:
        """Returned at end-of-turn for the cache_savings event.
        Cheap aggregate the Pipeline emits alongside turn_cost so the
        operator can see "the cache saved $X this turn"."""
        return {
            "hits": self._turn_hits,
            "estimated_usd_saved": round(self._turn_savings_usd, 6),
            "per_hit_estimate_usd": _CACHE_HIT_AVG_SAVINGS_USD,
        }

    # ---- classify (was Pipeline stage 5b) ------------------------------

    def classify(self, claim: dict, *, turn_id: int) -> Optional[ClaimCacheState]:
        """Run cache classification for one claim. Logs both decision
        events on the given turn_id. Returns the resulting
        ClaimCacheState, or None when the gate is disabled.

        v0.7.16 dispatch:
          * If a `combined_fn` is wired: ONE LLM call returns both
            scoping AND stability. Halves cache-classifier cost.
          * Else: legacy two-call path (scoping_fn → stability_fn).

        Failures in either path are logged but never raised — this is
        observability + cache-eligibility, not the verdict path.
        """
        if not self.enabled and self._combined_fn is None:
            return None
        claim_summary = {
            "pattern": claim.get("pattern"),
            "predicate": claim.get("predicate"),
            "slots": claim.get("slots"),
            "polarity": claim.get("polarity"),
        }

        # ---- v0.7.16: combined classifier path -----------------------
        if self._combined_fn is not None:
            try:
                combined = self._combined_fn(claim)
                scope = combined.scoping
                stab = combined.stability
            except Exception as exc:  # noqa: BLE001
                # Log under the scoping event since that's the first
                # decision the combined path produces.
                self._emit("cache_scoping_decision", turn_id, {
                    "claim": claim_summary,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                return None
            # Surface BOTH events on the trace so the operator's mental
            # model (scope first, then stability) is preserved even
            # though only one LLM call fired. Stability only emits when
            # scope was world_fact (mirrors the legacy path).
            self._emit("cache_scoping_decision", turn_id, {
                "claim": claim_summary, "decision": scope.to_dict(),
                "via": "combined_classifier",
            })
            if stab is not None:
                self._emit("cache_stability_decision", turn_id, {
                    "claim": claim_summary, "decision": stab.to_dict(),
                    "via": "combined_classifier",
                })
            key = canonicalize_claim_key(claim)
            state = ClaimCacheState(
                canonical_key=key, scope=scope, stability=stab,
            )
            self._states[key] = state
            return state

        # ---- legacy two-call path ------------------------------------
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
            state = ClaimCacheState(
                canonical_key=canonicalize_claim_key(claim), scope=scope,
            )
            self._states[state.canonical_key] = state
            return state

        key = canonicalize_claim_key(claim)
        state = ClaimCacheState(canonical_key=key, scope=scope)

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

        self._states[key] = state
        return state

    # ---- maybe_hit (was Router._maybe_cache_hit) -----------------------

    def maybe_hit(
        self, claim: dict, identity_slot_names: list[str], *, turn_id: int,
        require_eligible: bool = True,
    ) -> Optional[CacheHit]:
        """Try exact lookup, then semantic lookup. Emits the
        cache_lookup event in either branch (hit / semantic_hit /
        miss / error) so the trace UI sees a uniform record.

        Returns None if the gate is disabled, the claim isn't
        eligible (when require_eligible=True), the cache is unwired,
        or both lookups miss.

        v0.7.14: pass ``require_eligible=False`` to look up regardless
        of whether the claim was classified this turn. Used by the
        Pipeline-level tiered short-circuit, which consults the cache
        BEFORE the classify step runs — if there's a hit, classify is
        skipped entirely (saves 2 LLM calls per claim). The eligibility
        check originally existed because writes are gated; lookups
        are free and any miss is harmless.
        """
        if self._cache is None or not self.enabled:
            return None
        key = canonicalize_claim_key(claim)
        if require_eligible and key not in self.eligible_keys:
            return None
        # v0.7.14 dedup: if a previous lookup this turn missed on this
        # key, the second call (from defense-in-depth in
        # _route_retrieval after a tier-3 miss) would just emit a
        # duplicate cache_lookup event for nothing. Short-circuit.
        if key in self._missed_keys_this_turn:
            return None
        # Exact lookup.
        try:
            cached = self._cache.lookup(key)
        except Exception as exc:  # noqa: BLE001
            self._emit("cache_lookup", turn_id, {
                "canonical_key": key,
                "error": f"{type(exc).__name__}: {exc}",
            })
            self._missed_keys_this_turn.add(key)
            return None
        if cached is not None:
            self._record_hit_savings()
            self._emit("cache_lookup", turn_id, {
                "canonical_key": key, "result": "hit",
                "verdict": cached.verdict,
                "stability_class": cached.stability_class,
                "hit_count": cached.hit_count,
                "cached_at": cached.cached_at,
                "expires_at": cached.expires_at,
                "estimated_usd_saved": _CACHE_HIT_AVG_SAVINGS_USD,
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
            self._missed_keys_this_turn.add(key)
            return None
        if semantic is None:
            self._emit("cache_lookup", turn_id, {
                "canonical_key": key, "result": "miss",
            })
            self._missed_keys_this_turn.add(key)
            return None

        self._record_hit_savings()
        self._emit("cache_lookup", turn_id, {
            "canonical_key": key, "result": "semantic_hit",
            "matched_key": semantic.matched_key,
            "score": round(semantic.score, 3),
            "verdict": semantic.verdict.verdict,
            "stability_class": semantic.verdict.stability_class,
            "hit_count": semantic.verdict.hit_count,
            "cached_at": semantic.verdict.cached_at,
            "expires_at": semantic.verdict.expires_at,
            "estimated_usd_saved": _CACHE_HIT_AVG_SAVINGS_USD,
        })
        return CacheHit(
            verdict=semantic.verdict, is_semantic=True,
            matched_key=semantic.matched_key, score=semantic.score,
        )

    def _record_hit_savings(self) -> None:
        self._turn_hits += 1
        self._turn_savings_usd += _CACHE_HIT_AVG_SAVINGS_USD

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
        # v0.7.12: confidence floor. Don't write a verdict the verifier
        # wasn't sure about — caching low-conviction answers pollutes
        # future lookups; better to re-run on the next ask. The
        # router's CONF_RETRIEVAL_INCONCLUSIVE = 0.4 < 0.5 floor, so
        # this naturally filters retrieval_inconclusive verdicts out.
        confidence = getattr(decision, "confidence", None)
        if confidence is not None and confidence < _MIN_CONFIDENCE_TO_CACHE:
            self._emit("cache_write", turn_id, {
                "canonical_key": key,
                "verdict": verdict,
                "skipped": "below_confidence_floor",
                "confidence": confidence,
                "floor": _MIN_CONFIDENCE_TO_CACHE,
            })
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
                confidence=confidence,
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
