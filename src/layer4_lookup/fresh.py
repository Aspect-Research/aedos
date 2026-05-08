"""Fresh dispatcher — verifier dispatch entry point (v0.14 Phase 7e).

When the walker's earlier tiers (U, W, derivation) all miss or fall
through, the walker invokes this dispatcher. The dispatcher routes
the claim to the appropriate verifier based on Layer 2's
``routing_method``, runs it, maps the verifier's outcome to the
8-state ``verification_status`` enum, writes the result to Tier W
for future hits, and returns a ``WalkerDecision`` for Layer 5.

v1 → v2 cross-cut warning
=========================

This is the ONE place the v2 stack imports from ``src/`` (v1's
namespace). The verifier modules — RetrievalVerifier and
verify_via_code_generation — are stable enough that import-from-v1
is low-risk for Phase 7. Porting them to ``src/verifiers/``
is a future phase's scope. The integration is duck-typed: v1
verifiers expect ``store`` with the v1 FactStore method set
(insert_pipeline_event, cache_retrieval, get_cached_retrieval —
all present on v2 FactStore) and ``registry`` with the v1
PatternRegistry shape (.get(name) returning a Pattern with
.query_strategy — present on v2 PatternRegistry).

Method dispatch and 8-state status mapping
==========================================

  * ``python``                            → verify_via_code_generation
    Status mapping:
      verified                            → verified
      contradicted                        → contradicted
      code_execution_failed               → unverifiable_pending_implementation
      comparison_error                    → unverifiable_pending_implementation
    Stability class: immutable (TTL=None) — math/structural facts don't go stale.

  * ``python_with_canonical_constants``   → CodeGenerationVerifier.verify_with_cross_check
    Status mapping (additionally):
      canonical_constants_disagreement    → unverifiable_pending_implementation
    Stability class: immutable (same as python).

  * ``retrieval``                         → RetrievalVerifier.verify
    Status mapping:
      VERIFIED                            → verified
      CONTRADICTED                        → contradicted
      INCONCLUSIVE without error_flag     → retrieval_inconclusive
      INCONCLUSIVE with error_flag in     → retrieval_failed
        {retrieval_error, no_results,
         judge_parse_error, judge_error,
         no_query_constructible}
    Stability class (Phase 8): wired to v1's
    ``classify_combined.classify_for_cache``. The classifier returns
    scope + stability; for cacheable retrieval verdicts (verified,
    contradicted, retrieval_inconclusive at world_fact scope), the
    classifier's ``stability_class`` and ``ttl_seconds`` are used.
    Non-world-fact scope or volatile (ttl_seconds == 0) skips the
    Tier W write — we don't cache claims whose answer depends on
    user/session, and we don't cache things that change faster than
    the cache hit horizon. Classifier failures (LLM error, malformed
    output) also skip the write conservatively.

  * ``user_authoritative``                → out-of-domain at fresh.
    User-authoritative claims should resolve at Tier U; reaching
    fresh means routing missed something. Returns
    verification_status='routing_anomaly' with an explanatory note.
    No Tier W write.

  * ``unverifiable``                      → no verifier; terminal at
    verification_status='unverifiable_in_principle'.
    No Tier W write (the routing said it's unverifiable; caching
    that doesn't add value).

  * unknown / None method                 → unverifiable_pending_implementation.
    No Tier W write.

"""

from __future__ import annotations

from typing import Any, Optional

from src.fact_store import DEFAULT_USER_ID, FactStore
from src.layer1_extraction.pattern_registry import PatternRegistry
from src.layer3_substrate.classifier_base import _safe_emit_event
from src.layer4_lookup import tier_w as _tier_w
from src.layer4_lookup.types import (
    LookupOutcome,
    WalkerDecision,
)
from src.cache.classify_combined import classify_for_cache
from src.llm_client import LLMClient


# ============================================================================
# Stability classification (v0.14 Phase 8f)
# ============================================================================

# Python verdicts: math/structural facts. Immutable, no TTL. The
# stability classifier is not consulted on the python path — math
# doesn't have stability ambiguity.
_PYTHON_STABILITY_CLASS = "immutable"
_PYTHON_TTL_SECONDS: Optional[int] = None

# Retrieval verdicts: classify per-claim via classify_for_cache.
# The classifier returns scope + stability. We cache only world_fact
# scope; user_specific / session_specific are not cacheable. The
# classifier's stability_class and ttl_seconds are used directly.

