"""Layer 2 entry point — composes validator + routing memo + LLM router.

Flow:

  1. ``validate(claim)``. On Anomaly, emit ``routing_validation_failed``
     event and return a ``ROUTING_ANOMALY`` ``Decision``. The LLM
     router is NOT consulted; no memo row is written.
  2. ``memo.lookup(pattern, predicate)``. On hit, emit ``routing_memo_hit``,
     ``touch_consulted`` (last_consulted_at metadata only — counts
     untouched per principle 3), emit a ``routing_decision`` event for
     trace-UI parity with the cold path, and return a ``CLASSIFIED``
     Decision with ``memo_hit=True``.
  3. On miss, run the LLM router. UPSERT the memo row, emit
     ``routing_memo_write``, emit ``routing_decision``, and return a
     ``CLASSIFIED`` Decision with ``memo_hit=False``.

Phase 2 is classification only. Verifier dispatch (CodeGen, Retrieval,
store-match, cache, session model) is layered on in Phases 3-7. The
``Decision`` shape carries the routing classification; downstream
layers wrap it when verifier dispatch runs.

What this file does NOT do (deliberately):

  * No origin (user/model) split. The architecture's user-vs-world
    routing happens in Layer 4's walker via ``is_self_attribute``;
    Layer 2 just classifies the verification method.
  * No verification execution. The routing methods are labels;
    actually running the verifier lives in later layers.
  * No fact storage. Phase 6's Tier U rewrite owns user-fact storage;
    Phase 4+'s walker owns world-fact verification cache.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from src.fact_store import FactStore
from src.layer1_extraction.pattern_registry import PatternRegistry
from src.layer2_routing.llm_router import (
    ROUTING_METHODS,
    RoutingDecision,
    route_claim,
)
from src.layer2_routing.routing_memo import RoutingMemo
from src.layer2_routing.types import (
    Decision,
    RoutingOutcome,
    ValidationResult,
)
from src.layer2_routing.validator import validate
from src.llm_client import LLMClient


# A routing function is anything that takes a claim and returns a
# RoutingDecision. The default uses the LLM router; tests inject a
# stub. Mirrors v1's RoutingFn shape so the migration is mechanical.
RoutingFn = Callable[[dict], RoutingDecision]


class Router:
    """Layer 2 orchestrator.

    Constructed with a FactStore (for the routing_memo + pipeline_events
    tables) and a PatternRegistry (for the validator). Either an
    ``LLMClient`` or an explicit ``routing_fn`` must be provided —
    without one, the router can still classify cached (memo-hit)
    claims but raises on the first memo miss.

    The ``memo`` argument is normally None; the router constructs a
    ``RoutingMemo(store)`` itself. Tests pass an explicit memo to share
    state across multiple routers in a single test, but production
    code uses one memo per store.
    """

    def __init__(
        self,
        store: FactStore,
        registry: PatternRegistry,
        *,
        llm: Optional[LLMClient] = None,
        routing_fn: Optional[RoutingFn] = None,
        memo: Optional[RoutingMemo] = None,
    ):
        self.store = store
        self.registry = registry
        self.llm = llm
        if routing_fn is None and llm is not None:
            routing_fn = lambda claim, _llm=llm: route_claim(claim, _llm)
        self.routing_fn = routing_fn
        self.memo = memo if memo is not None else RoutingMemo(store)

    # ---- entry point ---------------------------------------------------

    def classify(self, claim: dict, *, source_turn_id: int) -> Decision:
        """Classify a single claim. Pure routing — no dispatch."""
        validation = validate(claim, self.registry)
        if not validation.ok:
            return self._handle_anomaly(claim, validation, source_turn_id)

        pattern = claim.get("pattern", "")
        predicate = claim.get("predicate", "")
        if not pattern or not predicate:
            # Validator's required-slot invariant should already have
            # caught this, but defend in case the validator's contract
            # is loosened later — a memo lookup on empty key is wrong.
            raise ValueError(
                "claim is missing pattern or predicate after validation; "
                "this is a validator bug"
            )

        cached = self.memo.lookup(pattern, predicate)
        if cached is not None:
            return self._handle_memo_hit(
                claim, cached, source_turn_id,
            )

        return self._handle_memo_miss(
            claim, pattern, predicate, source_turn_id,
        )

    # ---- branches ------------------------------------------------------

    def _handle_anomaly(
        self,
        claim: dict,
        validation: ValidationResult,
        source_turn_id: int,
    ) -> Decision:
        decision = Decision(
            claim=claim,
            outcome=RoutingOutcome.ROUTING_ANOMALY,
            method=None,
            reason=None,
            memo_hit=False,
            validation=validation,
            routing_decision=None,
            notes=[
                f"validator anomaly: invariant {validation.invariant!r} "
                f"failed on slot {validation.slot!r} "
                f"(expected {validation.expected!r}, got {validation.actual!r})"
            ],
        )
        self._log(
            source_turn_id,
            "routing_validation_failed",
            {
                "claim": claim,
                "validation": validation.to_dict(),
            },
        )
        # Re-emit on the routing_anomaly_detected stage too so v2's
        # event vocabulary keeps parity with v1's anomaly stream
        # (existing UI consumers grep for that stage name).
        self._log(
            source_turn_id,
            "routing_anomaly_detected",
            {
                "claim": claim,
                "validation": validation.to_dict(),
            },
        )
        return decision

    def _handle_memo_hit(
        self,
        claim: dict,
        cached,  # RoutingMemoEntry
        source_turn_id: int,
    ) -> Decision:
        # Touch_consulted updates last_consulted_at as observability
        # metadata. Counts are NOT incremented (principle 3: reads
        # are not writes). The trace UI uses last_consulted_at to
        # surface stale-row hints.
        self.memo.touch_consulted(cached.pattern, cached.predicate)

        routing_payload = {
            "method": cached.method,
            "reason": cached.reason,
            "python_inputs_self_contained": None,
            "retrieval_query_hint": None,
            "canonical_constants_needed": None,
        }
        decision = Decision(
            claim=claim,
            outcome=RoutingOutcome.CLASSIFIED,
            method=cached.method,
            reason=cached.reason,
            memo_hit=True,
            validation=ValidationResult.passed(),
            routing_decision=routing_payload,
            notes=[
                f"served from routing memo "
                f"(pattern={cached.pattern!r}, predicate={cached.predicate!r}, "
                f"affirmed_count={cached.affirmed_count}, "
                f"contradicted_count={cached.contradicted_count})"
            ],
        )

        self._log(
            source_turn_id,
            "routing_memo_hit",
            {
                "pattern": cached.pattern,
                "predicate": cached.predicate,
                "method": cached.method,
                "reason": cached.reason,
                "affirmed_count": cached.affirmed_count,
                "contradicted_count": cached.contradicted_count,
                "created_at": cached.created_at,
            },
        )
        # Mirror the cold-path's routing_decision event so trace-UI
        # consumers see the chosen method on every classification,
        # regardless of whether it came from memo or LLM.
        self._log_routing_decision(decision, source_turn_id)
        return decision

    def _handle_memo_miss(
        self,
        claim: dict,
        pattern: str,
        predicate: str,
        source_turn_id: int,
    ) -> Decision:
        if self.routing_fn is None:
            raise RuntimeError(
                "Router needs an llm or a routing_fn to classify novel "
                f"(pattern, predicate) pairs (got {pattern!r}, {predicate!r})"
            )
        routing = self.routing_fn(claim)
        method = (
            routing.method
            if routing.method in ROUTING_METHODS
            else "unverifiable"
        )

        # Write-through. UPSERT preserves counts; first-time inserts
        # default counts to 0.
        self.memo.record(pattern, predicate, method, routing.reason)
        self._log(
            source_turn_id,
            "routing_memo_write",
            {
                "pattern": pattern,
                "predicate": predicate,
                "method": method,
                "reason": routing.reason,
            },
        )

        decision = Decision(
            claim=claim,
            outcome=RoutingOutcome.CLASSIFIED,
            method=method,
            reason=routing.reason,
            memo_hit=False,
            validation=ValidationResult.passed(),
            routing_decision=routing.to_dict(),
            notes=[],
        )
        self._log_routing_decision(decision, source_turn_id)
        return decision

    # ---- event logging -------------------------------------------------

    def _log(
        self,
        source_turn_id: int,
        stage: str,
        data: dict[str, Any],
    ) -> None:
        try:
            self.store.insert_pipeline_event(source_turn_id, stage, data)
        except Exception:
            # Logging must never crash routing. The orchestrator's
            # responsibility is the classification; observability is
            # best-effort. Aligned with v1's _log_routing_decision.
            pass

    def _log_routing_decision(
        self,
        decision: Decision,
        source_turn_id: int,
    ) -> None:
        self._log(
            source_turn_id,
            "routing_decision",
            {
                "claim": decision.claim,
                "decision": decision.routing_decision,
                "outcome": decision.outcome.value,
                "method": decision.method,
                "memo_hit": decision.memo_hit,
            },
        )
