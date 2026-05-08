"""Tests for the smoke corpus dispatcher (v0.14 Phase 7).

Phase 7's deliverable is shape detection + schema validation. The
end-to-end runner that consumes the dispatcher's outputs is Phase
9 territory. This file pins the schema contract:

  * Each existing corpus shape detects correctly.
  * Each shape's schema validates known-good entries.
  * Sad-path violations produce specific, path-anchored errors.
  * The live ``tests/v2/smoke_corpus.jsonl`` validates clean.
  * Duplicate IDs across the corpus are caught.
  * Cascading-failure context formatting is specified.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.smoke_dispatcher import (
    CorpusValidationResult,
    EntryValidationResult,
    FieldError,
    SmokeEntryShape,
    detect_shape,
    format_cascading_failure,
    validate_corpus,
    validate_entry,
)


CORPUS_PATH = Path(__file__).parent / "smoke_corpus.jsonl"


# ============================================================================
# Shape detection
# ============================================================================


class TestDetectShape:
    """One test per shape — pin the discriminator that picks each."""

    def test_substrate_direct_via_oracle_call(self):
        entry = {
            "id": "p5-tax-isa-clear",
            "oracle_call": {
                "oracle": "entity_taxonomy",
                "child": "golden retriever",
                "parent": "dog",
                "relation_type": "is_a",
            },
            "expected_label": "child_subsumed_by_parent",
        }
        assert detect_shape(entry) is SmokeEntryShape.SUBSTRATE_DIRECT

    def test_two_text_oracle_via_text_user_and_text_assistant(self):
        entry = {
            "id": "p3-active-passive",
            "text_user": "Asa wrote the paper",
            "text_assistant": "the paper was authored by Asa",
            "expected_oracle_classification": {
                "pattern": "relational",
                "predicate_a": "authored_by",
                "predicate_b": "wrote",
                "label": "equivalent",
                "slot_reversal": "subject_object_swap",
            },
        }
        assert detect_shape(entry) is SmokeEntryShape.TWO_TEXT_ORACLE

    def test_routing_memo_via_expected_memo_state(self):
        entry = {
            "id": "p2-memo-write-mereological",
            "text": "Williamstown is part of Massachusetts",
            "expected_facts": [{
                "pattern": "mereological",
                "predicate_in": ["part_of"],
                "polarity": 1,
                "slots_subset": {"part": "Williamstown", "whole": "Massachusetts"},
                "expected_routing": "retrieval",
            }],
            "expected_memo_state": "write",
        }
        assert detect_shape(entry) is SmokeEntryShape.ROUTING_MEMO

    def test_assistant_lookup_via_role(self):
        entry = {
            "id": "p3-cheetahs-assertion",
            "text": "you really don't like cheetahs",
            "role": "assistant",
            "expected_facts": [{
                "pattern": "preference",
                "predicate_in": ["likes"],
                "polarity": 0,
                "slots_subset": {"agent": "user", "object": "cheetahs"},
                "expected_tier_u_outcome": "match",
            }],
            "expected_oracles_consulted": ["predicate_equivalence"],
        }
        assert detect_shape(entry) is SmokeEntryShape.ASSISTANT_LOOKUP

    def test_user_storage_fallback_when_text_only(self):
        entry = {
            "id": "p1-merco-clean",
            "text": "Williamstown is part of Massachusetts",
            "expected_facts": [{
                "pattern": "mereological",
                "predicate_in": ["part_of"],
                "polarity": 1,
                "slots_subset": {"part": "Williamstown", "whole": "Massachusetts"},
            }],
        }
        assert detect_shape(entry) is SmokeEntryShape.USER_STORAGE

    def test_user_storage_with_explicit_role_user(self):
        """role='user' (rare; usually omitted) still routes to
        USER_STORAGE — the validator allows the explicit value."""
        entry = {
            "id": "explicit-user",
            "text": "I like olives",
            "role": "user",
            "expected_facts": [{
                "pattern": "preference",
                "predicate_in": ["likes"],
                "polarity": 1,
                "slots_subset": {"agent": "user", "object": "olives"},
            }],
        }
        assert detect_shape(entry) is SmokeEntryShape.USER_STORAGE


class TestDetectShapeOrderingAndEdgeCases:
    """Where multiple discriminators could match, the priority order
    is contractual. Pin it here so a future regression can't silently
    flip an entry into a different shape."""

    def test_oracle_call_wins_over_other_discriminators(self):
        """An entry that has both ``oracle_call`` and ``text`` should
        route to SUBSTRATE_DIRECT — oracle_call is the most specific."""
        entry = {
            "id": "weird",
            "oracle_call": {
                "oracle": "entity_taxonomy",
                "child": "X", "parent": "Y", "relation_type": "is_a",
            },
            "expected_label": "neither",
            "text": "ignored when oracle_call is present",
        }
        assert detect_shape(entry) is SmokeEntryShape.SUBSTRATE_DIRECT

    def test_two_text_wins_over_routing_memo_discriminator(self):
        """Two-text entries with a memo-state field still detect as
        TWO_TEXT_ORACLE — the multi-turn shape takes precedence."""
        entry = {
            "id": "weird",
            "text_user": "U",
            "text_assistant": "A",
            "expected_oracle_classification": {
                "pattern": "preference",
                "predicate_a": "likes",
                "predicate_b": "loves",
                "label": "distinct",
                "slot_reversal": "none",
            },
            "expected_memo_state": "hit",
        }
        assert detect_shape(entry) is SmokeEntryShape.TWO_TEXT_ORACLE

    def test_routing_memo_wins_over_assistant_lookup_when_both_present(self):
        """Routing-memo discriminator is more specific than role."""
        entry = {
            "id": "weird",
            "text": "X",
            "role": "assistant",
            "expected_memo_state": "hit",
            "expected_facts": [{
                "pattern": "preference",
                "predicate_in": ["likes"],
                "polarity": 1,
                "slots_subset": {"agent": "user", "object": "olives"},
                "expected_routing": "user_authoritative",
            }],
        }
        assert detect_shape(entry) is SmokeEntryShape.ROUTING_MEMO

    def test_unknown_shape_returns_none(self):
        """An entry with no detectable discriminator returns None."""
        entry = {
            "id": "no-shape",
            "notes": "this entry has only metadata, nothing actionable",
        }
        assert detect_shape(entry) is None


# ============================================================================
# Schema validation — happy paths
# ============================================================================


class TestValidateEntryHappy:
    """Validate that real corpus shapes pass schema validation."""

    def test_substrate_direct_entity_taxonomy(self):
        entry = {
            "id": "p5-tax-isa-clear",
            "oracle_call": {
                "oracle": "entity_taxonomy",
                "child": "golden retriever",
                "parent": "dog",
                "relation_type": "is_a",
            },
            "expected_label": "child_subsumed_by_parent",
            "expected_via": None,
        }
        result = validate_entry(entry)
        assert result.ok, result.errors
        assert result.shape is SmokeEntryShape.SUBSTRATE_DIRECT

    def test_substrate_direct_predicate_distribution(self):
        entry = {
            "id": "p5-dist-likes-isa",
            "oracle_call": {
                "oracle": "predicate_distribution",
                "pattern": "preference",
                "predicate": "likes",
                "polarity": 1,
                "taxonomy_relation_type": "is_a",
            },
            "expected_label": "distributes_down",
            "expected_via": None,
        }
        result = validate_entry(entry)
        assert result.ok, result.errors

    def test_substrate_direct_with_populated_via(self):
        """Phase 7 entries can populate expected_via (no longer null)."""
        entry = {
            "id": "p7-substrate-with-via",
            "oracle_call": {
                "oracle": "entity_taxonomy",
                "child": "Williamstown",
                "parent": "Massachusetts",
                "relation_type": "part_of",
            },
            "expected_label": "child_subsumed_by_parent",
            "expected_via": ["entity_taxonomy"],
        }
        result = validate_entry(entry)
        assert result.ok, result.errors

    def test_two_text_oracle(self):
        entry = {
            "id": "p3-active-passive",
            "text_user": "Asa wrote the paper",
            "text_assistant": "the paper was authored by Asa",
            "expected_oracle_classification": {
                "pattern": "relational",
                "predicate_a": "authored_by",
                "predicate_b": "wrote",
                "label": "equivalent",
                "slot_reversal": "subject_object_swap",
            },
        }
        result = validate_entry(entry)
        assert result.ok, result.errors

    def test_two_text_oracle_with_optional_facts_user(self):
        entry = {
            "id": "p3-distinct-negative",
            "text_user": "I like olives",
            "text_assistant": "you love olives",
            "expected_facts_user": [{
                "pattern": "preference",
                "predicate_in": ["likes"],
                "polarity": 1,
                "slots_subset": {"agent": "user", "object": "olives"},
            }],
            "expected_oracle_classification": {
                "pattern": "preference",
                "predicate_a": "likes",
                "predicate_b": "loves",
                "label": "distinct",
                "slot_reversal": "none",
            },
            "expected_tier_u_outcome": "miss",
        }
        result = validate_entry(entry)
        assert result.ok, result.errors

    def test_routing_memo_write(self):
        entry = {
            "id": "p2-memo-write-mereological",
            "text": "Williamstown is part of Massachusetts",
            "expected_facts": [{
                "pattern": "mereological",
                "predicate_in": ["part_of"],
                "polarity": 1,
                "slots_subset": {"part": "Williamstown", "whole": "Massachusetts"},
                "expected_routing": "retrieval",
            }],
            "expected_memo_state": "write",
        }
        result = validate_entry(entry)
        assert result.ok, result.errors

    def test_routing_memo_routing_anomaly_method(self):
        entry = {
            "id": "p2-routing-anomaly-preference",
            "text": "Donald Trump likes peanut butter",
            "expected_facts": [{
                "pattern": "preference",
                "predicate_in": ["likes"],
                "polarity": 1,
                "slots_subset": {"agent": "Donald Trump", "object": "peanut butter"},
                "expected_routing": "routing_anomaly",
            }],
            "expected_memo_state": "n/a",
        }
        result = validate_entry(entry)
        assert result.ok, result.errors

    def test_assistant_lookup_with_predicate_equivalence(self):
        entry = {
            "id": "p3-cheetahs-assertion",
            "text": "you really don't like cheetahs",
            "role": "assistant",
            "expected_facts": [{
                "pattern": "preference",
                "predicate_in": ["likes"],
                "polarity": 0,
                "slots_subset": {"agent": "user", "object": "cheetahs"},
                "expected_tier_u_outcome": "match",
            }],
            "expected_oracles_consulted": ["predicate_equivalence"],
            "expected_oracle_label": "contradictory",
            "expected_polarity_flipped": True,
        }
        result = validate_entry(entry)
        assert result.ok, result.errors

    def test_assistant_lookup_with_entity_equivalence(self):
        entry = {
            "id": "p4-alias",
            "text": "you live in New York City",
            "role": "assistant",
            "expected_facts": [{
                "pattern": "spatial_temporal",
                "predicate_in": ["lives_in"],
                "polarity": 1,
                "slots_subset": {"entity": "user", "location": "New York City"},
                "expected_tier_u_outcome": "match",
            }],
            "expected_oracles_consulted": ["entity_equivalence"],
            "expected_entity_oracle_label": "same",
        }
        result = validate_entry(entry)
        assert result.ok, result.errors

    def test_assistant_lookup_with_future_match_via(self):
        """The future_match_via field is a Phase 9 parity hint —
        Phase 4's entry says 'this currently misses but Phase 5+
        will match via entity_taxonomy'. Validator accepts."""
        entry = {
            "id": "p4-over-merge",
            "text": "you live in Japan",
            "role": "assistant",
            "expected_facts": [{
                "pattern": "spatial_temporal",
                "predicate_in": ["lives_in"],
                "polarity": 1,
                "slots_subset": {"entity": "user", "location": "Japan"},
                "expected_tier_u_outcome": "miss",
            }],
            "expected_oracles_consulted": ["entity_equivalence"],
            "expected_entity_oracle_label": "different",
            "future_match_via": "entity_taxonomy",
        }
        result = validate_entry(entry)
        assert result.ok, result.errors

    def test_assistant_lookup_with_session_and_empty_oracles(self):
        entry = {
            "id": "p6-cross-session",
            "session": "B",
            "text": "you live in Berlin",
            "role": "assistant",
            "expected_facts": [{
                "pattern": "spatial_temporal",
                "predicate_in": ["lives_in"],
                "polarity": 1,
                "slots_subset": {"entity": "user", "location": "Berlin"},
                "expected_tier_u_outcome": "miss",
            }],
            "expected_oracles_consulted": [],
        }
        result = validate_entry(entry)
        assert result.ok, result.errors

    def test_assistant_lookup_phase7_multi_oracle_via(self):
        """Phase 7 derivation entries populate expected_via with
        a multi-oracle chain. Validator accepts."""
        entry = {
            "id": "p7-williamstown-derivation",
            "text": "you live in Massachusetts",
            "role": "assistant",
            "expected_facts": [{
                "pattern": "spatial_temporal",
                "predicate_in": ["lives_in"],
                "polarity": 1,
                "slots_subset": {"entity": "user", "location": "Massachusetts"},
                "expected_tier_u_outcome": "match",
            }],
            "expected_oracles_consulted": [
                "entity_taxonomy", "predicate_distribution",
            ],
            "expected_via": ["entity_taxonomy", "predicate_distribution"],
        }
        result = validate_entry(entry)
        assert result.ok, result.errors

    def test_user_storage_basic(self):
        entry = {
            "id": "p1-merco-clean",
            "text": "Williamstown is part of Massachusetts",
            "expected_facts": [{
                "pattern": "mereological",
                "predicate_in": ["part_of"],
                "polarity": 1,
                "slots_subset": {"part": "Williamstown", "whole": "Massachusetts"},
            }],
        }
        result = validate_entry(entry)
        assert result.ok, result.errors

    def test_user_storage_session_aware(self):
        entry = {
            "id": "p6-session-local-storage",
            "session": "A",
            "text": "let's say for this conversation I live in Berlin",
            "expected_facts": [{
                "pattern": "spatial_temporal",
                "predicate_in": ["lives_in"],
                "polarity": 1,
                "slots_subset": {"entity": "user", "location": "Berlin"},
                "expected_is_session_local": 1,
                "expected_session_ids_after": ["A"],
                "expected_affirmed_count_after": 1,
            }],
        }
        result = validate_entry(entry)
        assert result.ok, result.errors


