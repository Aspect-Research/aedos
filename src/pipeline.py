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
from src.pattern_registry import PatternRegistry as PredicateRegistry  # v0.3 alias
from src.router import Decision, Router, RoutingOutcome


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
        registry: PredicateRegistry,
        llm: LLMClient,
        extractor: ClaimExtractor,
        router: Router,
        corrector: Corrector,
    ):
        self.store = store
        self.registry = registry
        self.llm = llm
        self.extractor = extractor
        self.router = router
        self.corrector = corrector

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
            for c in user_extraction.valid_claims
        ]
        self.store.insert_pipeline_event(
            user_turn_id,
            "user_storage",
            {"decisions": [d.to_dict() for d in user_decisions]},
        )

        # Stage 4 — generate the assistant draft with ground-truth context.
        system_prompt = self._build_chat_system_prompt(self.store.all_user_facts())
        history = self._build_chat_history()
        draft = self.llm.chat(system_prompt, history)

        assistant_turn_id = self.store.insert_turn("assistant", draft)
        self.store.insert_pipeline_event(
            assistant_turn_id, "assistant_draft", {"content": draft}
        )

        # Stage 5 — extract claims from the assistant draft.
        asst_extraction = self.extractor.extract(draft, role="assistant")
        self.store.insert_pipeline_event(
            assistant_turn_id,
            "assistant_extraction",
            asst_extraction.to_dict(),
        )

        # Stage 6 — route each assistant claim through the appropriate verifier.
        verification_decisions: list[Decision] = [
            self.router.route(c, origin="model", source_turn_id=assistant_turn_id)
            for c in asst_extraction.valid_claims
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
            self.store.insert_pipeline_event(
                assistant_turn_id,
                "routing_anomaly_detected",
                {
                    "claim": d.claim,
                    "stored_fact_id": d.stored_fact_id,
                    "warning": (
                        "user-authoritative predicate "
                        f"{d.claim['predicate']!r} was asserted about non-user "
                        f"subject {d.claim['subject']!r}; this almost always "
                        "indicates an extractor error rather than a wrong fact"
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

    def _build_chat_system_prompt(self, user_facts: list[Fact]) -> str:
        if not user_facts:
            facts_block = "(the user has not yet stated any facts about themselves)"
        else:
            lines = []
            for f in user_facts:
                # Emit a compact typed-triple form the model can read directly.
                pol = "+" if f.polarity == 1 else "-"
                lines.append(
                    f"- ({f.subject}, {f.predicate}, {f.object}) "
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
    registry: PredicateRegistry | None = None,
) -> Pipeline:
    """Convenience constructor used by app.py and integration tests."""
    from src.pattern_registry import load_default_registry
    from src.verifiers.retrieval_verifier import RetrievalVerifier

    store = FactStore(db_path)
    registry = registry or load_default_registry()
    llm = llm or LLMClient()
    extractor = ClaimExtractor(llm, registry)
    retrieval_verifier = RetrievalVerifier(store=store, llm=llm, registry=registry)
    router = Router(store, registry, retrieval_verifier=retrieval_verifier)
    corrector = Corrector(llm)
    return Pipeline(store, registry, llm, extractor, router, corrector)
