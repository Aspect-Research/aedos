"""Tests for src.llm_router (v0.5)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from src.legacy.llm_router import (
    ROUTING_METHODS,
    RoutingDecision,
    _ROUTER_SYSTEM,
    route_claim,
)


@dataclass
class FakeLLM:
    response: dict
    calls: list[dict] = field(default_factory=list)
    extractor_model: str = "claude-sonnet-4-6"
    corrector_model: str = "claude-haiku-4-5"

    def extract_with_tool(self, system, user_message, tool, max_tokens=2048, **_kwargs):
        self.calls.append({"system": system, "user_message": user_message, "tool": tool})
        return dict(self.response)


# ---------- shape ----------


def test_decision_dict_carries_every_field():
    llm = FakeLLM(response={
        "method": "python",
        "reason": "Pure computation.",
        "confidence": 0.95,
        "python_inputs_self_contained": True,
    })
    d = route_claim({"pattern": "quantitative", "predicate": "has_count",
                     "slots": {"subject": "x"}, "polarity": 1}, llm)
    payload = d.to_dict()
    assert payload["method"] == "python"
    assert payload["reason"] == "Pure computation."
    # v0.13: no confidence field on RoutingDecision.
    assert "confidence" not in payload
    assert payload["python_inputs_self_contained"] is True
    assert payload["retrieval_query_hint"] is None
    assert payload["canonical_constants_needed"] is None


@pytest.mark.parametrize("method", list(ROUTING_METHODS))
def test_each_method_round_trips(method):
    llm = FakeLLM(response={"method": method, "reason": "x", "confidence": 0.5})
    d = route_claim({"pattern": "x", "predicate": "x", "slots": {}, "polarity": 1}, llm)
    assert d.method == method


# ---------- coercion ----------


def test_unknown_method_coerces_to_unverifiable():
    llm = FakeLLM(response={"method": "magic", "reason": "x", "confidence": 0.5})
    d = route_claim({"pattern": "x", "predicate": "x", "slots": {}, "polarity": 1}, llm)
    assert d.method == "unverifiable"


def test_extra_confidence_field_ignored_silently():
    """v0.13: RoutingDecision no longer has a confidence field. If
    the LLM still emits one (older prompts, drift), the parser
    drops it without complaint — confidence is now derived from
    counts, not LLM self-rating."""
    llm = FakeLLM(response={"method": "python", "reason": "", "confidence": 0.95})
    d = route_claim({"pattern": "x", "predicate": "x", "slots": {}, "polarity": 1}, llm)
    assert d.method == "python"
    assert not hasattr(d, "confidence") or "confidence" not in d.to_dict()


def test_query_hint_only_returned_as_string_or_none():
    llm = FakeLLM(response={
        "method": "retrieval", "reason": "x", "confidence": 0.5,
        "retrieval_query_hint": "  some query  ",
    })
    d = route_claim({"pattern": "x", "predicate": "x", "slots": {}, "polarity": 1}, llm)
    assert d.retrieval_query_hint == "  some query  "


def test_empty_query_hint_becomes_none():
    llm = FakeLLM(response={
        "method": "retrieval", "reason": "x", "confidence": 0.5,
        "retrieval_query_hint": "   ",
    })
    d = route_claim({"pattern": "x", "predicate": "x", "slots": {}, "polarity": 1}, llm)
    assert d.retrieval_query_hint is None


def test_canonical_constants_filtered_to_strings():
    llm = FakeLLM(response={
        "method": "python_with_canonical_constants",
        "reason": "x", "confidence": 0.7,
        "canonical_constants_needed": ["list of US states", 7, None, "months"],
    })
    d = route_claim({"pattern": "x", "predicate": "x", "slots": {}, "polarity": 1}, llm)
    # None should be dropped; "7" should be coerced.
    assert d.canonical_constants_needed == ["list of US states", "7", "months"]


def test_canonical_constants_all_garbage_collapses_to_none():
    """If every entry in canonical_constants_needed is filtered out
    (none are str/int/float — e.g. all dicts or None), the field
    collapses to None rather than an empty list."""
    llm = FakeLLM(response={
        "method": "python_with_canonical_constants",
        "reason": "x", "confidence": 0.7,
        "canonical_constants_needed": [None, {"k": "v"}, [1, 2]],
    })
    d = route_claim({"pattern": "x", "predicate": "x", "slots": {}, "polarity": 1}, llm)
    assert d.canonical_constants_needed is None


def test_inputs_self_contained_only_bool_or_none():
    llm = FakeLLM(response={
        "method": "python", "reason": "x", "confidence": 0.9,
        "python_inputs_self_contained": "yes",  # not a bool
    })
    d = route_claim({"pattern": "x", "predicate": "x", "slots": {}, "polarity": 1}, llm)
    assert d.python_inputs_self_contained is None


# ---------- prompt content invariants ----------


def test_router_system_lists_all_five_methods():
    s = _ROUTER_SYSTEM.lower()
    for m in ROUTING_METHODS:
        assert m in s, f"router prompt missing method {m}"


def test_router_system_documents_arithmetic_around_retrieved_values():
    """The 'Marie Curie was born ... so she lived 67 years' convention
    must be in the router prompt — it's the multi-claim convention from
    Section 2."""
    s = _ROUTER_SYSTEM.lower()
    assert "multi-claim" in s or "arithmetic" in s
    assert "1934" in _ROUTER_SYSTEM or "1867" in _ROUTER_SYSTEM


def test_router_system_distinguishes_arithmetic_from_dates():
    """Trump's first-term-duration vs. first-term-start-year is the
    canonical 'compute vs. retrieve' boundary; it should be in the prompt
    by name."""
    assert "Trump's first term" in _ROUTER_SYSTEM


def test_router_system_warns_against_external_string_verification_as_python():
    """The Gettysburg-Address case (string-shape suggests python but
    actually requires retrieval) should be one of the worked examples."""
    assert "Gettysburg" in _ROUTER_SYSTEM


# ---------- request shape ----------


def test_user_message_includes_claim_metadata():
    llm = FakeLLM(response={"method": "python", "reason": "x", "confidence": 0.9})
    claim = {
        "pattern": "quantitative",
        "predicate": "has_count",
        "slots": {"subject": "abc", "property": "letter_a", "value": 1},
        "polarity": 1,
        "source_text": "abc has 1 a",
    }
    route_claim(claim, llm)
    msg = llm.calls[0]["user_message"]
    assert "quantitative" in msg
    assert "has_count" in msg
    assert "letter_a" in msg
    assert "abc" in msg
