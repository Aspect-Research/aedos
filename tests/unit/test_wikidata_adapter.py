"""Tests for WikidataAdapter (fixture-backed)."""

from __future__ import annotations

import pytest

from aedos.layer4_sources.kb_protocol import LocalContext, SubsumptionResult
from aedos.layer4_sources.kb_wikidata import (
    _CONTINENT_QIDS,
    _GEO_CONTAINER_TYPES,
    _LOCATION_KB_PROPERTIES,
    FixtureNotFoundError,
    WikidataAdapter,
)


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


# ---------------------------------------------------------------------------
# TestGeographicCluster (v0.16.1 WS5a)
#
# Focused unit tests of the geographic predicate cluster relocated from CORE
# (kb_verifier) into the adapter behind the kb_protocol seam. These exercise the
# adapter's own `geographic_disjoint` / `is_location_property` /
# `geo_container_types` methods directly — mirroring the former in-CORE
# `_location_disjoint` tests — driving the real `_geographic_disjoint` free
# function and the real `_CONTINENT_QIDS` closed set. The subsumption stub
# scripts exactly the verdicts the live/fixture path would return for the pinned
# geo cases, so the disjoint logic is exercised byte-for-byte without a network
# call. §3.2: positive KB evidence required; fail-closed on uncertainty.
# ---------------------------------------------------------------------------


class _StubSubsumptionAdapter(WikidataAdapter):
    """WikidataAdapter whose `subsumption` returns a scripted verdict from a
    `(a, b, relation_type) -> verdict` map (default 'unrelated'), so the real
    `geographic_disjoint` method runs against deterministic subsumption."""

    def __init__(self, verdict_map):
        super().__init__()
        self._verdict_map = verdict_map

    def subsumption(self, entity_a, entity_b, relation_type):
        verdict = self._verdict_map.get((entity_a, entity_b, relation_type), "unrelated")
        return SubsumptionResult(verdict=verdict)


class TestGeographicCluster:
    def test_is_location_property(self):
        adapter = WikidataAdapter()
        # The relocated closed set: P131/P17/P30/P361/P206/P276 are geographic;
        # a relational predicate (P108 employer) is not.
        assert adapter.is_location_property("P131") is True
        assert adapter.is_location_property("P30") is True
        assert adapter.is_location_property("P361") is True
        assert adapter.is_location_property("P108") is False
        assert adapter.is_location_property("not-a-pid") is False
        # Matches the module-level constant exactly.
        for pid in _LOCATION_KB_PROPERTIES:
            assert adapter.is_location_property(pid) is True

    def test_geo_container_types_is_continent_set(self):
        adapter = WikidataAdapter()
        assert adapter.geo_container_types() == _GEO_CONTAINER_TYPES
        assert "Q5107" in adapter.geo_container_types()  # continent

    def test_geographic_disjoint_continent_path_true(self):
        # Path (a): Vatican (Q237) in Africa (Q15). Africa is itself a continent
        # (in _CONTINENT_QIDS); the value is a_subsumed_by_b a DIFFERENT
        # continent (Europe Q46) and unrelated to Africa. => disjoint True.
        # The "Vatican is in Africa" CONTRADICTED pin.
        assert "Q15" in _CONTINENT_QIDS  # Africa
        assert "Q46" in _CONTINENT_QIDS  # Europe
        adapter = _StubSubsumptionAdapter({
            ("Q237", "Q46", "part_of"): "a_subsumed_by_b",  # Vatican ⊂ Europe
            # unrelated to Africa (default)
        })
        assert adapter.geographic_disjoint("Q237", "Q15") is True

    def test_geographic_disjoint_shared_continent_subregion_true(self):
        # Path (b): Rome's region Lazio (Q1282) vs Germany (Q183). Germany is NOT
        # a continent, so path (a) does not apply. Both Lazio and Germany are
        # a_subsumed_by_b the SAME continent (Europe Q46), and Lazio is
        # `unrelated` to Germany in BOTH part_of directions => disjoint True.
        # The "Rome is in Germany" CONTRADICTED shape.
        adapter = _StubSubsumptionAdapter({
            ("Q1282", "Q46", "part_of"): "a_subsumed_by_b",  # Lazio ⊂ Europe
            ("Q183", "Q46", "part_of"): "a_subsumed_by_b",   # Germany ⊂ Europe
            # Lazio<->Germany unrelated in both directions (default)
        })
        assert adapter.geographic_disjoint("Q1282", "Q183") is True

    def test_geographic_disjoint_subregion_of_expected_false(self):
        # Île-de-France (Q13917) vs Europe (Q46). Europe is a continent, so path
        # (a) applies — but the value is subsumed by Europe ITSELF (the expected
        # continent), and is unrelated to every OTHER continent. No
        # different-continent ancestor exists => disjoint False (NOT disjoint;
        # this is the true "Paris/France is in Europe" shape that must VERIFY,
        # never contradict).
        adapter = _StubSubsumptionAdapter({
            ("Q13917", "Q46", "part_of"): "a_subsumed_by_b",  # Île-de-France ⊂ Europe
        })
        assert adapter.geographic_disjoint("Q13917", "Q46") is False

    def test_geographic_disjoint_unrelated_to_all_false(self):
        # Fail-closed: a value unrelated to every continent (no positive
        # subsumption evidence) cannot be confirmed disjoint => False (abstain),
        # never a fabricated contradiction.
        adapter = _StubSubsumptionAdapter({})  # all 'unrelated'
        assert adapter.geographic_disjoint("Q9999", "Q15") is False

    def test_geographic_disjoint_same_value_false(self):
        # Identical value and expected => not disjoint (the value is the place).
        adapter = _StubSubsumptionAdapter({})
        assert adapter.geographic_disjoint("Q46", "Q46") is False

    def test_geographic_disjoint_subsumption_error_fails_closed(self):
        # §3.2: if subsumption raises, the disjoint check swallows it and cannot
        # confirm disjointness => False (abstain), never contradict.
        class _RaisingAdapter(WikidataAdapter):
            def subsumption(self, a, b, relation_type):
                raise RuntimeError("kb down")

        adapter = _RaisingAdapter()
        assert adapter.geographic_disjoint("Q237", "Q15") is False
