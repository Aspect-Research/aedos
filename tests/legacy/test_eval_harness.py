"""Tests for the eval harness helpers (substring extraction +
classification). The harness itself is run-and-observe; we just lock
in the comparison logic."""

from __future__ import annotations

from scripts.eval_harness import (
    _classify_turn,
    _expected_substrings,
    _matches_expected,
)


def test_substring_keeps_multi_word_proper_nouns():
    out = _expected_substrings(
        "Matthew Prince, Lee Holloway, Michelle Zatlyn"
    )
    assert out == ["matthew prince", "lee holloway", "michelle zatlyn"]


def test_substring_drops_parentheticals():
    out = _expected_substrings("8,848.86 m (Nepal/China joint)")
    # The number is dropped because it has special chars and short
    # tokens, but the key intent is parentheticals don't leak.
    assert all("nepal" not in s and "china" not in s and "joint" not in s
               for s in out)


def test_substring_handles_or_and():
    out = _expected_substrings("Maine and New Hampshire or Vermont")
    assert "maine" in out
    assert "new hampshire" in out
    assert "vermont" in out


def test_substring_skips_negative_phrases():
    """'no such novel exists' should not produce 'no such novel' as a
    needle — we don't want to match haystacks that say 'there is no
    such novel'."""
    out = _expected_substrings("no such novel exists")
    assert all(not s.startswith("no ") for s in out)


def test_matches_expected_substring_match():
    assert _matches_expected("Tokyo was called Edo before 1868.", "Edo")
    assert _matches_expected("the answer is thimphu", "Thimphu")


def test_matches_expected_no_match():
    assert not _matches_expected("Tokyo was called Yamato.", "Edo")
    assert not _matches_expected("", "Edo")


def test_classify_caught_when_raw_wrong_and_aedos_right():
    cls = _classify_turn(
        raw_resp="The answer is Yamato.",
        aedos_resp="Actually, it was Edo.",
        expected="Edo",
        intervened=True,
        verdicts=["contradicted"],
    )
    assert cls == "caught"


def test_classify_preserved_when_both_correct():
    cls = _classify_turn(
        raw_resp="Edo was the name.",
        aedos_resp="Edo was the name.",
        expected="Edo",
        intervened=False,
        verdicts=["verified"],
    )
    assert cls == "preserved"


def test_classify_broken_when_raw_right_but_aedos_wrong():
    cls = _classify_turn(
        raw_resp="The answer is Edo.",
        aedos_resp="The answer is Yamato.",
        expected="Edo",
        intervened=True,
        verdicts=["contradicted"],
    )
    assert cls == "broken"


def test_classify_missed_when_neither_has_it():
    cls = _classify_turn(
        raw_resp="The answer is Yamato.",
        aedos_resp="The answer is Yamato.",
        expected="Edo",
        intervened=False,
        verdicts=["verified"],
    )
    assert cls == "missed"


def test_classify_uncertain_when_inconclusive():
    cls = _classify_turn(
        raw_resp="Edo",
        aedos_resp="I think Edo",
        expected="Edo",
        intervened=True,
        verdicts=["retrieval_inconclusive"],
    )
    assert cls == "uncertain"


def test_classify_uncertain_for_retrieval_failed():
    cls = _classify_turn(
        raw_resp="Edo",
        aedos_resp="Edo",
        expected="Edo",
        intervened=False,
        verdicts=["retrieval_failed"],
    )
    assert cls == "uncertain"
