"""End-to-end pipeline tests with a mocked LLM.

Five canonical scenarios, from the project spec:

1. User asserts a fact.
2. Model hallucinates a count and gets corrected.
3. Model contradicts a prior user-asserted fact and gets corrected.
4. Model makes an unverifiable claim (flagged, not corrected).
5. Model makes a correctly verifiable claim.
"""

from __future__ import annotations

import pytest
pytestmark = pytest.mark.skip(
    reason="v0.3 migration: integration scenarios rewritten in Section 9"
)

import json
from dataclasses import dataclass, field
from typing import Any

from src.corrector import Corrector
from src.extractor import ClaimExtractor
from src.fact_store import FactStore
from src.pipeline import Pipeline
from src.pattern_registry import load_default_registry, reset_cache
from src.router import Router


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_cache()
    yield
    reset_cache()


@dataclass
class MockLLM:
    """Stand-in for LLMClient with ordered response queues for each method."""

    chats: list[str] = field(default_factory=list)
    extracts: list[dict[str, Any]] = field(default_factory=list)
    rewrites: list[str] = field(default_factory=list)

    def chat(self, system, messages, max_tokens=4096):
        return self.chats.pop(0)

    def extract_with_tool(self, system, user_message, tool, max_tokens=2048):
        return self.extracts.pop(0)

    def rewrite(self, system, user_message, max_tokens=2048):
        return self.rewrites.pop(0)


def _make_pipeline(tmp_path, mock: MockLLM, search_fn=None) -> Pipeline:
    store = FactStore(tmp_path / "int.db")
    registry = load_default_registry()
    extractor = ClaimExtractor(mock, registry)
    retrieval_verifier = None
    if search_fn is not None:
        from src.verifiers.retrieval_verifier import RetrievalVerifier

        retrieval_verifier = RetrievalVerifier(
            store=store, llm=mock, registry=registry,
            search_fn=search_fn, ttl_hours=1,
        )
    router = Router(store, registry, retrieval_verifier=retrieval_verifier)
    corrector = Corrector(mock)
    return Pipeline(store, registry, mock, extractor, router, corrector)


# ---- scenario 1: user asserts a fact ---------------------------------


def test_user_asserts_fact_is_stored(tmp_path):
    mock = MockLLM(
        chats=["Got it — you like peanut butter."],
        extracts=[
            # extract from user message
            {
                "claims": [
                    {
                        "subject": "user",
                        "predicate": "likes",
                        "object": "peanut butter",
                        "object_type": "entity",
                        "polarity": 1,
                        "source_text": "I like peanut butter",
                    }
                ]
            },
            # extract from assistant draft
            {"claims": []},
        ],
    )
    p = _make_pipeline(tmp_path, mock)
    trace = p.run_turn("I like peanut butter")

    assert trace.original_content is None  # no correction applied
    assert "peanut butter" in trace.final_content

    facts = p.store.query_facts(predicate="likes", asserted_by="user")
    assert len(facts) == 1
    assert facts[0].object == "peanut butter"
    assert facts[0].verification_status == "user_asserted"


# ---- scenario 2: model hallucinates a count --------------------------


def test_model_hallucinated_count_gets_corrected(tmp_path):
    bad_count = json.dumps({"item": "p", "count": 3})
    mock = MockLLM(
        chats=["There are 3 p's in strawberry."],
        extracts=[
            {"claims": []},  # user question has no claims
            {
                "claims": [
                    {
                        "subject": "strawberry",
                        "predicate": "has_count",
                        "object": bad_count,
                        "object_type": "count",
                        "polarity": 1,
                        "source_text": "3 p's in strawberry",
                    }
                ]
            },
        ],
        rewrites=["There are 0 p's in strawberry."],
    )
    p = _make_pipeline(tmp_path, mock)
    trace = p.run_turn("How many p's are in strawberry?")

    assert trace.original_content == "There are 3 p's in strawberry."
    assert trace.final_content == "There are 0 p's in strawberry."
    assert len(trace.interventions) == 1
    iv = trace.interventions[0]
    assert iv["intervention_type"] == "replace"
    assert json.loads(iv["verified_value"]) == {"item": "p", "count": 0}

    # A corrected fact (not the wrong claim) is stored as verified.
    corrections_in_store = p.store.query_facts(asserted_by="python_verifier")
    assert len(corrections_in_store) == 1
    assert corrections_in_store[0].verification_status == "verified"


# ---- scenario 3: model contradicts a prior user fact -----------------