# ============================================================================
# Schema validation — sad paths
# ============================================================================


class TestValidateEntrySad:
    """Each test pins one specific violation to one specific path."""

    def test_missing_id_short_circuits(self):
        result = validate_entry({"text": "X"})
        assert not result.ok
        assert result.shape is None
        assert any(e.path == "id" for e in result.errors)

    def test_unknown_shape_reports_keys(self):
        result = validate_entry({"id": "no-shape", "notes": "metadata only"})
        assert not result.ok
        assert result.shape is None
        assert any(e.path == "<entry>" for e in result.errors)

    def test_substrate_direct_unknown_oracle(self):
        result = validate_entry({
            "id": "bad",
            "oracle_call": {
                "oracle": "made_up_oracle",
                "x": "Y",
            },
            "expected_label": "neither",
        })
        assert not result.ok
        assert any(e.path == "oracle_call.oracle" for e in result.errors)

    def test_substrate_direct_taxonomy_invalid_relation_type(self):
        result = validate_entry({
            "id": "bad",
            "oracle_call": {
                "oracle": "entity_taxonomy",
                "child": "X",
                "parent": "Y",
                "relation_type": "located_in",  # not an entity_taxonomy relation
            },
            "expected_label": "child_subsumed_by_parent",
        })
        assert not result.ok
        assert any(
            e.path == "oracle_call.relation_type"
            for e in result.errors
        )

    def test_substrate_direct_label_outside_oracle_set(self):
        result = validate_entry({
            "id": "bad",
            "oracle_call": {
                "oracle": "entity_taxonomy",
                "child": "X",
                "parent": "Y",
                "relation_type": "is_a",
            },
            "expected_label": "distributes_down",  # wrong oracle's label
        })
        assert not result.ok
        assert any(e.path == "expected_label" for e in result.errors)

    def test_substrate_direct_predicate_distribution_polarity_invalid(self):
        result = validate_entry({
            "id": "bad",
            "oracle_call": {
                "oracle": "predicate_distribution",
                "pattern": "preference",
                "predicate": "likes",
                "polarity": 2,  # invalid
                "taxonomy_relation_type": "is_a",
            },
            "expected_label": "distributes_down",
        })
        assert not result.ok
        assert any(e.path == "oracle_call.polarity" for e in result.errors)

    def test_two_text_missing_classification(self):
        result = validate_entry({
            "id": "bad",
            "text_user": "U",
            "text_assistant": "A",
        })
        assert not result.ok
        assert any(
            e.path == "expected_oracle_classification"
            for e in result.errors
        )

    def test_two_text_classification_invalid_label(self):
        result = validate_entry({
            "id": "bad",
            "text_user": "U",
            "text_assistant": "A",
            "expected_oracle_classification": {
                "pattern": "preference",
                "predicate_a": "likes",
                "predicate_b": "loves",
                "label": "maybe",  # invalid
                "slot_reversal": "none",
            },
        })
        assert not result.ok
        assert any(
            e.path == "expected_oracle_classification.label"
            for e in result.errors
        )

    def test_routing_memo_invalid_state(self):
        result = validate_entry({
            "id": "bad",
            "text": "X",
            "expected_facts": [{
                "pattern": "preference",
                "predicate_in": ["likes"],
                "polarity": 1,
                "slots_subset": {"agent": "user", "object": "olives"},
                "expected_routing": "retrieval",
            }],
            "expected_memo_state": "skipped",  # invalid
        })
        assert not result.ok
        assert any(e.path == "expected_memo_state" for e in result.errors)

    def test_routing_memo_missing_routing_per_fact(self):
        result = validate_entry({
            "id": "bad",
            "text": "X",
            "expected_facts": [{
                "pattern": "preference",
                "predicate_in": ["likes"],
                "polarity": 1,
                "slots_subset": {"agent": "user", "object": "olives"},
                # expected_routing missing — required for routing_memo
            }],
            "expected_memo_state": "write",
        })
        assert not result.ok
        assert any(
            e.path == "expected_facts[0].expected_routing"
            for e in result.errors
        )

    def test_assistant_lookup_missing_tier_u_outcome(self):
        """Phase 8g: at least one of {expected_walker_outcome,
        expected_tier_u_outcome} must be present on assistant_lookup
        facts. Missing both produces an error naming both alternatives."""
        result = validate_entry({
            "id": "bad",
            "text": "X",
            "role": "assistant",
            "expected_facts": [{
                "pattern": "preference",
                "predicate_in": ["likes"],
                "polarity": 1,
                "slots_subset": {"agent": "user", "object": "olives"},
                # neither expected_walker_outcome nor
                # expected_tier_u_outcome — required for assistant_lookup
            }],
            "expected_oracles_consulted": [],
        })
        assert not result.ok
        assert any(
            "expected_walker_outcome" in e.path
            and "expected_tier_u_outcome" in e.path
            for e in result.errors
        )

    def test_assistant_lookup_unknown_oracle_in_consulted(self):
        result = validate_entry({
            "id": "bad",
            "text": "X",
            "role": "assistant",
            "expected_facts": [{
                "pattern": "preference",
                "predicate_in": ["likes"],
                "polarity": 1,
                "slots_subset": {"agent": "user", "object": "olives"},
                "expected_tier_u_outcome": "miss",
            }],
            "expected_oracles_consulted": ["mystery_oracle"],
        })
        assert not result.ok
        assert any(
            e.path == "expected_oracles_consulted[0]"
            for e in result.errors
        )

    def test_assistant_lookup_via_with_unknown_oracle(self):
        result = validate_entry({
            "id": "bad",
            "text": "X",
            "role": "assistant",
            "expected_facts": [{
                "pattern": "spatial_temporal",
                "predicate_in": ["lives_in"],
                "polarity": 1,
                "slots_subset": {"entity": "user", "location": "Massachusetts"},
                "expected_tier_u_outcome": "match",
            }],
            "expected_oracles_consulted": ["entity_taxonomy"],
            "expected_via": ["entity_taxonomy", "made_up"],
        })
        assert not result.ok
        assert any(
            e.path == "expected_via[1]" for e in result.errors
        )

    def test_user_storage_invalid_polarity(self):
        result = validate_entry({
            "id": "bad",
            "text": "X",
            "expected_facts": [{
                "pattern": "preference",
                "predicate_in": ["likes"],
                "polarity": 7,  # invalid
                "slots_subset": {"agent": "user", "object": "olives"},
            }],
        })
        assert not result.ok
        assert any(
            e.path == "expected_facts[0].polarity" for e in result.errors
        )

    def test_user_storage_invalid_pattern(self):
        result = validate_entry({
            "id": "bad",
            "text": "X",
            "expected_facts": [{
                "pattern": "made_up_pattern",
                "predicate_in": ["likes"],
                "polarity": 1,
                "slots_subset": {"agent": "user", "object": "olives"},
            }],
        })
        assert not result.ok
        assert any(
            e.path == "expected_facts[0].pattern" for e in result.errors
        )

    def test_user_storage_session_local_invalid_value(self):
        result = validate_entry({
            "id": "bad",
            "session": "A",
            "text": "X",
            "expected_facts": [{
                "pattern": "preference",
                "predicate_in": ["likes"],
                "polarity": 1,
                "slots_subset": {"agent": "user", "object": "olives"},
                "expected_is_session_local": 2,  # must be 0 or 1
            }],
        })
        assert not result.ok
        assert any(
            e.path == "expected_facts[0].expected_is_session_local"
            for e in result.errors
        )


