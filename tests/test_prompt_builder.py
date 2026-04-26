"""Tests for src.verifiers.code_generation.prompt_builder (v0.4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from src.verifiers.code_generation.prompt_builder import (
    CodePrompt,
    build_code_prompt,
    detect_leak,
)


@dataclass
class QueueLLM:
    queue: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def extract_with_tool(self, system, user_message, tool, max_tokens=2048):
        self.calls.append(
            {"system": system, "user_message": user_message, "tool": tool}
        )
        if not self.queue:
            raise RuntimeError("QueueLLM has no responses queued")
        return self.queue.pop(0)


def _claim(pattern, predicate, slots, polarity=1, source_text="<src>"):
    return {
        "pattern": pattern, "predicate": predicate, "slots": slots,
        "polarity": polarity, "source_text": source_text,
    }


# ---------- happy paths: each type ----------


def test_int_prompt_does_not_leak_value():
    llm = QueueLLM(queue=[{
        "prompt": "Compute the number of times 'r' appears in 'strawperpy'. Print only the integer.",
        "expected_output_type": "int",
    }])
    out = build_code_prompt(
        _claim("quantitative", "has_count",
               {"subject": "strawperpy", "property": "letter_r", "value": 7}),
        llm,
    )
    assert isinstance(out, CodePrompt)
    assert out.expected_output_type == "int"
    assert "7" not in out.prompt
    assert out.compromised is False
    # First (and only) attempt was clean.
    assert len(out.attempts) == 1
    assert out.attempts[0].leak_detected is False


def test_string_prompt_does_not_leak_subject():
    llm = QueueLLM(queue=[{
        "prompt": "Compute the reverse of the string 'egalitarian'. Print only the result.",
        "expected_output_type": "string",
    }])
    out = build_code_prompt(
        _claim("relational", "reverse_of",
               {"subject": "nairatilage", "object": "egalitarian"}),
        llm,
    )
    assert "nairatilage" not in out.prompt
    assert out.compromised is False


def test_bool_prompt_unchanged_no_leak_to_check():
    llm = QueueLLM(queue=[{
        "prompt": "Decide whether 'listen' and 'silent' have the same multiset of letters. Print True or False.",
        "expected_output_type": "bool",
    }])
    out = build_code_prompt(
        _claim("relational", "is_anagram_of",
               {"subject": "listen", "object": "silent"}),
        llm,
    )
    assert out.expected_output_type == "bool"
    assert out.attempts[0].leak_detected is False


def test_float_prompt():
    llm = QueueLLM(queue=[{
        "prompt": "Compute the square root of 2. Print only the float.",
        "expected_output_type": "float",
    }])
    out = build_code_prompt(
        _claim("quantitative", "sqrt_equals", {"subject": 2, "value": 1.4142135}),
        llm,
    )
    assert out.expected_output_type == "float"


def test_list_prompt_type():
    llm = QueueLLM(queue=[{
        "prompt": "Compute the sorted unique characters of 'banana'. Print as a JSON list.",
        "expected_output_type": "list",
    }])
    out = build_code_prompt(
        _claim("quantitative", "sorted_unique_chars",
               {"subject": "banana", "value": ["a", "b", "n"]}),
        llm,
    )
    assert out.expected_output_type == "list"


# ---------- leak detection + retry ----------


def test_leak_detected_triggers_retry():
    llm = QueueLLM(queue=[
        # First attempt leaks "7"
        {"prompt": "Verify that 'strawperpy' has 7 r's. Print only the integer.",
         "expected_output_type": "int"},
        # Retry is clean
        {"prompt": "Compute the count of 'r' in 'strawperpy'. Print only the integer.",
         "expected_output_type": "int"},
    ])
    out = build_code_prompt(
        _claim("quantitative", "has_count",
               {"subject": "strawperpy", "property": "letter_r", "value": 7}),
        llm,
    )
    assert len(out.attempts) == 2
    assert out.attempts[0].leak_detected is True
    assert out.attempts[1].leak_detected is False
    assert out.compromised is False
    # Final prompt is the retry, not the leaky first.
    assert "7" not in out.prompt
    # Two LLM calls were made.
    assert len(llm.calls) == 2


def test_both_attempts_leak_marks_compromised():
    llm = QueueLLM(queue=[
        {"prompt": "Compute count; expect 7.", "expected_output_type": "int"},
        {"prompt": "Confirm 7 r's in strawperpy.", "expected_output_type": "int"},
    ])
    out = build_code_prompt(
        _claim("quantitative", "has_count",
               {"subject": "strawperpy", "property": "letter_r", "value": 7}),
        llm,
    )
    assert len(out.attempts) == 2
    assert out.compromised is True
    assert all(a.leak_detected for a in out.attempts)


def test_word_boundary_prevents_false_positive():
    """Asserted value 25 must not match '125' or '1259' inside a longer number."""
    p = "Compute the count of x in '1259'. Print only the integer."
    claim = _claim("quantitative", "has_count",
                   {"subject": "1259", "property": "x", "value": 25})
    assert detect_leak(p, claim) is False


def test_word_boundary_catches_exact_match():
    p = "Verify that the count is 25 in 'whatever'. Print 25."
    claim = _claim("quantitative", "has_count",
                   {"subject": "whatever", "property": "x", "value": 25})
    assert detect_leak(p, claim) is True


def test_string_leak_detection_is_case_insensitive():
    p = "Compute the reverse — should equal NaiRatiLage."
    claim = _claim("relational", "reverse_of",
                   {"subject": "nairatilage", "object": "egalitarian"})
    assert detect_leak(p, claim) is True


def test_short_string_value_not_flagged():
    """Short generic strings would create false positives — skip them."""
    p = "Compute whatever. Print 'a'."
    # subject="a" wouldn't be flagged because len < 3 in the heuristic.
    claim = _claim("relational", "reverse_of", {"subject": "a", "object": "a"})
    assert detect_leak(p, claim) is False


def test_bool_value_never_flagged():
    """For booleans, 'true'/'false' appear naturally — leak detector skips them."""
    p = "Compute whether the strings are anagrams. Print True or False."
    claim = _claim("relational", "is_anagram_of",
                   {"subject": "listen", "object": "silent"})
    # Claimed value is True (boolean) — skip leak check.
    assert detect_leak(p, claim) is False


# ---------- expected_output_type validation ----------


def test_unknown_output_type_coerced_to_string():
    """If the LLM returns an unrecognized type, coerce to 'string'."""
    llm = QueueLLM(queue=[{
        "prompt": "Compute something benign.",
        "expected_output_type": "made_up_type",
    }])
    out = build_code_prompt(
        _claim("relational", "reverse_of",
               {"subject": "abc", "object": "cba"}),
        llm,
    )
    assert out.expected_output_type == "string"