def test_model_contradicts_prior_user_fact_gets_corrected(tmp_path):
    mock = MockLLM(
        chats=[
            "Noted — peanut butter is on the list.",
            "No, you don't like peanut butter.",
        ],
        extracts=[
            # Turn 1: user asserts
            {
                "claims": [
                    {
                        "subject": "user",
                        "predicate": "likes",
                        "object": "peanut butter",
                        "object_type": "entity",
                        "polarity": 1,
                        "source_text": "I like peanut butter",
                    }
                ]
            },
            {"claims": []},  # assistant draft for turn 1
            # Turn 2: user asks a question
            {"claims": []},  # user question has no claims
            # assistant draft for turn 2 — wrong
            {
                "claims": [
                    {
                        "subject": "user",
                        "predicate": "likes",
                        "object": "peanut butter",
                        "object_type": "entity",
                        "polarity": 0,  # "you don't like"
                        "source_text": "you don't like peanut butter",
                    }
                ]
            },
        ],
        rewrites=["Yes, you like peanut butter."],
    )
    p = _make_pipeline(tmp_path, mock)

    p.run_turn("I like peanut butter")
    trace = p.run_turn("Do I like peanut butter?")

    assert trace.original_content == "No, you don't like peanut butter."
    assert trace.final_content == "Yes, you like peanut butter."
    assert len(trace.interventions) == 1
    assert trace.interventions[0]["intervention_type"] == "replace"


# ---- scenario 4: model makes an unverifiable claim ------------------


def test_model_unverifiable_in_principle_is_softened(tmp_path):
    """v0.2: a definite-future-fact unverifiable claim now gets a soften intervention."""
    mock = MockLLM(
        chats=["It will rain tomorrow."],
        extracts=[
            {"claims": []},
            {
                "claims": [
                    {
                        "subject": "weather",
                        "predicate": "will_happen",
                        "object": "rain tomorrow",
                        "object_type": "string",
                        "polarity": 1,
                        "source_text": "It will rain tomorrow",
                    }
                ]
            },
        ],
        rewrites=["It might rain tomorrow."],
    )
    p = _make_pipeline(tmp_path, mock)
    trace = p.run_turn("What's the weather?")

    # The fact is still stored as unverifiable_in_principle.
    flagged = p.store.query_facts(predicate="will_happen")
    assert len(flagged) == 1
    assert flagged[0].verification_status == "unverifiable_in_principle"
    assert flagged[0].confidence == pytest.approx(0.3)

    # And the corrector planned a SOFTEN intervention.
    assert len(trace.interventions) == 1
    assert trace.interventions[0]["intervention_type"] == "soften"
    # The rewrite reflects the softened version.
    assert trace.original_content == "It will rain tomorrow."
    assert trace.final_content == "It might rain tomorrow."


# ---- scenario 5: model correctly verifiable -------------------------


def test_model_correctly_verifiable_fact_passes(tmp_path):
    good_count = json.dumps({"item": "r", "count": 3})
    mock = MockLLM(
        chats=["There are 3 r's in strawberry."],
        extracts=[
            {"claims": []},
            {
                "claims": [
                    {
                        "subject": "strawberry",
                        "predicate": "has_count",
                        "object": good_count,
                        "object_type": "count",
                        "polarity": 1,
                        "source_text": "3 r's in strawberry",
                    }
                ]
            },
        ],
    )
    p = _make_pipeline(tmp_path, mock)
    trace = p.run_turn("How many r's are in strawberry?")

    assert trace.original_content is None
    assert trace.final_content == "There are 3 r's in strawberry."

    verified = p.store.query_facts(predicate="has_count", verification_status="verified")
    assert len(verified) == 1
    assert verified[0].asserted_by == "model"
    assert verified[0].confidence >= 0.95


# ---- pipeline_events coverage ---------------------------------------


# =====================================================================
# Section 8: v0.2 scenarios
# =====================================================================


# ---- scenario A: out-of-vocabulary abstention -----------------------


def test_out_of_vocab_user_message_extracts_zero_claims(tmp_path):
    """Photosynthesis sentence: the extractor must abstain, no facts stored."""
    mock = MockLLM(
        chats=["Got it — anything else?"],
        extracts=[
            {"claims": []},  # user message: out-of-vocab
            {"claims": []},  # assistant draft: no claims either
        ],
    )
    p = _make_pipeline(tmp_path, mock)
    trace = p.run_turn(
        "Photosynthesis converts sunlight into chemical energy."
    )

    assert trace.user_extraction["valid_claims"] == []
    assert trace.user_decisions == []
    assert trace.verification_decisions == []
    assert trace.interventions == []
    assert trace.original_content is None
    assert trace.final_content == "Got it — anything else?"
    # And the fact store stays empty.
    assert p.store.query_facts() == []