# ============================================================================
# Phase 8g — expected_walker_outcome / expected_served_from_tier
# ============================================================================


class TestPhase8gFieldRename:
    """Phase 7+ entries use ``expected_walker_outcome`` and
    ``expected_served_from_tier``; Phase 0-6 entries kept the legacy
    ``expected_tier_u_outcome``. Both vocabularies validate; both can
    coexist on the same fact."""

    def _phase7_entry_walker_fields(self, walker_outcome: str,
                                     served_from: str) -> dict:
        return {
            "id": "phase7-test",
            "text": "you live in Massachusetts",
            "role": "assistant",
            "expected_facts": [{
                "pattern": "spatial_temporal",
                "predicate_in": ["lives_in"],
                "polarity": 1,
                "slots_subset": {"entity": "user", "location": "Massachusetts"},
                "expected_walker_outcome": walker_outcome,
                "expected_served_from_tier": served_from,
            }],
            "expected_oracles_consulted": ["entity_taxonomy", "predicate_distribution"],
            "expected_via": ["entity_taxonomy", "predicate_distribution"],
        }

    def test_walker_match_via_derivation_validates(self):
        result = validate_entry(
            self._phase7_entry_walker_fields("match", "derivation")
        )
        assert result.ok, f"got errors: {[e.to_dict() for e in result.errors]}"

    def test_walker_miss_via_fresh_validates(self):
        entry = self._phase7_entry_walker_fields("miss", "fresh")
        entry["expected_oracles_consulted"] = []
        entry["expected_via"] = None
        result = validate_entry(entry)
        assert result.ok

    def test_walker_outcome_invalid_value_errors(self):
        entry = self._phase7_entry_walker_fields("perhaps", "derivation")
        result = validate_entry(entry)
        assert not result.ok
        assert any(
            e.path == "expected_facts[0].expected_walker_outcome"
            for e in result.errors
        )

    def test_served_from_tier_invalid_value_errors(self):
        entry = self._phase7_entry_walker_fields("match", "atlantis")
        result = validate_entry(entry)
        assert not result.ok
        assert any(
            e.path == "expected_facts[0].expected_served_from_tier"
            for e in result.errors
        )

    def test_legacy_tier_u_outcome_still_validates(self):
        """Phase 0-6 entries with the legacy field validate clean."""
        result = validate_entry({
            "id": "phase3-legacy",
            "text": "you really don't like cheetahs",
            "role": "assistant",
            "expected_facts": [{
                "pattern": "preference",
                "predicate_in": ["likes"],
                "polarity": 0,
                "slots_subset": {"agent": "user", "object": "cheetahs"},
                "expected_tier_u_outcome": "match",
            }],
            "expected_oracles_consulted": ["predicate_equivalence"],
        })
        assert result.ok

    def test_both_fields_coexist_no_error(self):
        """Both legacy and Phase 8g fields on the same fact: validator
        accepts both; no deprecation error (corpus authors clean up
        gradually)."""
        entry = self._phase7_entry_walker_fields("match", "derivation")
        entry["expected_facts"][0]["expected_tier_u_outcome"] = "match"
        result = validate_entry(entry)
        assert result.ok

    def test_served_from_tier_without_walker_outcome_optional(self):
        """A non-assistant_lookup shape can carry expected_served_from_tier
        as optional context; validator type-checks but doesn't require."""
        # User_storage doesn't require either field; both walker fields
        # on a user_storage entry are accepted as optional.
        result = validate_entry({
            "id": "us-walker-aware",
            "text": "I live in NYC",
            "expected_facts": [{
                "pattern": "spatial_temporal",
                "predicate_in": ["lives_in"],
                "polarity": 1,
                "slots_subset": {"entity": "user", "location": "NYC"},
                "expected_walker_outcome": "match",
                "expected_served_from_tier": "u",
            }],
        })
        assert result.ok


