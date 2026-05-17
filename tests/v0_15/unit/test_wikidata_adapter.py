"""Tests for WikidataAdapter (fixture-backed)."""

from __future__ import annotations

import pytest

from src.aedos_v0_15.layer4_sources.kb_protocol import LocalContext
from src.aedos_v0_15.layer4_sources.kb_wikidata import FixtureNotFoundError, WikidataAdapter


@pytest.fixture
def adapter():
    return WikidataAdapter()


# ---------------------------------------------------------------------------
# TestEntityResolution
# ---------------------------------------------------------------------------

class TestEntityResolution:
    def test_obama_resolves_to_q76(self, adapter):
        lc = LocalContext(predicate="holds_role", slot_position="subject")
        candidates = adapter.resolve_entity("Obama", lc)
        assert len(candidates) > 0
        assert candidates[0].kb_identifier == "Q76"

    def test_obama_top_score_highest(self, adapter):
        lc = LocalContext(predicate="holds_role", slot_position="subject")
        candidates = adapter.resolve_entity("Obama", lc)
        assert candidates[0].score >= candidates[-1].score

    def test_williams_college_resolves(self, adapter):
        lc = LocalContext(predicate="located_in", slot_position="subject")
        candidates = adapter.resolve_entity("Williams College", lc)
        assert candidates[0].kb_identifier == "Q49112"

    def test_google_resolves_to_q95(self, adapter):
        lc = LocalContext(predicate="employed_by", slot_position="object")
        candidates = adapter.resolve_entity("Google", lc)
        assert candidates[0].kb_identifier == "Q95"

    def test_no_match_returns_empty_list(self, adapter):
        lc = LocalContext(predicate="holds_role", slot_position="subject")
        candidates = adapter.resolve_entity("xyzzy_nonexistent_entity_42", lc)
        assert candidates == []

    def test_fixture_not_found_raises(self, adapter):
        lc = LocalContext(predicate="holds_role", slot_position="subject")
        with pytest.raises(FixtureNotFoundError):
            adapter.resolve_entity("totally_unknown_xq9z", lc)

    def test_multiple_candidates_returned(self, adapter):
        lc = LocalContext(predicate="holds_role", slot_position="subject")
        candidates = adapter.resolve_entity("Obama", lc)
        assert len(candidates) == 2

    def test_candidate_has_provenance(self, adapter):
        lc = LocalContext(predicate="holds_role", slot_position="subject")
        candidates = adapter.resolve_entity("Obama", lc)
        assert "label" in candidates[0].provenance


# ---------------------------------------------------------------------------
# TestStatementLookup
# ---------------------------------------------------------------------------

class TestStatementLookup:
    def test_p39_q76_returns_statements(self, adapter):
        stmts = adapter.lookup_statements("Q76", "P39")
        assert len(stmts) == 1

    def test_p39_q76_value_is_q11696(self, adapter):
        stmts = adapter.lookup_statements("Q76", "P39")
        assert stmts[0].value == "Q11696"

    def test_p39_q76_has_p580_qualifier(self, adapter):
        stmts = adapter.lookup_statements("Q76", "P39")
        assert "P580" in stmts[0].qualifiers

    def test_p39_q76_has_p582_qualifier(self, adapter):
        stmts = adapter.lookup_statements("Q76", "P39")
        assert "P582" in stmts[0].qualifiers

    def test_p580_value_is_date(self, adapter):
        stmts = adapter.lookup_statements("Q76", "P39")
        assert stmts[0].qualifiers["P580"] == "2009-01-20"

    def test_p131_q49112_returns_statements(self, adapter):
        stmts = adapter.lookup_statements("Q49112", "P131")
        assert len(stmts) == 1
        assert stmts[0].value == "Q771397"

    def test_p131_rank_is_preferred(self, adapter):
        stmts = adapter.lookup_statements("Q49112", "P131")
        assert stmts[0].rank == "preferred"

    def test_missing_fixture_returns_empty(self, adapter):
        stmts = adapter.lookup_statements("Q99999", "P999")
        assert stmts == []

    def test_no_match_sparql_returns_empty(self, adapter):
        stmts = adapter.lookup_statements("Q_no_match", "P_no_match")
        assert stmts == []


# ---------------------------------------------------------------------------
# TestSubsumption
# ---------------------------------------------------------------------------

class TestSubsumption:
    def test_q95_has_subsumption_chain(self, adapter):
        result = adapter.subsumption("Q95", "Q4830453", "subclass")
        assert result.verdict == "a_subsumed_by_b"

    def test_q95_traversal_chain_nonempty(self, adapter):
        result = adapter.subsumption("Q95", "Q4830453", "subclass")
        assert len(result.traversal_chain) > 0

    def test_establishing_property_set(self, adapter):
        result = adapter.subsumption("Q95", "Q4830453", "subclass")
        assert result.establishing_property is not None

    def test_unknown_entity_returns_unrelated(self, adapter):
        result = adapter.subsumption("Q99999_unknown", "Q1", "subclass")
        assert result.verdict == "unrelated"

    def test_traversal_chain_contains_q_ids(self, adapter):
        result = adapter.subsumption("Q95", "Q4830453", "subclass")
        for q in result.traversal_chain:
            assert q.startswith("Q")