# ---- scenario B: role claim + retrieval SUPPORTED -------------------


def _us_president_claim():
    return {
        "subject": "Donald Trump",
        "predicate": "holds_role",
        "object": "US President",
        "object_type": "string",
        "polarity": 1,
        "source_text": "Donald Trump is the US President",
    }


def test_role_claim_with_retrieval_supported_passes_through(tmp_path):
    """Section 8 #2: the model says Trump is president, retrieval supports it,
    response delivered as-is, fact stored as verified."""
    from src.verifiers.retrieval_verifier import Snippet

    mock = MockLLM(
        chats=["Donald Trump is the US President."],
        extracts=[
            {"claims": []},
            {"claims": [_us_president_claim()]},
        ],
        rewrites=[
            "SUPPORTED\nJustification: snippet 1 confirms Trump holds the office.",
        ],
    )

    def search(q):
        return [
            Snippet(
                title="Donald Trump - Wikipedia",
                snippet="Donald John Trump (born June 14, 1946) is the 47th President of the United States.",
                url="https://en.wikipedia.org/wiki/Donald_Trump",
            )
        ]

    p = _make_pipeline(tmp_path, mock, search_fn=search)
    trace = p.run_turn("Who is the current US president?")

    assert trace.original_content is None  # nothing to fix
    assert trace.final_content == "Donald Trump is the US President."
    assert trace.interventions == []

    # Decision should be VERIFIED with retrieval evidence.
    assert len(trace.verification_decisions) == 1
    d = trace.verification_decisions[0]
    assert d["outcome"] == "verified"
    assert d["verification_status"] == "verified"
    assert d["retrieval_result"] is not None
    assert d["retrieval_result"]["verdict"]["verdict"] == "SUPPORTED"

    # And the fact landed in the store as verified.
    facts = p.store.query_facts(predicate="holds_role")
    assert len(facts) == 1
    assert facts[0].verification_status == "verified"


# ---- scenario C: role claim + retrieval network error → hedged ------


def test_role_claim_with_retrieval_failure_gets_hedged(tmp_path):
    """Section 8 #3: retrieval errors → unverifiable_pending_implementation
    → corrector hedges → original and corrected versions both stored."""
    import httpx

    mock = MockLLM(
        chats=["Donald Trump is the US President."],
        extracts=[
            {"claims": []},
            {"claims": [_us_president_claim()]},
        ],
        rewrites=[
            # Only call to .rewrite() is the corrector's hedge.
            "Based on my training data, Donald Trump is the US President "
            "as of January 2025, though you may want to verify with a "
            "current source.",
        ],
    )

    def search(q):
        raise httpx.ConnectError("network down")

    p = _make_pipeline(tmp_path, mock, search_fn=search)
    trace = p.run_turn("Who is the current US president?")

    # The original draft was confident; the final response is hedged.
    assert trace.original_content == "Donald Trump is the US President."
    assert trace.final_content != trace.original_content
    assert "verify" in trace.final_content.lower() or "training" in trace.final_content.lower()

    # The intervention was a HEDGE.
    assert len(trace.interventions) == 1
    assert trace.interventions[0]["intervention_type"] == "hedge"

    # The fact is stored as pending, with the retrieval_error flag visible.
    facts = p.store.query_facts(predicate="holds_role")
    assert len(facts) == 1
    assert facts[0].verification_status == "unverifiable_pending_implementation"

    # The turn row carries both versions.
    asst_turn = p.store.get_turn(trace.assistant_turn_id)
    assert asst_turn["content"] == trace.final_content
    assert asst_turn["original_content"] == trace.original_content


# ---- scenario D: routing anomaly ------------------------------------