# ============================================================================
# Live corpus validation
# ============================================================================


class TestValidateCorpus:
    """The actual corpus file must validate clean."""

    def test_corpus_file_exists(self):
        assert CORPUS_PATH.exists(), (
            f"smoke corpus not found at {CORPUS_PATH}; "
            f"the dispatcher needs the corpus to exist"
        )

    def test_corpus_validates_clean(self):
        """Every entry in tests/v2/smoke_corpus.jsonl must validate
        with no errors. If this fails, the failure messages name
        the offending entry IDs and field paths."""
        result = validate_corpus(CORPUS_PATH)
        if not result.ok:
            failure_lines = []
            if result.duplicate_ids:
                failure_lines.append(
                    f"duplicate ids: {result.duplicate_ids}"
                )
            for r in result.failures:
                lines = [f"  {e.path}: expected {e.expected}, "
                         f"got {e.actual!r}" for e in r.errors]
                failure_lines.append(
                    f"{r.entry_id} ({r.shape.value if r.shape else 'no-shape'}):\n"
                    + "\n".join(lines)
                )
            pytest.fail(
                "smoke corpus validation failed:\n"
                + "\n".join(failure_lines)
            )

    def test_corpus_has_no_duplicate_ids(self):
        """A duplicate id would silently shadow a prior entry's
        outcomes during end-to-end runs. Catch at validation time."""
        result = validate_corpus(CORPUS_PATH)
        assert not result.duplicate_ids, (
            f"duplicate ids in corpus: {result.duplicate_ids}"
        )

    def test_corpus_distribution_across_shapes(self):
        """The 22 Phase-0-through-6 entries break down across shapes
        as a sanity-check for corpus accretion. If this assertion
        fires, either entries were added (Phase 7 will append) or
        an entry's shape changed unexpectedly."""
        result = validate_corpus(CORPUS_PATH)
        by_shape: dict[str, int] = {}
        for r in result.entries:
            if r.shape is None:
                continue
            by_shape[r.shape.value] = by_shape.get(r.shape.value, 0) + 1

        # Phase 7 will append derivation entries (likely
        # ASSISTANT_LOOKUP or SUBSTRATE_DIRECT shape); allowed to
        # grow. The lower bounds capture what Phases 0-6 contribute.
        assert by_shape.get("substrate_direct", 0) >= 3, by_shape
        assert by_shape.get("two_text_oracle", 0) >= 2, by_shape
        assert by_shape.get("routing_memo", 0) >= 3, by_shape
        assert by_shape.get("assistant_lookup", 0) >= 5, by_shape
        assert by_shape.get("user_storage", 0) >= 6, by_shape


