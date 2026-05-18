"""Tests for multi-participant event decomposition."""

from __future__ import annotations

import pytest

from aedos.layer1_extraction.decomposition import decompose_event


def _base_raw(**kwargs):
    return {
        "subject": "TestSubject",
        "predicate": "co_founded",
        "object": "Acme",
        "polarity": 1,
        "valid_from": "2020",
        "valid_until": None,
        "valid_during_ref": None,
        "source_text": "Asa and Mike co-founded Acme in 2020",
        "verb_tense": "past",
        "reified_event_id": None,
        "event_type": None,
        "participants": [],
        **kwargs,
    }


class TestBinaryClaimPassthrough:
    def test_no_participants_returns_unchanged(self):
        raw = _base_raw(participants=[])
        result = decompose_event(raw)
        assert result == [raw]

    def test_none_participants_treated_as_empty(self):
        raw = _base_raw(participants=None)
        result = decompose_event(raw)
        assert result == [raw]

    def test_friendship_is_binary_not_decomposed(self):
        # "Asa and Bob are friends" should remain a single binary claim
        raw = {
            "subject": "Asa",
            "predicate": "has_friendship",
            "object": "Bob",
            "polarity": 1,
            "source_text": "Asa and Bob are friends",
            "verb_tense": "present",
            "participants": [],
            "reified_event_id": None,
            "event_type": None,
            "valid_from": None,
            "valid_until": None,
            "valid_during_ref": None,
        }
        result = decompose_event(raw)
        assert len(result) == 1
        assert result[0]["subject"] == "Asa"
        assert result[0]["object"] == "Bob"


class TestTwoParticipantEvent:
    def setup_method(self):
        self.raw = _base_raw(
            participants=["Asa", "Mike"],
            event_type="company_founding",
            object="Acme",
        )
        self.result = decompose_event(self.raw)

    def test_produces_multiple_claims(self):
        assert len(self.result) > 1

    def test_has_participant_for_each_participant(self):
        hp = [c for c in self.result if c["predicate"] == "has_participant"]
        assert len(hp) == 2
        objects = {c["object"] for c in hp}
        assert objects == {"Asa", "Mike"}

    def test_all_share_same_reified_event_id(self):
        ids = {c["reified_event_id"] for c in self.result}
        assert len(ids) == 1

    def test_event_type_claim_included(self):
        et = [c for c in self.result if c["predicate"] == "event_type"]
        assert len(et) == 1
        assert et[0]["object"] == "company_founding"

    def test_target_claim_included(self):
        targets = [c for c in self.result if c["predicate"] == "target"]
        assert len(targets) == 1
        assert targets[0]["object"] == "Acme"

    def test_all_claims_have_event_id_as_subject(self):
        for claim in self.result:
            assert claim["subject"] == claim["reified_event_id"]


class TestThreeParticipantEvent:
    def test_three_has_participant_claims(self):
        raw = _base_raw(
            participants=["Alice", "Bob", "Carol"],
            event_type="meeting",
            object="BoardRoom",
        )
        result = decompose_event(raw)
        hp = [c for c in result if c["predicate"] == "has_participant"]
        assert len(hp) == 3

    def test_all_participants_represented(self):
        raw = _base_raw(
            participants=["Alice", "Bob", "Carol"],
            event_type="meeting",
            object="BoardRoom",
        )
        result = decompose_event(raw)
        hp_objects = {c["object"] for c in result if c["predicate"] == "has_participant"}
        assert hp_objects == {"Alice", "Bob", "Carol"}


class TestReifiedEventId:
    def test_existing_reified_event_id_preserved(self):
        raw = _base_raw(
            participants=["Asa", "Mike"],
            reified_event_id="event_existing_123",
        )
        result = decompose_event(raw)
        for claim in result:
            assert claim["reified_event_id"] == "event_existing_123"

    def test_generated_event_id_starts_with_event_(self):
        raw = _base_raw(participants=["Asa", "Mike"], reified_event_id=None)
        result = decompose_event(raw)
        assert result[0]["reified_event_id"].startswith("event_")

    def test_base_fields_propagated(self):
        raw = _base_raw(
            participants=["Asa", "Mike"],
            valid_from="2020",
            source_text="Asa and Mike co-founded Acme in 2020",
        )
        result = decompose_event(raw)
        for claim in result:
            assert claim["valid_from"] == "2020"
            assert claim["source_text"] == "Asa and Mike co-founded Acme in 2020"

    def test_participants_cleared_in_decomposed_claims(self):
        raw = _base_raw(participants=["Asa", "Mike"])
        result = decompose_event(raw)
        for claim in result:
            assert claim.get("participants") == []
