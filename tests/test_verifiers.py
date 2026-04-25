"""Tests for src.verifiers.python_verifiers (v0.3 — slot-based)."""

from __future__ import annotations

import json

import pytest
# v0.3: tests rewritten in Section 4. Remove the global skip.

from src.verifiers.python_verifiers import (
    VerificationOutcome,
    get_verifier,
    verify_contains_substring,
    verify_has_count,
    verify_has_length,
    verify_is_anagram_of,
    verify_product_equals,
    verify_sum_equals,
)


def _claim(pattern, predicate, slots, polarity=1):
    return {
        "pattern": pattern,
        "predicate": predicate,
        "slots": slots,
        "polarity": polarity,
        "source_text": "",
    }


# ---------- has_count ----------


def test_has_count_strawberry_three_p_contradicted():
    c = _claim("quantitative", "has_count",
               {"subject": "strawberry", "property": "letter_p", "value": 3})
    r = verify_has_count(c)
    assert r.outcome is VerificationOutcome.CONTRADICTED
    assert r.actual_value == 0


def test_has_count_strawberry_three_r_verified():
    c = _claim("quantitative", "has_count",
               {"subject": "strawberry", "property": "letter_r", "value": 3})
    r = verify_has_count(c)
    assert r.outcome is VerificationOutcome.VERIFIED


def test_has_count_polarity_zero_with_zero_match_verified():
    """'strawberry does NOT have 3 p's' (polarity=0). Actual is 0. Verified."""
    c = _claim("quantitative", "has_count",
               {"subject": "strawberry", "property": "letter_p", "value": 3},
               polarity=0)
    r = verify_has_count(c)
    assert r.outcome is VerificationOutcome.VERIFIED


def test_has_count_property_without_letter_prefix():
    """Bare 'p' should also work (the extractor may not use the letter_ prefix)."""
    c = _claim("quantitative", "has_count",
               {"subject": "strawberry", "property": "p", "value": 0})
    r = verify_has_count(c)
    assert r.outcome is VerificationOutcome.VERIFIED


def test_has_count_multi_char_substring():
    c = _claim("quantitative", "has_count",
               {"subject": "abracadabra", "property": "bra", "value": 2})
    r = verify_has_count(c)
    assert r.outcome is VerificationOutcome.VERIFIED


def test_has_count_missing_subject_inconclusive():
    c = _claim("quantitative", "has_count", {"property": "p", "value": 1})
    r = verify_has_count(c)
    assert r.outcome is VerificationOutcome.INCONCLUSIVE


def test_has_count_non_int_value_inconclusive():
    c = _claim("quantitative", "has_count",
               {"subject": "x", "property": "p", "value": "not-a-number"})
    r = verify_has_count(c)
    assert r.outcome is VerificationOutcome.INCONCLUSIVE


# ---------- has_length ----------


@pytest.mark.parametrize(
    "subject,value,outcome",
    [
        ("hello", 5, VerificationOutcome.VERIFIED),
        ("hello", 4, VerificationOutcome.CONTRADICTED),
        ("", 0, VerificationOutcome.VERIFIED),
        ("abc", 10, VerificationOutcome.CONTRADICTED),
    ],
)
def test_has_length(subject, value, outcome):
    r = verify_has_length(
        _claim("quantitative", "has_length", {"subject": subject, "value": value})
    )
    assert r.outcome is outcome


def test_has_length_non_int_value_inconclusive():
    r = verify_has_length(
        _claim("quantitative", "has_length", {"subject": "hello", "value": "five"})
    )
    assert r.outcome is VerificationOutcome.INCONCLUSIVE


# ---------- contains_substring ----------


def test_contains_substring_true():
    r = verify_contains_substring(
        _claim("relational", "contains_substring",
               {"subject": "strawberry", "object": "berry"})
    )
    assert r.outcome is VerificationOutcome.VERIFIED


def test_contains_substring_false():
    r = verify_contains_substring(
        _claim("relational", "contains_substring",
               {"subject": "apple", "object": "berry"})
    )
    assert r.outcome is VerificationOutcome.CONTRADICTED


def test_contains_substring_case_insensitive():
    r = verify_contains_substring(
        _claim("relational", "contains_substring",
               {"subject": "HELLO WORLD", "object": "world"})
    )
    assert r.outcome is VerificationOutcome.VERIFIED


# ---------- is_anagram_of ----------


def test_is_anagram_listen_silent():
    r = verify_is_anagram_of(
        _claim("relational", "is_anagram_of",
               {"subject": "listen", "object": "silent"})
    )
    assert r.outcome is VerificationOutcome.VERIFIED


def test_is_anagram_dormitory_dirty_room():
    r = verify_is_anagram_of(
        _claim("relational", "is_anagram_of",
               {"subject": "dormitory", "object": "dirty room"})
    )
    assert r.outcome is VerificationOutcome.VERIFIED


def test_is_anagram_not():
    r = verify_is_anagram_of(
        _claim("relational", "is_anagram_of",
               {"subject": "hello", "object": "world"})
    )
    assert r.outcome is VerificationOutcome.CONTRADICTED


# ---------- sum_equals / product_equals ----------


def test_sum_equals_verified_with_list():
    r = verify_sum_equals(
        _claim("quantitative", "sum_equals", {"subject": [2, 3, 4], "value": 9})
    )
    assert r.outcome is VerificationOutcome.VERIFIED


def test_sum_equals_verified_with_json_string():
    r = verify_sum_equals(
        _claim("quantitative", "sum_equals",
               {"subject": json.dumps([2, 3, 4]), "value": 9})
    )
    assert r.outcome is VerificationOutcome.VERIFIED


def test_sum_equals_contradicted_returns_actual():
    r = verify_sum_equals(
        _claim("quantitative", "sum_equals", {"subject": [2, 3, 4], "value": 10})
    )
    assert r.outcome is VerificationOutcome.CONTRADICTED
    assert r.actual_value == 9


def test_product_equals_verified():
    r = verify_product_equals(
        _claim("quantitative", "product_equals", {"subject": [2, 3, 4], "value": 24})
    )
    assert r.outcome is VerificationOutcome.VERIFIED


def test_product_equals_contradicted():
    r = verify_product_equals(
        _claim("quantitative", "product_equals", {"subject": [2, 3], "value": 7})
    )
    assert r.outcome is VerificationOutcome.CONTRADICTED


# ---------- registry: keyed by predicate name ----------


def test_get_verifier_returns_function():
    assert get_verifier("has_count") is verify_has_count
    assert get_verifier("is_anagram_of") is verify_is_anagram_of


def test_get_verifier_unknown_predicate_returns_none():
    """Unlike v0.2 which raised, v0.3 returns None so the router can fall through."""
    assert get_verifier("not_a_predicate") is None
    assert get_verifier("weighs") is None  # quantitative pattern but no python verifier
