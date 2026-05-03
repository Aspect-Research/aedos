"""End-to-end pipeline tests with a mocked LLM (v0.3 / v0.4 / v0.5).

Each model-claim test scripts:
  - extract_with_tool responses (extractor + prompt builder)
  - rewrite responses (code writer + corrector + retrieval judge)
  - routing decisions (one per model-origin claim)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.corrector import Corrector
from src.extractor import ClaimExtractor
from src.fact_store import FactStore
from src.llm_router import RoutingDecision
from src.pattern_registry import load_default_registry, reset_cache
from src.pipeline import Pipeline
from src.router import Router
from src.verifiers.code_generation.pipeline import CodeGenVerificationResult


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
    # v0.5: routing decisions, one per model-origin claim, popped in order.
    routings: list[RoutingDecision] = field(default_factory=list)
    corrector_model: str = "mock-corrector"

    def chat(self, system, messages, max_tokens=4096, **_kwargs):
        return self.chats.pop(0)

    def extract_with_tool(self, system, user_message, tool, max_tokens=2048, **_kwargs):
        return self.extracts.pop(0)

    def rewrite(self, system, user_message, max_tokens=2048, temperature=None, **_kwargs):
        return self.rewrites.pop(0)


def _routing_fn_for(mock: MockLLM):
    """Build a routing_fn that pops decisions from ``mock.routings``."""
    def fn(_claim):
        if not mock.routings:
            raise RuntimeError(
                "MockLLM has no queued routing decision; "
                "test must script one decision per model-origin claim"
            )
        return mock.routings.pop(0)
    return fn


# Convenience constructors — concise routing decisions.

def _route_python(reason="pure computation", confidence=0.95):
    return RoutingDecision(
        method="python", reason=reason, confidence=confidence,
        python_inputs_self_contained=True,
    )


def _route_python_canonical(constants, reason="needs canonical reference",
                             confidence=0.85):
    return RoutingDecision(
        method="python_with_canonical_constants",
        reason=reason, confidence=confidence,
        python_inputs_self_contained=False,
        canonical_constants_needed=list(constants),
    )


def _route_retrieval(query="x", reason="external data", confidence=0.9):
    return RoutingDecision(
        method="retrieval", reason=reason, confidence=confidence,
        retrieval_query_hint=query,
    )


def _route_user_auth(reason="claim about user", confidence=0.95):
    return RoutingDecision(method="user_authoritative",
                           reason=reason, confidence=confidence)


def _route_unverifiable(reason="no method applies", confidence=0.85):
    return RoutingDecision(method="unverifiable",
                           reason=reason, confidence=confidence)


@dataclass
class StubCodeGenVerifier:
    """Test double — returns queued CodeGenVerificationResults."""

    results: list[CodeGenVerificationResult] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def verify(self, claim, *, source_turn_id=None):
        self.calls.append({"claim": claim, "source_turn_id": source_turn_id})
        if not self.results:
            raise RuntimeError("StubCodeGenVerifier has no queued result")
        return self.results.pop(0)

    def verify_with_cross_check(self, claim, *, source_turn_id=None):
        # For tests that don't exercise canonical-constants, the cross-check
        # path falls back to the same queue.
        return self.verify(claim, source_turn_id=source_turn_id)


def _make_pipeline(
    tmp_path,
    mock: MockLLM,
    search_fn=None,
    code_gen_results: list | None = None,
) -> Pipeline:
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
    code_gen_verifier = None
    if code_gen_results is not None:
        code_gen_verifier = StubCodeGenVerifier(results=list(code_gen_results))
    router = Router(
        store, registry,
        routing_fn=_routing_fn_for(mock),
        retrieval_verifier=retrieval_verifier,
        code_gen_verifier=code_gen_verifier,
    )
    corrector = Corrector(mock)
    return Pipeline(store, registry, mock, extractor, router, corrector)


# ---------------------------------------------------------------------
# Scenario 1: pattern dispatch — one claim of each pattern type
# ---------------------------------------------------------------------


def test_pattern_dispatch_each_pattern_routes_correctly(tmp_path):
    """A response with claims under multiple patterns. Each routes to the
    pattern's appropriate verifier and gets a coherent verification_status."""

    facts = [
        # python (code-gen) path
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
        routings=[
            _route_python(reason="counting letters in literal word"),
            _route_user_auth(),
        ],
    )
    code_gen_results = [
        CodeGenVerificationResult(
            status="verified", confidence=0.99, actual_value=3,
            explanation="claimed 3; computed 3",
        ),
    ]
    p = _make_pipeline(tmp_path, mock, code_gen_results=code_gen_results)
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
        routings=[
            _route_retrieval(query="Tokyo city"),
            _route_retrieval(query="Tokyo Japan"),
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
# v0.9.0: parallel Tier 4 verification — ordering + concurrency
# ---------------------------------------------------------------------


def test_v090_parallel_verify_preserves_decision_order(tmp_path):
    """With multiple model claims dispatched on a worker pool, the
    final decisions must come back in the same order as the input
    claims regardless of which worker finishes first."""
    import threading
    import time

    from src.verifiers.retrieval_verifier import Snippet

    # Three retrieval claims; per-claim search latency varies so the
    # workers naturally finish out-of-order.
    facts = [
        {
            "pattern": "categorical", "predicate": "is_a",
            "slots": {"entity": "Tokyo", "category": "city"},
            "polarity": 1, "source_text": "Tokyo is a city",
        },
        {
            "pattern": "categorical", "predicate": "is_a",
            "slots": {"entity": "Mars", "category": "planet"},
            "polarity": 1, "source_text": "Mars is a planet",
        },
        {
            "pattern": "categorical", "predicate": "is_a",
            "slots": {"entity": "Beethoven", "category": "composer"},
            "polarity": 1, "source_text": "Beethoven was a composer",
        },
    ]
    finish_order: list[str] = []
    finish_lock = threading.Lock()

    # Per-entity delay; the verifier walks query_strategy templates so
    # multiple queries fire per claim — key by the first slot value
    # that appears in the query string.
    delays = {"Tokyo": 0.30, "Mars": 0.10, "Beethoven": 0.20}

    def search_fn(query):
        for entity, delay in delays.items():
            if entity in query:
                time.sleep(delay)
                with finish_lock:
                    if entity not in finish_order:
                        finish_order.append(entity)
                break
        # Two snippets so the retrieval verifier accepts the first
        # attempt (≥2 results) instead of falling through templates.
        return [
            Snippet("t1", f"{query} confirmed.", "https://x"),
            Snippet("t2", f"{query} confirmed again.", "https://y"),
        ]

    mock = MockLLM(
        chats=["Tokyo, Mars, and Beethoven."],
        extracts=[{"facts": []}, {"facts": facts}],
        rewrites=["SUPPORTED\n"] * 3,
        routings=[
            _route_retrieval(query="Tokyo city"),
            _route_retrieval(query="Mars planet"),
            _route_retrieval(query="Beethoven composer"),
        ],
    )
    p = _make_pipeline(tmp_path, mock, search_fn=search_fn)
    trace = p.run_turn("tell me about three things")

    # Workers finished out-of-input-order (Mars first, Beethoven, Tokyo).
    assert finish_order == ["Mars", "Beethoven", "Tokyo"]
    # But decisions came back in input order.
    decision_entities = [
        d["claim"]["slots"]["entity"]
        for d in trace.verification_decisions
    ]
    assert decision_entities == ["Tokyo", "Mars", "Beethoven"]
    statuses = [d["verification_status"] for d in trace.verification_decisions]
    assert all(s == "verified" for s in statuses)


def test_v090_parallel_verify_overlaps_wall_clock(tmp_path):
    """Three claims, each 200ms of (simulated) retrieval time. Pre-v0.9.0
    the for-loop took ~600ms wall-clock; with parallel dispatch it
    should finish near 200ms. Threshold padded for CI noise."""
    import time

    from src.verifiers.retrieval_verifier import Snippet

    facts = [
        {
            "pattern": "categorical", "predicate": "is_a",
            "slots": {"entity": e, "category": "thing"},
            "polarity": 1, "source_text": f"{e} is a thing",
        }
        for e in ("A", "B", "C")
    ]
    def slow_search(_q):
        time.sleep(0.20)
        return [
            Snippet("t1", "ok.", "https://x"),
            Snippet("t2", "ok again.", "https://y"),
        ]

    mock = MockLLM(
        chats=["A, B, and C are all things."],
        extracts=[{"facts": []}, {"facts": facts}],
        rewrites=["SUPPORTED\n"] * 3,
        routings=[
            _route_retrieval(query="A"),
            _route_retrieval(query="B"),
            _route_retrieval(query="C"),
        ],
    )
    p = _make_pipeline(tmp_path, mock, search_fn=slow_search)
    t0 = time.monotonic()
    trace = p.run_turn("tell me about A, B, C")
    elapsed = time.monotonic() - t0

    assert len(trace.verification_decisions) == 3
    # Sequential would be ~600ms; parallel with 3 workers should be
    # near 200ms. Threshold padded to 0.55s for CI / busy-machine
    # noise — the proof-of-overlap signal is "well under sequential",
    # not "exactly the lower bound".
    assert elapsed < 0.55, (
        f"verification took {elapsed:.2f}s — expected parallel dispatch "
        f"to finish under 0.55s with 200ms search latency × 3 claims"
    )


# ---------------------------------------------------------------------
# v0.9.0: streaming chat draft via live_emit
# ---------------------------------------------------------------------


def test_v100_user_world_claim_routed_through_verifier_not_sacrosanct(tmp_path):
    """v0.10.0 — user-stated world claim ("Cairo is 9:56 AM") gets the
    same verifier treatment as a model claim. Self-attribute claims
    ("I like peanut butter") still bypass verification."""
    # User says: "It's 9:56 AM in Cairo right now" — entity is Cairo,
    # not the user → world claim → verify. Stub the code-gen verifier
    # to return contradicted with actual_value="11:00 AM".
    user_world_claim = {
        "pattern": "quantitative", "predicate": "current_time",
        "slots": {"subject": "Cairo", "property": "time", "value": "9:56 AM"},
        "polarity": 1, "source_text": "It's 9:56 AM in Cairo right now",
    }
    cg_result = CodeGenVerificationResult(
        status="contradicted", actual_value="11:00 AM",
        explanation="zoneinfo shows Cairo current time as 11:00 AM",
    )
    mock = MockLLM(
        chats=["Got it."],
        extracts=[
            {"facts": [user_world_claim]},  # user extraction
            {"facts": []},                  # assistant extraction
        ],
        rewrites=[],
        routings=[
            # The new world-claim path consults the LLM router for the
            # user-side claim, then dispatches to the code-gen verifier.
            _route_python(reason="time + zoneinfo is python territory"),
        ],
    )
    p = _make_pipeline(tmp_path, mock,
                       code_gen_results=[cg_result])
    trace = p.run_turn("It's 9:56 AM in Cairo right now")

    # The user's claim got verified — and the verifier disagreed.
    assert len(trace.user_decisions) == 1
    d = trace.user_decisions[0]
    assert d["user_world_claim"] is True
    assert d["verification_status"] == "contradicted"
    # The fact is stored with asserted_by="user" but with the
    # verifier's verdict — not the legacy "user_asserted" status.
    facts = p.store.query_facts(
        pattern="quantitative", asserted_by="user", user_id=None,
    )
    assert len(facts) == 1
    assert facts[0].verification_status == "contradicted"


def test_v100_user_self_attribute_stays_sacrosanct(tmp_path):
    """v0.10.0 — user preference about themselves still bypasses
    verification, stores at CONF_USER_ASSERTED."""
    self_claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "peanut butter"},
        "polarity": 1, "source_text": "I like peanut butter",
    }
    mock = MockLLM(
        chats=["Noted."],
        extracts=[
            {"facts": [self_claim]},  # user extraction
            {"facts": []},            # assistant extraction
        ],
        rewrites=[],
        routings=[],  # no routings should be popped — sacrosanct path
    )
    p = _make_pipeline(tmp_path, mock)
    trace = p.run_turn("I like peanut butter")

    assert len(trace.user_decisions) == 1
    d = trace.user_decisions[0]
    assert d["user_world_claim"] is False
    assert d["verification_status"] == "user_asserted"
    # The router LLM was NOT consulted (no routings popped).
    assert mock.routings == []


