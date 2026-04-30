"""Pipeline orchestrator.

Runs a single user↔assistant turn through every stage of the system:

    user message
        ↓
    extract user claims → route & store → pipeline_event
        ↓
    build chat context (history + user-asserted facts)
        ↓
    LLM generates assistant draft
        ↓
    extract assistant claims → route through verifiers → pipeline_event
        ↓
    if any contradictions: rewrite draft with corrections → pipeline_event
        ↓
    return trace for UI

Every stage writes a pipeline_events row. The UI rebuilds the full trace
straight from that table.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from src.corrector import Corrector, Intervention
from src.extractor import ClaimExtractor
from src.fact_store import (
    DEFAULT_SESSION_ID,
    DEFAULT_USER_ID,
    Fact,
    FactStore,
)
from src.llm_client import ChatMessage, LLMClient
from src.pattern_registry import PatternRegistry
from src.router import Decision, Router, RoutingOutcome


# A "chat backend" is anything with a chat(system, messages, *,
# max_tokens, store, turn_id) -> str method. LLMClient itself satisfies
# the older positional signature, so passing chat_backend=None falls back
# to ``llm`` and preserves the test MockLLM contract.


CHAT_SYSTEM_TEMPLATE = """You are a helpful assistant in a single-conversation demo.

Facts the user has stated about themselves (ground truth — never contradict these):
{facts_block}