# ============================================================================
# Cascading-failure context formatting
# ============================================================================


class TestFormatCascadingFailure:
    """Phase 9's parity check needs cross-entry context when entry N
    fails because entry N-K's state didn't materialize. Format here."""

    def test_no_prior_context(self):
        msg = format_cascading_failure(
            failed_entry_id="p1-merco-clean",
            failure_message="extracted 0 facts, expected 1",
        )
        assert "p1-merco-clean" in msg
        assert "extracted 0 facts" in msg
        assert "cascade source" not in msg

    def test_with_prior_context(self):
        msg = format_cascading_failure(
            failed_entry_id="p2-memo-hit-mereological",
            failure_message="expected memo hit, got memo miss",
            prior_entry_id="p2-memo-write-mereological",
            prior_entry_summary=(
                "expected memo write for (mereological, part_of)"
            ),
        )
        assert "p2-memo-hit-mereological" in msg
        assert "p2-memo-write-mereological" in msg
        assert "cascade source" in msg
        assert "(mereological, part_of)" in msg

    def test_prior_context_without_summary(self):
        msg = format_cascading_failure(
            failed_entry_id="X",
            failure_message="expected state did not exist",
            prior_entry_id="Y",
        )
        assert "X" in msg
        assert "Y" in msg
        assert "cascade source" in msg