def test_v100_disputed_user_claim_surfaces_in_chat_system_prompt(tmp_path):
    """When the verifier contradicted a user world claim, the chat
    system prompt for the same turn includes a 'gentle correction'
    section so the model addresses it conversationally."""
    user_world_claim = {
        "pattern": "quantitative", "predicate": "current_time",
        "slots": {"subject": "Cairo", "property": "time", "value": "9:56 AM"},
        "polarity": 1, "source_text": "It's 9:56 AM in Cairo right now",
    }
    cg_result = CodeGenVerificationResult(
        status="contradicted", actual_value="11:00 AM",
        explanation="zoneinfo shows 11:00 AM",
    )
    captured_prompts: list[str] = []

    class _CapturingMockLLM(MockLLM):
        def chat(self, system, messages, max_tokens=4096, **_kwargs):
            captured_prompts.append(system)
            return self.chats.pop(0)

    mock = _CapturingMockLLM(
        chats=["OK."],
        extracts=[
            {"facts": [user_world_claim]},
            {"facts": []},
        ],
        rewrites=[],
        routings=[_route_python(reason="time + zoneinfo")],
    )
    p = _make_pipeline(tmp_path, mock, code_gen_results=[cg_result])
    p.run_turn("It's 9:56 AM in Cairo right now")

    assert len(captured_prompts) == 1
    sys_prompt = captured_prompts[0]
    # The disputes section is present and names the verifier's value.
    assert "IMPORTANT" in sys_prompt
    assert "11:00 AM" in sys_prompt
    assert "It's 9:56 AM in Cairo right now" in sys_prompt


