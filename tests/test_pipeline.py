"""Tests for src.pipeline (v0.14 turn orchestrator).

Single-turn smoke tests with a stub LLMClient. Cover: empty turn (no
claims either side), self-attribute storage path, assistant-claim
walker → intervention → no-correction path, and routing-anomaly
short-circuit. The end-to-end live path is covered by manual UI
smoke; these tests pin the orchestration shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import pytest

from src.fact_store import FactStore
from src.layer1_extraction.extractor import ClaimExtractor
from src.layer1_extraction.pattern_registry import (
    load_default_registry,
    reset_cache,
)
from src.layer2_routing.llm_router import RoutingDecision
from src.layer2_routing.router import Router
from src.layer3_substrate.entity_equivalence import EntityEquivalence
from src.layer3_substrate.entity_taxonomy import EntityTaxonomy
from src.layer3_substrate.predicate_distribution import PredicateDistribution
from src.layer3_substrate.predicate_equivalence import PredicateEquivalence
from src.layer5_decision.corrector import Corrector
from src.llm_client import ChatMessage
from src.pipeline import Pipeline, TurnTrace


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_cache()
    yield
    reset_cache()


@dataclass
class StubLLM:
    """Queues canned responses per call type. Tests pre-load the queues
    in order; raises if a call comes after the queue is empty (catches
    'pipeline made an extra LLM call' bugs)."""

    extracts: list[dict] = field(default_factory=list)
    chats: list[str] = field(default_factory=list)
    rewrites: list[str] = field(default_factory=list)
    routes: list[RoutingDecision] = field(default_factory=list)

    def extract_with_tool(self, system, user_message, tool, max_tokens=2048, **_kw):
        if not self.extracts:
            raise AssertionError(
                f"unexpected extract_with_tool call (queue empty); "
                f"user_message={user_message[:80]!r}"
            )
        return self.extracts.pop(0)

    def chat(self, system, messages: Iterable[ChatMessage],
             max_tokens=4096, purpose="chat"):
        if not self.chats:
            raise AssertionError("unexpected chat call (queue empty)")
        return self.chats.pop(0)

    def chat_stream(self, system, messages: Iterable[ChatMessage],
                    on_token=None, max_tokens=4096, purpose="chat"):
        """Pipeline uses streaming chat in v0.14.1. The stub fires the
        full canned reply through on_token in one delta and returns the
        same string. Tests that need delta-by-delta streaming can
        override on a per-test basis."""
        if not self.chats:
            raise AssertionError("unexpected chat_stream call (queue empty)")
        text = self.chats.pop(0)
        if on_token is not None:
            try:
                on_token(text)
            except Exception:
                pass
        return text

    # Carries the same model attribute the real LLMClient exposes;
    # pipeline reads it to populate chat_model_call's provider/model
    # fields. "claude-haiku-4-5" matches the production default.
    model: str = "claude-haiku-4-5"

    def rewrite(self, system, user_message, purpose="corrector"):
        if not self.rewrites:
            raise AssertionError("unexpected rewrite call (queue empty)")
        return self.rewrites.pop(0)

    # A routing fn for the Router. Returns from self.routes queue.
    def route_fn(self, claim):
        if not self.routes:
            return RoutingDecision(
                method="user_authoritative", reason="default stub",
                python_inputs_self_contained=None,
                retrieval_query_hint=None,
                canonical_constants_needed=None,
            )
        return self.routes.pop(0)


def _build_pipeline(tmp_path, llm: StubLLM) -> Pipeline:
    store = FactStore(str(tmp_path / "pipeline_test.db"))
    registry = load_default_registry()
    extractor = ClaimExtractor(llm, registry)
    router = Router(store, registry, routing_fn=llm.route_fn)
    corrector = Corrector(llm)
    return Pipeline(
        store=store,
        registry=registry,
        llm=llm,
        extractor=extractor,
        router=router,
        corrector=corrector,
        predicate_oracle=PredicateEquivalence(store),
        entity_oracle=EntityEquivalence(store),
        taxonomy_oracle=EntityTaxonomy(store),
        distribution_oracle=PredicateDistribution(store),
    )


# ============================================================================
# 1. Empty turn — no claims either side
# ============================================================================


def test_empty_turn_no_claims_passthrough(tmp_path):
    """User says 'hello'; extractor finds no facts; chat draft is
    'hi'; extractor finds no facts; no walker runs; no correction.
    Verifies the orchestration runs cleanly with empty extractions."""
    llm = StubLLM(
        extracts=[
            {"facts": []},  # user-side
            {"facts": []},  # assistant-side
        ],
        chats=["hi back!"],
    )
    p = _build_pipeline(tmp_path, llm)

    trace = p.run_turn("hello")

    assert isinstance(trace, TurnTrace)
    assert trace.final_content == "hi back!"
    assert trace.original_content is None
    assert trace.user_decisions == []
    assert trace.verification_decisions == []
    assert trace.interventions == []
    assert trace.routing_anomalies == []

    # Pipeline events exist for both turns.
    user_events = p.store.get_pipeline_events(trace.user_turn_id)
    asst_events = p.store.get_pipeline_events(trace.assistant_turn_id)
    user_stages = {e["stage"] for e in user_events}
    asst_stages = {e["stage"] for e in asst_events}
    assert "user_extraction" in user_stages
    assert "assistant_extraction" in asst_stages
    assert "chat_model_call" in asst_stages
    assert "final" in asst_stages


# ============================================================================
# 2. User self-attribute → Tier U store
# ============================================================================


def test_user_self_attribute_stores_in_tier_u(tmp_path):
    """User says 'I like olives'; extractor produces a preference fact;
    Layer 2 routes user_authoritative; tier_u.store_user_fact persists
    a row with asserted_by='user', verification_status='user_asserted'."""
    llm = StubLLM(
        extracts=[
            {"facts": [{
                "pattern": "preference",
                "predicate": "likes",
                "polarity": 1,
                "slots": {"agent": "user", "object": "olives"},
                "source_text": "I like olives",
            }]},
            {"facts": []},  # assistant-side
        ],
        chats=["Got it — you like olives."],
        routes=[
            RoutingDecision(method="user_authoritative", reason="self-attr",
                            python_inputs_self_contained=None,
                            retrieval_query_hint=None,
                            canonical_constants_needed=None),
        ],
    )
    p = _build_pipeline(tmp_path, llm)

    trace = p.run_turn("I like olives")

    assert len(trace.user_decisions) == 1
    ud = trace.user_decisions[0]
    assert ud["is_self_attribute"] is True
    assert ud["is_anomaly"] is False
    assert ud["storage_outcome"] in ("inserted", "reaffirmed", "noop")

    # Fact landed in the store.
    facts = p.store.query_facts(
        pattern="preference", predicate="likes",
        asserted_by="user", only_valid=True, user_id=p.user_id,
    )
    assert len(facts) == 1
    assert facts[0].slots == {"agent": "user", "object": "olives"}
    assert facts[0].verification_status == "user_asserted"


# ============================================================================
# 3. Routing anomaly short-circuits storage
# ============================================================================


def test_routing_anomaly_skips_storage(tmp_path):
    """User says 'Donald Trump likes peanut butter'; the validator's
    USER_SUBJECT_PATTERNS invariant rejects the preference claim with a
    non-user agent. Storage is skipped; routing_anomalies list carries
    the validation payload."""
    llm = StubLLM(
        extracts=[
            {"facts": [{
                "pattern": "preference",
                "predicate": "likes",
                "polarity": 1,
                "slots": {"agent": "Donald Trump", "object": "peanut butter"},
                "source_text": "Donald Trump likes peanut butter",
            }]},
            {"facts": []},
        ],
        chats=["Noted."],
    )
    p = _build_pipeline(tmp_path, llm)

    trace = p.run_turn("Donald Trump likes peanut butter")

    assert len(trace.user_decisions) == 1
    assert trace.user_decisions[0]["is_anomaly"] is True
    assert len(trace.routing_anomalies) == 1
    # No fact was stored.
    facts = p.store.query_facts(
        pattern="preference", predicate="likes",
        only_valid=True, user_id=p.user_id,
    )
    assert facts == []


# ============================================================================
# 4. Assistant claim against stored Tier U → walker MATCH, no correction
# ============================================================================


def test_assistant_claim_matches_tier_u_no_correction(tmp_path):
    """Pre-stored 'user likes olives'. Assistant draft 'you like olives'
    extracts to the same claim. Walker hits Tier U literal match;
    intervention = pass_through; corrector is NOT called."""
    llm = StubLLM(
        extracts=[
            {"facts": []},  # user-side: nothing
            {"facts": [{   # assistant-side: 'you like olives'
                "pattern": "preference",
                "predicate": "likes",
                "polarity": 1,
                "slots": {"agent": "user", "object": "olives"},
                "source_text": "you like olives",
            }]},
        ],
        chats=["You like olives."],
        # rewrites queue intentionally empty: corrector must not fire.
    )
    p = _build_pipeline(tmp_path, llm)

    # Pre-seed Tier U with the matching user fact.
    from src.fact_store import Fact
    p.store.insert_fact(Fact(
        pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"},
        polarity=1, asserted_by="user",
        verification_status="user_asserted",
        confidence=0.5, affirmed_count=1,
        is_session_local=0, session_ids=[],
        user_id=p.user_id,
    ))

    trace = p.run_turn("what do I like?")

    assert len(trace.verification_decisions) == 1
    vd = trace.verification_decisions[0]
    assert vd["walker"]["served_from_tier"] == "u"
    assert vd["walker"]["outcome"] == "match"
    assert vd["intervention"]["intervention_type"] == "pass_through"
    # Final == draft (no correction).
    assert trace.final_content == "You like olives."
    assert trace.original_content is None


# ============================================================================
# 5. Tier U contradiction → corrector fires with replace intervention
# ============================================================================


# ============================================================================
# 6. Aggregate user_storage event always fires (even when 0 user claims)
# ============================================================================


def test_aggregate_user_storage_event_emitted_for_zero_user_claims(tmp_path):
    """Chitchat turn ('how are you?') extracts no user claims. The
    aggregate user_storage event still fires so the UI's User Message
    step transitions out of the "verifying…" placeholder. Same applies
    to the Claims step's aggregate verification event."""
    llm = StubLLM(
        extracts=[
            {"facts": []},  # user-side: nothing
            {"facts": []},  # assistant-side: nothing
        ],
        chats=["I'm doing well, thanks!"],
    )
    p = _build_pipeline(tmp_path, llm)

    trace = p.run_turn("how are you?")

    user_events = p.store.get_pipeline_events(trace.user_turn_id)
    asst_events = p.store.get_pipeline_events(trace.assistant_turn_id)
    user_stages = [e["stage"] for e in user_events]
    asst_stages = [e["stage"] for e in asst_events]

    # user_storage fires once with empty decisions list.
    assert "user_storage" in user_stages
    storage_event = next(e for e in user_events if e["stage"] == "user_storage")
    assert storage_event["data"]["decisions"] == []
    assert storage_event["data"]["n_claims"] == 0

    # verification fires once with empty decisions list.
    assert "verification" in asst_stages
    verification_event = next(e for e in asst_events if e["stage"] == "verification")
    assert verification_event["data"]["decisions"] == []
    assert verification_event["data"]["n_claims"] == 0


