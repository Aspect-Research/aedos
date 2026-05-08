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
    })
    d = classify_scope(_claim(), llm)
    assert d.scope == "world_fact"
    assert d.reason == "geographic fact"


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
    d = ScopingDecision(scope="world_fact", reason="r")
    assert d.to_dict() == {
        "scope": "world_fact", "reason": "r",
    }


def test_scoping_methods_constant_matches_decision_field():
    assert "user_specific" in SCOPING_METHODS
    assert "session_specific" in SCOPING_METHODS
    assert "world_fact" in SCOPING_METHODS
    assert len(SCOPING_METHODS) == 3


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
