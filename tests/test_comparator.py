"""Tests for src.verifiers.code_generation.comparator (v0.4)."""

from __future__ import annotations

import json

import pytest

from src.verifiers.code_generation.comparator import compare


def _claim(pattern, predicate, slots, polarity=1):
    return {
        "pattern": pattern, "predicate": predicate, "slots": slots,
        "polarity": polarity, "source_text": "<src>",
    }


# ---------- int ----------


def test_int_match_verifies():
    c = _claim("quantitative", "has_count",
               {"subject": "x", "property": "y", "value": 3})
    r = compare(c, "3\n", "int")
    assert r.verdict == "verified"
    assert r.claimed_value == 3
    assert r.computed_value == 3


def test_int_mismatch_contradicts():
    c = _claim("quantitative", "has_count",
               {"subject": "x", "property": "y", "value": 3})
    r = compare(c, "0\n", "int")
    assert r.verdict == "contradicted"
    assert r.computed_value == 0


def test_int_polarity_zero_match_contradicts():
    """polarity=0 means the claim asserts NOT 3. If actual is 3, claim is wrong."""
    c = _claim("quantitative", "has_count",
               {"subject": "x", "property": "y", "value": 3}, polarity=0)
    r = compare(c, "3\n", "int")
    assert r.verdict == "contradicted"


def test_int_polarity_zero_mismatch_verifies():
    c = _claim("quantitative", "has_count",
               {"subject": "x", "property": "y", "value": 3}, polarity=0)
    r = compare(c, "0\n", "int")
    assert r.verdict == "verified"


def test_int_tolerates_float_with_zero_fraction():
    c = _claim("quantitative", "has_count",
               {"subject": "x", "property": "y", "value": 3})
    r = compare(c, "3.0\n", "int")
    assert r.verdict == "verified"


# ---------- float ----------


def test_float_close_enough():
    c = _claim("quantitative", "sqrt_equals", {"subject": 2, "value": 1.4142135})
    r = compare(c, "1.41421356237\n", "float")
    # math.isclose with default tolerances rejects this; tighten asserts.
    # Default rel_tol=1e-9 — these differ at ~1e-7. Should NOT match.
    assert r.verdict == "contradicted"


def test_float_exact_match():
    c = _claim("quantitative", "sqrt_equals", {"subject": 4, "value": 2.0})
    r = compare(c, "2.0\n", "float")
    assert r.verdict == "verified"


# ---------- string ----------


def test_string_match():
    c = _claim("relational", "reverse_of",
               {"subject": "nairatilage", "object": "egalitarian"})
    r = compare(c, "nairatilage\n", "string")
    assert r.verdict == "verified"


def test_string_mismatch_contradicts():
    c = _claim("relational", "reverse_of",
               {"subject": "wrong", "object": "egalitarian"})
    r = compare(c, "nairatilage\n", "string")
    assert r.verdict == "contradicted"


def test_string_preserves_internal_whitespace():
    c = _claim("relational", "reverse_of",
               {"subject": "hello world", "object": "dlrow olleh"})
    r = compare(c, "hello world\n", "string")
    assert r.verdict == "verified"


def test_string_strips_only_trailing_newline():
    c = _claim("relational", "reverse_of",
               {"subject": "abc\n", "object": "cba"})
    # If the claim subject literally has a trailing newline, only the
    # final \n in stdout is stripped — internal newlines remain.
    r = compare(c, "abc\n\n", "string")
    # Computed = "abc\n", claimed = "abc\n" → match.
    assert r.verdict == "verified"


# ---------- bool ----------


def test_bool_true_polarity_one_verifies():
    c = _claim("relational", "is_anagram_of",
               {"subject": "listen", "object": "silent"}, polarity=1)
    r = compare(c, "True\n", "bool")
    assert r.verdict == "verified"


def test_bool_false_polarity_one_contradicts():
    c = _claim("relational", "is_anagram_of",
               {"subject": "listen", "object": "world"}, polarity=1)
    r = compare(c, "False\n", "bool")
    assert r.verdict == "contradicted"


def test_bool_false_polarity_zero_verifies():
    """The claim says they're NOT anagrams; code says False (not anagrams). Verified."""
    c = _claim("relational", "is_anagram_of",
               {"subject": "listen", "object": "world"}, polarity=0)
    r = compare(c, "False\n", "bool")
    assert r.verdict == "verified"


def test_bool_alternate_truthy_strings():
    c = _claim("relational", "is_anagram_of",
               {"subject": "x", "object": "y"})
    assert compare(c, "true\n", "bool").verdict == "verified"
    assert compare(c, "1\n", "bool").verdict == "verified"
    assert compare(c, "yes\n", "bool").verdict == "verified"


# ---------- list ----------


def test_list_exact_match():
    c = _claim("quantitative", "sorted_chars",
               {"subject": "banana", "value": ["a", "b", "n"]})
    r = compare(c, json.dumps(["a", "b", "n"]) + "\n", "list")
    assert r.verdict == "verified"


def test_list_order_sensitive_by_default():
    c = _claim("quantitative", "sorted_chars",
               {"subject": "banana", "value": ["a", "b", "n"]})
    r = compare(c, json.dumps(["b", "a", "n"]) + "\n", "list")
    assert r.verdict == "contradicted"


# ---------- parse failures ----------


def test_int_parse_failure_yields_comparison_error():
    c = _claim("quantitative", "has_count", {"value": 3})
    r = compare(c, "not a number\n", "int")
    assert r.verdict == "comparison_error"


def test_list_invalid_json_yields_comparison_error():
    c = _claim("quantitative", "list_thing", {"value": [1, 2]})
    r = compare(c, "[1, 2,]\n", "list")
    # Python's json.loads rejects trailing commas.
    assert r.verdict == "comparison_error"


def test_empty_stdout_yields_comparison_error():
    c = _claim("quantitative", "has_count", {"value": 3})
    r = compare(c, "", "int")
    assert r.verdict == "comparison_error"


def test_unknown_pattern_yields_comparison_error():
    """If extract_claimed_value can't determine the asserted slot, surface it."""
    c = _claim("propositional_attitude", "feels", {"agent": "user", "proposition": "x"})
    r = compare(c, "5\n", "int")
    assert r.verdict == "comparison_error"
