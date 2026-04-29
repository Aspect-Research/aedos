"""Tests for the v0.6 cache-eligibility scoping classifier.

The classifier is one LLM call. Most tests mock the LLM and assert the
parsing/wiring is correct. A real-API calibration test is gated behind
RUN_API_TESTS=1.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.cache.scoping_classifier import (
    SCOPING_METHODS,
    ScopingDecision,
    classify_scope,
)


@dataclass
class _MockLLM:
    canned: dict = field(default_factory=dict)

    def extract_with_tool(self, system, user_message, tool, max_tokens=2048, **_kwargs):
        return self.canned


def _claim(**kwargs):
    base = {
        "pattern": "spatial_temporal",
        "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"},
        "polarity": 1,
        "source_text": "Tokyo is in Japan",
    }
    base.update(kwargs)
    return base


def test_returns_world_fact_when_llm_says_so():
    llm = _MockLLM(canned={
        "scope": "world_fact",
        "reason": "geographic fact",
        "confidence": 0.95,
    })
    d = classify_scope(_claim(), llm)
    assert d.scope == "world_fact"
    assert d.confidence == 0.95


def test_returns_user_specific_for_preference():
    llm = _MockLLM(canned={
        "scope": "user_specific",
        "reason": "user preference",
        "confidence": 0.99,
    })
    d = classify_scope(
        _claim(pattern="preference", predicate="likes",
               slots={"agent": "user", "object": "tea"}),
        llm,
    )
    assert d.scope == "user_specific"


def test_returns_session_specific_for_self_referential():
    llm = _MockLLM(canned={
        "scope": "session_specific",
        "reason": "literal sentence from this conversation",
        "confidence": 0.95,
    })
    d = classify_scope(
        _claim(pattern="quantitative", predicate="has_count",
               slots={"subject": "the quick brown fox",
                      "property": "words_with_o", "value": 2}),
        llm,
    )
    assert d.scope == "session_specific"


def test_invalid_scope_raises():
    llm = _MockLLM(canned={
        "scope": "made_up_scope", "reason": "garbled", "confidence": 0.5,
    })
    with pytest.raises(RuntimeError, match="invalid scope"):
        classify_scope(_claim(), llm)


def test_decision_to_dict_shape():
    d = ScopingDecision(scope="world_fact", reason="r", confidence=0.9)
    assert d.to_dict() == {
        "scope": "world_fact", "reason": "r", "confidence": 0.9,
    }


def test_scoping_methods_constant_matches_decision_field():
    assert "user_specific" in SCOPING_METHODS
    assert "session_specific" in SCOPING_METHODS
    assert "world_fact" in SCOPING_METHODS
    assert len(SCOPING_METHODS) == 3


# ---- pipeline integration: observation mode ----------------------------


def test_pipeline_logs_scoping_decisions_in_observation_mode(tmp_path):
    """The classifier runs per assistant claim and writes a
    cache_scoping_decision event. It does NOT change verification or
    routing — pure observation."""
    from src.corrector import Corrector
    from src.extractor import ClaimExtractor
    from src.fact_store import FactStore
    from src.llm_router import RoutingDecision
    from src.pattern_registry import load_default_registry, reset_cache
    from src.pipeline import Pipeline
    from src.router import Router

    reset_cache()

    @dataclass
    class _PipelineMockLLM:
        chats: list = field(default_factory=list)
        extracts: list = field(default_factory=list)
        rewrites: list = field(default_factory=list)
        corrector_model: str = "mock"

        def chat(self, system, messages, max_tokens=4096, **_kwargs):
            return self.chats.pop(0)

        def extract_with_tool(self, system, user_message, tool, max_tokens=2048, **_kwargs):
            return self.extracts.pop(0)

        def rewrite(self, system, user_message, max_tokens=2048, temperature=None, **_kwargs):
            return self.rewrites.pop(0)

    asst_facts = [
        {"pattern": "spatial_temporal", "predicate": "located_in",
         "slots": {"entity": "Tokyo", "location": "Japan"},
         "polarity": 1, "source_text": "Tokyo is in Japan"},
    ]
    mock = _PipelineMockLLM(
        chats=["Tokyo is in Japan."],
        extracts=[{"facts": []}, {"facts": asst_facts}],
        rewrites=["Tokyo might be in Japan."],  # corrector softens unverifiable
    )
    store = FactStore(tmp_path / "p.db")
    registry = load_default_registry()
    extractor = ClaimExtractor(mock, registry)
    router = Router(store, registry, routing_fn=lambda c: RoutingDecision(
        method="unverifiable", reason="x", confidence=0.9))

    captured_claims: list[dict] = []

    def fake_classifier(claim):
        captured_claims.append(claim)
        return ScopingDecision(
            scope="world_fact", reason="geo fact", confidence=0.95,
        )

    p = Pipeline(store, registry, mock, extractor, router, Corrector(mock),
                 scoping_classifier=fake_classifier)
    trace = p.run_turn("where is tokyo")

    # The classifier was called once per assistant claim.
    assert len(captured_claims) == 1
    assert captured_claims[0]["slots"]["entity"] == "Tokyo"

    # An event landed.
    events = store.get_pipeline_events(trace.assistant_turn_id)
    scope_events = [e for e in events if e["stage"] == "cache_scoping_decision"]
    assert len(scope_events) == 1
    data = scope_events[0]["data"]
    assert data["decision"]["scope"] == "world_fact"
    # The event payload includes the claim it pertains to so the
    # trace UI can show "scoping decision FOR which claim". Without
    # this, cache decisions floated context-free in the trace.
    assert "claim" in data
    assert data["claim"]["pattern"] == "spatial_temporal"
    assert data["claim"]["predicate"] == "located_in"
    assert data["claim"]["slots"]["entity"] == "Tokyo"
    # Routing/verification still ran (no behavior change).
    assert any(e["stage"] == "verification" for e in events)


def test_pipeline_continues_when_classifier_raises(tmp_path):
    """Observation mode must not break the pipeline. A classifier
    exception is logged but doesn't propagate."""
    from src.corrector import Corrector
    from src.extractor import ClaimExtractor
    from src.fact_store import FactStore
    from src.llm_router import RoutingDecision
    from src.pattern_registry import load_default_registry, reset_cache
    from src.pipeline import Pipeline
    from src.router import Router

    reset_cache()

    @dataclass
    class _PipelineMockLLM:
        chats: list = field(default_factory=list)
        extracts: list = field(default_factory=list)
        rewrites: list = field(default_factory=list)
        corrector_model: str = "mock"

        def chat(self, system, messages, max_tokens=4096, **_kwargs):
            return self.chats.pop(0)

        def extract_with_tool(self, system, user_message, tool, max_tokens=2048, **_kwargs):
            return self.extracts.pop(0)

        def rewrite(self, system, user_message, max_tokens=2048, temperature=None, **_kwargs):
            return self.rewrites.pop(0)

    asst_facts = [
        {"pattern": "spatial_temporal", "predicate": "located_in",
         "slots": {"entity": "Tokyo", "location": "Japan"},
         "polarity": 1, "source_text": "Tokyo is in Japan"},
    ]
    mock = _PipelineMockLLM(
        chats=["Tokyo is in Japan."],
        extracts=[{"facts": []}, {"facts": asst_facts}],
        rewrites=["Tokyo might be in Japan."],  # corrector softens unverifiable
    )
    store = FactStore(tmp_path / "p.db")
    registry = load_default_registry()
    extractor = ClaimExtractor(mock, registry)
    router = Router(store, registry, routing_fn=lambda c: RoutingDecision(
        method="unverifiable", reason="x", confidence=0.9))

    def boom_classifier(claim):
        raise RuntimeError("classifier exploded")

    p = Pipeline(store, registry, mock, extractor, router, Corrector(mock),
                 scoping_classifier=boom_classifier)
    # Pipeline runs to completion despite the classifier raising.
    trace = p.run_turn("test")
    # The corrector softened the unverifiable claim. The point of this
    # test is the pipeline DIDN'T raise — final_content content shape
    # doesn't matter, just that it landed.
    assert trace.final_content  # non-empty

    events = store.get_pipeline_events(trace.assistant_turn_id)
    scope_events = [e for e in events if e["stage"] == "cache_scoping_decision"]
    assert len(scope_events) == 1
    assert "error" in scope_events[0]["data"]
    assert "classifier exploded" in scope_events[0]["data"]["error"]


