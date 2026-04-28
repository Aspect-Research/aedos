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
        # Lazy default — only construct the LLM-backed classifier when
        # the caller didn't pass one and didn't pass a MockLLM (test
        # doubles can opt in by passing scoping_classifier=fn).
        self._scoping_classifier = scoping_classifier
        self._stability_classifier = stability_classifier
        self._verification_cache = verification_cache
        # Per-claim scope/stability decisions, populated in stage 5b
        # and consumed in stage 7. Reset each turn (one entry per
        # canonical_key encountered this turn).
        self._cache_decisions: dict[str, dict[str, Any]] = {}
        # When no explicit backend is provided, use ``llm`` directly. This
        # preserves the long-standing test contract where MockLLM provides
        # ``chat(system, messages, max_tokens=...)`` and is passed in as
        # the llm.
        self.chat_backend = chat_backend if chat_backend is not None else llm

    def run_turn(self, user_message: str) -> TurnTrace:
        # Stage 1 — log the user turn.
        user_turn_id = self.store.insert_turn(
            "user", user_message, user_id=self.user_id,
        )

        # Stage 2 — extract claims from the user message.
        user_extraction = self.extractor.extract(user_message, role="user")
        self.store.insert_pipeline_event(
            user_turn_id, "user_extraction", user_extraction.to_dict()
        )

        # Stage 3 — route each user claim (store / boost / close-and-reopen).
        user_decisions: list[Decision] = [
            self.router.route(c, origin="user", source_turn_id=user_turn_id)
            for c in user_extraction.valid_facts
        ]
        self.store.insert_pipeline_event(
            user_turn_id,
            "user_storage",
            {"decisions": [d.to_dict() for d in user_decisions]},
        )

        # Stage 4 — generate the assistant draft with ground-truth context.
        # The chat backend is the configurable seam: AEDOS_CHAT_MODEL_PROVIDER
        # selects Anthropic (Claude) or Modal (GLM-5.1-FP8). The assistant
        # turn must exist before the call so a chat_model_call event has a
        # turn_id to attach to even if the backend raises.
        system_prompt = self._build_chat_system_prompt(
            self.store.all_user_facts(user_id=self.user_id),
        )
        history = self._build_chat_history()
        assistant_turn_id = self.store.insert_turn(
            "assistant", "", user_id=self.user_id,
        )
        draft = self._invoke_chat_backend(system_prompt, history, assistant_turn_id)
        self.store.update_turn_content(
            assistant_turn_id, draft, original_content=None
        )
        self.store.insert_pipeline_event(
            assistant_turn_id, "assistant_draft", {"content": draft}
        )

        # Stage 5 — extract claims from the assistant draft.
        # The user's preceding message is passed as context so the
        # extractor can resolve self-references like "this sentence" to
        # the literal text and embed it as a slot value. Without this,
        # claims like "this sentence has 7 words with 'e'" lose the
        # actual sentence and the verification pipeline can't compute
        # an answer.
        asst_extraction = self.extractor.extract(
            draft, role="assistant", context=user_message,
        )
        self.store.insert_pipeline_event(
            assistant_turn_id,
            "assistant_extraction",
            asst_extraction.to_dict(),
        )

        # Reset per-turn cache state.
        self._cache_decisions = {}

        # Stage 5b — (v0.6, observation mode) classify each assistant
        # claim's cache scope. Logs only — does not gate caching or
        # routing. After two sessions of these logs we calibrate, then
        # wire to actual cache writes. Failures here MUST NOT break the
        # pipeline; observation mode is opt-in instrumentation.
        if self._scoping_classifier is not None:
            from src.cache import canonicalize_claim_key
            for claim in asst_extraction.valid_facts:
                claim_summary = {
                    "pattern": claim.get("pattern"),
                    "predicate": claim.get("predicate"),
                    "slots": claim.get("slots"),
                    "polarity": claim.get("polarity"),
                }
                try:
                    scope_decision = self._scoping_classifier(claim)
                    self.store.insert_pipeline_event(
                        assistant_turn_id, "cache_scoping_decision",
                        {"claim": claim_summary,
                         "decision": scope_decision.to_dict()},
                    )
                except Exception as exc:  # noqa: BLE001
                    self.store.insert_pipeline_event(
                        assistant_turn_id, "cache_scoping_decision",
                        {"claim": claim_summary,
                         "error": f"{type(exc).__name__}: {exc}"},
                    )
                    continue

                # Stash for cache-write step. Only world_fact entries are
                # cache-eligible — store nothing for the others.
                if scope_decision.scope != "world_fact":
                    continue

                key = canonicalize_claim_key(claim)
                self._cache_decisions[key] = {
                    "scope": scope_decision.to_dict(),
                    "claim_summary": claim_summary,
                }

                # Stability runs only for cache-eligible (world_fact) claims.
                if self._stability_classifier is not None:
                    try:
                        stab_decision = self._stability_classifier(claim)
                        self.store.insert_pipeline_event(
                            assistant_turn_id, "cache_stability_decision",
                            {"claim": claim_summary,
                             "decision": stab_decision.to_dict()},
                        )
                        self._cache_decisions[key]["stability"] = (
                            stab_decision.to_dict()
                        )
                    except Exception as exc:  # noqa: BLE001
                        self.store.insert_pipeline_event(
                            assistant_turn_id, "cache_stability_decision",
                            {"claim": claim_summary,
                             "error": f"{type(exc).__name__}: {exc}"},
                        )

        # Stage 6 — route each assistant claim through the appropriate verifier.
        # Thread the per-turn cache eligibility (set of canonical_keys
        # the scoping pass marked world_fact + non-volatile) into the
        # router. The router uses this to gate cache lookups.
        self.router._cache_eligible_keys = set(self._cache_decisions.keys())
        if self._verification_cache is not None:
            self.router._verification_cache = self._verification_cache
        verification_decisions: list[Decision] = [
            self.router.route(c, origin="model", source_turn_id=assistant_turn_id)
            for c in asst_extraction.valid_facts
        ]
        self.store.insert_pipeline_event(
            assistant_turn_id,
            "verification",
            {"decisions": [d.to_dict() for d in verification_decisions]},
        )

        # Stage 6b — (v0.6) opportunistic cache writes.
        # For each claim that (a) was scope=world_fact, (b) had a
        # stability decision, (c) verified or contradicted via the
        # retrieval path, write the verdict to the cache. Skip claims
        # whose stability is volatile (TTL=0) — caller-side gate.
        if self._verification_cache is not None and self._cache_decisions:
            self._maybe_write_cache(
                verification_decisions, assistant_turn_id,
            )

        # Stage 7a — log routing anomalies as their own prominent event.
        # These signal extractor errors and don't trigger a content-level
        # rewrite (the corrector skips them).
        routing_anomaly_decisions = [
            d for d in verification_decisions
            if d.outcome is RoutingOutcome.ROUTING_ANOMALY
        ]
        for d in routing_anomaly_decisions:
            slot_info = d.anomaly_slot or {}
            self.store.insert_pipeline_event(
                assistant_turn_id,
                "routing_anomaly_detected",
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

        # Stage 7a' — log verifier failures (retrieval_failed) as their own
        # warning event. The corrector deliberately does NOT hedge these,
        # because a verifier failure is not evidence of uncertainty about
        # the claim.
        verifier_failures = [
            d for d in verification_decisions
            if d.verification_status == "retrieval_failed"
        ]
        for d in verifier_failures:
            self.store.insert_pipeline_event(
                assistant_turn_id,
                "verifier_failure",
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
                        d.retrieval_result.to_dict() if d.retrieval_result else None
                    ),
                    "notes": d.notes,
                },
            )

        # Stage 7b — plan and apply interventions on the assistant draft.
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
                    assistant_turn_id, final_content, original_content=draft
                )
            self.store.insert_pipeline_event(
                assistant_turn_id,
                "correction",
                {
                    "original": draft,
                    "corrected": final_content,
                    "interventions": [i.to_dict() for i in interventions],
                },
            )

        self.store.insert_pipeline_event(
            assistant_turn_id, "final", {"content": final_content}
        )

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
            routing_anomalies=[d.to_dict() for d in routing_anomaly_decisions],
        )

    # ---- internal helpers -----------------------------------------------

    # Conversational chat responses are short by nature; 1024 tokens is
    # ample. The earlier 4096 default mattered against reasoning models
    # like GLM-5.1: a high cap let the model spend tokens on a long
    # ``reasoning_content`` chain before the user-visible content,
    # blowing past Modal's 300s timeout on cold starts. Two of two
    # cold-starts in the Phase-2 dogfood timed out for this reason.
    # Lowering the cap to 1024 caps the reasoning chain too, which is
    # the right knob for AEDOS's chat use case.
    CHAT_MAX_TOKENS = 1024

    def _maybe_write_cache(
        self, verification_decisions: list[Decision], assistant_turn_id: int,
    ) -> None:
        """For each verified/contradicted retrieval claim that was
        scoped world_fact + stability-classified, write the verdict
        to the cache. Failures don't propagate."""
        from src.cache import canonicalize_claim_key
        for decision in verification_decisions:
            claim = decision.claim
            key = canonicalize_claim_key(claim)
            entry = self._cache_decisions.get(key)
            if entry is None:
                continue  # not cache-eligible per scoping
            stab = entry.get("stability")
            if stab is None:
                continue  # stability classifier didn't run / errored
            ttl = stab.get("ttl_seconds")
            if ttl == 0:
                continue  # volatile — don't cache
            verdict = decision.verification_status
            # Cache only verdicts produced by the retrieval verifier.
            # Python verifications are cheap to redo; user-authoritative
            # is per-user; routing anomalies are already broken.
            retrieval_verdicts = {
                "verified", "contradicted",
                "retrieval_inconclusive",
            }
            if verdict not in retrieval_verdicts:
                continue
            # Skip if the verdict came from the python (code-gen) path —
            # cache_gen_result is set for python verifications.
            if decision.code_gen_result is not None:
                continue
            evidence = None
            if decision.retrieval_result is not None:
                evidence = decision.retrieval_result.to_dict()
            try:
                self._verification_cache.write(
                    canonical_key=key,
                    pattern=claim.get("pattern", ""),
                    predicate=claim.get("predicate", ""),
                    verdict=verdict,
                    stability_class=stab.get("stability_class", "unknown"),
                    ttl_seconds=ttl,
                    evidence=evidence,
                )
                self.store.insert_pipeline_event(
                    assistant_turn_id, "cache_write",
                    {"canonical_key": key, "verdict": verdict,
                     "stability_class": stab.get("stability_class"),
                     "ttl_seconds": ttl},
                )
            except Exception as exc:  # noqa: BLE001
                self.store.insert_pipeline_event(
                    assistant_turn_id, "cache_write",
                    {"canonical_key": key,
                     "error": f"{type(exc).__name__}: {exc}"},
                )

    def _invoke_chat_backend(
        self, system_prompt: str, history: list[ChatMessage], turn_id: int
    ) -> str:
        """Call ``self.chat_backend.chat`` with provenance kwargs when the
        backend exposes a ``provider`` attribute (the new backends do;
        LLMClient and MockLLM don't). This routes the chat_model_call
        event without forcing the test doubles to grow new arguments."""
        if hasattr(self.chat_backend, "provider"):
            return self.chat_backend.chat(
                system_prompt, history,
                max_tokens=self.CHAT_MAX_TOKENS,
                store=self.store, turn_id=turn_id,
            )
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

    # v0.6 Phase 6 — scoping + stability classifiers in observation
    # mode. Off by default to avoid burning the extra LLM calls on
    # every turn while the pipeline is settling. AEDOS_CACHE_SCOPING=1
    # turns on scoping; AEDOS_CACHE_STABILITY=1 turns on stability
    # (only fires for claims scoping marked world_fact). Stability
    # without scoping is meaningless, so we require both.
    scoping_classifier = None
    stability_classifier = None
    verification_cache = None
    if os.getenv("AEDOS_CACHE_SCOPING") == "1":
        from src.cache import classify_scope
        scoping_classifier = lambda claim, _llm=llm: classify_scope(claim, _llm)
        if os.getenv("AEDOS_CACHE_STABILITY") == "1":
            from src.cache import classify_stability
            stability_classifier = (
                lambda claim, _llm=llm: classify_stability(claim, _llm)
            )
            # Cache writes need both scoping and stability to know what
            # to write (key) and when it expires (TTL).
            if os.getenv("AEDOS_CACHE_WRITES") == "1":
                from src.cache import VerificationCache
                verification_cache = VerificationCache(store)

    return Pipeline(
        store, registry, llm, extractor, router, corrector,
        chat_backend=chat_backend, user_id=user_id,
        scoping_classifier=scoping_classifier,
        stability_classifier=stability_classifier,
        verification_cache=verification_cache,
    )
