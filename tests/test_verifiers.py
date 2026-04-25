"""Tests for src.verifiers.python_verifiers."""

from __future__ import annotations

import json

import pytest

from src.verifiers.python_verifiers import (
    VerificationOutcome,
    get_verifier,
    verify_contains_substring,
    verify_equals,
    verify_greater_than,
    verify_has_count,
    verify_has_length,
    verify_is_anagram_of,
    verify_less_than,
    verify_product_equals,
    verify_spelled_as,
    verify_sum_equals,
)


def _claim(subject, object, polarity=1, object_type="string"):
    return {
        "subject": subject,
        "predicate": "irrelevant",
        "object": object if isinstance(object, str) else str(object),
        "object_type": object_type,
        "polarity": polarity,
        "source_text": "",
    }


# ---------- has_count ----------


def test_has_count_strawberry_zero_ps_contradicts_claim_of_three():
    r = verify_has_count(_claim("strawberry", json.dumps({"item": "p", "count": 3})))
    assert r.outcome is VerificationOutcome.CONTRADICTED
    correction = json.loads(r.actual_value)
    assert correction == {"item": "p", "count": 0}


def test_has_count_strawberry_three_rs_verified():
    r = verify_has_count(_claim("strawberry", json.dumps({"item": "r", "count": 3})))
    assert r.outcome is VerificationOutcome.VERIFIED


def test_has_count_polarity_zero_plus_zero_match_is_verified():
    # "strawberry does NOT have 3 p's" (polarity=0). Actual is 0, so the
    # positive form is false, and the negative form is TRUE — verified.
    r = verify_has_count(
        _claim("strawberry", json.dumps({"item": "p", "count": 3}), polarity=0)
    )
    assert r.outcome is VerificationOutcome.VERIFIED


def test_has_count_case_insensitive():
    r = verify_has_count(_claim("Mississippi", json.dumps({"item": "s", "count": 4})))
    assert r.outcome is VerificationOutcome.VERIFIED


def test_has_count_multi_char_substring():
    r = verify_has_count(_claim("abracadabra", json.dumps({"item": "bra", "count": 2})))
    assert r.outcome is VerificationOutcome.VERIFIED


def test_has_count_malformed_object_is_inconclusive():
    r = verify_has_count(_claim("strawberry", "not json at all"))
    assert r.outcome is VerificationOutcome.INCONCLUSIVE


def test_has_count_missing_field_is_inconclusive():
    r = verify_has_count(_claim("strawberry", json.dumps({"item": "p"})))
    assert r.outcome is VerificationOutcome.INCONCLUSIVE


# ---------- spelled_as ----------


def test_spelled_as_hyphenated():
    r = verify_spelled_as(_claim("strawberry", "s-t-r-a-w-b-e-r-r-y"))
    assert r.outcome is VerificationOutcome.VERIFIED


def test_spelled_as_plain():
    r = verify_spelled_as(_claim("Strawberry", "strawberry"))
    assert r.outcome is VerificationOutcome.VERIFIED


def test_spelled_as_wrong_contradicts():
    r = verify_spelled_as(_claim("strawberry", "s-t-r-a-b-e-r-r-y"))  # missing letters
    assert r.outcome is VerificationOutcome.CONTRADICTED


def test_spelled_as_empty_is_inconclusive():
    r = verify_spelled_as(_claim("", "foo"))
    assert r.outcome is VerificationOutcome.INCONCLUSIVE


# ---------- has_length ----------


@pytest.mark.parametrize(
    "subject,claimed,outcome",
    [
        ("hello", 5, VerificationOutcome.VERIFIED),
        ("hello", 4, VerificationOutcome.CONTRADICTED),
        ("", 0, VerificationOutcome.VERIFIED),
        ("abc", 10, VerificationOutcome.CONTRADICTED),
    ],
)
def test_has_length(subject, claimed, outcome):
    r = verify_has_length(_claim(subject, str(claimed)))
    assert r.outcome is outcome


def test_has_length_non_int_object_is_inconclusive():
    r = verify_has_length(_claim("hello", "five"))
    assert r.outcome is VerificationOutcome.INCONCLUSIVE


# ---------- equals ----------