def test_routing_anomaly_logged_but_response_unchanged(tmp_path):
    """Section 8 #4: extractor produces a user-authoritative claim about a
    non-user subject. Pipeline flags it loudly via routing_anomaly_detected
    but does NOT rewrite the assistant response based on this alone."""
    mock = MockLLM(
        chats=["Donald Trump apparently likes peanut butter."],
        extracts=[
            {"claims": []},
            {
                "claims": [
                    {
                        "subject": "Donald Trump",
                        "predicate": "likes",
                        "object": "peanut butter",
                        "object_type": "entity",
                        "polarity": 1,
                        "source_text": "Donald Trump apparently likes peanut butter",
                    }
                ]
            },
        ],
        # No rewrites needed — anomaly is logged, not rewritten.
    )
    p = _make_pipeline(tmp_path, mock)
    trace = p.run_turn("What does Donald Trump like?")

    # Decision shows routing_anomaly.
    assert len(trace.verification_decisions) == 1
    d = trace.verification_decisions[0]
    assert d["outcome"] == "routing_anomaly"
    assert d["verification_status"] == "routing_anomaly"
    assert d["confidence"] == pytest.approx(0.2)

    # No corrector intervention based on the anomaly alone.
    assert trace.interventions == []
    assert trace.original_content is None

    # But a routing_anomaly_detected event is in the pipeline log.
    events = p.store.get_pipeline_events(trace.assistant_turn_id)
    stages = [e["stage"] for e in events]
    assert "routing_anomaly_detected" in stages
    anomaly_event = next(e for e in events if e["stage"] == "routing_anomaly_detected")
    assert anomaly_event["data"]["claim"]["subject"] == "Donald Trump"
    assert anomaly_event["data"]["claim"]["predicate"] == "likes"
    assert "warning" in anomaly_event["data"]


# ---- scenario E: mixed verification ---------------------------------


def test_mixed_verification_in_one_response(tmp_path):
    """Section 8 #5: a draft mixing python-verifiable, retrieval, and
    unverifiable claims should route each correctly and produce distinct
    statuses, and the corrector handles each appropriately."""
    from src.verifiers.retrieval_verifier import Snippet

    draft = "Strawberry has 3 r's. Donald Trump is the US President. It will rain tomorrow."
    mock = MockLLM(
        chats=[draft],
        extracts=[
            {"claims": []},
            {
                "claims": [
                    {
                        "subject": "strawberry",
                        "predicate": "has_count",
                        "object": json.dumps({"item": "r", "count": 3}),
                        "object_type": "count",
                        "polarity": 1,
                        "source_text": "3 r's",
                    },
                    _us_president_claim(),
                    {
                        "subject": "weather",
                        "predicate": "will_happen",
                        "object": "rain tomorrow",
                        "object_type": "string",
                        "polarity": 1,
                        "source_text": "It will rain tomorrow",
                    },
                ]
            },
        ],
        rewrites=[
            "SUPPORTED\nJustification: snippet confirms.",
            "Strawberry has 3 r's. Donald Trump is the US President. It might rain tomorrow.",
        ],
    )

    def search(q):
        return [
            Snippet(
                title="Donald Trump",
                snippet="Donald Trump is the 47th US President.",
                url="https://wiki/trump",
            )
        ]

    p = _make_pipeline(tmp_path, mock, search_fn=search)
    trace = p.run_turn(
        "How many r's are in strawberry, who's the US president, and what's the weather?"
    )

    # Three decisions, each with a distinct verification_status.
    statuses = [d["verification_status"] for d in trace.verification_decisions]
    assert sorted(statuses) == sorted(
        ["verified", "verified", "unverifiable_in_principle"]
    )

    # Only the unverifiable-in-principle claim drives an intervention.
    assert len(trace.interventions) == 1
    assert trace.interventions[0]["intervention_type"] == "soften"

    # The final response only differs in the softened predictive clause.
    assert trace.original_content == draft
    assert trace.final_content == (
        "Strawberry has 3 r's. Donald Trump is the US President. "
        "It might rain tomorrow."
    )


# =====================================================================
# pipeline_events coverage
# =====================================================================


def test_every_turn_logs_all_expected_stages(tmp_path):
    """A correction turn should produce events for every stage in the spec."""
    bad_count = json.dumps({"item": "p", "count": 3})
    mock = MockLLM(
        chats=["3 p's in strawberry."],
        extracts=[
            {"claims": []},
            {
                "claims": [
                    {
                        "subject": "strawberry",
                        "predicate": "has_count",
                        "object": bad_count,
                        "object_type": "count",
                        "polarity": 1,
                        "source_text": "3 p's in strawberry",
                    }
                ]
            },
        ],
        rewrites=["0 p's in strawberry."],
    )
    p = _make_pipeline(tmp_path, mock)
    trace = p.run_turn("count")

    user_events = p.store.get_pipeline_events(trace.user_turn_id)
    asst_events = p.store.get_pipeline_events(trace.assistant_turn_id)
    user_stages = {e["stage"] for e in user_events}
    asst_stages = {e["stage"] for e in asst_events}

    assert {"user_extraction", "user_storage"} <= user_stages
    assert (
        {"assistant_draft", "assistant_extraction", "verification", "correction", "final"}
        <= asst_stages
    )
