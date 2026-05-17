"""Tests for KB protocol dataclasses and Protocol structural typing."""

from __future__ import annotations

import pytest

from src.aedos_v0_15.layer4_sources.kb_protocol import (
    KBProtocol,
    LocalContext,
    ResolutionCandidate,
    Statement,
    SubsumptionResult,
)


class TestLocalContext:
    def test_required_fields(self):
        lc = LocalContext(predicate="holds_role", slot_position="subject")
        assert lc.predicate == "holds_role"
        assert lc.slot_position == "subject"

    def test_optional_fields_default(self):
        lc = LocalContext(predicate="p", slot_position="object")
        assert lc.asserting_party is None
        assert lc.prior_resolutions == []

    def test_full_construction(self):
        c = ResolutionCandidate(kb_identifier="Q76")
        lc = LocalContext(predicate="p", slot_position="subject", asserting_party="user", prior_resolutions=[c])
        assert lc.asserting_party == "user"
        assert len(lc.prior_resolutions) == 1


class TestResolutionCandidate:
    def test_defaults(self):
        rc = ResolutionCandidate(kb_identifier="Q76")
        assert rc.kb_identifier == "Q76"
        assert rc.provenance == {}
        assert rc.score == 0.0

    def test_with_score(self):
        rc = ResolutionCandidate(kb_identifier="Q76", score=0.9)
        assert rc.score == 0.9


class TestStatement:
    def test_defaults(self):
        stmt = Statement(value="Q76", value_type="entity")
        assert stmt.value == "Q76"
        assert stmt.value_type == "entity"
        assert stmt.qualifiers == {}
        assert stmt.rank == "normal"

    def test_with_qualifiers(self):
        stmt = Statement(value="Q76", value_type="entity", qualifiers={"P580": "2009-01-20"})
        assert stmt.qualifiers["P580"] == "2009-01-20"

    def test_rank_preferred(self):
        stmt = Statement(value="Q76", value_type="entity", rank="preferred")
        assert stmt.rank == "preferred"


class TestSubsumptionResult:
    def test_unrelated(self):
        r = SubsumptionResult(verdict="unrelated")
        assert r.verdict == "unrelated"
        assert r.establishing_property is None
        assert r.traversal_chain == []

    def test_with_chain(self):
        r = SubsumptionResult(verdict="a_subsumed_by_b", establishing_property="P31", traversal_chain=["Q43229"])
        assert r.traversal_chain == ["Q43229"]


class TestKBProtocolStructural:
    def test_adapter_satisfies_protocol(self):
        from src.aedos_v0_15.layer4_sources.kb_wikidata import WikidataAdapter
        adapter = WikidataAdapter()
        assert isinstance(adapter, KBProtocol)
