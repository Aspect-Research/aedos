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
        #
        # v0.16.1 cycle-2 path-b GATE: the expected object (Germany) must be a
        # confirmed geographic place (is_a country Q6256). Scripted below.
        adapter = _StubSubsumptionAdapter({
            ("Q1282", "Q46", "part_of"): "a_subsumed_by_b",  # Lazio ⊂ Europe
            ("Q183", "Q46", "part_of"): "a_subsumed_by_b",   # Germany ⊂ Europe
            ("Q183", "Q6256", "is_a"): "a_subsumed_by_b",    # Germany is_a country
            # Lazio<->Germany unrelated in both directions (default)
        })
        assert adapter.geographic_disjoint("Q1282", "Q183") is True

    def test_geographic_disjoint_path_b_nongeographic_object_false(self):
        # v0.16.1 cycle-2 REGRESSION: "Germany (Q183) located_in the European
        # Union (Q458)". The EU carries P30=Europe, so under the part_of
        # alternation BOTH Germany and the EU are a_subsumed_by_b the same
        # continent (Europe Q46), and Germany<->EU is `unrelated` in both
        # directions (EU membership rides P463, invisible to P131/P30/P17). The
        # OLD path b therefore false-contradicted this TRUE membership claim.
        # The object gate: the EU is NOT a confirmed geographic place (False on
        # every _GEO_PLACE_CLASSES is_a verdict — default 'unrelated'), so path b
        # fails closed => disjoint False => the verifier ABSTAINS, not contradict.
        adapter = _StubSubsumptionAdapter({
            ("Q183", "Q46", "part_of"): "a_subsumed_by_b",  # Germany ⊂ Europe
            ("Q458", "Q46", "part_of"): "a_subsumed_by_b",  # EU ⊂ Europe (via P30)
            # EU is_a <place class> => all 'unrelated' (the gate fails closed)
        })
        assert adapter.geographic_disjoint("Q183", "Q458") is False

    def test_geographic_disjoint_path_b_organization_object_false(self):
        # v0.16.1 cycle-2 REGRESSION: "Williams College (Q49112) part_of the
        # Consortium (Q_consortium)". P361 is a location property, so an
        # ORGANIZATIONAL part_of reached path b. The consortium is not a
        # confirmed geographic place => path b fails closed => disjoint False
        # => ABSTAIN. (Even if a shared-continent ancestor existed, the gate
        # blocks the contradiction.)
        adapter = _StubSubsumptionAdapter({
            ("Q49112", "Q46", "part_of"): "a_subsumed_by_b",   # Williams ⊂ Europe (hypoth.)
            ("Q_consortium", "Q46", "part_of"): "a_subsumed_by_b",
            # consortium is_a <place class> => all 'unrelated' (gate fails closed)
        })
        assert adapter.geographic_disjoint("Q49112", "Q_consortium") is False

    def test_geographic_disjoint_path_b_river_object_still_true(self):
        # v0.16.1 cycle-2 (b): the GATE must NOT over-block a genuine
        # geographic-place object. A region (Q_region) vs the Thames (Q_thames),
        # a non-continent geographic PLACE confirmed via is_a river (Q4022). Both
        # share a continent ancestor (Europe Q46) and are mutually unrelated, so
        # the region is a disjoint sub-region. The Thames passes the place gate
        # (is_a river) => path (b) CONTRADICTS as before. Object is NOT a
        # continent, so this genuinely exercises path (b) (not path a).
        assert "Q_thames" not in _CONTINENT_QIDS
        adapter = _StubSubsumptionAdapter({
            ("Q_region", "Q46", "part_of"): "a_subsumed_by_b",
            ("Q_thames", "Q46", "part_of"): "a_subsumed_by_b",
            ("Q_thames", "Q4022", "is_a"): "a_subsumed_by_b",  # Thames is_a river (gate)
            # region<->Thames unrelated in both part_of directions (default)
        })
        assert adapter.geographic_disjoint("Q_region", "Q_thames") is True

    def test_geographic_disjoint_path_b_geographic_place_object_still_true(self):
        # v0.16.1 cycle-2 (b): the canonical path-b geographic shape with the
        # object confirmed a PLACE via EACH _GEO_PLACE_CLASSES member in turn —
        # proves the gate accepts any place class (country, river, body of
        # water, ...), not just the first. Lazio (Q1282) vs an object Q_obj that
        # is_a the place class; both share Europe; mutually unrelated => True.
        from aedos.layer4_sources.kb_wikidata import _GEO_PLACE_CLASSES

        for place_class in _GEO_PLACE_CLASSES:
            adapter = _StubSubsumptionAdapter({
                ("Q1282", "Q46", "part_of"): "a_subsumed_by_b",   # Lazio ⊂ Europe
                ("Q_obj", "Q46", "part_of"): "a_subsumed_by_b",   # object ⊂ Europe
                ("Q_obj", place_class, "is_a"): "a_subsumed_by_b",  # object is_a <place>
            })
            assert adapter.geographic_disjoint("Q1282", "Q_obj") is True, place_class

    def test_geographic_disjoint_path_b_place_gate_equivalent_verdict_true(self):
        # The gate accepts an `equivalent` is_a verdict, not only
        # `a_subsumed_by_b` (an object that IS the place class). Pin it so a
        # later tightening to a single verdict can't silently reopen the bug on
        # the inverse side (real path-b contradiction still fires).
        adapter = _StubSubsumptionAdapter({
            ("Q1282", "Q46", "part_of"): "a_subsumed_by_b",
            ("Q_obj", "Q46", "part_of"): "a_subsumed_by_b",
            ("Q_obj", "Q6256", "is_a"): "equivalent",  # object equivalent to country
        })
        assert adapter.geographic_disjoint("Q1282", "Q_obj") is True

    def test_geographic_disjoint_path_b_france_in_europe_not_disjoint(self):
        # v0.16.1 cycle-2 (c): the TRUE "France (Q142) is in Europe (Q46)" shape
        # must NEVER contradict. Europe is a continent, so this resolves via path
        # (a): France is subsumed by Europe ITSELF (the expected continent) and
        # unrelated to every OTHER continent => no different-continent ancestor
        # => disjoint False. The path-b gate is not even consulted (path a
        # returns first), so the VERIFY path is preserved.
        assert "Q46" in _CONTINENT_QIDS  # Europe
        adapter = _StubSubsumptionAdapter({
            ("Q142", "Q46", "part_of"): "a_subsumed_by_b",  # France ⊂ Europe
        })
        assert adapter.geographic_disjoint("Q142", "Q46") is False

    def test_geographic_disjoint_path_b_place_gate_subsumption_error_fails_closed(self):
        # v0.16.1 cycle-2 (d): fail-closed when the OBJECT's geographic-type
        # subsumption is UNCONFIRMED via an ERROR (not merely 'unrelated'). The
        # part_of probes resolve fine (both subsumed by Europe, mutually
        # unrelated) so the OLD path b would contradict; but every is_a place
        # probe RAISES, so `_is_confirmed_geographic_place` swallows it and
        # returns False => the gate fails closed => disjoint False => ABSTAIN.
        # §3.2: uncertainty about the object's geo-type can never contradict.
        class _PartOfOkIsaRaisesAdapter(WikidataAdapter):
            def subsumption(self, entity_a, entity_b, relation_type):
                if relation_type == "is_a":
                    raise RuntimeError("kb down on the place-class probe")
                # part_of: both Germany-shape and EU-shape share Europe, mutually
                # unrelated — the path-b precondition that the gate must override.
                if (entity_a, entity_b) in (("Q183", "Q46"), ("Q458", "Q46")):
                    return SubsumptionResult(verdict="a_subsumed_by_b")
                return SubsumptionResult(verdict="unrelated")

        adapter = _PartOfOkIsaRaisesAdapter()
        assert adapter.geographic_disjoint("Q183", "Q458") is False

    def test_geographic_disjoint_path_b_place_gate_unknown_object_fails_closed(self):
        # v0.16.1 cycle-2 (d): fail-closed when the object's geo-type is simply
        # UNKNOWN (every is_a place probe returns the default 'unrelated'), while
        # the shared-continent + mutual-non-containment path-b precondition holds.
        # Mirrors the Germany-in-EU shape but stated generically. => disjoint
        # False => ABSTAIN.
        adapter = _StubSubsumptionAdapter({
            ("Q_subj", "Q46", "part_of"): "a_subsumed_by_b",
            ("Q_unknown_obj", "Q46", "part_of"): "a_subsumed_by_b",
            # no is_a entries for Q_unknown_obj => all place-class probes unrelated
        })
        assert adapter.geographic_disjoint("Q_subj", "Q_unknown_obj") is False

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
