"""Tests for temporal scope extraction."""

from __future__ import annotations

import pytest

from src.aedos_v0_15.layer1_extraction.temporal import (
    BEFORE_PRESENT,
    TemporalScope,
    extract_temporal_scope,
)


class TestExplicitScope:
    def test_valid_from_and_until(self):
        scope = extract_temporal_scope(
            verb_tense="past",
            valid_from_raw="2008-01-20",
            valid_until_raw="2017-01-20",
        )
        assert scope.valid_from == "2008-01-20"
        assert scope.valid_until == "2017-01-20"
        assert scope.is_future is False

    def test_valid_from_only(self):
        scope = extract_temporal_scope(verb_tense="present", valid_from_raw="2020")
        assert scope.valid_from == "2020"
        assert scope.valid_until is None

    def test_valid_until_only(self):
        scope = extract_temporal_scope(verb_tense="past", valid_until_raw="2016")
        assert scope.valid_until == "2016"
        assert scope.valid_from is None

    def test_explicit_scope_overrides_past_tense_sentinel(self):
        # Even with past tense verb, explicit dates should not produce before_present
        scope = extract_temporal_scope(
            verb_tense="past",
            valid_from_raw="2008",
            valid_until_raw="2016",
        )
        assert scope.valid_until == "2016"
        assert scope.valid_until != BEFORE_PRESENT

    def test_explicit_scope_with_during_ref(self):
        scope = extract_temporal_scope(
            verb_tense="past",
            valid_from_raw="2008",
            valid_until_raw="2016",
            valid_during_ref="claim_abc",
        )
        assert scope.valid_during_ref == "claim_abc"
        assert scope.valid_from == "2008"


class TestImplicitPastTense:
    def test_past_tense_no_markers_gives_before_present(self):
        scope = extract_temporal_scope(verb_tense="past")
        assert scope.valid_until == BEFORE_PRESENT
        assert scope.valid_from is None

    def test_before_present_is_string_sentinel(self):
        scope = extract_temporal_scope(verb_tense="past")
        assert isinstance(scope.valid_until, str)
        assert scope.valid_until == "before_present"

    def test_past_tense_no_other_fields(self):
        scope = extract_temporal_scope(verb_tense="past")
        assert scope.valid_from is None
        assert scope.valid_during_ref is None
        assert scope.is_future is False


class TestNoTemporalMarkers:
    def test_present_tense_no_markers_is_unscoped(self):
        scope = extract_temporal_scope(verb_tense="present")
        assert scope.valid_from is None
        assert scope.valid_until is None
        assert scope.valid_during_ref is None
        assert scope.is_future is False

    def test_unscoped_is_not_before_present(self):
        scope = extract_temporal_scope(verb_tense="present")
        assert scope.valid_until != BEFORE_PRESENT


class TestRelativeScope:
    def test_valid_during_ref_only(self):
        scope = extract_temporal_scope(verb_tense="present", valid_during_ref="claim_xyz")
        assert scope.valid_during_ref == "claim_xyz"
        assert scope.valid_from is None
        assert scope.valid_until is None

    def test_relative_scope_with_past_tense_no_dates(self):
        # valid_during_ref takes precedence over past-tense inference when set
        scope = extract_temporal_scope(verb_tense="past", valid_during_ref="claim_xyz")
        assert scope.valid_during_ref == "claim_xyz"
        assert scope.valid_until is None  # not before_present


class TestFutureTense:
    def test_future_tense_sets_is_future(self):
        scope = extract_temporal_scope(verb_tense="future")
        assert scope.is_future is True

    def test_future_tense_clears_other_fields(self):
        scope = extract_temporal_scope(verb_tense="future")
        assert scope.valid_from is None
        assert scope.valid_until is None
        assert scope.valid_during_ref is None

    def test_future_with_explicit_dates_still_is_future(self):
        # future_tense check runs before explicit scope check
        scope = extract_temporal_scope(
            verb_tense="future", valid_from_raw="2030", valid_until_raw="2040"
        )
        assert scope.is_future is True


class TestConflictingSignals:
    def test_past_tense_with_present_tense_date_marker(self):
        # Explicit date overrides tense-based inference — not before_present
        scope = extract_temporal_scope(
            verb_tense="past", valid_from_raw="2023-01-01"
        )
        assert scope.valid_from == "2023-01-01"
        assert scope.valid_until is None

    def test_year_boundary_preserved(self):
        scope = extract_temporal_scope(
            verb_tense="past", valid_from_raw="2008", valid_until_raw="2016"
        )
        assert scope.valid_from == "2008"
        assert scope.valid_until == "2016"
