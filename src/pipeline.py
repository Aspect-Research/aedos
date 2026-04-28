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

from dataclasses import dataclass
from typing import Any

from src.corrector import Corrector, Intervention
from src.extractor import ClaimExtractor
from src.fact_store import Fact, FactStore
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
    ):
        self.store = store
        self.registry = registry
        self.llm = llm
        self.extractor = extractor
        self.router = router
        self.corrector = corrector
        # When no explicit backend is provided, use ``llm`` directly. This
        # preserves the long-standing test contract where MockLLM provides
        # ``chat(system, messages, max_tokens=...)`` and is passed in as
        # the llm.
        self.chat_backend = chat_backend if chat_backend is not None else llm

    def run_turn(self, user_message: str) -> TurnTrace:
        # Stage 1 — log the user turn.
        user_turn_id = self.store.insert_turn("user", user_message)

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
        system_prompt = self._build_chat_system_prompt(self.store.all_user_facts())
        history = self._build_chat_history()
        assistant_turn_id = self.store.insert_turn("assistant", "")
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

        # Stage 6 — route each assistant claim through the appropriate verifier.
        verification_decisions: list[Decision] = [
            self.router.route(c, origin="model", source_turn_id=assistant_turn_id)
            for c in asst_extraction.valid_facts
        ]
        self.store.insert_pipeline_event(
            assistant_turn_id,
            "verification",
            {"decisions": [d.to_dict() for d in verification_decisions]},
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
                max_tokens=4096, store=self.store, turn_id=turn_id,
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
        """Every past turn, in order, in the shape the LLM expects."""
        msgs: list[ChatMessage] = []
        for t in self.store.list_turns():
            msgs.append(ChatMessage(role=t["role"], content=t["content"]))
        return msgs


def build_pipeline(
    db_path: str,
    *,
    llm: LLMClient | None = None,
    registry: PatternRegistry | None = None,
    chat_backend: Any | None = None,
) -> Pipeline:
    """Convenience constructor used by app.py and integration tests.

    Selects the chat backend (the model under test for hallucination
    catching) by AEDOS_CHAT_MODEL_PROVIDER. Everything else stays on the
    Anthropic ``LLMClient``.
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
    )
    corrector = Corrector(llm)
    chat_backend = chat_backend if chat_backend is not None else build_chat_backend(llm=llm)
    return Pipeline(
        store, registry, llm, extractor, router, corrector,
        chat_backend=chat_backend,
    )
