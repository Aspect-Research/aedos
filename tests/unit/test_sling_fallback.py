"""v0.16 WS1 — tests for SlingFallback (distant-supervision binding proposer).

For a predicate whose Wikidata property ontology can't constrain it,
SlingFallback.propose_bindings samples entity pairs the oracle's primary
property links, enumerates the co-occurring KB properties (via
enumerate_neighbors), and proposes the most-frequent co-occurring property as a
single low-rank PredicateBinding (source='sling').

FAIL-OPEN: any KB/LLM error, a missing primary property, or no co-occurring
signal returns []. SLING bindings never license a contradiction
(single_valued is forced False). Nothing here raises.
"""

from __future__ import annotations

from aedos.database import open_memory_db
from aedos.layer3_substrate.predicate_translation import PredicateBinding
from aedos.layer3_substrate.sling_fallback import SlingFallback


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class _StubKB:
    """KB whose enumerate_neighbors returns a pre-canned property->values dict
    per entity. Records calls."""

    def __init__(self, neighbors_by_entity=None, raise_on_enumerate=False):
        self._neighbors = neighbors_by_entity or {}
        self._raise = raise_on_enumerate
        self.enumerate_calls: list[str] = []

    def enumerate_neighbors(self, entity, properties, direction="outgoing"):
        self.enumerate_calls.append(entity)
        if self._raise:
            raise RuntimeError("SPARQL endpoint down")
        return self._neighbors.get(entity, {})


class _NoEnumerateKB:
    """A KB adapter without enumerate_neighbors — SLING must fall open."""


def _oracle_raw(**kw) -> dict:
    base = {
        "kb_property": "P39",
        "kb_namespace": "wikidata",
        "slot_to_qualifier": None,
        "subject_entity_types": ["Q5"],
        "object_entity_types": None,
    }
    base.update(kw)
    return base


def _make_sling(kb):
    return SlingFallback(db=open_memory_db(), kb_protocol=kb, llm_client=None)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestProposeBindings:
    def test_proposes_most_frequent_cooccurring_property(self):
        # Two sample entities; P106 co-occurs on both, P19 on one. The most
        # frequent co-occurring property (P106) becomes the SLING candidate.
        kb = _StubKB({
            "Q42": {"P39": ["Qx"], "P106": ["Qa", "Qb"], "P19": ["Qc"]},
            "Q76": {"P39": ["Qy"], "P106": ["Qd"]},
        })
        sling = _make_sling(kb)
        raw = _oracle_raw(sample_subject_qids=["Q42", "Q76"])
        bindings = sling.propose_bindings("works_as", raw)
        assert len(bindings) == 1
        b = bindings[0]
        assert isinstance(b, PredicateBinding)
        assert b.kb_property == "P106"
        assert b.source == "sling"
        # SLING never licenses a contradiction.
        assert b.single_valued is False
        # Low rank so any grounding ontology/oracle binding outranks it.
        assert b.rank < 0.5

    def test_primary_property_excluded_from_candidates(self):
        # The oracle's own primary property must not be proposed as the SLING
        # candidate even if it co-occurs (it's already a binding).
        kb = _StubKB({"Q42": {"P39": ["Qx", "Qy", "Qz"], "P106": ["Qa"]}})
        sling = _make_sling(kb)
        bindings = sling.propose_bindings("works_as", _oracle_raw(sample_subject_qids=["Q42"]))
        assert len(bindings) == 1
        assert bindings[0].kb_property == "P106"  # not P39

    def test_binding_carries_oracle_namespace_and_slots(self):
        kb = _StubKB({"Q42": {"P106": ["Qa"]}})
        sling = _make_sling(kb)
        raw = _oracle_raw(
            sample_subject_qids=["Q42"],
            slot_to_qualifier={"subject": "statement_subject"},
        )
        b = sling.propose_bindings("works_as", raw)[0]
        assert b.kb_namespace == "wikidata"
        assert b.slot_to_qualifier == {"subject": "statement_subject"}


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------

class TestFailOpen:
    def test_no_primary_property_returns_empty(self):
        kb = _StubKB({"Q42": {"P106": ["Qa"]}})
        sling = _make_sling(kb)
        raw = _oracle_raw(kb_property=None, sample_subject_qids=["Q42"])
        assert sling.propose_bindings("works_as", raw) == []

    def test_no_sample_entities_returns_empty(self):
        # No sample_subject_qids / example_qids → no sampling → no SLING binding.
        kb = _StubKB({"Q42": {"P106": ["Qa"]}})
        sling = _make_sling(kb)
        assert sling.propose_bindings("works_as", _oracle_raw()) == []

    def test_no_cooccurring_signal_returns_empty(self):
        # Sample entities resolve but carry only the primary property → no
        # other property to propose.
        kb = _StubKB({"Q42": {"P39": ["Qx"]}})
        sling = _make_sling(kb)
        raw = _oracle_raw(sample_subject_qids=["Q42"])
        assert sling.propose_bindings("works_as", raw) == []

    def test_enumerate_error_returns_empty_never_raises(self):
        kb = _StubKB(raise_on_enumerate=True)
        sling = _make_sling(kb)
        raw = _oracle_raw(sample_subject_qids=["Q42"])
        assert sling.propose_bindings("works_as", raw) == []  # must not raise

    def test_kb_without_enumerate_returns_empty(self):
        sling = _make_sling(_NoEnumerateKB())
        raw = _oracle_raw(sample_subject_qids=["Q42"])
        assert sling.propose_bindings("works_as", raw) == []

    def test_non_dict_oracle_raw_returns_empty(self):
        sling = _make_sling(_StubKB())
        assert sling.propose_bindings("works_as", "not a dict") == []

    def test_non_q_sample_entities_ignored(self):
        # Sample entries that aren't Q-ids are filtered out → no sampling.
        kb = _StubKB({"not_a_qid": {"P106": ["Qa"]}})
        sling = _make_sling(kb)
        raw = _oracle_raw(sample_subject_qids=["not_a_qid", "P39", ""])
        assert sling.propose_bindings("works_as", raw) == []


# ---------------------------------------------------------------------------
# Edge caching
# ---------------------------------------------------------------------------

class TestEdgeCaching:
    def test_discovered_edge_cached_into_property_relations(self):
        kb = _StubKB({"Q42": {"P106": ["Qa"]}})
        db = open_memory_db()
        sling = SlingFallback(db=db, kb_protocol=kb, llm_client=None)
        sling.propose_bindings("works_as", _oracle_raw(sample_subject_qids=["Q42"]))
        row = db.execute(
            "SELECT relation_type, related_value, source FROM property_relations "
            "WHERE kb_property='P39'"
        ).fetchone()
        assert row is not None
        assert row["relation_type"] == "related"
        assert row["related_value"] == "P106"
        assert row["source"] == "sling"
