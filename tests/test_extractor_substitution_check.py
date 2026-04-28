"""Tests for the extractor's substitution-detection check.

The check: if a fact's source_text isn't a substring of the input
(after case-folding + whitespace collapse), flag it. Strong signal
that the extractor rewrote the claim, often because it substituted
a 'correct' value for what the chat model said.

Pure unit tests on _flag_substitutions. Integration tests confirm
the warnings flow into the pipeline as events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from src.extractor import ClaimExtractor, ExtractionResult


# ---- _flag_substitutions ------------------------------------------------


def _make_result(facts: list[dict]) -> ExtractionResult:
    return ExtractionResult(valid_facts=list(facts))


def test_substring_match_no_warning():
    result = _make_result([{
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"value": 3},
        "source_text": "Saturn has 3 moons",
    }])
    ClaimExtractor._flag_substitutions(result, "I think Saturn has 3 moons total.")
    assert result.warnings == []


def test_case_insensitive():
    result = _make_result([{
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"value": 3},
        "source_text": "saturn has 3 moons",
    }])
    ClaimExtractor._flag_substitutions(result, "I think SATURN HAS 3 MOONS total.")
    assert result.warnings == []


def test_whitespace_normalized():
    result = _make_result([{
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"value": 3},
        "source_text": "Saturn  has\t3 moons",
    }])
    ClaimExtractor._flag_substitutions(
        result, "Saturn has 3 moons in this sentence.",
    )
    assert result.warnings == []


def test_substituted_number_flagged():
    """The bug case: model said 274, extractor wrote 146 in source_text."""
    result = _make_result([{
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"value": 146},
        "source_text": "Saturn has 146 confirmed moons",
    }])
    ClaimExtractor._flag_substitutions(
        result, "Saturn has 274 confirmed moons.",
    )
    assert len(result.warnings) == 1
    w = result.warnings[0]
    assert w["fact_index"] == 0
    assert w["kind"] == "source_text_not_in_input"
    assert "146" in w["detail"]


def test_partial_paraphrase_flagged():
    """If the source_text is paraphrased even slightly, flag it. False
    positives here are acceptable — better to over-warn than miss a
    real substitution."""
    result = _make_result([{
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"value": 3},
        "source_text": "There are exactly 3 moons",  # input says "Saturn has 3 moons"
    }])
    ClaimExtractor._flag_substitutions(result, "Saturn has 3 moons.")
    assert len(result.warnings) == 1


def test_empty_source_text_not_flagged():
    result = _make_result([{
        "pattern": "x", "predicate": "y", "slots": {},
        "source_text": "",
    }])
    ClaimExtractor._flag_substitutions(result, "anything")
    assert result.warnings == []


def test_empty_input_not_flagged():
    """If the input itself is empty, there's nothing to compare
    against — skip."""
    result = _make_result([{
        "pattern": "x", "predicate": "y", "slots": {},
        "source_text": "anything",
    }])
    ClaimExtractor._flag_substitutions(result, "")
    assert result.warnings == []


def test_multiple_facts_warned_independently():
    result = _make_result([
        {"pattern": "x", "predicate": "y", "slots": {},
         "source_text": "Saturn has 3 moons"},  # in input
        {"pattern": "x", "predicate": "y", "slots": {},
         "source_text": "Mars has 5 moons"},   # NOT in input
        {"pattern": "x", "predicate": "y", "slots": {},
         "source_text": "Earth has 1 moon"},   # in input
    ])
    ClaimExtractor._flag_substitutions(
        result,
        "Saturn has 3 moons. Mars has 2 moons. Earth has 1 moon.",
    )
    assert len(result.warnings) == 1
    assert result.warnings[0]["fact_index"] == 1


# ---- pipeline integration -----------------------------------------------


def test_pipeline_emits_substitution_warning_event(tmp_path):
    """End-to-end: when the extractor flags a substitution, the
    pipeline emits an extractor_substitution_warning event."""
    from src.corrector import Corrector
    from src.fact_store import FactStore
    from src.llm_router import RoutingDecision
    from src.pattern_registry import load_default_registry, reset_cache
    from src.pipeline import Pipeline
    from src.router import Router

    reset_cache()

    # Mock LLM where the assistant-extraction step returns a fact
    # whose source_text doesn't appear in the model draft.
    @dataclass
    class _MockLLM:
        chats: list = field(default_factory=list)
        extracts: list = field(default_factory=list)
        rewrites: list = field(default_factory=list)
        corrector_model: str = "mock"

        def chat(self, system, messages, max_tokens=4096):
            return self.chats.pop(0)

        def extract_with_tool(self, system, user_message, tool, max_tokens=2048):
            return self.extracts.pop(0)

        def rewrite(self, system, user_message, max_tokens=2048, temperature=None):
            return self.rewrites.pop(0)

    # Note: model draft says "274"; extractor (simulated) returns
    # source_text="Saturn has 146 confirmed moons" — substitution.
    asst_facts = {
        "facts": [{
            "pattern": "quantitative", "predicate": "has_count",
            "slots": {"subject": "Saturn", "property": "moons", "value": 146},
            "polarity": 1,
            "source_text": "Saturn has 146 confirmed moons",
        }]
    }
    mock = _MockLLM(
        chats=["Saturn has 274 confirmed moons."],
        extracts=[{"facts": []}, asst_facts],
        rewrites=["soft"] * 5,
    )
    store = FactStore(tmp_path / "p.db")
    registry = load_default_registry()
    from src.extractor import ClaimExtractor
    extractor = ClaimExtractor(mock, registry)
    router = Router(store, registry, routing_fn=lambda c: RoutingDecision(
        method="unverifiable", reason="x", confidence=0.9))
    p = Pipeline(store, registry, mock, extractor, router, Corrector(mock))
    trace = p.run_turn("how many moons does Saturn have")

    events = store.get_pipeline_events(trace.assistant_turn_id)
    warning_events = [e for e in events
                      if e["stage"] == "extractor_substitution_warning"]
    assert len(warning_events) == 1
    data = warning_events[0]["data"]
    assert data["warning"]["kind"] == "source_text_not_in_input"
    assert "146" in data["warning"]["detail"]
    # Event payload includes the fact AND the model's draft for context.
    assert data["fact"]["slots"]["value"] == 146
    assert "274" in data["model_draft"]


def test_pipeline_no_warning_when_source_text_matches(tmp_path):
    """Sanity: when the extractor doesn't substitute, no warning event
    is emitted."""
    from src.corrector import Corrector
    from src.fact_store import FactStore
    from src.llm_router import RoutingDecision
    from src.pattern_registry import load_default_registry, reset_cache
    from src.pipeline import Pipeline
    from src.router import Router

    reset_cache()

    @dataclass
    class _MockLLM:
        chats: list = field(default_factory=list)
        extracts: list = field(default_factory=list)
        rewrites: list = field(default_factory=list)
        corrector_model: str = "mock"

        def chat(self, system, messages, max_tokens=4096):
            return self.chats.pop(0)

        def extract_with_tool(self, system, user_message, tool, max_tokens=2048):
            return self.extracts.pop(0)

        def rewrite(self, system, user_message, max_tokens=2048, temperature=None):
            return self.rewrites.pop(0)

    asst_facts = {
        "facts": [{
            "pattern": "quantitative", "predicate": "has_count",
            "slots": {"subject": "Saturn", "property": "moons", "value": 274},
            "polarity": 1,
            "source_text": "Saturn has 274 confirmed moons",  # matches draft
        }]
    }
    mock = _MockLLM(
        chats=["Saturn has 274 confirmed moons."],
        extracts=[{"facts": []}, asst_facts],
        rewrites=["soft"] * 5,
    )
    store = FactStore(tmp_path / "p.db")
    registry = load_default_registry()
    from src.extractor import ClaimExtractor
    extractor = ClaimExtractor(mock, registry)
    router = Router(store, registry, routing_fn=lambda c: RoutingDecision(
        method="unverifiable", reason="x", confidence=0.9))
    p = Pipeline(store, registry, mock, extractor, router, Corrector(mock))
    trace = p.run_turn("test")

    events = store.get_pipeline_events(trace.assistant_turn_id)
    warning_events = [e for e in events
                      if e["stage"] == "extractor_substitution_warning"]
    assert warning_events == []
