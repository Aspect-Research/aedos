"""Tests for temporal scope extraction."""

from __future__ import annotations

import pytest

from aedos.layer1_extraction.temporal import (
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


class TestEventRelativeBoundRefs:
    """v0.16.1 WS8 Stage 1: valid_from_ref / valid_until_ref are event-relative
    bound references mirroring valid_during_ref. WRITE-ONLY metadata — no
    grounding/verdict path reads them (Stage 2 resolver deferred). These pin the
    Stage 1 extraction contract: the refs default None and round-trip through
    extract_temporal_scope; either ref alone suppresses the implicit-past-tense
    before_present default (the ref-only branch fires on any of the three refs)."""

    def test_defaults_none_on_default_scope(self):
        scope = TemporalScope()
        assert scope.valid_from_ref is None
        assert scope.valid_until_ref is None

    def test_valid_until_ref_only_before_event(self):
        # "before X" upper bound → valid_until_ref, no absolute dates.
        scope = extract_temporal_scope(
            verb_tense="past", valid_until_ref="claim_acquisition"
        )
        assert scope.valid_until_ref == "claim_acquisition"
        assert scope.valid_from_ref is None
        assert scope.valid_during_ref is None
        assert scope.valid_from is None
        # The ref suppresses the implicit-past-tense before_present default.
        assert scope.valid_until is None
        assert scope.valid_until != BEFORE_PRESENT

    def test_valid_from_ref_only_after_event(self):
        # "after/since X" lower bound → valid_from_ref, no absolute dates.
        scope = extract_temporal_scope(
            verb_tense="past", valid_from_ref="claim_election"
        )
        assert scope.valid_from_ref == "claim_election"
        assert scope.valid_until_ref is None
        assert scope.valid_during_ref is None
        assert scope.valid_until is None  # ref suppresses before_present
        assert scope.valid_from is None

    def test_both_refs_round_trip_without_dates(self):
        scope = extract_temporal_scope(
            verb_tense="present",
            valid_from_ref="claim_start",
            valid_until_ref="claim_end",
        )
        assert scope.valid_from_ref == "claim_start"
        assert scope.valid_until_ref == "claim_end"
        assert scope.valid_from is None
        assert scope.valid_until is None

    def test_refs_round_trip_alongside_explicit_dates(self):
        # Explicit-scope branch carries the refs through too.
        scope = extract_temporal_scope(
            verb_tense="past",
            valid_from_raw="2008",
            valid_until_raw="2016",
            valid_from_ref="claim_start",
            valid_until_ref="claim_end",
        )
        assert scope.valid_from == "2008"
        assert scope.valid_until == "2016"
        assert scope.valid_from_ref == "claim_start"
        assert scope.valid_until_ref == "claim_end"

    def test_future_tense_clears_refs(self):
        scope = extract_temporal_scope(
            verb_tense="future",
            valid_from_ref="claim_start",
            valid_until_ref="claim_end",
        )
        assert scope.is_future is True
        assert scope.valid_from_ref is None
        assert scope.valid_until_ref is None


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