def test_v090_streaming_chat_draft_fires_chat_draft_token_events(tmp_path):
    """When pipeline.live_emit is wired and the backend accepts
    on_token, every text fragment from the chat backend gets pushed
    through live_emit as a chat_draft_token event with the cumulative
    buffer."""
    fact = {
        "pattern": "categorical", "predicate": "is_a",
        "slots": {"entity": "Tokyo", "category": "city"},
        "polarity": 1, "source_text": "Tokyo is a city",
    }

    deltas = ["Tok", "yo is ", "a city."]

    class _StreamingBackend:
        provider = "fake"
        model = "fake-model"

        def chat(self, system, messages, *, max_tokens, store, turn_id,
                 cost_recorder=None, on_token=None):
            full = ""
            for d in deltas:
                full += d
                if on_token is not None:
                    on_token(d)
            return full

    from src.verifiers.retrieval_verifier import Snippet
    mock = MockLLM(
        chats=[],  # backend handles the chat call
        extracts=[{"facts": []}, {"facts": [fact]}],
        rewrites=["SUPPORTED\n"],
        routings=[_route_retrieval(query="Tokyo city")],
    )
    p = _make_pipeline(
        tmp_path, mock,
        search_fn=lambda q: [Snippet("t1", "ok.", "https://x"),
                             Snippet("t2", "ok2.", "https://y")],
    )
    p.chat_backend = _StreamingBackend()

    live: list[tuple] = []
    p.live_emit = lambda turn_id, stage, data: live.append(
        (turn_id, stage, data),
    )

    trace = p.run_turn("hi")

    # Three deltas → three chat_draft_token events with cumulative text.
    token_events = [(s, d) for (_t, s, d) in live if s == "chat_draft_token"]
    assert [t[1]["text"] for t in token_events] == [
        "Tok", "Tokyo is ", "Tokyo is a city.",
    ]
    # The final draft on the trace matches the streamed text.
    assert "Tokyo is a city." in trace.final_content


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
        routings=[_route_retrieval(query="Donald Trump 45th president")],
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
        routings=[_route_retrieval(query="Donald Trump 47th President")],
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
        routings=[_route_retrieval(query="Donald Trump 47th president")],
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
        routings=[_route_retrieval(query="X is Y")],
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
        routings=[_route_retrieval(query="Marie Curie physicist")],
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


