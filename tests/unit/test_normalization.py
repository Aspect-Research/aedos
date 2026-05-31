"""Tests for predicate normalization.

v0.16 WS1 (Decision 1.g): the surface->canonical synonym map (`_CANONICAL_MAP`)
was DELETED from `normalization.py`. `normalize_predicate` is now MECHANICAL
ONLY (lower/strip -> underscore<->space -> strip ONE leading auxiliary verb ->
strip ONE trailing article -> snake_case). Predicate synonymy is no longer a
hardcoded lookup table: it is carried by the substrate's multi-property binding
discovery (Wikidata ontology + SLING) and the seed pack's canonical rows. The
former synonym-mapping tests (e.g. "served as" -> "holds_role", "won" ->
"received_award", "works at" -> "employed_by") are reclassified below:

  - the mechanical-normalization assertions are KEPT (and corrected to the
    mechanical output where the map previously rewrote the surface form);
  - the pure-synonym expectations move to `TestSynonymyIsNotMechanical`, which
    pins that normalization NO LONGER collapses synonyms (the synonym surface
    now flows through to discovery as its mechanical snake_case form).
"""

from __future__ import annotations

import pytest

from aedos.layer1_extraction import normalization
from aedos.layer1_extraction.normalization import normalize_predicate


class TestCommonForms:
    def test_snake_case_passthrough(self):
        assert normalize_predicate("employed_by") == "employed_by"

    def test_simple_space_to_snake(self):
        # "lives in" -> mechanical snake_case "lives_in"
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
        # v0.16 WS1: mechanical only. "was born" strips the "was" auxiliary and
        # snake_cases the remainder -> "born". The old map rewrote it to
        # "born_in"; that rewrite is now discovery's job, not normalization's.
        assert normalize_predicate("was born") == "born"

    def test_was_awarded(self):
        # v0.16 WS1: mechanical only. "was awarded" -> strip "was" -> "awarded".
        # (The old map collapsed it to "received_award".)
        assert normalize_predicate("was awarded") == "awarded"

    def test_was_awarded_the(self):
        # v0.16 WS1: strip "was" auxiliary, strip trailing "the" article ->
        # "awarded". (The old map collapsed it to "received_award".)
        assert normalize_predicate("was awarded the") == "awarded"


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
        # v0.16 WS1: mechanical only. "is situated in" -> strip "is" ->
        # "situated_in". (The old map rewrote "situated in" -> "located_in";
        # that synonymy is now discovery's job.)
        assert normalize_predicate("is situated in") == "situated_in"

    def test_is_affiliated_with(self):
        assert normalize_predicate("is affiliated with") == "affiliated_with"


class TestMechanicalSnakeCase:
    """Formerly TestCanonicalMap. These inputs whose mechanical snake_case form
    happens to equal their canonical form survive unchanged. The two pure-
    synonym cases (`served as` -> holds_role, `won` -> received_award) moved to
    TestSynonymyIsNotMechanical below."""

    def test_graduated_from(self):
        # No auxiliary, no article; mechanical snake_case is already canonical.
        assert normalize_predicate("graduated from") == "graduated_from"

    def test_co_founded_hyphen(self):
        # Hyphen is a non-word char -> underscore.
        assert normalize_predicate("co-founded") == "co_founded"

    def test_co_founded_space(self):
        assert normalize_predicate("co founded") == "co_founded"


class TestSynonymyIsNotMechanical:
    """v0.16 WS1 (Decision 1.g): `normalize_predicate` no longer collapses
    surface synonyms onto a canonical predicate. The deleted `_CANONICAL_MAP`
    entries now flow through as their MECHANICAL snake_case form, and the
    substrate's binding discovery (Wikidata ontology + SLING) resolves the KB
    property at consult time. These assertions pin that the rewrite is GONE —
    a regression that re-introduced a hardcoded synonym map would fail here."""

    def test_served_as_is_not_collapsed_to_holds_role(self):
        # Pre-v0.16: "served as" -> "holds_role". Now mechanical only.
        result = normalize_predicate("served as")
        assert result == "served_as"
        assert result != "holds_role"

    def test_won_is_not_collapsed_to_received_award(self):
        # Pre-v0.16: "won" -> "received_award". Now passes through unchanged.
        result = normalize_predicate("won")
        assert result == "won"
        assert result != "received_award"

    def test_works_at_is_not_collapsed_to_employed_by(self):
        # Pre-v0.16 (Phase H Cluster 3): "works at" / "works_at" -> "employed_by".
        # Now both normalize mechanically to "works_at"; the employed_by
        # synonymy is carried by discovery / the seed pack's canonical row.
        assert normalize_predicate("works at") == "works_at"
        assert normalize_predicate("works_at") == "works_at"

    def test_canonical_map_symbol_is_deleted(self):
        # Structural guard: the hardcoded lookup table itself must not return.
        # (No hardcoded mappings — knowledge belongs in prompt/KB/oracle.)
        assert not hasattr(normalization, "_CANONICAL_MAP")


class TestAdversarialForms:
    def test_irregular_passive_born(self):
        # v0.16 WS1: mechanical only. "was born" strips the "was" auxiliary ->
        # "born". (Pre-v0.16 the map rewrote it to "born_in"; that completion is
        # now discovery's job, not a hardcoded normalization rule.)
        assert normalize_predicate("was born") == "born"

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
    normalize EQUIVALENTLY to their space-separated counterparts — the
    underscore<->space mechanical step guarantees `works_at` and "works at"
    land on the same string.

    v0.16 WS1 (Decision 1.g): the canonical-map rewrite `works at` ->
    `employed_by` is DELETED. The invariant that survives — and is the point of
    this class — is that the two SURFACE FORMS normalize identically, NOT that
    they map to `employed_by`. (The `works_at` -> employed_by synonymy now lives
    in discovery; see TestSynonymyIsNotMechanical.)"""

    def test_works_at_underscored_equals_spaced(self):
        # The load-bearing invariant: both forms normalize identically. Their
        # common mechanical form is "works_at" (no longer "employed_by").
        assert normalize_predicate("works_at") == normalize_predicate("works at")
        assert normalize_predicate("works_at") == "works_at"

    def test_underscored_equivalent_to_spaced(self):
        for surface in ["works at", "is employed by", "was born in", "died in",
                         "is located in", "is a", "won the", "graduated from"]:
            underscored = surface.replace(" ", "_")
            assert normalize_predicate(surface) == normalize_predicate(underscored), (
                f"divergence for {surface!r} vs {underscored!r}"
            )

    def test_already_canonical_passthrough(self):
        # A name already in canonical snake_case passes through unchanged.
        assert normalize_predicate("employed_by") == "employed_by"
        assert normalize_predicate("located_in") == "located_in"

    def test_aux_prefix_with_underscores(self):
        # "is_employed_by" → space form "is employed by" → aux strip →
        # "employed by" → snake_case → "employed_by" (mechanical, no map).
        assert normalize_predicate("is_employed_by") == "employed_by"
