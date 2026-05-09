"""Walker — Layer 4 tier orchestrator (v0.14 Phase 7d).

Composes Tier U, Tier W, derivation, and fresh in the fall-through
order locked in by the Phase 7 plan. Each step returns a verdict;
on miss / below-threshold the walker advances to the next tier.

Tier order
==========

  1. **Tier U** (user microtheory): facts the user has asserted, with
     the substrate's three-stage oracle resolution chain (literal
     → predicate_equivalence → entity_equivalence). Filtered by
     session-locality.
  2. **Tier W** (world cache): verifier-output cache, same three-stage
     resolution. Carries the 8-state ``verification_status``.
  3. **Derivation walk**: bounded BFS over the substrate, looking for
     a multi-step chain that supports the claim.
  4. **Fresh**: classify-and-route to the actual verifier (Python /
     retrieval / etc.). Phase 7e wires this; Phase 7d accepts a
     pluggable ``fresh_dispatch`` callable so the walker is testable
     without the verifier stack.

Refined fall-through table (Ambiguity #4 of the Phase 7 plan)
=============================================================

A Tier W MATCH terminates the walk if the cached row's
``verification_status`` is verified, contradicted, or
unverifiable_in_principle. Those are real verdicts; derivation
won't improve on them.

A Tier W MATCH falls through to derivation if the cached row's
status is retrieval_inconclusive, retrieval_failed, or
unverifiable_pending_implementation. Those say "we don't really
know"; derivation walks the substrate (a different evidence
source) and may yield a verdict the cached retrieval verdict
didn't see.

When fall-through happens because of an inconclusive/failed Tier W
row, the WalkerDecision's ``notes`` field records that the cached
row existed and what its status was — Layer 5 (Phase 8) and the
trace UI surface this so the operator sees the chain of reasoning.

A Tier W CONTRADICTION (opposite-polarity row exists) is terminal
regardless of the inner verdict. Layer 5 reads the cached
verification_status to decide intervention.

routing_method contract (always populated by Layer 2)
====================================================

Layer 2's Decision carries a ``method`` field (one of the 5 routing
methods, or None on routing_anomaly). The walker carries this
through to the WalkerDecision UNCHANGED, regardless of which tier
resolved. Layer 5 reads ``routing_method`` to know what verifier
*would* have run if fresh dispatched, even when an earlier tier
resolved.

The only case where ``routing_method`` is None is a routing_anomaly
short-circuit (Layer 2's validator rejected the claim). Then the
walker's ``served_from_tier`` is also "routing_anomaly".

What the walker emits to pipeline_events
=========================================

  * ``walker_decision`` — the final verdict, with served_from_tier
    + verification_status + outcome + via + chain_reliability.
  * Tier-specific events fire from inside the tier modules
    (tier_u_storage, tier_w_lookup / _hit / _write, derivation_walk_*,
    fresh_dispatch). The walker doesn't re-log them.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from src.fact_store import DEFAULT_USER_ID, FactStore
from src.layer1_extraction.pattern_registry import PatternRegistry
from src.layer2_routing.constants import KEY_SLOTS_BY_PATTERN
from src.layer2_routing.types import Decision, RoutingOutcome
from src.layer3_substrate.classifier_base import _safe_emit_event
from src.layer3_substrate.entity_equivalence import EntityEquivalence
from src.layer3_substrate.entity_taxonomy import EntityTaxonomy
from src.layer3_substrate.predicate_distribution import (
    PredicateDistribution,
)
from src.layer3_substrate.predicate_equivalence import (
    PredicateEquivalence,
)
from src.layer4_lookup import derivation as _derivation
from src.layer4_lookup import tier_u as _tier_u
from src.layer4_lookup import tier_w as _tier_w
from src.layer4_lookup.types import (
    LookupOutcome,
    WalkerDecision,
)
from src.llm_client import LLMClient


# ============================================================================
# Tier W fall-through policy (refined per Ambiguity #4)
# ============================================================================

_TERMINAL_W_STATUSES = {
    "verified",
    "contradicted",
    "unverifiable_in_principle",
}
"""Cached statuses that terminate the walk on Tier W MATCH.

