"""End-to-end pipeline tests with a mocked LLM (v0.3 — pattern-based).

Seven canonical v0.3 scenarios:

1. Pattern dispatch — one claim of each pattern type routes correctly.
2. Free-form predicate within an existing pattern — `adores` works.
3. Multi-pattern single sentence — "Tokyo is a city in Japan" yields two facts.
4. Temporal scoping — valid_from / valid_until populate the fact's columns.
5. Query strategy fallback — attempt 2 wins when attempt 1 returns 0.
6. Verifier failure does NOT trigger hedge — the v0.2 bug fix.
7. Pattern abstention — "the sunset was beautiful" stores nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.corrector import Corrector
from src.extractor import ClaimExtractor
from src.fact_store import FactStore
from src.pattern_registry import load_default_registry, reset_cache
from src.pipeline import Pipeline
from src.router import Router


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_cache()
    yield
    reset_cache()


@dataclass
class MockLLM:
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


# ---------------------------------------------------------------------
# Scenario 1: pattern dispatch — one claim of each pattern type
# ---------------------------------------------------------------------


def test_pattern_dispatch_each_pattern_routes_correctly(tmp_path):
    """A response with claims under multiple patterns. Each routes to the
    pattern's appropriate verifier and gets a coherent verification_status."""
    from src.verifiers.retrieval_verifier import Snippet

    # Three claims: a python-verifiable quantitative, a user-authoritative
    # preference (model asserts it about the user — store-lookup miss),
    # and an unverifiable propositional_attitude (non-user agent).
    facts = [
        # python verifier path
        {
            "pattern": "quantitative", "predicate": "has_count",
            "slots": {"subject": "strawberry", "property": "letter_r", "value": 3},
            "polarity": 1, "source_text": "3 r's in strawberry",
        },
        # store lookup miss → unverifiable_pending_implementation
        {
            "pattern": "preference", "predicate": "likes",
            "slots": {"agent": "user", "object": "lavender"},
            "polarity": 1, "source_text": "you like lavender",
        },
    ]
    mock = MockLLM(
        chats=["strawberry has 3 r's; you like lavender"],
        extracts=[{"facts": []}, {"facts": facts}],
        # The 'likes' claim about user with no prior assertion ends up as
        # unverifiable_pending_implementation (conf 0.4 < 0.5) → corrector hedges.
        rewrites=["strawberry has 3 r's; I think you might like lavender."],
    )
    p = _make_pipeline(tmp_path, mock)
    trace = p.run_turn("dispatch test")

    statuses = sorted(d["verification_status"] for d in trace.verification_decisions)
    assert "verified" in statuses                  # python: count is correct
    assert "unverifiable_pending_implementation" in statuses  # user said nothing


# ---------------------------------------------------------------------
# Scenario 2: free-form predicate within an existing pattern
# ---------------------------------------------------------------------


def test_freeform_predicate_within_preference_routes_normally(tmp_path):
    """`adores` isn't in example_predicates but is valid within preference.
    Should route via the pattern's user_authoritative branch (agent=user)."""
    facts = [
        {
            "pattern": "preference", "predicate": "adores",
            "slots": {"agent": "user", "object": "sourdough", "intensity": "strong"},
            "polarity": 1, "source_text": "I adore sourdough",
        }
    ]
    mock = MockLLM(
        chats=["Got it!"],
        extracts=[{"facts": facts}, {"facts": []}],
    )
    p = _make_pipeline(tmp_path, mock)
    trace = p.run_turn("I adore sourdough")

    # Stored as user_asserted regardless of the unfamiliar predicate label.
    user_facts = p.store.query_facts(asserted_by="user")
    assert len(user_facts) == 1
    assert user_facts[0].predicate == "adores"
    assert user_facts[0].pattern == "preference"
    assert user_facts[0].verification_status == "user_asserted"


# ---------------------------------------------------------------------
# Scenario 3: multi-pattern single sentence
# ---------------------------------------------------------------------


def test_multi_pattern_single_sentence(tmp_path):
    """'Tokyo is a city in Japan' → categorical AND spatial_temporal."""
    from src.verifiers.retrieval_verifier import Snippet

    facts = [
        {
            "pattern": "categorical", "predicate": "is_a",
            "slots": {"entity": "Tokyo", "category": "city"},
            "polarity": 1, "source_text": "Tokyo is a city",
        },
        {
            "pattern": "spatial_temporal", "predicate": "located_in",
            "slots": {"entity": "Tokyo", "location": "Japan",
                      "relation_kind": "containment"},
            "polarity": 1, "source_text": "Tokyo is a city in Japan",
        },
    ]
    snippets = [
        Snippet("Tokyo - Wikipedia", "Tokyo is the capital city of Japan.", "https://x"),
        Snippet("Japan", "Cities of Japan include Tokyo, Osaka, ...", "https://y"),
    ]
    mock = MockLLM(
        chats=["Tokyo is a city in Japan"],
        extracts=[{"facts": []}, {"facts": facts}],
        # Two retrieval calls (one per fact), each gets a SUPPORTED verdict.
        rewrites=[
            "SUPPORTED\nJustification: snippet 1 confirms.",
            "SUPPORTED\nJustification: snippet 1 confirms.",
        ],
    )
    p = _make_pipeline(tmp_path, mock, search_fn=lambda q: snippets)
    trace = p.run_turn("Tell me about Tokyo")

    assert len(trace.verification_decisions) == 2
    patterns = sorted(d["claim"]["pattern"] for d in trace.verification_decisions)
    assert patterns == ["categorical", "spatial_temporal"]
    statuses = [d["verification_status"] for d in trace.verification_decisions]
    assert all(s == "verified" for s in statuses)