def test_equals_case_and_whitespace_insensitive():
    r = verify_equals(_claim(" Foo ", "foo"))
    assert r.outcome is VerificationOutcome.VERIFIED


def test_equals_contradiction():
    r = verify_equals(_claim("foo", "bar"))
    assert r.outcome is VerificationOutcome.CONTRADICTED


# ---------- greater_than / less_than ----------


def test_greater_than_true():
    assert verify_greater_than(_claim("10", "5")).outcome is VerificationOutcome.VERIFIED


def test_greater_than_false():
    assert (
        verify_greater_than(_claim("3", "5")).outcome is VerificationOutcome.CONTRADICTED
    )


def test_greater_than_equal_is_false():
    assert (
        verify_greater_than(_claim("5", "5")).outcome is VerificationOutcome.CONTRADICTED
    )


def test_greater_than_non_numeric_is_inconclusive():
    assert verify_greater_than(_claim("ten", "5")).outcome is VerificationOutcome.INCONCLUSIVE


def test_less_than_true():
    assert verify_less_than(_claim("3", "5")).outcome is VerificationOutcome.VERIFIED


def test_less_than_false():
    assert verify_less_than(_claim("10", "5")).outcome is VerificationOutcome.CONTRADICTED


# ---------- contains_substring ----------


def test_contains_substring_true():
    r = verify_contains_substring(_claim("strawberry", "berry"))
    assert r.outcome is VerificationOutcome.VERIFIED


def test_contains_substring_false():
    r = verify_contains_substring(_claim("apple", "berry"))
    assert r.outcome is VerificationOutcome.CONTRADICTED


def test_contains_substring_case_insensitive():
    r = verify_contains_substring(_claim("HELLO WORLD", "world"))
    assert r.outcome is VerificationOutcome.VERIFIED


# ---------- is_anagram_of ----------


def test_is_anagram_listen_silent():
    r = verify_is_anagram_of(_claim("listen", "silent"))
    assert r.outcome is VerificationOutcome.VERIFIED


def test_is_anagram_not():
    r = verify_is_anagram_of(_claim("hello", "world"))
    assert r.outcome is VerificationOutcome.CONTRADICTED


def test_is_anagram_case_insensitive():
    r = verify_is_anagram_of(_claim("Listen", "SILENT"))
    assert r.outcome is VerificationOutcome.VERIFIED


def test_is_anagram_ignores_spaces():
    r = verify_is_anagram_of(_claim("dormitory", "dirty room"))
    assert r.outcome is VerificationOutcome.VERIFIED


# ---------- sum_equals / product_equals ----------


def test_sum_equals_verified():
    r = verify_sum_equals(_claim(json.dumps([2, 3, 4]), "9"))
    assert r.outcome is VerificationOutcome.VERIFIED


def test_sum_equals_contradicted_returns_actual():
    r = verify_sum_equals(_claim(json.dumps([2, 3, 4]), "10"))
    assert r.outcome is VerificationOutcome.CONTRADICTED
    assert r.actual_value == 9


def test_sum_equals_bad_subject_is_inconclusive():
    r = verify_sum_equals(_claim("not-a-list", "9"))
    assert r.outcome is VerificationOutcome.INCONCLUSIVE


def test_product_equals_verified():
    r = verify_product_equals(_claim(json.dumps([2, 3, 4]), "24"))
    assert r.outcome is VerificationOutcome.VERIFIED


def test_product_equals_contradicted():
    r = verify_product_equals(_claim(json.dumps([2, 3]), "7"))
    assert r.outcome is VerificationOutcome.CONTRADICTED


# ---------- registry ----------


def test_get_verifier_by_name():
    assert get_verifier("verify_has_count") is verify_has_count
    assert get_verifier("verify_spelled_as") is verify_spelled_as


def test_get_unknown_verifier_raises():
    with pytest.raises(KeyError):
        get_verifier("verify_telepathy")


def test_every_python_predicate_has_a_verifier_registered():
    """The predicate registry declares python_verifier names that must resolve."""
    from src.predicate_registry import load_default_registry

    reg = load_default_registry()
    for p in reg.by_method("python"):
        # Should not raise.
        get_verifier(p.python_verifier)