# =====================================================================
# v0.4 — Code-generated verification scenarios (Section 10)
# =====================================================================


def _scripted_codegen_llm(extracts, rewrites):
    """Build a MockLLM that scripts the code-generation stages alongside
    the rest of the pipeline. The integration tests exercise the full
    verify_via_code_generation flow against a real subprocess sandbox."""
    return MockLLM(extracts=list(extracts), rewrites=list(rewrites))


def _make_pipeline_with_code_gen(tmp_path, mock):
    """A pipeline whose Router has a real CodeGenerationVerifier wired to
    the same mock LLM. The mock must script (in order):

      - extract_with_tool: user extraction → assistant extraction →
        prompt-builder per claim
      - rewrite: code writer per claim → corrector (if interventions)
      - routings: one RoutingDecision per model-origin claim
    """
    from src.verifiers.code_generation import CodeGenerationVerifier

    store = FactStore(tmp_path / "int.db")
    registry = load_default_registry()
    extractor = ClaimExtractor(mock, registry)
    code_gen_verifier = CodeGenerationVerifier(store=store, llm=mock)
    router = Router(
        store, registry,
        routing_fn=_routing_fn_for(mock),
        code_gen_verifier=code_gen_verifier,
    )
    corrector = Corrector(mock)
    return Pipeline(store, registry, mock, extractor, router, corrector)