# ---------------------------------------------------------------------
# Scenario 4: temporal scoping
# ---------------------------------------------------------------------


def test_temporal_scoping_lifts_to_columns(tmp_path):
    """role_assignment with valid_from/valid_until populates the fact's columns."""
    from src.verifiers.retrieval_verifier import Snippet

    fact = {
        "pattern": "role_assignment", "predicate": "served_as",
        "slots": {
            "agent": "Donald Trump",
            "role": "45th President",
            "org": "United States",
            "valid_from": "2017-01-20",
            "valid_until": "2021-01-20",
        },
        "polarity": 1,
        "source_text": "Trump served as the 45th president from 2017 to 2021",
    }
    snippets = [
        Snippet("Donald Trump", "Trump was the 45th US President 2017–2021.", "https://x"),
        Snippet("US Presidents", "45th: Donald J. Trump (2017–2021).", "https://y"),
    ]
    mock = MockLLM(
        chats=["Trump served as the 45th president from 2017 to 2021"],
        extracts=[{"facts": []}, {"facts": [fact]}],
        rewrites=["SUPPORTED\nJ: snippet confirms time period."],
    )
    p = _make_pipeline(tmp_path, mock, search_fn=lambda q: snippets)
    trace = p.run_turn("when was trump president before?")

    facts = p.store.query_facts(pattern="role_assignment")
    assert len(facts) == 1
    f = facts[0]
    assert f.valid_from == "2017-01-20"
    assert f.valid_until == "2021-01-20"
    # And the verifier's judge prompt was the historical one.
    d = trace.verification_decisions[0]
    assert d["retrieval_result"]["historical"] is True


# ---------------------------------------------------------------------
# Scenario 5: query strategy fallback
# ---------------------------------------------------------------------


def test_query_strategy_falls_through_when_first_attempt_returns_zero(tmp_path):
    """Section 5: attempt 1 → 0 results, attempt 2 → 3 results, attempt 2 used.
    Trace shows BOTH attempts."""
    from src.verifiers.retrieval_verifier import Snippet

    fact = {
        "pattern": "role_assignment", "predicate": "holds_role",
        "slots": {"agent": "Donald Trump", "role": "47th President", "org": "United States"},
        "polarity": 1, "source_text": "Donald Trump is the 47th President",
    }
    # "{agent} {role}" returns 0; "{agent} {org} {role}" returns 3.
    results = {
        "Donald Trump 47th President": [],
        "Donald Trump United States 47th President": [
            Snippet("a", "...", "u1"),
            Snippet("b", "...", "u2"),
            Snippet("c", "...", "u3"),
        ],
    }
    mock = MockLLM(
        chats=["Donald Trump is the 47th President"],
        extracts=[{"facts": []}, {"facts": [fact]}],
        rewrites=["SUPPORTED\nJ: snippet 1"],
    )
    p = _make_pipeline(tmp_path, mock, search_fn=lambda q: results.get(q, []))
    trace = p.run_turn("who is the current US president?")

    d = trace.verification_decisions[0]
    rr = d["retrieval_result"]
    assert len(rr["attempts"]) == 2
    assert rr["attempts"][0]["used"] is False
    assert rr["attempts"][0]["result_count"] == 0
    assert rr["attempts"][1]["used"] is True
    assert rr["attempts"][1]["result_count"] == 3
    assert d["verification_status"] == "verified"

    # And both attempts were logged as pipeline_events.
    events = p.store.get_pipeline_events(trace.assistant_turn_id)
    attempt_events = [e for e in events if e["stage"] == "retrieval_query_attempt"]
    assert len(attempt_events) == 2


# ---------------------------------------------------------------------
# Scenario 6: verifier failure does NOT trigger hedge
# ---------------------------------------------------------------------