# Retrieval errors that map to retrieval_failed (verifier broke; no
# evidence to hedge on). Other inconclusive paths map to
# retrieval_inconclusive (judge ran cleanly, evidence thin).
_RETRIEVAL_ERROR_FLAGS = {
    "retrieval_error",
    "no_results",
    "judge_parse_error",
    "judge_error",
    "no_query_constructible",
}


def _classify_stability_for_caching(
    claim: dict,
    *,
    llm: LLMClient,
    store: FactStore,
    source_turn_id: Optional[int],
) -> Optional[tuple[str, Optional[int]]]:
    """Classify the claim's scope + stability for cache-write decisions.

    Returns ``(stability_class, ttl_seconds)`` when the verdict is
    cacheable, else None. None means: don't write Tier W. Three
    not-cacheable cases:

      * ``scope != world_fact`` (user_specific / session_specific):
        the answer depends on which user / session; caching would
        return the wrong verdict on a different user / session.
      * ``ttl_seconds == 0`` (volatile): the underlying fact changes
        faster than any reasonable cache horizon.
      * Classifier failure (LLM exception, malformed output, missing
        stability decision): conservative — don't cache something we
        can't classify.

    Each non-cacheable case emits a ``cache_scoping_decision`` event
    so the trace UI sees why caching was skipped.
    """
    try:
        decision = classify_for_cache(claim, llm)
    except Exception as exc:
        _safe_emit_event(
            store, source_turn_id, "cache_scoping_decision",
            {
                "decision": "skip_cache",
                "reason": (
                    f"stability classifier failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            },
        )
        return None

    scope = decision.scoping.scope
    if scope != "world_fact":
        _safe_emit_event(
            store, source_turn_id, "cache_scoping_decision",
            {
                "decision": "skip_cache",
                "scope": scope,
                "scope_reason": decision.scoping.reason,
            },
        )
        return None

    if decision.stability is None:
        # Defensive: classifier returned world_fact without stability.
        _safe_emit_event(
            store, source_turn_id, "cache_scoping_decision",
            {
                "decision": "skip_cache",
                "reason": (
                    "world_fact scope but stability decision missing"
                ),
            },
        )
        return None

    stability_class = decision.stability.stability_class
    ttl_seconds = decision.stability.ttl_seconds

    _safe_emit_event(
        store, source_turn_id, "cache_stability_decision",
        {
            "stability_class": stability_class,
            "stability_reason": decision.stability.reason,
            "ttl_seconds": ttl_seconds,
            "scope": scope,
        },
    )

    if ttl_seconds == 0:
        # Volatile: classifier said don't cache.
        _safe_emit_event(
            store, source_turn_id, "cache_scoping_decision",
            {
                "decision": "skip_cache",
                "reason": "stability_class=volatile (ttl_seconds=0)",
                "stability_class": stability_class,
            },
        )
        return None

    return (stability_class, ttl_seconds)


# ============================================================================
# Public dispatcher
# ============================================================================


def dispatch(
    claim: dict,
    *,
    routing_method: Optional[str],
    store: FactStore,
    registry: PatternRegistry,
    llm: Optional[LLMClient],
    source_turn_id: Optional[int],
    user_id: str = DEFAULT_USER_ID,
    current_session: Optional[str] = None,
    prior_notes: list[str] = (),
) -> WalkerDecision:
    """Route the claim to the verifier its routing_method names.

    Signature matches the walker's ``FreshDispatch`` callable type.
    The walker passes ``prior_notes`` (any fall-through context from
    Tier W's inconclusive/failed cases); fresh prepends those onto
    its own notes so the trace UI sees the chain of reasoning.

    Returns a WalkerDecision with served_from_tier="fresh".
    """
    notes = list(prior_notes)
    method = routing_method

    _safe_emit_event(
        store, source_turn_id, "fresh_dispatch",
        {
            "routing_method": method,
            "claim_pattern": claim.get("pattern"),
            "claim_predicate": claim.get("predicate"),
        },
    )

    if method == "python":
        return _dispatch_python(
            claim, store=store, registry=registry, llm=llm,
            source_turn_id=source_turn_id, notes=notes,
            cross_check=False,
        )
    if method == "python_with_canonical_constants":
        return _dispatch_python(
            claim, store=store, registry=registry, llm=llm,
            source_turn_id=source_turn_id, notes=notes,
            cross_check=True,
        )
    if method == "retrieval":
        return _dispatch_retrieval(
            claim, store=store, registry=registry, llm=llm,
            source_turn_id=source_turn_id, notes=notes,
        )
    if method == "user_authoritative":
        # Out-of-domain at fresh. Tier U should have resolved.
        # Treat as routing_anomaly — Layer 5 will surface to operator.
        return WalkerDecision(
            claim=claim,
            served_from_tier="fresh",
            outcome=LookupOutcome.MISS,
            verification_status="routing_anomaly",
            routing_method=method,
            chain_reliability=1.0,
            notes=notes + [
                "user_authoritative reached fresh dispatcher; "
                "Tier U should have resolved this; flagging anomaly"
            ],
        )
    if method == "unverifiable":
        # Routing said no verifier applies. Terminal.
        return WalkerDecision(
            claim=claim,
            served_from_tier="fresh",
            outcome=LookupOutcome.MISS,
            verification_status="unverifiable_in_principle",
            routing_method=method,
            chain_reliability=1.0,
            notes=notes + [
                "routing_method=unverifiable; no verifier applies"
            ],
        )

    # Unknown method (None or something exotic).
    return WalkerDecision(
        claim=claim,
        served_from_tier="fresh",
        outcome=LookupOutcome.MISS,
        verification_status="unverifiable_pending_implementation",
        routing_method=method,
        chain_reliability=1.0,
        notes=notes + [
            f"unknown routing_method {method!r}; no verifier dispatched"
        ],
    )


# ============================================================================
# Python verifier dispatch
# ============================================================================


def _dispatch_python(
    claim: dict,
    *,
    store: FactStore,
    registry: PatternRegistry,
    llm: Optional[LLMClient],
    source_turn_id: Optional[int],
    notes: list[str],
    cross_check: bool,
) -> WalkerDecision:
    """Run the v1 code-generation verifier (single-shot or cross-check)
    and map the result onto the 8-state status enum."""
    if llm is None:
        return WalkerDecision(
            claim=claim,
            served_from_tier="fresh",
            outcome=LookupOutcome.MISS,
            verification_status="unverifiable_pending_implementation",
            routing_method=(
                "python_with_canonical_constants" if cross_check else "python"
            ),
            chain_reliability=1.0,
            notes=notes + [
                "no LLM provided; cannot run code-generation verifier"
            ],
        )

    if cross_check:
        from src.verifiers.code_generation.pipeline import (
            CodeGenerationVerifier,
        )
        verifier = CodeGenerationVerifier(store, llm)
        result = verifier.verify_with_cross_check(
            claim, source_turn_id=source_turn_id,
        )
    else:
        from src.verifiers.code_generation.pipeline import (
            verify_via_code_generation,
        )
        result = verify_via_code_generation(
            claim, llm, store=store, source_turn_id=source_turn_id,
        )

    status = _map_python_status(result.status)
    outcome = _outcome_from_status(status)

    method_name = (
        "python_with_canonical_constants" if cross_check else "python"
    )

    # Cache verifier output in Tier W when the verdict is
    # verified/contradicted (real verdicts worth caching). Skip
    # caching of pending-implementation statuses — they describe
    # verifier-side problems, not facts about the world.
    if status in ("verified", "contradicted"):
        _tier_w.write_verifier_result(
            claim, store,
            verification_status=status,
            registry=registry,
            evidence={"trace": result.trace, "actual_value": result.actual_value},
            stability_class=_PYTHON_STABILITY_CLASS,
            ttl_seconds=_PYTHON_TTL_SECONDS,
            source_turn_id=source_turn_id,
        )

    return WalkerDecision(
        claim=claim,
        served_from_tier="fresh",
        outcome=outcome,
        verification_status=status,
        routing_method=method_name,
        evidence={"trace": result.trace, "actual_value": result.actual_value},
        chain_reliability=1.0,
        notes=notes + [
            f"python verifier returned {result.status!r} -> "
            f"verification_status={status!r}"
        ],
    )


def _map_python_status(v1_status: str) -> str:
    """v1 CodeGenVerificationResult.status → 8-state verification_status."""
    if v1_status == "verified":
        return "verified"
    if v1_status == "contradicted":
        return "contradicted"
    # code_execution_failed, comparison_error, canonical_constants_disagreement
    return "unverifiable_pending_implementation"


# ============================================================================
# Retrieval verifier dispatch
# ============================================================================


def _dispatch_retrieval(
    claim: dict,
    *,
    store: FactStore,
    registry: PatternRegistry,
    llm: Optional[LLMClient],
    source_turn_id: Optional[int],
    notes: list[str],
) -> WalkerDecision:
    """Run the v1 retrieval verifier and map its outcome."""
    if llm is None:
        return WalkerDecision(
            claim=claim,
            served_from_tier="fresh",
            outcome=LookupOutcome.MISS,
            verification_status="unverifiable_pending_implementation",
            routing_method="retrieval",
            chain_reliability=1.0,
            notes=notes + [
                "no LLM provided; cannot run retrieval verifier"
            ],
        )

    from src.verifiers.retrieval_verifier import RetrievalVerifier
    from src.verifiers.types import VerificationOutcome

    # The v1 RetrievalVerifier was written against v1's PatternRegistry.
    # v2's PatternRegistry is duck-compatible: same .get(name) → Pattern
    # with .query_strategy field. Same FactStore method set. So we
    # pass v2's instances and rely on duck typing.
    verifier = RetrievalVerifier(store, llm, registry)
    result = verifier.verify(claim, source_turn_id=source_turn_id)
    status = _map_retrieval_status(result.outcome, result.error_flag)
    outcome = _outcome_from_status(status)

    # Cache the retrieval verdict in Tier W. The architectural rule
    # for retrieval (Ambiguity #6): cache real verdicts AND
    # inconclusive results, but not failed (the verifier broke; no
    # signal to cache). Phase 8f: classifier-driven stability — only
    # cache when scope is world_fact and stability is non-volatile.
    if status in ("verified", "contradicted", "retrieval_inconclusive"):
        evidence = result.to_dict()
        cache_decision = _classify_stability_for_caching(
            claim, llm=llm, store=store, source_turn_id=source_turn_id,
        )
        if cache_decision is not None:
            stability_class, ttl_seconds = cache_decision
            _tier_w.write_verifier_result(
                claim, store,
                verification_status=status,
                registry=registry,
                evidence=evidence,
                stability_class=stability_class,
                ttl_seconds=ttl_seconds,
                source_turn_id=source_turn_id,
            )

    return WalkerDecision(
        claim=claim,
        served_from_tier="fresh",
        outcome=outcome,
        verification_status=status,
        routing_method="retrieval",
        evidence=result.to_dict(),
        chain_reliability=1.0,
        notes=notes + [
            f"retrieval verifier outcome={result.outcome.value!r} "
            f"error_flag={result.error_flag!r} -> "
            f"verification_status={status!r}"
        ],
    )


def _map_retrieval_status(
    v1_outcome: Any, error_flag: Optional[str],
) -> str:
    """v1 RetrievalResult outcome+error_flag → 8-state verification_status.

    The split between retrieval_inconclusive and retrieval_failed is
    load-bearing under the v0.14 architecture (the latter is a no-op
    at Layer 5; the former is a hedge).

    INCONCLUSIVE without an error flag means the judge ran cleanly
    and said "not enough evidence to decide" — that's
    retrieval_inconclusive (informative, hedge-worthy). INCONCLUSIVE
    with an error flag means the verifier broke — that's
    retrieval_failed (no evidence; noop at Layer 5).
    """
    # v1's VerificationOutcome enum has .value strings.
    outcome_value = getattr(v1_outcome, "value", str(v1_outcome))
    if outcome_value == "verified":
        return "verified"
    if outcome_value == "contradicted":
        return "contradicted"
    # INCONCLUSIVE — split by error_flag.
    if error_flag in _RETRIEVAL_ERROR_FLAGS:
        return "retrieval_failed"
    return "retrieval_inconclusive"


# ============================================================================
# Helper: outcome from status
# ============================================================================


def _outcome_from_status(status: str) -> LookupOutcome:
    """Map an 8-state verification_status to the LookupOutcome the
    walker reports.

    A verifier verdict of ``verified`` is conceptually a MATCH (the
    claim is supported); ``contradicted`` is a CONTRADICTION; all
    others are MISS (the verifier didn't decisively support or
    contradict the claim, so no positive lookup result).
    """
    if status == "verified":
        return LookupOutcome.MATCH
    if status == "contradicted":
        return LookupOutcome.CONTRADICTION
    return LookupOutcome.MISS
