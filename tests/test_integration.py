"""End-to-end pipeline tests with a mocked LLM.

Five canonical scenarios, from the project spec:

1. User asserts a fact.
2. Model hallucinates a count and gets corrected.
3. Model contradicts a prior user-asserted fact and gets corrected.
4. Model makes an unverifiable claim (flagged, not corrected).
5. Model makes a correctly verifiable claim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.corrector import Corrector
from src.extractor import ClaimExtractor
from src.fact_store import FactStore
from src.pipeline import Pipeline
from src.predicate_registry import load_default_registry, reset_cache
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


def _make_pipeline(tmp_path, mock: MockLLM) -> Pipeline:
    store = FactStore(tmp_path / "int.db")
    registry = load_default_registry()
    extractor = ClaimExtractor(mock, registry)
    router = Router(store, registry)
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
    assert len(trace.corrections) == 1
    corr = trace.corrections[0]
    assert json.loads(corr["corrected_object"]) == {"item": "p", "count": 0}

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
    assert len(trace.corrections) == 1


# ---- scenario 4: model makes an unverifiable claim ------------------


def test_model_unverifiable_is_flagged_not_corrected(tmp_path):
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
    )
    p = _make_pipeline(tmp_path, mock)
    trace = p.run_turn("What's the weather?")

    assert trace.original_content is None  # no correction
    assert trace.final_content == "It will rain tomorrow."

    flagged = p.store.query_facts(predicate="will_happen")
    assert len(flagged) == 1
    assert flagged[0].verification_status == "unverifiable_in_principle"
    assert flagged[0].confidence == pytest.approx(0.3)


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
