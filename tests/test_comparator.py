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


# ---------- additional coverage gaps ----------


def test_string_preserves_internal_whitespace():
    """The string parser strips only the trailing newline — internal
    whitespace must be preserved verbatim."""
    c = _claim("relational", "reverse_of",
               {"subject": "  hello  world  ", "object": "x"})
    r = compare(c, "  hello  world  \n", "string")
    # The trailing \n is stripped; the leading/internal whitespace stays.
    assert r.computed_value == "  hello  world  "


def test_string_preserves_no_trailing_newline():
    """When stdout doesn't end with \\n, return as-is."""
    c = _claim("relational", "reverse_of", {"subject": "abc", "object": "x"})
    r = compare(c, "abc", "string")
    assert r.computed_value == "abc"


def test_string_strips_crlf():
    """Windows-style CRLF should be stripped just like LF."""
    c = _claim("relational", "reverse_of", {"subject": "abc", "object": "x"})
    r = compare(c, "abc\r\n", "string")
    assert r.computed_value == "abc"


def test_int_tolerates_fractional_zero():
    """'42.0' should parse as 42 — generated code sometimes prints as
    float."""
    c = _claim("quantitative", "has_count", {"value": 42})
    r = compare(c, "42.0\n", "int")
    assert r.verdict == "verified"
    assert r.computed_value == 42


def test_int_rejects_non_integer_float():
    c = _claim("quantitative", "has_count", {"value": 3})
    r = compare(c, "3.5\n", "int")
    assert r.verdict == "comparison_error"


def test_bool_accepts_yes_no():
    c = _claim("quantitative", "is_true", {"value": True})
    assert compare(c, "yes\n", "bool").computed_value is True
    assert compare(c, "no\n", "bool").computed_value is False


def test_bool_accepts_1_0():
    c = _claim("quantitative", "is_true", {"value": True})
    assert compare(c, "1\n", "bool").computed_value is True
    assert compare(c, "0\n", "bool").computed_value is False


def test_bool_rejects_garbage():
    c = _claim("quantitative", "is_true", {"value": True})
    r = compare(c, "maybe\n", "bool")
    assert r.verdict == "comparison_error"


def test_list_non_list_json_yields_comparison_error():
    c = _claim("quantitative", "list_thing", {"value": [1, 2, 3]})
    r = compare(c, '"not a list"\n', "list")
    assert r.verdict == "comparison_error"


def test_unknown_expected_type_yields_comparison_error():
    c = _claim("quantitative", "has_count", {"value": 3})
    r = compare(c, "3\n", "tuple")  # not a supported type
    assert r.verdict == "comparison_error"
