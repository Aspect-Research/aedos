"""v1 stack runner for the Phase 9 parity audit.

Direct-Python access to the v1 (legacy) layers — no HTTP. v1 has no
equivalent for the substrate, two-text-oracle, or routing-memo
shapes; those return NOT_APPLICABLE. USER_STORAGE and
ASSISTANT_LOOKUP are exercised through ``src.router.Router`` (with
a stub routing_fn for ASSISTANT_LOOKUP) and
``src.verifiers.store_verifier.store_lookup_verify``.

The audit DOES want to surface where v1 silently does the wrong
thing (returns MATCH when expected MISS, etc.), so the runner reports
PASS only when v1's outcome matches the corpus's expected outcome
under the same semantic mapping the v2 walker uses
(``RoutingOutcome.VERIFIED → match``, ``CONTRADICTED →
contradiction``, ``UNVERIFIED → miss``).

Where v1 lacks the v0.14 schema's session model
(``is_session_local`` / ``session_ids``), USER_STORAGE entries that
expect those columns can't pass — v1 has no equivalent surface. Those
land in the EXPECTED_DIVERGENCE bucket via the registry's
``session_local`` tag.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.legacy.fact_store import (
    DEFAULT_USER_ID,
    Fact,
    FactStore,
)
from src.legacy.llm_router import RoutingDecision
from src.legacy.pattern_registry import PatternRegistry, load_default_registry
from src.legacy.router.constants import KEY_SLOTS_BY_PATTERN
from src.legacy.router.router import Router as V1Router
from src.legacy.router.types import RoutingOutcome
from tests.parity_runners.corpus import shape_of
from tests.parity_runners.types import StackResult, StackVerdict
from tests.smoke_dispatcher import SmokeEntryShape


# ============================================================================
# Stack scaffold
# ============================================================================


@dataclass
class V1Stack:
    """v1 singletons the audit needs."""

    store: FactStore
    registry: PatternRegistry
    next_turn_id: int = 1

    @property
    def synthetic_turn_id(self) -> int:
        tid = self.next_turn_id
        self.next_turn_id += 1
        return tid


def build_v1_stack(db_path: Path) -> V1Stack:
    store = FactStore(str(db_path))
    return V1Stack(
        store=store,
        registry=load_default_registry(),
    )


# ============================================================================
# Helpers
# ============================================================================


_OUTCOME_TO_LOOKUP_VOCAB = {
    RoutingOutcome.VERIFIED: "match",
    RoutingOutcome.CONTRADICTED: "contradiction",
    RoutingOutcome.UNVERIFIED: "miss",
    RoutingOutcome.UNVERIFIABLE_IN_PRINCIPLE: "miss",
    RoutingOutcome.USER_STORED: "match",      # user-side: stored ok
    RoutingOutcome.USER_DUPLICATE: "match",
    RoutingOutcome.USER_CONTRADICTED_PRIOR: "contradiction",
    RoutingOutcome.USER_CONTRADICTED_SELF: "contradiction",
    RoutingOutcome.ROUTING_ANOMALY: "miss",   # short-circuits without lookup
}


def _claim_from_expected(
    fact: dict, source_text: str, registry: PatternRegistry,
) -> dict:
    """Same shape as v2_runner. Fills missing required slots with a
    placeholder so the v2-strict claim shape works on the v1 path
    too (v1 doesn't validate them, but parity is cheap)."""
    from tests.parity_runners.corpus import reconstruct_claim_slots
    predicates = fact.get("predicate_in") or []
    predicate = predicates[0] if predicates else ""
    pattern_name = fact["pattern"]
    required: list[str] = []
    if registry.has(pattern_name):
        # v1 PatternRegistry has the same .required_slot_names()
        # API as v2's; both descend from a shared shape.
        try:
            required = list(registry.get(pattern_name).required_slot_names())
        except AttributeError:
            # v1 may not have required_slot_names; fall back to
            # filtering pattern.slots for required ones.
            p = registry.get(pattern_name)
            required = [s.name for s in p.slots if s.required]
    return {
        "pattern": pattern_name,
        "predicate": predicate,
        "polarity": int(fact["polarity"]),
        "slots": reconstruct_claim_slots(
            pattern_name, dict(fact.get("slots_subset", {})),
            required_slots=required,
        ),
        "source_text": source_text,
    }


def _stub_routing_fn(method: str) -> Any:
    """Returns a v1 RoutingDecision — uses v1's RoutingDecision class
    so the Router consumes it unchanged."""
    def _fn(_claim: dict) -> RoutingDecision:
        return RoutingDecision(
            method=method,
            reason="audit stub",
            python_inputs_self_contained=None,
            retrieval_query_hint=None,
            canonical_constants_needed=None,
        )
    return _fn


# ============================================================================
# Per-shape runners
# ============================================================================


def _run_user_storage(entry: dict, stack: V1Stack) -> StackVerdict:
    """Route each expected fact through v1 Router with origin='user'.

    v1 has no ``is_session_local``; if any expected fact carries
    ``expected_is_session_local`` or ``expected_session_ids_after``,
    those will not match v1's Fact shape and the entry FAILS — the
    EXPECTED_DIVERGENCE registry handles the bucketing.
    """
    raw_text = entry["text"]
    has_session_expectations = any(
        ("expected_is_session_local" in f
         or "expected_session_ids_after" in f
         or "expected_affirmed_count_after" in f)
        for f in entry["expected_facts"]
    )

    stored: list[str] = []
    for fact in entry["expected_facts"]:
        claim = _claim_from_expected(fact, source_text=raw_text, registry=stack.registry)
        if not claim["predicate"]:
            continue
        # v1 Router needs a routing_fn for model-origin claims; for user
        # origin it doesn't run the routing_fn for self-attributes. Stub
        # for safety.
        router = V1Router(
            stack.store, stack.registry,
            routing_fn=_stub_routing_fn("user_authoritative"),
        )
        if not stack.registry.has(claim["pattern"]):
            return StackVerdict(
                StackResult.FAIL,
                detail=f"v1 registry has no pattern {claim['pattern']!r}",
            )
        try:
            decision = router.route(
                claim, origin="user",
                source_turn_id=stack.synthetic_turn_id,
                raw_text=raw_text,
            )
        except Exception as exc:
            return StackVerdict(
                StackResult.ERROR,
                detail="v1 Router.route raised on user-origin claim",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        # Stored ok.
        stored.append(
            f"{claim['pattern']}.{claim['predicate']}/{decision.outcome.value}"
        )

    if has_session_expectations:
        # v1 has no is_session_local / session_ids columns; the
        # session-aware expectations on the corpus entry can't be
        # satisfied even when the fact itself stored. Surface the
        # mismatch — the registry bucket-tags it as EXPECTED_DIVERGENCE.
        return StackVerdict(
            StackResult.FAIL,
            detail=(
                "v1 has no is_session_local / session_ids columns; "
                "session-aware expectations cannot be satisfied "
                "(stored: "
                + "; ".join(stored) + ")"
            ),
        )

    return StackVerdict(
        StackResult.PASS,
        detail=f"v1 stored {len(stored)} fact(s): {'; '.join(stored)}",
    )


def _run_assistant_lookup(entry: dict, stack: V1Stack) -> StackVerdict:
    """Route each expected fact through v1 Router with origin='model'.

    Maps v1 RoutingOutcome to the lookup vocabulary {match, miss,
    contradiction} and compares to ``expected_walker_outcome`` (or
    ``expected_tier_u_outcome``).
    """
    raw_text = entry["text"]
    for fact in entry["expected_facts"]:
        claim = _claim_from_expected(fact, source_text=raw_text, registry=stack.registry)
        if not claim["predicate"]:
            return StackVerdict(
                StackResult.FAIL,
                detail="expected_facts[0].predicate_in empty",
            )
        if not stack.registry.has(claim["pattern"]):
            return StackVerdict(
                StackResult.FAIL,
                detail=f"v1 registry has no pattern {claim['pattern']!r}",
            )
        # All assistant-lookup smoke entries are user-attribute claims
        # (preference / spatial_temporal / quantitative about the user)
        # so user_authoritative is the right method. Stub returns it
        # unconditionally — the audit isn't validating routing
        # decisions on assistant claims, only the lookup outcome.
        router = V1Router(
            stack.store, stack.registry,
            routing_fn=_stub_routing_fn("user_authoritative"),
        )
        try:
            decision = router.route(
                claim, origin="model",
                source_turn_id=stack.synthetic_turn_id,
            )
        except Exception as exc:
            return StackVerdict(
                StackResult.ERROR,
                detail="v1 Router.route raised on model-origin claim",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        v1_lookup_outcome = _OUTCOME_TO_LOOKUP_VOCAB.get(
            decision.outcome, "miss",
        )
        expected_outcome = (
            fact.get("expected_walker_outcome")
            or fact.get("expected_tier_u_outcome")
        )
        if expected_outcome is None:
            return StackVerdict(
                StackResult.FAIL,
                detail="entry has neither expected_walker_outcome nor "
                       "expected_tier_u_outcome",
            )
        if v1_lookup_outcome != expected_outcome:
            return StackVerdict(
                StackResult.FAIL,
                detail=f"v1 outcome={decision.outcome.value!r} → "
                       f"lookup={v1_lookup_outcome!r}, "
                       f"expected {expected_outcome!r}",
            )
    return StackVerdict(
        StackResult.PASS,
        detail=f"v1 store_lookup matched expected outcome on "
               f"{len(entry['expected_facts'])} fact(s)",
    )


# ============================================================================
# Public dispatch
# ============================================================================


def run_entry(entry: dict, stack: V1Stack) -> StackVerdict:
    shape = shape_of(entry)
    if shape in (
        SmokeEntryShape.SUBSTRATE_DIRECT,
        SmokeEntryShape.TWO_TEXT_ORACLE,
        SmokeEntryShape.ROUTING_MEMO,
    ):
        return StackVerdict(
            StackResult.NOT_APPLICABLE,
            detail=f"v1 has no equivalent code path for shape {shape.value!r}",
        )
    if shape is SmokeEntryShape.USER_STORAGE:
        return _run_user_storage(entry, stack)
    if shape is SmokeEntryShape.ASSISTANT_LOOKUP:
        return _run_assistant_lookup(entry, stack)
    return StackVerdict(
        StackResult.ERROR,
        detail=f"unknown shape {shape!r}",
    )
