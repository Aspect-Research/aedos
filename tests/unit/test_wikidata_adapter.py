"""Tests for WikidataAdapter (fixture-backed)."""

from __future__ import annotations

import pytest

from aedos.layer4_sources.kb_protocol import LocalContext
from aedos.layer4_sources.kb_wikidata import FixtureNotFoundError, WikidataAdapter


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
# TestIntervalQualifierRoundTrip  (v0.16 WS6 T1: the interval resolver reads
# P580 (start time) / P582 (end time) qualifiers off a base-relation
# statement. This pins that a P108 (employer) fixture carrying P580/P582
# round-trips them into stmt.qualifiers — converted to YYYY-MM-DD — via the
# same _parse_statement_bindings the live path uses. The fixture path is the
# resolver's qualifier-read coverage; no live SPARQL needed.)
# ---------------------------------------------------------------------------

class TestIntervalQualifierRoundTrip:
    def test_p108_q937_returns_two_statements(self, adapter):
        # Einstein P108: IAS (Q11942, preferred) + ETH Zurich (Q11920, normal).
        stmts = adapter.lookup_statements("Q937", "P108")
        assert len(stmts) == 2

    def test_p108_preferred_statement_carries_both_qualifiers(self, adapter):
        stmts = adapter.lookup_statements("Q937", "P108")
        ias = next(s for s in stmts if s.value == "Q11942")
        assert ias.rank == "preferred"
        assert "P580" in ias.qualifiers
        assert "P582" in ias.qualifiers

    def test_p108_p580_qualifier_normalized_to_iso_date(self, adapter):
        # The fixture stores '+1933-10-01T00:00:00Z'; the adapter truncates time
        # values to YYYY-MM-DD (day precision is the finest the parser keeps).
        stmts = adapter.lookup_statements("Q937", "P108")
        ias = next(s for s in stmts if s.value == "Q11942")
        assert ias.qualifiers["P580"] == "1933-10-01"
        assert ias.qualifiers["P582"] == "1955-04-18"

    def test_p108_open_end_statement_has_no_p582(self, adapter):
        # ETH Zurich has a start (P580) but NO end (P582) — an OPEN interval the
        # resolver treats as ongoing. The qualifier is simply absent.
        stmts = adapter.lookup_statements("Q937", "P108")
        eth = next(s for s in stmts if s.value == "Q11920")
        assert eth.qualifiers.get("P580") == "1912-01-01"
        assert "P582" not in eth.qualifiers

    def test_p463_q937_membership_qualifiers_round_trip(self, adapter):
        # The single P463 (member of) fixture carries a closed interval.
        stmts = adapter.lookup_statements("Q937", "P463")
        assert len(stmts) >= 1
        s = stmts[0]
        assert "P580" in s.qualifiers
        assert "P582" in s.qualifiers


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