# ============================================================================
# 7. chat_model_call payload carries provider + model so the UI doesn't
#    show "?:?" in the Chat Model card.
# ============================================================================


def test_chat_model_call_payload_includes_provider_and_model(tmp_path):
    """The Chat Model card reads chat_model_call.{provider, model,
    response_chars} to render its meta line. v0.14.0 emitted only
    {system_prompt_length, history_messages, draft_length}, leaving
    the UI to display "?:?". v0.14.1 fills the full shape."""
    llm = StubLLM(
        extracts=[{"facts": []}, {"facts": []}],
        chats=["hello there"],
    )
    p = _build_pipeline(tmp_path, llm)

    trace = p.run_turn("hi")

    asst_events = p.store.get_pipeline_events(trace.assistant_turn_id)
    chat_event = next(e for e in asst_events if e["stage"] == "chat_model_call")
    assert chat_event["data"]["provider"] == "anthropic"
    assert chat_event["data"]["model"] == "claude-haiku-4-5"
    assert chat_event["data"]["response_chars"] == len("hello there")


# ============================================================================
# 8. Streaming chat — chat_draft_token broadcasts fire as text arrives
# ============================================================================


def test_streaming_chat_broadcasts_chat_draft_token(tmp_path):
    """Pipeline calls llm.chat_stream with an on_token callback that
    broadcasts chat_draft_token events through FactStore.broadcast_event.
    Subscribers see the cumulative text as it builds. Tokens MUST NOT
    persist to pipeline_events (no chat_draft_token rows in the table —
    streaming would otherwise produce hundreds of rows per turn)."""
    llm = StubLLM(
        extracts=[{"facts": []}, {"facts": []}],
        chats=["streaming reply text"],
    )
    p = _build_pipeline(tmp_path, llm)

    seen_events: list[tuple[int, str, dict]] = []
    p.store.register_event_subscriber(
        lambda turn_id, stage, data: seen_events.append((turn_id, stage, data)),
    )

    trace = p.run_turn("hi")

    # At least one chat_draft_token event was broadcast.
    token_events = [e for e in seen_events if e[1] == "chat_draft_token"]
    assert token_events, "expected at least one chat_draft_token broadcast"
    # Last token's text == final draft.
    assert token_events[-1][2]["text"] == "streaming reply text"

    # chat_draft_token events were NOT inserted into pipeline_events
    # (broadcast_event bypasses persistence by design).
    asst_events = p.store.get_pipeline_events(trace.assistant_turn_id)
    asst_stages = {e["stage"] for e in asst_events}
    assert "chat_draft_token" not in asst_stages
    # assistant_draft (the durable record) IS in pipeline_events.
    assert "assistant_draft" in asst_stages