def test_strawperpy_count_verified_end_to_end(tmp_path):
    """The flagship case: 'How many r's in strawperpy?' verified by code gen.

    'strawperpy'.count('r') == 2.
    """
    fact = {
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"subject": "strawperpy", "property": "letter_r", "value": 2},
        "polarity": 1, "source_text": "2 r's in strawperpy",
    }
    mock = MockLLM(
        chats=["There are 2 r's in strawperpy."],
        extracts=[
            {"facts": []},                         # user message extraction
            {"facts": [fact]},                     # assistant draft extraction
            {"prompt": "Count occurrences of the lowercase letter 'r' in 'strawperpy'. Print only the integer.",
             "expected_output_type": "int"},
        ],
        rewrites=[
            "print('strawperpy'.count('r'))",      # code writer
        ],
        routings=[_route_python(reason="counting letters")],
    )
    p = _make_pipeline_with_code_gen(tmp_path, mock)
    trace = p.run_turn("How many r's in strawperpy?")

    assert len(trace.verification_decisions) == 1
    d = trace.verification_decisions[0]
    assert d["verification_status"] == "verified"
    cg = d["code_gen_result"]
    assert cg["status"] == "verified"
    assert cg["actual_value"] == 2
    # Trace artifacts present (no triage in v0.5).
    trace_d = cg["trace"]
    assert "prompt" in trace_d
    assert "code" in trace_d
    assert "execution" in trace_d
    assert "comparison" in trace_d
    assert "triage" not in trace_d
    # Pipeline events: routing_decision + code stages.
    asst_events = p.store.get_pipeline_events(trace.assistant_turn_id)
    asst_stages = {e["stage"] for e in asst_events}
    assert {"routing_decision", "code_prompt_built", "code_generated",
            "code_executed", "code_comparison"} <= asst_stages
    assert "code_triage" not in asst_stages


def test_strawperpy_wrong_count_contradicted_end_to_end(tmp_path):
    """Same setup, but the assistant claimed 3 — corrector should replace with 2."""
    fact = {
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"subject": "strawperpy", "property": "letter_r", "value": 3},
        "polarity": 1, "source_text": "3 r's in strawperpy",
    }
    mock = MockLLM(
        chats=["There are 3 r's in strawperpy."],
        extracts=[
            {"facts": []},
            {"facts": [fact]},
            {"prompt": "Count occurrences of 'r' in 'strawperpy'. Print only the integer.",
             "expected_output_type": "int"},
        ],
        rewrites=[
            "print('strawperpy'.count('r'))",
            "There are 2 r's in strawperpy.",  # corrector rewrite
        ],
        routings=[_route_python(reason="counting letters")],
    )
    p = _make_pipeline_with_code_gen(tmp_path, mock)
    trace = p.run_turn("How many r's in strawperpy?")

    d = trace.verification_decisions[0]
    assert d["verification_status"] == "contradicted"
    assert d["correction"]["corrected_object"] == 2
    assert trace.original_content == "There are 3 r's in strawperpy."
    assert trace.final_content == "There are 2 r's in strawperpy."


def test_reverse_of_routes_to_python_via_router(tmp_path):
    """relational.reverse_of is python because the LLM router classifies
    string reversal as pure computation — pattern is no longer consulted
    for routing in v0.5.
    """
    fact = {
        "pattern": "relational", "predicate": "reverse_of",
        "slots": {"subject": "nairatilage", "object": "egalitarian"},
        "polarity": 1, "source_text": "egalitarian backwards is nairatilage",
    }
    mock = MockLLM(
        chats=["egalitarian backwards is nairatilage"],
        extracts=[
            {"facts": []},
            {"facts": [fact]},
            {"prompt": "Compute the reverse of the string 'egalitarian'. Print only the result.",
             "expected_output_type": "string"},
        ],
        rewrites=["print('egalitarian'[::-1])"],
        routings=[_route_python(reason="string reversal is deterministic")],
    )
    p = _make_pipeline_with_code_gen(tmp_path, mock)
    trace = p.run_turn("spell egalitarian backwards")
    d = trace.verification_decisions[0]
    assert d["verification_status"] == "verified"
    assert d["code_gen_result"]["status"] == "verified"


def test_primes_count_verified_end_to_end(tmp_path):
    """quantitative claim with code generation computes prime count."""
    fact = {
        "pattern": "quantitative", "predicate": "prime_count",
        "slots": {"subject": "primes 1-100", "property": "count", "value": 25},
        "polarity": 1, "source_text": "25 primes between 1 and 100",
    }
    mock = MockLLM(
        chats=["There are 25 primes between 1 and 100."],
        extracts=[
            {"facts": []},
            {"facts": [fact]},
            {"prompt": "Compute the count of prime numbers strictly greater than 1 and strictly less than 100. Print only the integer.",
             "expected_output_type": "int"},
        ],
        rewrites=[
            "n = 100\nprint(sum(1 for i in range(2, n) if all(i % j for j in range(2, int(i**0.5) + 1))))",
        ],
        routings=[_route_python(reason="prime sieve is deterministic")],
    )
    p = _make_pipeline_with_code_gen(tmp_path, mock)
    trace = p.run_turn("how many primes between 1 and 100?")

    d = trace.verification_decisions[0]
    assert d["verification_status"] == "verified"
    assert d["code_gen_result"]["actual_value"] == 25


