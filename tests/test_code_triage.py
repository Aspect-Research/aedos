"""Tests for src.verifiers.code_generation.triage (v0.4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from src.verifiers.code_generation.triage import TriageResult, triage_claim


@dataclass
class FakeLLM:
    return_value: dict[str, Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def extract_with_tool(self, system, user_message, tool, max_tokens=2048):
        self.calls.append(
            {"system": system, "user_message": user_message, "tool": tool}
        )
        return self.return_value


def _claim(pattern, predicate, slots, polarity=1, source_text="<src>"):
    return {
        "pattern": pattern, "predicate": predicate, "slots": slots,
        "polarity": polarity, "source_text": source_text,
    }


# ---------- positive cases ----------


def test_reverse_string_is_verifiable():
    llm = FakeLLM({"verifiable": True, "reason": "string reversal is deterministic"})
    r = triage_claim(
        _claim("relational", "reverse_of",
               {"subject": "nairatilage", "object": "egalitarian"}),
        llm,
    )
    assert isinstance(r, TriageResult)
    assert r.verifiable is True
    assert "deterministic" in r.reason


def test_prime_count_is_verifiable():
    llm = FakeLLM({"verifiable": True, "reason": "prime counting is a textbook algorithm"})
    r = triage_claim(
        _claim("quantitative", "prime_count",
               {"subject": "primes 1-100", "value": 25}),
        llm,
    )
    assert r.verifiable is True


def test_letter_count_is_verifiable():
    llm = FakeLLM({"verifiable": True, "reason": "char counting is deterministic"})
    r = triage_claim(
        _claim("quantitative", "has_count",
               {"subject": "strawperpy", "property": "letter_r", "value": 7}),
        llm,
    )
    assert r.verifiable is True


def test_square_root_is_verifiable():
    llm = FakeLLM({"verifiable": True, "reason": "math.sqrt is deterministic"})
    r = triage_claim(
        _claim("quantitative", "sqrt_equals",
               {"subject": 144, "value": 12}),
        llm,
    )
    assert r.verifiable is True


# ---------- negative cases ----------


def test_birth_year_is_not_verifiable():
    llm = FakeLLM({"verifiable": False, "reason": "requires biographical data"})
    r = triage_claim(
        _claim("quantitative", "born_in_year",
               {"subject": "Donald Trump", "property": "birth_year", "value": 1946}),
        llm,
    )
    assert r.verifiable is False
    assert "biographical" in r.reason


def test_aesthetic_judgment_is_not_verifiable():
    llm = FakeLLM({"verifiable": False, "reason": "subjective judgment"})
    r = triage_claim(
        _claim("propositional_attitude", "feels",
               {"agent": "user", "attitude": "thinks",
                "proposition": "the sunset was beautiful"}),
        llm,
    )
    assert r.verifiable is False


# ---------- prompt + tool plumbing ----------


def test_prompt_contains_pattern_and_slots():
    """The triage LLM needs the structural shape of the claim."""
    llm = FakeLLM({"verifiable": True, "reason": "ok"})
    triage_claim(
        _claim("quantitative", "has_count",
               {"subject": "x", "property": "y", "value": 1}),
        llm,
    )
    msg = llm.calls[0]["user_message"]
    assert "pattern: 'quantitative'" in msg
    assert "predicate: 'has_count'" in msg
    assert "subject" in msg


def test_tool_schema_fields():
    """Tool schema enforces verifiable boolean + reason string."""
    llm = FakeLLM({"verifiable": True, "reason": "ok"})
    triage_claim(_claim("quantitative", "has_count", {"value": 1}), llm)
    tool = llm.calls[0]["tool"]
    assert tool["name"] == "record_triage"
    schema = tool["input_schema"]
    assert "verifiable" in schema["properties"]
    assert "reason" in schema["properties"]
    assert schema["properties"]["verifiable"]["type"] == "boolean"
    assert "verifiable" in schema["required"]


def test_default_when_reason_missing():
    """If the LLM omits reason, we still produce a TriageResult."""
    llm = FakeLLM({"verifiable": True})  # no reason
    r = triage_claim(_claim("quantitative", "has_count", {"value": 1}), llm)
    assert r.verifiable is True
    assert r.reason == ""