# ============================================================================
# 9. Parallel verification — many claims, all decisions land
# ============================================================================


def test_parallel_verification_returns_all_decisions_in_order(tmp_path):
    """Five assistant claims, one verifier per claim. Parallel dispatch
    must return decisions in the same order as the input claims (so the
    chat bubble's correction lines up by claim) regardless of which
    workers finish first."""
    facts = [
        {
            "pattern": "preference", "predicate": "likes",
            "polarity": 1, "slots": {"agent": "user", "object": obj},
            "source_text": f"you like {obj}",
        }
        for obj in ["olives", "bread", "wine", "cheese", "honey"]
    ]
    llm = StubLLM(
        extracts=[
            {"facts": []},          # user-side
            {"facts": list(facts)}, # assistant-side
        ],
        chats=["You like all of those."],
    )
    p = _build_pipeline(tmp_path, llm)

    # Pre-seed Tier U with the five matching user facts so the walker
    # resolves at Tier U for each.
    from src.fact_store import Fact
    for f in facts:
        p.store.insert_fact(Fact(
            pattern=f["pattern"], predicate=f["predicate"],
            slots=f["slots"], polarity=f["polarity"],
            asserted_by="user", verification_status="user_asserted",
            confidence=0.5, affirmed_count=1,
            is_session_local=0, session_ids=[],
            user_id=p.user_id,
        ))

    trace = p.run_turn("what do I like?")

    assert len(trace.verification_decisions) == 5
    # Order preserved — claims[i] aligns with verification_decisions[i].
    for i, vd in enumerate(trace.verification_decisions):
        assert vd["claim"]["slots"]["object"] == facts[i]["slots"]["object"], (
            f"order mismatch at index {i}"
        )

    # All five claim_decision events fired (one per claim).
    asst_events = p.store.get_pipeline_events(trace.assistant_turn_id)
    decisions = [e for e in asst_events if e["stage"] == "claim_decision"]
    assert len(decisions) == 5


