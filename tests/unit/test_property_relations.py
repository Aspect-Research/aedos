"""v0.16 WS1 — tests for PropertyRelations (Wikidata property-ontology cache).

PropertyRelations.fetch(prop) is cache-then-generate: a fresh set of
`property_relations` rows is returned directly; otherwise the KB
(`fetch_property_ontology`) is consulted, the result is cached, and returned.

FAIL-OPEN throughout: any DB/KB error — and the common case of a property with
no recorded constraints — returns an EMPTY PropertyOntology. Discovery is
additive enrichment; an empty ontology falls the caller back to the oracle's
primary binding. Nothing here raises.
"""

from __future__ import annotations

from aedos.database import open_memory_db
from aedos.layer3_substrate.property_relations import (
    PropertyOntology,
    PropertyRelations,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class _StubKB:
    """A KB whose fetch_property_ontology returns a pre-canned dict per
    property. Records call counts so cache-hit behavior is observable."""

    def __init__(self, by_property: dict[str, dict] | None = None, raise_on=None):
        self._by_property = by_property or {}
        self._raise_on = raise_on
        self.calls: list[str] = []

    def fetch_property_ontology(self, prop):
        self.calls.append(prop)
        if self._raise_on is not None:
            raise self._raise_on
        return self._by_property.get(prop, {})


class _NoOntologyKB:
    """A KB adapter that predates v0.16 — no fetch_property_ontology method.
    PropertyRelations consults via getattr and must fall open to empty."""


def _ontology_dict(**kw) -> dict:
    base = {
        "subject_type_qids": [],
        "value_type_qids": [],
        "inverse_pids": [],
        "subproperty_pids": [],
        "related_pids": [],
        "single_valued": False,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# PropertyOntology dataclass
# ---------------------------------------------------------------------------

class TestPropertyOntologyDataclass:
    def test_empty_by_default(self):
        assert PropertyOntology().is_empty() is True

    def test_non_empty_with_value_types(self):
        assert PropertyOntology(value_type_qids=["Q5"]).is_empty() is False

    def test_single_valued_alone_is_non_empty(self):
        assert PropertyOntology(single_valued=True).is_empty() is False


# ---------------------------------------------------------------------------
# fetch — cache-then-generate
# ---------------------------------------------------------------------------

class TestFetchQueryAndCache:
    def test_kb_queried_on_cache_miss(self):
        kb = _StubKB({"P39": _ontology_dict(value_type_qids=["Q4164871"], single_valued=True)})
        db = open_memory_db()
        pr = PropertyRelations(db, kb)
        ont = pr.fetch("P39")
        assert kb.calls == ["P39"]
        assert ont.value_type_qids == ["Q4164871"]
        assert ont.single_valued is True

    def test_second_fetch_hits_cache_not_kb(self):
        kb = _StubKB({"P39": _ontology_dict(value_type_qids=["Q4164871"])})
        db = open_memory_db()
        pr = PropertyRelations(db, kb)
        first = pr.fetch("P39")
        second = pr.fetch("P39")
        # KB consulted exactly once — the second fetch is served from the cache.
        assert kb.calls == ["P39"]
        assert first.value_type_qids == second.value_type_qids == ["Q4164871"]

    def test_constraints_round_trip_through_cache(self):
        kb = _StubKB({"P40": _ontology_dict(
            subject_type_qids=["Q5"],
            value_type_qids=["Q5"],
            inverse_pids=["P22"],
            subproperty_pids=["P1038"],
            related_pids=["P25"],
            single_valued=False,
        )})
        db = open_memory_db()
        pr = PropertyRelations(db, kb)
        pr.fetch("P40")  # populate cache
        # New accessor over the same DB → reads the cached rows.
        pr2 = PropertyRelations(db, _StubKB())
        ont = pr2.fetch("P40")
        assert ont.subject_type_qids == ["Q5"]
        assert ont.value_type_qids == ["Q5"]
        assert ont.inverse_pids == ["P22"]
        assert ont.subproperty_pids == ["P1038"]
        assert ont.related_pids == ["P25"]

    def test_rows_persisted_to_property_relations_table(self):
        kb = _StubKB({"P39": _ontology_dict(value_type_qids=["Q4164871"], single_valued=True)})
        db = open_memory_db()
        pr = PropertyRelations(db, kb)
        pr.fetch("P39")
        rows = db.execute(
            "SELECT relation_type, related_value FROM property_relations "
            "WHERE kb_property='P39'"
        ).fetchall()
        relation_types = {r["relation_type"] for r in rows}
        assert "value_type_constraint" in relation_types
        assert "single_value" in relation_types


# ---------------------------------------------------------------------------
# Known-empty caching — a property with no constraints
# ---------------------------------------------------------------------------

class TestEmptyOntologyCaching:
    def test_empty_result_is_empty_ontology(self):
        kb = _StubKB({"P9999": _ontology_dict()})  # no constraints
        db = open_memory_db()
        pr = PropertyRelations(db, kb)
        ont = pr.fetch("P9999")
        assert ont.is_empty() is True

    def test_empty_result_is_cached_as_sentinel_not_requeried(self):
        # A long-tail property the ontology can't constrain is cached as a
        # known-empty sentinel so it isn't re-queried within the TTL.
        kb = _StubKB({"P9999": _ontology_dict()})
        db = open_memory_db()
        pr = PropertyRelations(db, kb)
        pr.fetch("P9999")
        pr.fetch("P9999")
        assert kb.calls == ["P9999"]  # only one KB query
        sentinel = db.execute(
            "SELECT relation_type FROM property_relations WHERE kb_property='P9999'"
        ).fetchone()
        assert sentinel["relation_type"] == "empty"


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------

class TestFailOpen:
    def test_empty_property_id_returns_empty(self):
        pr = PropertyRelations(open_memory_db(), _StubKB())
        assert pr.fetch("").is_empty() is True

    def test_kb_error_returns_empty_never_raises(self):
        kb = _StubKB(raise_on=RuntimeError("SPARQL endpoint down"))
        pr = PropertyRelations(open_memory_db(), kb)
        ont = pr.fetch("P39")  # must not raise
        assert ont.is_empty() is True

    def test_kb_without_fetch_method_returns_empty(self):
        # A pre-v0.16 adapter has no fetch_property_ontology; getattr falls
        # through to an empty ontology (and the empty sentinel is cached).
        pr = PropertyRelations(open_memory_db(), _NoOntologyKB())
        ont = pr.fetch("P39")
        assert ont.is_empty() is True

    def test_kb_returns_non_dict_returns_empty(self):
        kb = _StubKB()
        kb._by_property = {"P39": ["not", "a", "dict"]}  # malformed
        pr = PropertyRelations(open_memory_db(), kb)
        assert pr.fetch("P39").is_empty() is True


# ---------------------------------------------------------------------------
# Live-fixture path: the WikidataAdapter fixture-backed fetch_property_ontology
# ---------------------------------------------------------------------------

class TestFetchOntologyFixture:
    def test_fixture_backed_ontology_round_trips_through_relations(self):
        # PropertyRelations over the real fixture-mode WikidataAdapter: a
        # property with a fixture yields its constrained ontology; the fail-open
        # contract holds for a property with no fixture.
        from aedos.layer4_sources.kb_wikidata import WikidataAdapter

        adapter = WikidataAdapter()  # fixture mode (RUN_LIVE_KB unset)
        db = open_memory_db()
        pr = PropertyRelations(db, adapter)
        # P26 has a fixture committed by this test suite (see fixtures dir).
        ont = pr.fetch("P26")
        assert ont.value_type_qids == ["Q5"]  # spouse value-type: human
        assert ont.single_valued is False

    def test_missing_fixture_falls_open_to_empty(self):
        from aedos.layer4_sources.kb_wikidata import WikidataAdapter

        adapter = WikidataAdapter()  # fixture mode (RUN_LIVE_KB unset)
        pr = PropertyRelations(open_memory_db(), adapter)
        # A property with no fixture file → empty ontology (fail open).
        assert pr.fetch("P999999").is_empty() is True