Respond naturally and directly. When answering questions whose answer appears above, state it plainly. Do not speculate about user preferences that aren't listed — say you don't know instead."""


@dataclass
class TurnTrace:
    user_turn_id: int
    assistant_turn_id: int
    final_content: str
    original_content: str | None  # non-None iff a correction was applied
    user_extraction: dict
    user_decisions: list[dict]
    assistant_extraction: dict
    verification_decisions: list[dict]
    interventions: list[dict]  # what the corrector planned (may be empty)
    routing_anomalies: list[dict]  # decisions flagged as anomalies

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_turn_id": self.user_turn_id,
            "assistant_turn_id": self.assistant_turn_id,
            "final_content": self.final_content,
            "original_content": self.original_content,
            "user_extraction": self.user_extraction,
            "user_decisions": self.user_decisions,
            "assistant_extraction": self.assistant_extraction,
            "verification_decisions": self.verification_decisions,
            "interventions": self.interventions,
            "routing_anomalies": self.routing_anomalies,
        }


class Pipeline:
    def __init__(
        self,
        store: FactStore,
        registry: PatternRegistry,
        llm: LLMClient,
        extractor: ClaimExtractor,
        router: Router,
        corrector: Corrector,
        chat_backend: Any | None = None,
        user_id: str = DEFAULT_USER_ID,
        session_id: str = DEFAULT_SESSION_ID,
        # v0.6 Phase 6 — when set, the scoping classifier runs on every
        # model-origin claim and logs a cache_scoping_decision event.
        # Pure observation: no behavior change, no cache reads, no cache
        # writes. Set to a callable for tests; defaults to the LLM
        # implementation when ``llm`` is real.
        scoping_classifier: Any | None = None,
        # v0.6 — runs only when scoping_classifier marked the claim
        # world_fact. Returns a StabilityDecision; logged but not
        # acted on yet.
        stability_classifier: Any | None = None,
        # v0.7.16 — when set, ONE LLM call returns both scoping AND
        # stability for a claim. Halves cache-classifier LLM cost.
        # CacheGate prefers this over the legacy two-call path.
        # build_pipeline wires it from
        # src.cache.classify_combined.classify_for_cache.
        combined_cache_classifier: Any | None = None,
        # v0.6 — when set AND scoping/stability are also set, the
        # pipeline writes successful retrieval verdicts to this cache.
        # No lookups yet (this is the "fill the cache" stage; lookups
        # come in a follow-up commit so we can measure hit rate first).
        verification_cache: Any | None = None,
    ):
        self.store = store
        self.registry = registry
        self.llm = llm
        self.extractor = extractor
        self.router = router
        self.corrector = corrector
        self.user_id = user_id
        # v0.7.14: per-conversation context. Microtheory entries are
        # session-scoped; cross-session user-asserted facts use NULL.
        self.session_id = session_id
        # Propagate to the router so user-side _route_user can stamp
        # session_id on session-scoped assertions.
        if hasattr(router, "session_id"):
            router.session_id = session_id
        # CacheGate consolidates scoping + stability + lookup + write.
        # Pre-refactor these were three separate fields scattered
        # across pipeline + router; now one owner.
        from src.cache import CacheGate
        self._cache_gate = CacheGate(
            cache=verification_cache,
            scoping_fn=scoping_classifier,
            stability_fn=stability_classifier,
            combined_fn=combined_cache_classifier,
            store=store,
        )
        # Backward-compat aliases for tests that read these fields
        # directly. New code should reach for self._cache_gate.
        self._scoping_classifier = scoping_classifier
        self._stability_classifier = stability_classifier
        self._verification_cache = verification_cache
        # When no explicit backend is provided, use ``llm`` directly. This
        # preserves the long-standing test contract where MockLLM provides
        # ``chat(system, messages, max_tokens=...)`` and is passed in as
        # the llm.
        self.chat_backend = chat_backend if chat_backend is not None else llm
        # Active per-turn model override; set by run_turn when a model
        # parameter is supplied. Used by _invoke_chat_backend to dispatch
        # to the right chat backend without mutating self.chat_backend.
        self._active_chat_model: str | None = None
        # v0.9.0: optional callback for live (non-persisted) events
        # like ``chat_draft_token``. Set by /api/chat/stream to push
        # streaming tokens to the SSE consumer; None disables streaming
        # (the chat backend then makes a single blocking call). Stays
        # None for the non-stream /api/chat path and for tests.
        self.live_emit: Any | None = None

    def run_turn(
        self, user_message: str, *, model: str | None = None,
    ) -> TurnTrace:
        """Run one user→assistant turn.

        ``model`` (optional) makes EVERY LLM call (chat, extractor,
        router, judge, corrector, scoping, stability, code-gen) use
        the named model. ``None`` uses the pipeline's defaults (the
        model attrs on ``self.llm`` and the chat backend supplied at
        construction time)."""
        # Tolerant of LLM clients that don't expose with_active_model
        # (test MockLLMs don't). When the LLM doesn't have the method,
        # the model selection is a no-op on the Anthropic side; the
        # chat backend dispatch via _active_chat_model still works.
        ctx = getattr(self.llm, "with_active_model", None)
        if ctx is not None:
            with ctx(model):
                self._active_chat_model = model
                try:
                    return self._run_turn_inner(user_message)
                finally:
                    self._active_chat_model = None
        else:
            self._active_chat_model = model
            try:
                return self._run_turn_inner(user_message)
            finally:
                self._active_chat_model = None

    def _run_turn_inner(self, user_message: str) -> TurnTrace:
        """Top-level orchestrator. Each stage is a clearly-named
        method that returns the inputs the next stage needs. Reading
        this function tells you the shape of a turn."""
        user_turn_id, user_extraction, user_decisions = (
            self._stage_user_side(user_message)
        )
        assistant_turn_id, draft = self._stage_chat_draft(user_message)
        asst_extraction = self._stage_assistant_extract(
            draft, user_message, assistant_turn_id,
        )
        verification_decisions = self._stage_verify(
            asst_extraction.valid_facts, assistant_turn_id,
        )
        self._stage_anomaly_and_failure_events(
            verification_decisions, assistant_turn_id,
        )
        final_content, original_content, interventions = self._stage_correct(
            draft, verification_decisions, assistant_turn_id,
        )
        self._stage_finalize(final_content, assistant_turn_id)

        return TurnTrace(
            user_turn_id=user_turn_id,
            assistant_turn_id=assistant_turn_id,
            final_content=final_content,
            original_content=original_content,
            user_extraction=user_extraction.to_dict(),
            user_decisions=[d.to_dict() for d in user_decisions],
            assistant_extraction=asst_extraction.to_dict(),
            verification_decisions=[d.to_dict() for d in verification_decisions],
            interventions=[i.to_dict() for i in interventions],
            routing_anomalies=[
                d.to_dict() for d in verification_decisions
                if d.outcome is RoutingOutcome.ROUTING_ANOMALY
            ],
        )

    # ---- per-stage methods (one method per turn phase) -----------------

    def _stage_user_side(self, user_message: str):
        """Stages 1–3: log user turn, extract user claims, route them."""
        user_turn_id = self.store.insert_turn(
            "user", user_message, user_id=self.user_id,
        )
        user_extraction = self.extractor.extract(user_message, role="user")
        self.store.insert_pipeline_event(
            user_turn_id, "user_extraction", user_extraction.to_dict()
        )
        self._emit_substitution_warnings(
            user_extraction, user_turn_id,
            extra={"user_input": user_message, "side": "user"},
        )
        user_decisions: list[Decision] = [
            self.router.route(c, origin="user", source_turn_id=user_turn_id)
            for c in user_extraction.valid_facts
        ]
        self.store.insert_pipeline_event(
            user_turn_id, "user_storage",
            {"decisions": [d.to_dict() for d in user_decisions]},
        )
        return user_turn_id, user_extraction, user_decisions

    def _stage_chat_draft(self, user_message: str):
        """Stage 4: generate the assistant draft with ground-truth
        context. Returns (assistant_turn_id, draft). The assistant
        turn is created BEFORE the chat call so the chat_model_call
        event always has a turn_id even if the backend raises."""
        system_prompt = self._build_chat_system_prompt(
            self.store.all_user_facts(user_id=self.user_id),
        )
        history = self._build_chat_history()
        assistant_turn_id = self.store.insert_turn(
            "assistant", "", user_id=self.user_id,
        )
        draft = self._invoke_chat_backend(
            system_prompt, history, assistant_turn_id,
        )
        self.store.update_turn_content(
            assistant_turn_id, draft, original_content=None,
        )
        self.store.insert_pipeline_event(
            assistant_turn_id, "assistant_draft", {"content": draft}
        )
        return assistant_turn_id, draft

    def _stage_assistant_extract(self, draft, user_message, assistant_turn_id):
        """Stage 5: extract claims from the assistant draft. The
        user's preceding message is passed as context so the extractor
        can resolve self-references."""
        asst_extraction = self.extractor.extract(
            draft, role="assistant", context=user_message,
        )
        self.store.insert_pipeline_event(
            assistant_turn_id, "assistant_extraction",
            asst_extraction.to_dict(),
        )
        self._emit_substitution_warnings(
            asst_extraction, assistant_turn_id,
            extra={"model_draft": draft},
        )
        return asst_extraction

    def _stage_verify(self, valid_facts, assistant_turn_id):
        """v0.7.14 tiered precedence verification.

        For each claim, walk through the precedence tiers cheapest-to-
        costliest, returning at the first tier that produces a Decision:

          * Tier 1 — microtheory (this conversation's session-scoped
                     user assertions). Free SQL.
          * Tier 2 — user store (cross-session user assertions). Free SQL.
          * Tier 3 — verification cache (recently-confirmed world facts).
                     Free SQL.
          * Tier 4 — fresh classify + route + verify. Pays the LLM cost.

        v0.9.0: Tiers 1-3 are fast SQL — kept sequential. Tier 4 (the
        LLM-heavy path) is dispatched on a thread pool because the per-
        claim work (classify + route + verifier) is independent across
        claims. SQLite serializes writes internally; CacheGate guards
        its in-memory state with a lock; the LLM SDKs release the GIL on
        network I/O. Output ordering is preserved by tagging each claim
        with its index and reassembling.
        """
        self._cache_gate.reset_for_turn()
        # Hand the gate to the router so it can still short-circuit on
        # cache hits inside _route_retrieval (defense-in-depth + the
        # router's cache-as-evidence wiring depends on it).
        self.router._cache_gate = self._cache_gate

        # ---- Phase 1: walk tiers 1-3 sequentially (fast SQL) ----------
        decisions: list[Decision | None] = [None] * len(valid_facts)
        fresh_indices: list[int] = []
        for i, claim in enumerate(valid_facts):
            d = self._tier_microtheory_lookup(claim, assistant_turn_id)
            tier = "microtheory" if d else None
            if d is None:
                d = self._tier_user_store_lookup(claim, assistant_turn_id)
                tier = "user_store" if d else tier
            if d is None:
                d = self._tier_cache_lookup(claim, assistant_turn_id)
                tier = "cache" if d else tier
            if d is None:
                fresh_indices.append(i)
            else:
                d.served_from_tier = tier
                decisions[i] = d

        # ---- Phase 2: parallel Tier 4 (LLM-bound) --------------------
        # Single-claim turns skip the pool overhead. Larger turns get a
        # bounded worker pool sized to the claim count (capped at 8 to
        # avoid hammering the upstream APIs on pathological turns).
        if fresh_indices:
            def _run_fresh(idx: int) -> Decision:
                claim = valid_facts[idx]
                self._cache_gate.classify(claim, turn_id=assistant_turn_id)
                d = self.router.route(
                    claim, origin="model", source_turn_id=assistant_turn_id,
                )
                self._cache_gate.maybe_write(
                    d, claim, turn_id=assistant_turn_id,
                )
                d.served_from_tier = "fresh"
                return d

            if len(fresh_indices) == 1:
                idx = fresh_indices[0]
                decisions[idx] = _run_fresh(idx)
            else:
                workers = min(len(fresh_indices), 8)
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for idx, dec in zip(
                        fresh_indices,
                        pool.map(_run_fresh, fresh_indices),
                    ):
                        decisions[idx] = dec

        verification_decisions: list[Decision] = [d for d in decisions if d is not None]

        self.store.insert_pipeline_event(
            assistant_turn_id, "verification",
            {"decisions": [d.to_dict() for d in verification_decisions]},
        )
        return verification_decisions

    # ---- v0.7.14 tier helpers ------------------------------------------

    def _tier_microtheory_lookup(
        self, claim: dict, turn_id: int,
    ) -> Decision | None:
        """Tier 1: this conversation's session-scoped user assertions.
        Returns a Decision if a session-scoped user fact matches the
        claim's identity, or None on miss. Logs a tier_lookup event
        either way so the trace shows the precedence walk."""
        match = self._find_matching_user_fact(claim, session_id=self.session_id)
        if match is None:
            return None
        return self._fact_to_decision(claim, match, tier="microtheory",
                                      turn_id=turn_id)

    def _tier_user_store_lookup(
        self, claim: dict, turn_id: int,
    ) -> Decision | None:
        """Tier 2: cross-session user assertions (the original user
        store). Returns a Decision if a NULL-session-id user fact
        matches, or None on miss."""
        match = self._find_matching_user_fact(claim, session_id=None)
        if match is None:
            return None
        return self._fact_to_decision(claim, match, tier="user_store",
                                      turn_id=turn_id)

    def _tier_cache_lookup(
        self, claim: dict, turn_id: int,
    ) -> Decision | None:
        """Tier 3: verification cache. Looks up regardless of whether
        scoping/stability classified this claim THIS turn — a hit means
        the claim was previously classified eligible. On hit, the
        router builds the cache-hit Decision (with cache-as-evidence
        flow-through + earned-trust-curve confidence).
        """
        from src.router.constants import KEY_SLOTS_BY_PATTERN
        identity_slots = KEY_SLOTS_BY_PATTERN.get(claim.get("pattern", ""), [])
        hit = self._cache_gate.maybe_hit(
            claim, identity_slots, turn_id=turn_id, require_eligible=False,
        )
        if hit is None:
            return None
        # Reuse the router's cache-hit Decision builder so the same
        # earned-trust + evidence-flow-through logic runs.
        return self.router._build_cache_hit_decision(claim, hit, turn_id)

    def _find_matching_user_fact(
        self, claim: dict, *, session_id,
    ) -> "Fact | None":
        """Look up a currently-valid user-asserted fact whose pattern
        + identity-slot values match the claim. Polarity must match
        too — opposite-polarity is a contradiction, not a tier hit
        (those go through fresh verification so the corrector handles
        them with the existing intervention semantics)."""
        from src.router.constants import KEY_SLOTS_BY_PATTERN
        pattern = claim.get("pattern", "")
        identity_slot_names = KEY_SLOTS_BY_PATTERN.get(pattern, [])
        slots = claim.get("slots") or {}
        slot_match = {k: slots[k] for k in identity_slot_names if k in slots}
        if not slot_match:
            return None
        try:
            matches = self.store.find_currently_valid(
                pattern,
                predicate=claim.get("predicate"),
                slot_match=slot_match,
                polarity=int(claim.get("polarity", 1)),
                user_id=self.user_id,
                session_id=session_id,
            )
        except Exception:
            return None
        # Restrict to user-asserted only (model-asserted facts have
        # their own paths via the cache + boost mechanisms).
        user_asserted = [f for f in matches if f.asserted_by == "user"]
        return user_asserted[-1] if user_asserted else None

    def _fact_to_decision(
        self, claim: dict, fact: "Fact", *, tier: str, turn_id: int,
    ) -> Decision:
        """Build a Decision from a matched user-asserted fact (tier 1
        or tier 2 hit). Boosts the matched fact's confidence so
        repeated hits accumulate trust on the source row."""
        from src.router.constants import (
            CONF_STORE_VERIFIED, confidence_with_reinforcement,
        )
        # Reinforce the underlying fact and use the boosted value as
        # the Decision's confidence — the user's reinforcement vision
        # extended to the tiered path.
        try:
            new_conf = self.store.boost_confidence(
                fact.id, base_for_curve=CONF_STORE_VERIFIED,
            )
        except Exception:
            new_conf = float(fact.confidence)
        # Telemetry.
        try:
            self.store.insert_pipeline_event(turn_id, "tier_lookup", {
                "tier": tier,
                "matched_fact_id": fact.id,
                "matched_session_id": fact.session_id,
                "claim_pattern": claim.get("pattern"),
                "claim_predicate": claim.get("predicate"),
            })
        except Exception:
            pass
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.VERIFIED,
            verification_status="verified",
            confidence=new_conf,
            boosted_fact_id=fact.id,
            matching_fact_id=fact.id,
            notes=[
                f"served from {tier} (matched user fact id={fact.id}, "
                f"reinforcement_count={fact.reinforcement_count + 1})"
            ],
        )

    def _stage_anomaly_and_failure_events(
        self, verification_decisions, assistant_turn_id,
    ):
        """Stage 7a + 7a': emit routing-anomaly and verifier-failure
        events as separate prominent records. The corrector deliberately
        skips these — both classes of failure aren't evidence of
        uncertainty about the claim, so hedging would be wrong."""
        for d in verification_decisions:
            if d.outcome is RoutingOutcome.ROUTING_ANOMALY:
                slot_info = d.anomaly_slot or {}
                self.store.insert_pipeline_event(
                    assistant_turn_id, "routing_anomaly_detected",
                    {
                        "claim": d.claim,
                        "stored_fact_id": d.stored_fact_id,
                        "anomaly_slot": slot_info,
                        "warning": (
                            "pattern "
                            f"{d.claim.get('pattern')!r} expects slot "
                            f"{slot_info.get('slot')!r} = "
                            f"{slot_info.get('expected')!r} for the user-"
                            f"authoritative branch, but got "
                            f"{slot_info.get('actual')!r}; this almost always "
                            "indicates an extractor error rather than a wrong fact"
                        ),
                        "notes": d.notes,
                    },
                )
            if d.verification_status == "retrieval_failed":
                self.store.insert_pipeline_event(
                    assistant_turn_id, "verifier_failure",
                    {
                        "claim": d.claim,
                        "stored_fact_id": d.stored_fact_id,
                        "warning": (
                            "the retrieval verifier didn't produce useful signal "
                            "(network error, no results, or judge couldn't parse); "
                            "the corrector will NOT hedge this claim — verifier "
                            "failure is not evidence of uncertainty"
                        ),
                        "retrieval_result": (
                            d.retrieval_result.to_dict()
                            if d.retrieval_result else None
                        ),
                        "notes": d.notes,
                    },
                )

    def _stage_correct(self, draft, verification_decisions, assistant_turn_id):
        """Stage 7b: plan + apply interventions. Returns
        (final_content, original_content, interventions). When no
        interventions or the rewrite is identical to the draft,
        original_content stays None."""
        interventions: list[Intervention] = self.corrector.plan_interventions(
            verification_decisions
        )
        original_content: str | None = None
        final_content = draft
        if interventions:
            rewritten = self.corrector.apply(draft, interventions)
            if rewritten and rewritten != draft:
                final_content = rewritten
                original_content = draft
                self.store.update_turn_content(
                    assistant_turn_id, final_content, original_content=draft,
                )
            self.store.insert_pipeline_event(
                assistant_turn_id, "correction",
                {
                    "original": draft,
                    "corrected": final_content,
                    "interventions": [i.to_dict() for i in interventions],
                },
            )
        return final_content, original_content, interventions

    def _stage_finalize(self, final_content, assistant_turn_id):
        """Emit the ``final`` event + drain cost + cache-savings
        telemetry. Aggregator failures never break the turn — pure
        observability."""
        self.store.insert_pipeline_event(
            assistant_turn_id, "final", {"content": final_content}
        )
        try:
            calls = self.llm.pop_recorded_calls() if hasattr(
                self.llm, "pop_recorded_calls",
            ) else []
            if calls:
                from src.cost import aggregate_costs
                self.store.insert_pipeline_event(
                    assistant_turn_id, "turn_cost",
                    aggregate_costs(calls),
                )
        except Exception:
            pass
        # v0.7.12 — surface what the cache saved this turn alongside
        # turn_cost. Pure telemetry; never raises.
        try:
            gate = getattr(self, "_cache_gate", None)
            if gate is not None and hasattr(gate, "turn_savings"):
                savings = gate.turn_savings()
                if savings.get("hits", 0) > 0:
                    self.store.insert_pipeline_event(
                        assistant_turn_id, "cache_savings", savings,
                    )
        except Exception:
            pass

    def _emit_substitution_warnings(self, extraction, turn_id, *, extra):
        """Common helper for stages 2 + 5: emit one
        extractor_substitution_warning event per warned fact."""
        for w in extraction.warnings:
            fact_index = w.get("fact_index")
            if fact_index is None or fact_index >= len(extraction.valid_facts):
                fact = None
            else:
                fact = extraction.valid_facts[fact_index]
            self.store.insert_pipeline_event(
                turn_id, "extractor_substitution_warning",
                {"warning": w, "fact": fact, **extra},
            )

    # ---- internal helpers -----------------------------------------------

    # Conversational chat responses are short by nature; 1024 tokens is
    # ample. Override via AEDOS_CHAT_MAX_TOKENS for unusually long
    # prompts (the v0.7.15 GLM-removal pass eliminated the per-backend
    # split that previously needed a higher cap for GLM's
    # reasoning_content tokens).
    CHAT_MAX_TOKENS = int(os.getenv("AEDOS_CHAT_MAX_TOKENS", "1024"))

    def _max_tokens_for_chat(self, backend: Any | None = None) -> int:
        return self.CHAT_MAX_TOKENS

    def _invoke_chat_backend(
        self, system_prompt: str, history: list[ChatMessage], turn_id: int
    ) -> str:
        """Call the configured chat backend. Backends that expose a
        ``provider`` attribute (the AnthropicChatBackend wrapper does;
        LLMClient and MockLLM don't) get provenance + cost-recorder
        kwargs so the chat_model_call event lands without forcing
        test doubles to grow new arguments.

        v0.9.0: when ``self.live_emit`` is set AND the backend accepts
        ``on_token``, the call streams: each text fragment fires a
        ``chat_draft_token`` live event with the cumulative text so far.
        These events are NOT persisted to pipeline_events (one DB row
        per token would be wasteful); they go through the live emit
        channel to the SSE consumer only.
        """
        backend = self.chat_backend
        if hasattr(backend, "provider"):
            kwargs: dict[str, Any] = {
                "max_tokens": self._max_tokens_for_chat(backend),
                "store": self.store,
                "turn_id": turn_id,
            }
            recorder = getattr(self.llm, "record_external_call", None)
            if recorder is not None:
                kwargs["cost_recorder"] = recorder
            # Streaming on_token wiring: only when an SSE consumer is
            # attached AND the backend accepts the kwarg. Buffer
            # cumulatively so the UI can render the in-progress draft
            # without rebuilding from deltas client-side.
            if self.live_emit is not None:
                buf: list[str] = []
                emit = self.live_emit
                def _on_token(delta: str) -> None:
                    buf.append(delta)
                    try:
                        emit(turn_id, "chat_draft_token",
                             {"text": "".join(buf)})
                    except Exception:
                        pass
                kwargs["on_token"] = _on_token
            try:
                return backend.chat(system_prompt, history, **kwargs)
            except TypeError:
                # Backend may not accept cost_recorder/on_token yet
                # (older versions or stubs in tests). Retry without
                # the optional kwargs.
                kwargs.pop("cost_recorder", None)
                kwargs.pop("on_token", None)
                return backend.chat(system_prompt, history, **kwargs)
        return backend.chat(system_prompt, history)

    def _build_chat_system_prompt(self, user_facts: list[Fact]) -> str:
        if not user_facts:
            facts_block = "(the user has not yet stated any facts about themselves)"
        else:
            lines = []
            for f in user_facts:
                pol = "+" if f.polarity == 1 else "-"
                slot_str = ", ".join(f"{k}={v!r}" for k, v in f.slots.items())
                lines.append(
                    f"- [{f.pattern}] {f.predicate}({slot_str}) "
                    f"[polarity={pol}, confidence={f.confidence:.2f}]"
                )
            facts_block = "\n".join(lines)
        return CHAT_SYSTEM_TEMPLATE.format(facts_block=facts_block)

    def _build_chat_history(self) -> list[ChatMessage]:
        """Every past turn for this user, in order, in the shape the LLM
        expects. Cross-user turns are excluded."""
        msgs: list[ChatMessage] = []
        for t in self.store.list_turns(user_id=self.user_id):
            msgs.append(ChatMessage(role=t["role"], content=t["content"]))
        return msgs


def build_pipeline(
    db_path: str,
    *,
    llm: LLMClient | None = None,
    registry: PatternRegistry | None = None,
    chat_backend: Any | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> Pipeline:
    """Convenience constructor used by app.py and integration tests.

    Selects the chat backend (the model under test for hallucination
    catching) by AEDOS_CHAT_MODEL_PROVIDER. Everything else stays on the
    Anthropic ``LLMClient``.

    ``user_id`` scopes the conversation. The default ``default_user``
    works for solo dogfooding. Future multi-user deployments thread per
    request.
    """
    from src.llm_clients import build_chat_backend
    from src.pattern_registry import load_default_registry
    from src.verifiers.code_generation import CodeGenerationVerifier
    from src.verifiers.retrieval_verifier import RetrievalVerifier

    store = FactStore(db_path)
    registry = registry or load_default_registry()
    llm = llm or LLMClient()
    extractor = ClaimExtractor(llm, registry)
    retrieval_verifier = RetrievalVerifier(store=store, llm=llm, registry=registry)
    code_gen_verifier = CodeGenerationVerifier(store=store, llm=llm)
    router = Router(
        store, registry,
        llm=llm,
        retrieval_verifier=retrieval_verifier,
        code_gen_verifier=code_gen_verifier,
        user_id=user_id,
    )
    corrector = Corrector(llm)
    chat_backend = chat_backend if chat_backend is not None else build_chat_backend(llm=llm)

    # v0.6 Tier 2 verification cache — always on. Scoping classifier
    # decides per-claim eligibility (only world_fact is cached); the
    # stability classifier picks the TTL; cache writes fill the cache
    # after every successful retrieval verdict; cache reads
    # short-circuit retrieval on hit.
    #
    # The cache should always be built up over time so the pipeline
    # gets faster the more it runs. The earlier AEDOS_CACHE_*
    # opt-in env vars are gone — callers that want a no-cache pipeline
    # for testing can construct Pipeline directly with
    # scoping_classifier=None / stability_classifier=None /
    # verification_cache=None.
    from src.cache import (
        VerificationCache, classify_scope, classify_stability,
    )
    from src.cache.classify_combined import classify_for_cache
    scoping_classifier = lambda claim, _llm=llm: classify_scope(claim, _llm)
    stability_classifier = (
        lambda claim, _llm=llm: classify_stability(claim, _llm)
    )
    # v0.7.16: combined classifier preferred over the two-call path.
    # CacheGate dispatches on this when wired; the per-call functions
    # remain wired as fallbacks (still used by tests + the historical-
    # period shortcut path).
    combined_cache_classifier = (
        lambda claim, _llm=llm: classify_for_cache(claim, _llm)
    )
    verification_cache = VerificationCache(store)

    p = Pipeline(
        store, registry, llm, extractor, router, corrector,
        chat_backend=chat_backend, user_id=user_id,
        scoping_classifier=scoping_classifier,
        stability_classifier=stability_classifier,
        combined_cache_classifier=combined_cache_classifier,
        verification_cache=verification_cache,
    )
    return p