def test_primes_count_contradicted_end_to_end(tmp_path):
    """Same setup with value=0; comparator should mark contradicted with computed=25."""
    fact = {
        "pattern": "quantitative", "predicate": "prime_count",
        "slots": {"subject": "primes 1-100", "property": "count", "value": 0},
        "polarity": 1, "source_text": "0 primes",
    }
    mock = MockLLM(
        chats=["There are 0 primes between 1 and 100."],
        extracts=[
            {"facts": []},
            {"facts": [fact]},
            {"prompt": "Compute the count of primes between 1 and 100. Print only the integer.",
             "expected_output_type": "int"},
        ],
        rewrites=[
            "n = 100\nprint(sum(1 for i in range(2, n) if all(i % j for j in range(2, int(i**0.5) + 1))))",
            "There are 25 primes between 1 and 100.",  # corrector
        ],
        routings=[_route_python(reason="prime sieve")],
    )
    p = _make_pipeline_with_code_gen(tmp_path, mock)
    trace = p.run_turn("how many primes between 1 and 100?")

    d = trace.verification_decisions[0]
    assert d["verification_status"] == "contradicted"
    assert d["code_gen_result"]["actual_value"] == 25


def test_router_routes_external_data_claim_to_retrieval_directly(tmp_path):
    """v0.5: the LLM router decides retrieval up-front for biographical
    facts — no triage fall-through is needed.
    """
    from src.verifiers.retrieval_verifier import RetrievalVerifier, Snippet
    from src.verifiers.code_generation import CodeGenerationVerifier

    fact = {
        "pattern": "quantitative", "predicate": "born_in_year",
        "slots": {"subject": "Donald Trump", "property": "birth_year", "value": 1946},
        "polarity": 1, "source_text": "Trump was born in 1946",
    }
    snippets = [
        Snippet("Trump", "Donald J. Trump (born 1946)", "u1"),
        Snippet("Trump bio", "Born June 14, 1946 in Queens.", "u2"),
    ]
    mock = MockLLM(
        chats=["Donald Trump was born in 1946."],
        extracts=[
            {"facts": []},
            {"facts": [fact]},
        ],
        rewrites=[
            # Retrieval judge call (the only LLM call after extraction).
            "SUPPORTED\nJustification: snippets confirm 1946.",
        ],
        routings=[_route_retrieval(query="Donald Trump birth year",
                                    reason="external biographical data")],
    )
    # Build a pipeline with both verifiers wired.
    store = FactStore(tmp_path / "int.db")
    registry = load_default_registry()
    extractor = ClaimExtractor(mock, registry)
    retrieval_verifier = RetrievalVerifier(
        store=store, llm=mock, registry=registry,
        search_fn=lambda q: snippets, ttl_hours=1,
    )
    code_gen_verifier = CodeGenerationVerifier(store=store, llm=mock)
    router = Router(
        store, registry,
        routing_fn=_routing_fn_for(mock),
        retrieval_verifier=retrieval_verifier,
        code_gen_verifier=code_gen_verifier,
    )
    corrector = Corrector(mock)
    p = Pipeline(store, registry, mock, extractor, router, corrector)

    trace = p.run_turn("when was trump born?")

    d = trace.verification_decisions[0]
    # Router said retrieval → snippets fetched → SUPPORTED → verified.
    assert d["verification_status"] == "verified"
    # No code-gen trace — code path was never invoked.
    assert d["code_gen_result"] is None
    # And the retrieval result is there.
    assert d["retrieval_result"] is not None
    assert d["retrieval_result"]["outcome"] == "verified"
    # The routing decision is preserved on the Decision.
    assert d["routing_decision"]["method"] == "retrieval"