def test_verifier_failure_does_not_hedge_response(tmp_path):
    """The v0.2 bug fix: when retrieval fails, do NOT hedge a true claim.

    Network error on every attempt → retrieval_failed → corrector noops →
    response delivered unchanged → pipeline emits verifier_failure event.
    """
    import httpx

    fact = {
        "pattern": "role_assignment", "predicate": "holds_role",
        "slots": {"agent": "Donald Trump", "role": "47th President", "org": "United States"},
        "polarity": 1, "source_text": "Donald Trump is the 47th President",
    }
    mock = MockLLM(
        chats=["Donald Trump is the 47th President."],
        extracts=[{"facts": []}, {"facts": [fact]}],
        # No rewrites — corrector should not be called.
    )

    def search(_q):
        raise httpx.ConnectError("network down")

    p = _make_pipeline(tmp_path, mock, search_fn=search)
    trace = p.run_turn("who is the current US president?")

    # The response is unchanged.
    assert trace.original_content is None
    assert trace.final_content == "Donald Trump is the 47th President."
    # No interventions were planned.
    assert trace.interventions == []
    # The decision is retrieval_failed.
    d = trace.verification_decisions[0]
    assert d["verification_status"] == "retrieval_failed"
    # And a verifier_failure event was emitted.
    events = p.store.get_pipeline_events(trace.assistant_turn_id)
    failures = [e for e in events if e["stage"] == "verifier_failure"]
    assert len(failures) == 1


def test_retrieval_inconclusive_DOES_hedge(tmp_path):
    """Mirror image: when retrieval *did* run and judge said insufficient,
    the corrector hedges (positive evidence of uncertainty)."""
    from src.verifiers.retrieval_verifier import Snippet

    fact = {
        "pattern": "categorical", "predicate": "is_a",
        "slots": {"entity": "obscure thing", "category": "common kind"},
        "polarity": 1, "source_text": "X is a Y",
    }
    snippets = [
        Snippet("a", "irrelevant snippet", "u1"),
        Snippet("b", "also irrelevant", "u2"),
    ]
    mock = MockLLM(
        chats=["X is a Y"],
        extracts=[{"facts": []}, {"facts": [fact]}],
        rewrites=[
            "INSUFFICIENT_EVIDENCE\nJ: snippets do not address the claim.",
            "I believe X is a Y, though you may want to verify.",
        ],
    )
    p = _make_pipeline(tmp_path, mock, search_fn=lambda q: snippets)
    trace = p.run_turn("tell me about X")

    d = trace.verification_decisions[0]
    assert d["verification_status"] == "retrieval_inconclusive"
    assert len(trace.interventions) == 1
    assert trace.interventions[0]["intervention_type"] == "hedge"
    assert trace.original_content == "X is a Y"
    assert trace.final_content != "X is a Y"


# ---------------------------------------------------------------------
# Scenario 7: pattern abstention
# ---------------------------------------------------------------------


def test_aesthetic_judgment_abstains_and_stores_nothing(tmp_path):
    """The sunset was beautiful → empty extraction → no facts stored."""
    mock = MockLLM(
        chats=["I'm glad you enjoyed it!"],
        extracts=[
            {"facts": []},  # user message
            {"facts": []},  # assistant draft
        ],
    )
    p = _make_pipeline(tmp_path, mock)
    trace = p.run_turn("The sunset was beautiful.")

    assert trace.user_decisions == []
    assert trace.verification_decisions == []
    assert trace.interventions == []
    assert p.store.query_facts() == []


def test_photosynthesis_abstention(tmp_path):
    """Same case for an out-of-vocab scientific process description."""
    mock = MockLLM(
        chats=["That's a great question."],
        extracts=[{"facts": []}, {"facts": []}],
    )
    p = _make_pipeline(tmp_path, mock)
    trace = p.run_turn("Photosynthesis converts sunlight into chemical energy.")

    assert p.store.query_facts() == []
    assert trace.original_content is None


# ---------------------------------------------------------------------
# Pipeline_events coverage
# ---------------------------------------------------------------------


def test_every_turn_logs_expected_stages(tmp_path):
    from src.verifiers.retrieval_verifier import Snippet

    fact = {
        "pattern": "categorical", "predicate": "is_a",
        "slots": {"entity": "Marie Curie", "category": "physicist"},
        "polarity": 1, "source_text": "Marie Curie was a physicist",
    }
    mock = MockLLM(
        chats=["Marie Curie was a physicist"],
        extracts=[{"facts": []}, {"facts": [fact]}],
        rewrites=["SUPPORTED\nJ: snippet."],
    )
    p = _make_pipeline(
        tmp_path, mock,
        search_fn=lambda q: [Snippet("a", "Marie Curie was a physicist", "u")] * 2,
    )
    trace = p.run_turn("Tell me about Marie Curie")

    user_events = p.store.get_pipeline_events(trace.user_turn_id)
    asst_events = p.store.get_pipeline_events(trace.assistant_turn_id)
    user_stages = {e["stage"] for e in user_events}
    asst_stages = {e["stage"] for e in asst_events}

    assert {"user_extraction", "user_storage"} <= user_stages
    assert (
        {"assistant_draft", "assistant_extraction", "verification", "final"}
        <= asst_stages
    )
    # Section 5: retrieval_query_attempt logged for the retrieval call.
    assert "retrieval_query_attempt" in asst_stages