These are real verdicts; derivation won't improve on them.
"""

_FALLTHROUGH_W_STATUSES = {
    "retrieval_inconclusive",
    "retrieval_failed",
    "unverifiable_pending_implementation",
}
"""Cached statuses that fall through to derivation despite a Tier W
MATCH. The cache says 'we tried, evidence is thin'; derivation walks
substrate (different evidence source) and may yield a verdict.
"""


# ============================================================================
# Public type aliases
# ============================================================================

# Phase 7e replaces this with a real dispatcher; Phase 7d defaults
# to a stub that emits unverifiable_pending_implementation.
FreshDispatch = Callable[..., WalkerDecision]


# ============================================================================
# Walker
# ============================================================================


def walk_claim(
    claim: dict,
    layer2_decision: Decision,
    store: FactStore,
    *,
    registry: PatternRegistry,
    predicate_oracle: PredicateEquivalence,
    entity_oracle: EntityEquivalence,
    taxonomy_oracle: EntityTaxonomy,
    distribution_oracle: PredicateDistribution,
    llm: Optional[LLMClient] = None,
    source_turn_id: Optional[int] = None,
    user_id: str = DEFAULT_USER_ID,
    current_session: Optional[str] = None,
    fresh_dispatch: Optional[FreshDispatch] = None,
    active_context_tokens: Optional[frozenset] = None,
) -> WalkerDecision:
    """Resolve a claim through the tier stack.

    Returns a ``WalkerDecision`` (Phase 7b's types module). The
    decision carries the resolved verification_status, the lookup
    outcome (MATCH / CONTRADICTION / MISS), the substrate consultation
    trail, and the derivation chain (when derivation resolved).

    ``layer2_decision`` is Layer 2's classification output. It carries
    the routing method which the walker propagates through to the
    WalkerDecision unchanged. On routing_anomaly, the walker
    short-circuits: no tier lookup runs.
    """
    # ---- 0. Routing anomaly short-circuit -----------------------------
    if layer2_decision.outcome is RoutingOutcome.ROUTING_ANOMALY:
        decision = WalkerDecision(
            claim=claim,
            served_from_tier="routing_anomaly",
            outcome=LookupOutcome.MISS,
            verification_status="routing_anomaly",
            routing_method=None,
            chain_reliability=1.0,
            notes=[
                "Layer 2 validator rejected the claim; no tier lookup ran",
            ],
        )
        _emit_walker_decision(store, source_turn_id, decision)
        return decision

    routing_method = layer2_decision.method
    pattern = claim.get("pattern", "")
    key_slot_names = KEY_SLOTS_BY_PATTERN.get(pattern, [])
    notes: list[str] = []

    # ---- 1. Tier U lookup --------------------------------------------
    tier_u_result = _tier_u.lookup(
        claim, store, predicate_oracle,
        key_slot_names=key_slot_names,
        user_id=user_id, current_session=current_session,
        llm=llm, source_turn_id=source_turn_id,
        entity_oracle=entity_oracle,
        active_context_tokens=active_context_tokens,
    )
    if tier_u_result.outcome is _tier_u.TierUOutcome.MATCH:
        decision = WalkerDecision(
            claim=claim,
            served_from_tier="u",
            outcome=LookupOutcome.MATCH,
            verification_status="user_asserted",
            routing_method=routing_method,
            matching_fact_id=tier_u_result.matching_fact.id
                if tier_u_result.matching_fact else None,
            via=list(tier_u_result.via),
            polarity_flipped=tier_u_result.polarity_flipped,
            chain_reliability=1.0,
            notes=list(tier_u_result.notes) + notes,
        )
        _emit_walker_decision(store, source_turn_id, decision)
        return decision

    if tier_u_result.outcome is _tier_u.TierUOutcome.CONTRADICTION:
        decision = WalkerDecision(
            claim=claim,
            served_from_tier="u",
            outcome=LookupOutcome.CONTRADICTION,
            verification_status="user_asserted",
            routing_method=routing_method,
            contradicting_fact_id=tier_u_result.contradicting_fact.id
                if tier_u_result.contradicting_fact else None,
            via=list(tier_u_result.via),
            polarity_flipped=tier_u_result.polarity_flipped,
            chain_reliability=1.0,
            notes=list(tier_u_result.notes) + notes,
        )
        _emit_walker_decision(store, source_turn_id, decision)
        return decision

    # Tier U MISS — fall through.

    # ---- 2. Tier W lookup --------------------------------------------
    tier_w_result = _tier_w.lookup(
        claim, store, predicate_oracle,
        key_slot_names=key_slot_names,
        registry=registry,
        llm=llm, source_turn_id=source_turn_id,
        entity_oracle=entity_oracle,
        active_context_tokens=active_context_tokens,
    )

    if tier_w_result.outcome is LookupOutcome.MATCH:
        status = tier_w_result.verification_status or ""
        if status in _TERMINAL_W_STATUSES:
            decision = WalkerDecision(
                claim=claim,
                served_from_tier="w",
                outcome=LookupOutcome.MATCH,
                verification_status=status,
                routing_method=routing_method,
                matching_w_row_id=tier_w_result.matching_row_id,
                evidence=tier_w_result.evidence,
                via=list(tier_w_result.via),
                polarity_flipped=tier_w_result.polarity_flipped,
                chain_reliability=1.0,
                notes=list(tier_w_result.notes) + notes,
            )
            _emit_walker_decision(store, source_turn_id, decision)
            return decision

        # Fall-through path: the cached row exists but the status says
        # "evidence thin / failed / pending". Derivation walks the
        # substrate (different evidence source) and may produce a
        # verdict. Record the cached row's status in notes so Layer
        # 5 / the trace UI sees the chain of reasoning.
        notes.append(
            f"tier_w cached row {tier_w_result.matching_row_id!r} "
            f"with status {status!r}; falling through to derivation "
            f"(retrieval evidence thin; substrate may compose a chain)"
        )

    elif tier_w_result.outcome is LookupOutcome.CONTRADICTION:
        # Tier W contradiction is terminal regardless of inner verdict.
        # Layer 5 (Phase 8) interprets the cached verification_status
        # to decide whether to render correction or hedge.
        decision = WalkerDecision(
            claim=claim,
            served_from_tier="w",
            outcome=LookupOutcome.CONTRADICTION,
            verification_status=tier_w_result.verification_status or "verified",
            routing_method=routing_method,
            contradicting_w_row_id=tier_w_result.contradicting_row_id,
            evidence=tier_w_result.evidence,
            via=list(tier_w_result.via),
            polarity_flipped=tier_w_result.polarity_flipped,
            chain_reliability=1.0,
            notes=list(tier_w_result.notes) + notes,
        )
        _emit_walker_decision(store, source_turn_id, decision)
        return decision

    # Tier W MISS or fall-through — proceed to derivation.

    # ---- 3. Derivation walk ------------------------------------------
    derivation_result = _derivation.walk(
        claim, store,
        key_slot_names=key_slot_names,
        registry=registry,
        predicate_oracle=predicate_oracle,
        entity_oracle=entity_oracle,
        taxonomy_oracle=taxonomy_oracle,
        distribution_oracle=distribution_oracle,
        llm=llm,
        source_turn_id=source_turn_id,
        user_id=user_id,
        current_session=current_session,
        active_context_tokens=active_context_tokens,
    )
    if derivation_result.outcome is LookupOutcome.MATCH:
        # Derivation produces a "verified" status — the chain
        # composes substrate-mediated witnesses into support for
        # the claim. The chain itself carries the provenance.
        decision = WalkerDecision(
            claim=claim,
            served_from_tier="derivation",
            outcome=LookupOutcome.MATCH,
            verification_status="verified",
            routing_method=routing_method,
            matching_fact_id=derivation_result.matching_fact_id,
            matching_w_row_id=derivation_result.matching_w_row_id,
            derivation_path=[edge.to_dict() for edge in derivation_result.chain],
            chain_reliability=derivation_result.chain_reliability,
            via=[edge.oracle for edge in derivation_result.chain],
            notes=list(derivation_result.notes) + notes,
        )
        _emit_walker_decision(store, source_turn_id, decision)
        return decision

    notes.append(
        f"derivation MISS: {derivation_result.abort_reason!r} "
        f"after exploring {derivation_result.explored_states} states"
    )

    # ---- 4. Fresh dispatch -------------------------------------------
    if fresh_dispatch is not None:
        decision = fresh_dispatch(
            claim,
            routing_method=routing_method,
            store=store,
            registry=registry,
            llm=llm,
            source_turn_id=source_turn_id,
            user_id=user_id,
            current_session=current_session,
            prior_notes=notes,
        )
        # The fresh dispatcher returns a WalkerDecision; ensure
        # routing_method is propagated even if the dispatcher
        # forgot to set it.
        if decision.routing_method is None and routing_method is not None:
            decision = _replace(decision, routing_method=routing_method)
        _emit_walker_decision(store, source_turn_id, decision)
        return decision

    # No fresh dispatcher provided (Phase 7d default; tests for the
    # walker proper). Emit a placeholder verdict.
    decision = WalkerDecision(
        claim=claim,
        served_from_tier="fresh",
        outcome=LookupOutcome.MISS,
        verification_status="unverifiable_pending_implementation",
        routing_method=routing_method,
        chain_reliability=1.0,
        notes=notes + [
            "no fresh dispatcher provided; Phase 7e wires this"
        ],
    )
    _emit_walker_decision(store, source_turn_id, decision)
    return decision


# ============================================================================
# Helpers
# ============================================================================


def _emit_walker_decision(
    store: FactStore,
    source_turn_id: Optional[int],
    decision: WalkerDecision,
) -> None:
    """Single ``walker_decision`` event per turn captures the final
    Layer-4 verdict. Fields chosen for trace UI consumption:
    served_from_tier, outcome, verification_status, via,
    chain_reliability, routing_method, plus any matching/
    contradicting fact/row IDs and the full derivation_path so the
    chain can be reconstructed from the event log alone (Phase 8.5)."""
    _safe_emit_event(
        store, source_turn_id, "walker_decision",
        {
            "served_from_tier": decision.served_from_tier,
            "outcome": decision.outcome.value,
            "verification_status": decision.verification_status,
            "routing_method": decision.routing_method,
            "matching_fact_id": decision.matching_fact_id,
            "contradicting_fact_id": decision.contradicting_fact_id,
            "matching_w_row_id": decision.matching_w_row_id,
            "contradicting_w_row_id": decision.contradicting_w_row_id,
            "via": list(decision.via),
            "chain_reliability": decision.chain_reliability,
            "polarity_flipped": decision.polarity_flipped,
            "derivation_path_length": len(decision.derivation_path),
            "derivation_path": [dict(s) for s in decision.derivation_path],
            "notes": list(decision.notes),
        },
    )


def _replace(d: WalkerDecision, **changes) -> WalkerDecision:
    """Replace fields on a frozen WalkerDecision (dataclasses.replace
    equivalent, kept local to avoid the import on the hot path)."""
    from dataclasses import replace
    return replace(d, **changes)