def test_prompt_leakage_detected_emits_warning_event(tmp_path):
    """Stage 2 retry path: first attempt leaks, second is clean."""
    fact = {
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"subject": "strawperpy", "property": "letter_r", "value": 7},
        "polarity": 1, "source_text": "7 r's",
    }
    mock = MockLLM(
        chats=["strawperpy has 7 r's."],
        extracts=[
            {"facts": []},
            {"facts": [fact]},
            # Leaky first prompt (contains "7")
            {"prompt": "Verify that strawperpy has 7 r's. Print int.",
             "expected_output_type": "int"},
            # Clean retry
            {"prompt": "Compute the count of 'r' in 'strawperpy'. Print int.",
             "expected_output_type": "int"},
        ],
        rewrites=[
            "print('strawperpy'.count('r'))",
            # Corrector rewrite (claim was 7, actual 2 → contradicted).
            "strawperpy has 2 r's.",
        ],
        routings=[_route_python(reason="char counting")],
    )
    p = _make_pipeline_with_code_gen(tmp_path, mock)
    trace = p.run_turn("how many r's in strawperpy?")

    d = trace.verification_decisions[0]
    assert d["verification_status"] == "contradicted"
    asst_events = p.store.get_pipeline_events(trace.assistant_turn_id)
    leakage = [e for e in asst_events if e["stage"] == "code_prompt_leakage_detected"]
    assert len(leakage) == 1


def test_hedged_count_still_extracts_primary_value(tmp_path):
    """Regression: an assistant draft like 'N if X, else M' with an
    enumerated list should still produce a single quantitative claim
    (value = primary count), not an empty extraction.

    Before this fix, the prompt's 'do NOT extract from context' language
    plus the conditional structure caused the extractor to return [].
    """
    user_msg = (
        "List all words in this extra super long sentence that contain "
        "the letter e and count them all up expeditiously, expeditiouslee."
    )
    draft = (
        "Here are the words containing the letter e:\n"
        "1. sentence\n2. letter\n3. e\n4. expeditiously\n5. expeditiouslee\n\n"
        "If you count the standalone 'e': 5 words. "
        "If you don't count it: 4 words."
    )
    # The extractor — given context + the new prompt rules — should extract
    # the PRIMARY count value (5, matching the enumerated list).
    primary_fact = {
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {
            "subject": user_msg,
            "property": "words_containing_letter_e",
            "value": 5,
        },
        "polarity": 1, "source_text": "5 words",
    }
    mock = MockLLM(
        chats=[draft],
        extracts=[
            {"facts": []},                    # user message (imperative, no claim)
            {"facts": [primary_fact]},        # assistant draft — extracts primary value
            {"prompt": (
                "Split the following string by whitespace and count tokens "
                "containing the lowercase letter 'e'. String: "
                + repr(user_msg) + ". Print the integer."),
             "expected_output_type": "int"},
        ],
        rewrites=[
            "s = " + repr(user_msg) + "\n"
            "print(sum(1 for w in s.split() if 'e' in w.lower()))",
            "Total count: 9.",            # corrector
        ],
        routings=[_route_python(
            reason="literal subject; counting words is deterministic")],
    )
    p = _make_pipeline_with_code_gen(tmp_path, mock)
    trace = p.run_turn(user_msg)

    assert len(trace.verification_decisions) == 1, (
        "extractor must produce a claim from a hedged count statement"
    )
    d = trace.verification_decisions[0]
    assert d["claim"]["slots"]["value"] == 5
    assert d["code_gen_result"]["status"] == "contradicted"
    assert d["code_gen_result"]["actual_value"] == 9


def test_self_referential_count_routes_through_python_end_to_end(tmp_path):
    """Regression test: 'how many words containing e in this sentence' must
    extract WITH the user's literal sentence as subject, then verify via
    code generation. Before the fix, the extractor produced an abstract
    subject ('sentence words containing letter e') and triage correctly
    rejected it as not_python_verifiable, falling back to retrieval.
    """
    user_msg = (
        "List all words in this extra super long sentence that contain "
        "the letter e and count them all up expeditiously, expeditiouslee."
    )
    # Assistant claims 7, real count is higher (extra and super both have 'e').
    fact = {
        "pattern": "quantitative", "predicate": "has_count",
        # The extractor (with context) embeds the user's sentence as subject.
        "slots": {
            "subject": user_msg,
            "property": "words_containing_letter_e",
            "value": 7,
        },
        "polarity": 1, "source_text": "Total count: 7",
    }
    mock = MockLLM(
        chats=[
            "Words with 'e': sentence, the, letter, e, them, expeditiously, "
            "expeditiouslee. Total count: 7."
        ],
        extracts=[
            {"facts": []},                                      # user extraction
            {"facts": [fact]},                                  # assistant extraction
            # Prompt builder produces a leak-free prompt.
            {"prompt": (
                "Split the following string by whitespace and count how many "
                "tokens contain the lowercase letter 'e' (case-insensitive). "
                "String: " + repr(user_msg) + ". Print only the integer."),
             "expected_output_type": "int"},
        ],
        rewrites=[
            # Code writer.
            "s = " + repr(user_msg) + "\n"
            "print(sum(1 for w in s.split() if 'e' in w.lower()))",
            # Corrector rewrites 7 → actual.
            "Words with 'e': ... Total count: 9.",
        ],
        routings=[_route_python(
            reason="literal subject; counting tokens is deterministic")],
    )
    p = _make_pipeline_with_code_gen(tmp_path, mock)
    trace = p.run_turn(user_msg)

    # The single fact was extracted with the literal sentence as subject.
    assert len(trace.verification_decisions) == 1
    d = trace.verification_decisions[0]
    assert d["claim"]["slots"]["subject"] == user_msg
    # Triage said yes; code ran; comparator said contradicted (claim was 7,
    # actual count of words with 'e' in the literal sentence is 9).
    cg = d["code_gen_result"]
    assert cg["status"] == "contradicted"
    assert cg["actual_value"] == 9
    assert d["verification_status"] == "contradicted"
    # And the corrector ran.
    assert trace.original_content is not None