# ============================================================================
# 10. Correction event always fires + payload shape
# ============================================================================


def test_correction_event_always_emitted_with_full_interventions(tmp_path):
    """The correction event must fire even when the corrector didn't
    rewrite (so the UI's Correction step transitions out of pending),
    and its interventions field must be the full Intervention.to_dict()
    list (not just intervention_type values) so the inline renderer
    can show each one's claim + reason. Final event must include
    final_content for the Final card preview."""
    llm = StubLLM(
        extracts=[{"facts": []}, {"facts": []}],  # zero claims either side
        chats=["hello there"],
    )
    p = _build_pipeline(tmp_path, llm)

    trace = p.run_turn("hi")

    asst_events = p.store.get_pipeline_events(trace.assistant_turn_id)
    asst_stages = [e["stage"] for e in asst_events]
    # Always emitted, even with zero interventions.
    assert "correction" in asst_stages
    correction_event = next(e for e in asst_events if e["stage"] == "correction")
    data = correction_event["data"]
    assert data["original"] == "hello there"
    assert data["corrected"] == "hello there"
    assert data["rewrote"] is False
    # Empty list (zero claims => zero interventions); the key exists.
    assert data["interventions"] == []

    # Final event carries final_content + rewrote flag.
    final_event = next(e for e in asst_events if e["stage"] == "final")
    final_data = final_event["data"]
    assert final_data["final_content"] == "hello there"
    assert final_data["rewrote"] is False


def test_assistant_claim_contradicts_tier_u_corrector_rewrites(tmp_path):
    """Pre-stored 'user likes olives'. Assistant draft says 'you don't
    like olives' (polarity=0). Walker returns CONTRADICTION via Tier U.
    Layer 5 plans a REPLACE intervention; corrector rewrites."""
    llm = StubLLM(
        extracts=[
            {"facts": []},
            {"facts": [{
                "pattern": "preference",
                "predicate": "likes",
                "polarity": 0,
                "slots": {"agent": "user", "object": "olives"},
                "source_text": "you don't like olives",
            }]},
        ],
        chats=["You don't like olives."],
        rewrites=["Actually, you like olives."],
    )
    p = _build_pipeline(tmp_path, llm)

    from src.fact_store import Fact
    p.store.insert_fact(Fact(
        pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"},
        polarity=1, asserted_by="user",
        verification_status="user_asserted",
        confidence=0.5, affirmed_count=1,
        is_session_local=0, session_ids=[],
        user_id=p.user_id,
    ))

    trace = p.run_turn("what about olives?")

    assert len(trace.verification_decisions) == 1
    vd = trace.verification_decisions[0]
    assert vd["walker"]["outcome"] == "contradiction"
    assert vd["intervention"]["intervention_type"] == "replace"
    # Corrector rewrote.
    assert trace.original_content == "You don't like olives."
    assert trace.final_content == "Actually, you like olives."
