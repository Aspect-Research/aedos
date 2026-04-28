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
from dataclasses import dataclass
from typing import Any

from src.corrector import Corrector, Intervention
from src.extractor import ClaimExtractor
from src.fact_store import DEFAULT_USER_ID, Fact, FactStore
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
        # CacheGate consolidates scoping + stability + lookup + write.
        # Pre-refactor these were three separate fields scattered
        # across pipeline + router; now one owner.
        from src.cache import CacheGate
        self._cache_gate = CacheGate(
            cache=verification_cache,
            scoping_fn=scoping_classifier,
            stability_fn=stability_classifier,
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
        # Optional Modal/GLM backend for the per-turn ``model='glm-5.1'``
        # selection path. Lazily constructed by build_pipeline if
        # MODAL_API_KEY is present; absent here when not configured.
        self._modal_backend: Any | None = None
        # Active per-turn model override; set by run_turn when a model
        # parameter is supplied. Used by _invoke_chat_backend to dispatch
        # to the right chat backend without mutating self.chat_backend.
        self._active_chat_model: str | None = None

    def run_turn(
        self, user_message: str, *, model: str | None = None,
    ) -> TurnTrace:
        """Run one user→assistant turn.

        ``model`` (optional) makes EVERY internal LLM call (chat,
        extractor, router, judge, corrector, scoping, stability,
        code-gen) use the named model. ``glm-5.1`` routes only the
        chat call to Modal — internal calls keep their prior Anthropic
        model since GLM doesn't support tool use. ``None`` uses the
        pipeline's defaults (the model attrs on ``self.llm`` and the
        chat backend supplied at construction time)."""
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
        """Stages 5b + 6 + 6b: cache classify, route+verify each
        claim, opportunistic cache writes."""
        # Reset per-turn cache state and classify each claim.
        self._cache_gate.reset_for_turn()
        for claim in valid_facts:
            self._cache_gate.classify(claim, turn_id=assistant_turn_id)
        # Hand the gate to the router so it can short-circuit on hits.
        self.router._cache_gate = self._cache_gate
        verification_decisions: list[Decision] = [
            self.router.route(c, origin="model", source_turn_id=assistant_turn_id)
            for c in valid_facts
        ]
        self.store.insert_pipeline_event(
            assistant_turn_id, "verification",
            {"decisions": [d.to_dict() for d in verification_decisions]},
        )
        # Opportunistic cache writes from successful retrievals.
        for d, claim in zip(verification_decisions, valid_facts):
            self._cache_gate.maybe_write(d, claim, turn_id=assistant_turn_id)
        return verification_decisions

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
        """Emit the ``final`` event + drain cost telemetry. Cost
        aggregator failures never break the turn — pure observability."""
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
    # ample for non-reasoning chat models. The earlier 4096 default
    # mattered against reasoning models like GLM-5.1: a high cap let
    # the model spend tokens on a long ``reasoning_content`` chain
    # before the user-visible content, blowing past Modal's 300s
    # timeout on cold starts. Two of two cold-starts in the Phase-2
    # dogfood timed out for this reason.
    #
    # But 1024 is too tight for reasoning models on hard prompts:
    # turn 4 of the hallucination corpus (spell
    # ``floccinaucinihilipilification`` backwards) returned
    # ``content=null`` because GLM spent the full 1024 tokens inside
    # the reasoning chain. Reasoning models need more headroom because
    # the cap counts reasoning + output, not output alone.
    #
    # The fix: per-backend defaults.
    #
    #   * Anthropic chat (no reasoning_content): 1024 — short answers.
    #   * Modal/GLM (reasoning_content burns tokens): 4096 — leaves
    #     ~3K for the answer after a typical chain.
    #
    # Both can be overridden via AEDOS_CHAT_MAX_TOKENS_ANTHROPIC and
    # AEDOS_CHAT_MAX_TOKENS_MODAL respectively. The global
    # AEDOS_CHAT_MAX_TOKENS still wins if set (backward compat).
    CHAT_MAX_TOKENS_ANTHROPIC = int(
        os.getenv("AEDOS_CHAT_MAX_TOKENS_ANTHROPIC", "1024")
    )
    CHAT_MAX_TOKENS_MODAL = int(
        os.getenv("AEDOS_CHAT_MAX_TOKENS_MODAL", "4096")
    )
    CHAT_MAX_TOKENS_DEFAULT = 1024
    CHAT_MAX_TOKENS_GLOBAL_OVERRIDE = (
        int(os.getenv("AEDOS_CHAT_MAX_TOKENS"))
        if os.getenv("AEDOS_CHAT_MAX_TOKENS") is not None
        else None
    )

    # Backward-compat alias kept for callers (and the existing test)
    # that read the historical single-value attribute. Resolves to the
    # global override if set, else the anthropic default (matching the
    # pre-split behaviour for the Anthropic-by-default deployment).
    CHAT_MAX_TOKENS = (
        CHAT_MAX_TOKENS_GLOBAL_OVERRIDE
        if CHAT_MAX_TOKENS_GLOBAL_OVERRIDE is not None
        else CHAT_MAX_TOKENS_ANTHROPIC
    )

    def _max_tokens_for_chat(self, backend: Any | None = None) -> int:
        """Per-backend cap selection. Global env override wins; else
        provider-specific default; else 1024. Reasoning-content models
        get more headroom because the cap counts reasoning tokens
        too. ``backend`` defaults to the configured chat_backend; the
        caller passes the dispatched backend when a per-turn model
        override (e.g. ``glm-5.1``) routes elsewhere."""
        if self.CHAT_MAX_TOKENS_GLOBAL_OVERRIDE is not None:
            return self.CHAT_MAX_TOKENS_GLOBAL_OVERRIDE
        provider = getattr(backend or self.chat_backend, "provider", None)
        if provider == "modal":
            return self.CHAT_MAX_TOKENS_MODAL
        if provider == "anthropic":
            return self.CHAT_MAX_TOKENS_ANTHROPIC
        return self.CHAT_MAX_TOKENS_DEFAULT

    def _select_chat_backend(self) -> Any:
        """Per-turn dispatch. ``glm-5.1`` → Modal backend (lazily
        constructed); anything else (or ``None``) → the default
        ``self.chat_backend`` whose model attribute already reflects
        the active LLM model from ``with_active_model``.

        Raises if GLM is requested but no Modal backend was built —
        signals a configuration problem (MODAL_API_KEY missing) early
        rather than silently falling back."""
        if self._active_chat_model == "glm-5.1":
            if self._modal_backend is None:
                raise RuntimeError(
                    "model='glm-5.1' was requested but no Modal backend "
                    "is available — set MODAL_API_KEY and rebuild the "
                    "pipeline."
                )
            return self._modal_backend
        return self.chat_backend

    def _invoke_chat_backend(
        self, system_prompt: str, history: list[ChatMessage], turn_id: int
    ) -> str:
        """Call the chat backend selected by ``_select_chat_backend``.
        Backends that expose a ``provider`` attribute (the new ones
        do; LLMClient and MockLLM don't) get provenance + cost-recorder
        kwargs so the chat_model_call event lands without forcing
        test doubles to grow new arguments."""
        backend = self._select_chat_backend()
        if hasattr(backend, "provider"):
            kwargs: dict[str, Any] = {
                "max_tokens": self._max_tokens_for_chat(backend),
                "store": self.store,
                "turn_id": turn_id,
            }
            recorder = getattr(self.llm, "record_external_call", None)
            if recorder is not None:
                kwargs["cost_recorder"] = recorder
            try:
                return backend.chat(system_prompt, history, **kwargs)
            except TypeError:
                # Backend may not accept cost_recorder yet (older
                # versions or stubs in tests). Retry without it.
                kwargs.pop("cost_recorder", None)
                return self.chat_backend.chat(system_prompt, history, **kwargs)
        return self.chat_backend.chat(system_prompt, history)

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

    # Per-turn ``model='glm-5.1'`` selection routes the chat call to a
    # Modal backend. Construct it eagerly when MODAL_API_KEY is set so
    # the pipeline can dispatch without rebuilding state mid-request.
    # Absent → selecting GLM in the UI will surface a configuration
    # error early.
    modal_backend = None
    if os.getenv("MODAL_API_KEY"):
        try:
            from src.llm_clients.modal_glm import ModalGLMBackend
            modal_backend = ModalGLMBackend.from_env()
        except Exception:
            modal_backend = None  # missing key / construct failure → no GLM

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
    scoping_classifier = lambda claim, _llm=llm: classify_scope(claim, _llm)
    stability_classifier = (
        lambda claim, _llm=llm: classify_stability(claim, _llm)
    )
    verification_cache = VerificationCache(store)

    p = Pipeline(
        store, registry, llm, extractor, router, corrector,
        chat_backend=chat_backend, user_id=user_id,
        scoping_classifier=scoping_classifier,
        stability_classifier=stability_classifier,
        verification_cache=verification_cache,
    )
    p._modal_backend = modal_backend
    return p