def test_code_execution_timeout_marks_pending(tmp_path):
    """If the generated code times out, status is unverifiable_pending_implementation."""
    fact = {
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"subject": "x", "property": "y", "value": 3},
        "polarity": 1, "source_text": "3",
    }
    mock = MockLLM(
        chats=["x has 3 y."],
        extracts=[
            {"facts": []},
            {"facts": [fact]},
            {"prompt": "Compute count.", "expected_output_type": "int"},
        ],
        rewrites=[
            "import time; time.sleep(60); print(3)",
            # Corrector hedges (pending + low confidence).
            "x might have 3 y.",
        ],
        routings=[_route_python(reason="counting")],
    )
    # Tighten the timeout so the test runs fast.
    from src.verifiers.code_generation import CodeGenerationVerifier

    store = FactStore(tmp_path / "int.db")
    registry = load_default_registry()
    extractor = ClaimExtractor(mock, registry)
    cg = CodeGenerationVerifier(store=store, llm=mock, sandbox_timeout_seconds=1)
    router = Router(
        store, registry,
        routing_fn=_routing_fn_for(mock),
        code_gen_verifier=cg,
    )
    corrector = Corrector(mock)
    p = Pipeline(store, registry, mock, extractor, router, corrector)

    trace = p.run_turn("how many y's in x?")

    d = trace.verification_decisions[0]
    assert d["verification_status"] == "unverifiable_pending_implementation"
    cg_result = d["code_gen_result"]
    assert cg_result["status"] == "code_execution_failed"
    assert "timed out" in cg_result["explanation"].lower() or "1s" in cg_result["explanation"]


# =====================================================================
# v0.5 — date/time arithmetic routes through python (§6)
# =====================================================================


def test_date_arithmetic_routes_to_python(tmp_path):
    """The flagship v0.5 case: 'Trump's first term lasted 4 years
    (2017-2021)' is computable from values stated in the claim itself,
    so the LLM router routes to python and the code writer emits
    datetime arithmetic.
    """
    fact = {
        "pattern": "quantitative", "predicate": "term_duration",
        "slots": {
            "subject": "Trump first term",
            "property": "years",
            "value": 4,
            "valid_from": "2017-01-20",
            "valid_until": "2021-01-20",
        },
        "polarity": 1, "source_text": "Trump's first term lasted 4 years (2017-2021)",
    }
    mock = MockLLM(
        chats=["Trump's first term lasted 4 years (2017-01-20 to 2021-01-20)."],
        extracts=[
            {"facts": []},
            {"facts": [fact]},
            {"prompt": (
                "Compute the number of full years between January 20, 2017 "
                "and January 20, 2021. Print only the integer result."),
             "expected_output_type": "int"},
        ],
        rewrites=[
            (
                "from datetime import date\n"
                "start = date(2017, 1, 20)\n"
                "end = date(2021, 1, 20)\n"
                "years = end.year - start.year - "
                "((end.month, end.day) < (start.month, start.day))\n"
                "print(years)"
            ),
        ],
        routings=[_route_python(
            reason="date arithmetic on values stated in the claim's slots",
            confidence=0.9,
        )],
    )
    p = _make_pipeline_with_code_gen(tmp_path, mock)
    trace = p.run_turn("how long was Trump's first term?")

    d = trace.verification_decisions[0]
    assert d["routing_decision"]["method"] == "python"
    assert d["verification_status"] == "verified"
    cg = d["code_gen_result"]
    assert cg["status"] == "verified"
    assert cg["actual_value"] == 4
