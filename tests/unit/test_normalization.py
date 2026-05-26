"""Tests for predicate normalization."""

from __future__ import annotations

import pytest

from aedos.layer1_extraction.normalization import normalize_predicate


class TestCommonForms:
    def test_snake_case_passthrough(self):
        assert normalize_predicate("employed_by") == "employed_by"

    def test_simple_space_to_snake(self):
        # "lives in" not in map → snake_case fallback
        result = normalize_predicate("lives in")
        assert "_" in result
        assert " " not in result

    def test_lowercase_output(self):
        result = normalize_predicate("LIVES_IN")
        assert result == result.lower()

    def test_empty_string_returns_unknown(self):
        result = normalize_predicate("")
        assert result == "unknown_predicate"

    def test_whitespace_only_returns_unknown(self):
        result = normalize_predicate("   ")
        assert result == "unknown_predicate"


class TestTenseStripping:
    def test_is_prefix_stripped(self):
        # "is employed by" → "employed_by"
        assert normalize_predicate("is employed by") == "employed_by"

    def test_was_prefix_stripped(self):
        assert normalize_predicate("was employed by") == "employed_by"

    def test_was_born_in(self):
        assert normalize_predicate("was born in") == "born_in"

    def test_was_born_without_in(self):
        assert normalize_predicate("was born") == "born_in"

    def test_was_awarded(self):
        assert normalize_predicate("was awarded") == "received_award"

    def test_was_awarded_the(self):
        assert normalize_predicate("was awarded the") == "received_award"


class TestVoiceNeutral:
    def test_passive_awarded_the_prize(self):
        # "was awarded the prize" → strip "was", strip trailing "prize" (not article),
        # result should not contain "was"
        result = normalize_predicate("was awarded the prize")
        assert "was" not in result
        assert " " not in result

    def test_is_employed_by(self):
        assert normalize_predicate("is employed by") == "employed_by"

    def test_is_located_in(self):
        assert normalize_predicate("is located in") == "located_in"

    def test_is_situated_in(self):
        assert normalize_predicate("is situated in") == "located_in"

    def test_is_affiliated_with(self):
        assert normalize_predicate("is affiliated with") == "affiliated_with"


class TestCanonicalMap:
    def test_graduated_from(self):
        assert normalize_predicate("graduated from") == "graduated_from"

    def test_served_as(self):
        assert normalize_predicate("served as") == "holds_role"

    def test_co_founded_hyphen(self):
        assert normalize_predicate("co-founded") == "co_founded"

    def test_co_founded_space(self):
        assert normalize_predicate("co founded") == "co_founded"

    def test_won(self):
        assert normalize_predicate("won") == "received_award"


class TestAdversarialForms:
    def test_irregular_passive_born(self):
        # "was born" — irregular; should not normalize to "born" but to "born_in"
        assert normalize_predicate("was born") == "born_in"

    def test_different_tenses_same_canonical(self):
        # "is employed by" and "was employed by" should give same result
        assert normalize_predicate("is employed by") == normalize_predicate("was employed by")

    def test_trailing_article_stripped(self):
        result = normalize_predicate("is a member of the")
        # "is a member of the" → strip "is", check "a member of the" → strip "the" → "a member of"
        # then → "a_member_of"
        assert not result.endswith("_the")

    def test_no_spaces_in_output(self):
        result = normalize_predicate("was a founding member of")
        assert " " not in result

    def test_non_ascii_stripped_gracefully(self):
        # Should not crash
        result = normalize_predicate("résides dans")
        assert isinstance(result, str)
        assert len(result) > 0


class TestUnderscoredInput:
    """Phase H Cluster 3 (2026-05-26): underscored surface forms should
    canonicalize equivalently to their space-separated counterparts.
    Pre-Cluster-3 an extractor that produced `works_at` would emit that
    string verbatim (snake_case fallback), bypassing the canonical map's
    `works at` → `employed_by` rule. Post-Cluster-3, both forms produce
    `employed_by`."""

    def test_works_at_underscored(self):
        assert normalize_predicate("works_at") == "employed_by"

    def test_works_at_spaced(self):
        assert normalize_predicate("works at") == "employed_by"

    def test_underscored_equivalent_to_spaced(self):
        for surface in ["works at", "is employed by", "was born in", "died in",
                         "is located in", "is a", "won the", "graduated from"]:
            underscored = surface.replace(" ", "_")
            assert normalize_predicate(surface) == normalize_predicate(underscored), (
                f"divergence for {surface!r} vs {underscored!r}"
            )

    def test_already_canonical_passthrough(self):
        # A name that's already in canonical form (and not in the map) falls
        # through to the snake_case fallback unchanged.
        assert normalize_predicate("employed_by") == "employed_by"
        assert normalize_predicate("located_in") == "located_in"

    def test_aux_prefix_with_underscores(self):
        # "is_employed_by" → space form "is employed by" → aux strip →
        # "employed by" → map hit → "employed_by".
        assert normalize_predicate("is_employed_by") == "employed_by"