# ---- always-on construction --------------------------------------------


def test_build_pipeline_always_wires_scoping_classifier(tmp_path, monkeypatch):
    """The scoping classifier is always wired by build_pipeline now —
    no env-var gate. Caches should accumulate across turns; opting
    out would defeat the purpose."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    for var in ("AEDOS_CACHE_TIER2", "AEDOS_CACHE_SCOPING"):
        monkeypatch.delenv(var, raising=False)

    from src.pipeline import build_pipeline
    p = build_pipeline(str(tmp_path / "x.db"))
    assert p._scoping_classifier is not None
    assert callable(p._scoping_classifier)
    p.store.close()


# ---- real-API calibration (gated) --------------------------------------


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="real API scoping classifier calibration gated behind RUN_API_TESTS=1",
)
def test_scoping_calibration_against_worked_examples():
    """Smoke-check that the scoping classifier picks the expected scope
    on its own worked examples. Real API; one call per case."""
    from src.llm_client import LLMClient

    cases = [
        # (claim, expected_scope)
        ({"pattern": "preference", "predicate": "likes",
          "slots": {"agent": "user", "object": "peanut butter"},
          "polarity": 1, "source_text": "I like peanut butter"},
         "user_specific"),
        ({"pattern": "spatial_temporal", "predicate": "located_in",
          "slots": {"entity": "Tokyo", "location": "Japan"},
          "polarity": 1, "source_text": "Tokyo is in Japan"},
         "world_fact"),
        ({"pattern": "quantitative", "predicate": "has_count",
          "slots": {"subject": "strawberry", "property": "letter_r", "value": 3},
          "polarity": 1, "source_text": "3 r's in strawberry"},
         "world_fact"),
        ({"pattern": "quantitative", "predicate": "has_count",
          "slots": {"subject": "the quick brown fox",
                    "property": "words_with_o", "value": 2},
          "polarity": 1, "source_text": "2 words contain 'o'"},
         "session_specific"),
    ]

    llm = LLMClient()
    correct = 0
    misses: list[str] = []
    for claim, expected in cases:
        d = classify_scope(claim, llm)
        if d.scope == expected:
            correct += 1
        else:
            misses.append(f"  claim={claim['source_text']!r} expected="
                          f"{expected} got={d.scope} reason={d.reason}")
    assert correct >= 3, (
        f"scoping classifier calibration: only {correct}/{len(cases)} correct\n"
        + "\n".join(misses)
    )
