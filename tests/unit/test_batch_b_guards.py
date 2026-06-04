"""v0.16.3 Batch B — pieces 2 (durable pinned flag) and 3 (coherence-aware
inverse-pair consistency guard).

Real-path: exercises the actual schema/migration, the seed loader, the
PredicateTranslation retract/regenerate gates, the ConsistencyChecker conflict
path, and the ContradictionTracer retract — no behavior is stubbed out.
"""

from __future__ import annotations

import json
import sqlite3

from aedos.database import open_memory_db
from aedos.layer3_substrate.consistency import ConsistencyChecker
from aedos.layer3_substrate.predicate_translation import PredicateTranslation
from aedos.layer4_sources.kb_wikidata import (
    WikidataAdapter,
    _build_property_example_query,
    _parse_property_example_bindings,
)
from aedos.layer5_result.contradiction_tracer import ContradictionTracer
from aedos.llm.client import LLMClient
from aedos.seed_loader import load_seeds_into_connection

_COUNTRY, _CITY, _CAPITAL = "Q6256", "Q515", "Q5119"
_STANDARD = '{"subject": "statement_subject", "object": "statement_value"}'
_INVERSE = '{"subject": "statement_value", "object": "statement_subject"}'


def _db():
    db = open_memory_db()
    db.row_factory = sqlite3.Row
    return db


def _insert(db, pred, prop, sq, pinned=0, subj=None, obj=None, ns="wikidata"):
    cur = db.execute(
        "INSERT INTO predicate_translation "
        "(aedos_predicate, object_type, routing_hint, kb_namespace, kb_property, "
        " slot_to_qualifier, subject_entity_types, object_entity_types, single_valued, "
        " pinned, reason, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            pred, "entity", "kb_resolvable", ns, prop, sq,
            json.dumps(subj) if subj else None,
            json.dumps(obj) if obj else None,
            1, pinned, "test", "2026-01-01T00:00:00",
        ),
    )
    db.commit()
    return cur.lastrowid


class _NoLLM:
    def extract_with_tool(self, *a, **k):
        raise AssertionError("LLM must not be called")

    def chat(self, *a, **k):
        return ""


# ---------------------------------------------------------------------------
# Piece 2 — durable pinned flag
# ---------------------------------------------------------------------------

class TestPinnedFlag:
    def test_schema_has_pinned_column_and_seeds_are_pinned(self):
        db = _db()
        n = load_seeds_into_connection(db)
        cols = [r["name"] for r in db.execute("PRAGMA table_info(predicate_translation)")]
        assert "pinned" in cols
        pinned = db.execute("SELECT COUNT(*) c FROM predicate_translation WHERE pinned=1").fetchone()["c"]
        assert pinned == n  # every seed row is pinned
        db.close()

    def test_retract_skips_pinned_row(self):
        db = _db()
        pt = PredicateTranslation(db=db, llm_client=LLMClient(_transport=_NoLLM()))
        rid = _insert(db, "capital", "P36", _STANDARD, pinned=1)
        pt.retract(rid, "should be blocked")
        row = db.execute("SELECT retracted_at FROM predicate_translation WHERE id=?", (rid,)).fetchone()
        assert row["retracted_at"] is None  # pinned → not retracted
        # an UNpinned row IS retracted
        rid2 = _insert(db, "stands_on", "P131", _STANDARD, pinned=0)
        pt.retract(rid2, "ok")
        row2 = db.execute("SELECT retracted_at FROM predicate_translation WHERE id=?", (rid2,)).fetchone()
        assert row2["retracted_at"] is not None
        db.close()

    def test_metadata_surfaces_pinned(self):
        db = _db()
        pt = PredicateTranslation(db=db, llm_client=LLMClient(_transport=_NoLLM()))
        _insert(db, "capital", "P36", _STANDARD, pinned=1)
        meta = pt.consult("capital")
        assert meta.pinned is True

    def test_generate_does_not_regenerate_over_pinned_row(self):
        """A pinned row is served; the oracle is never consulted (and so cannot
        replace it). _NoLLM raises if generation is attempted."""
        db = _db()
        pt = PredicateTranslation(db=db, llm_client=LLMClient(_transport=_NoLLM()))
        _insert(db, "capital", "P36", _STANDARD, pinned=1)
        meta = pt.consult("capital")  # would raise via _NoLLM if it regenerated
        assert meta.slot_to_qualifier == {"subject": "statement_subject", "object": "statement_value"}
        db.close()

    def test_consistency_resolve_skips_when_either_row_pinned(self):
        db = _db()
        cc = ConsistencyChecker(db=db)
        # Two genuinely-conflicting same-property rows (different, non-inverse sq),
        # one pinned. resolve must retract NEITHER.
        a = _insert(db, "capital", "P36", _STANDARD, pinned=1)
        b = _insert(db, "weird_cap", "P36",
                    '{"subject": "qualifier:P1", "object": "statement_value"}', pinned=0)
        from aedos.layer3_substrate.consistency import ConsistencyResult
        conflict = ConsistencyResult(
            status="conflict", inconsistency_class="transitive_equivalence_violation",
            row_a_id=a, row_b_id=b, table="predicate_translation",
        )
        cc.resolve_conflict(conflict)
        for rid in (a, b):
            r = db.execute("SELECT retracted_at FROM predicate_translation WHERE id=?", (rid,)).fetchone()
            assert r["retracted_at"] is None  # neither retracted (pin protects the pair)
        db.close()

    def test_contradiction_tracer_skips_pinned_predicate_row(self):
        db = _db()
        rid = _insert(db, "capital", "P36", _STANDARD, pinned=1)
        tracer = ContradictionTracer(db=db)
        # Seed the trace index so a contradiction names this predicate_translation row.
        tracer._propagator._trace_index["claimX"] = [("predicate_translation", rid)]
        tracer.trace_contradiction("claimX", {"source": "kb"})
        r = db.execute("SELECT retracted_at FROM predicate_translation WHERE id=?", (rid,)).fetchone()
        assert r["retracted_at"] is None  # pinned → never retracted
        db.close()


# ---------------------------------------------------------------------------
# Piece 3 — coherence-aware inverse-pair guard
# ---------------------------------------------------------------------------

class TestInverseCoherence:
    def test_coherent_inverse_pair_with_types_is_exempt(self):
        """capital_of (subject=city, inverse) + has_capital (subject=country,
        standard), both with entity-types: each puts country on statement_subject
        and city on statement_value → coherent → no conflict."""
        db = _db()
        cc = ConsistencyChecker(db=db)
        a = _insert(db, "has_capital", "P36", _STANDARD, subj=[_COUNTRY], obj=[_CITY])
        _insert(db, "capital_of", "P36", _INVERSE, subj=[_CITY, _CAPITAL], obj=[_COUNTRY])
        res = cc.check_on_write("predicate_translation", a)
        assert res.status == "pass"
        db.close()

    def test_incoherent_inverse_is_caught(self):
        """The bug shape: `capital` declares subject=country but an INVERSE map
        (country lands on the city-typed statement_value) against a standard
        has_capital peer → incoherent → conflict."""
        db = _db()
        cc = ConsistencyChecker(db=db)
        _insert(db, "has_capital", "P36", _STANDARD, subj=[_COUNTRY], obj=[_CITY, _CAPITAL])
        bad = _insert(db, "capital", "P36", _INVERSE, subj=[_COUNTRY], obj=[_CITY, _CAPITAL])
        res = cc.check_on_write("predicate_translation", bad)
        assert res.status == "conflict"
        assert res.inconsistency_class == "transitive_equivalence_violation"
        db.close()

    def test_untyped_inverse_pair_falls_open_to_exempt(self):
        """The seed capital_of/has_capital carry NO entity-types. Coherence is
        indeterminate → fall open to the prior structural exemption (no conflict),
        preserving N5."""
        db = _db()
        cc = ConsistencyChecker(db=db)
        a = _insert(db, "has_capital", "P36", _STANDARD)   # no types
        _insert(db, "capital_of", "P36", _INVERSE)         # no types
        res = cc.check_on_write("predicate_translation", a)
        assert res.status == "pass"
        db.close()

    def test_same_direction_same_property_no_conflict(self):
        """Two predicates on the same property with the SAME (standard) map are
        not a conflict (unchanged behavior)."""
        db = _db()
        cc = ConsistencyChecker(db=db)
        a = _insert(db, "capital", "P36", _STANDARD, subj=[_COUNTRY], obj=[_CITY])
        _insert(db, "has_capital", "P36", _STANDARD, subj=[_COUNTRY], obj=[_CITY])
        res = cc.check_on_write("predicate_translation", a)
        assert res.status == "pass"
        db.close()

    def test_coherent_inverse_with_distinct_equivalent_type_qids_is_exempt(self):
        """Adversarial-review fix [1]/[7]: two CORRECT inverse predicates may type
        the same KB role with equivalent-but-DISTINCT QIDs (country Q6256 vs
        sovereign-state Q3624078). Non-overlap must NOT be treated as a conflict —
        only POSITIVE cross-role contradiction is. This pair must stay exempt (no
        wrongful double-retraction)."""
        _SOV = "Q3624078"
        db = _db()
        cc = ConsistencyChecker(db=db)
        # has_capital (standard): country on statement_subject, city on value.
        a = _insert(db, "has_capital", "P36", _STANDARD, subj=[_COUNTRY], obj=[_CITY])
        # capital_of (inverse): country (as SOVEREIGN STATE) on statement_subject,
        # city on value — same roles, different country vocabulary.
        _insert(db, "capital_of", "P36", _INVERSE, subj=[_CITY], obj=[_SOV])
        res = cc.check_on_write("predicate_translation", a)
        assert res.status == "pass"   # NOT a conflict
        db.close()

    def test_symmetric_typed_inverse_pair_is_exempt(self):
        """A SYMMETRIC property (P26 spouse) types every role [human]; a swapped-sq
        pair overlaps cross-role trivially but the type is on BOTH roles → not
        role-discriminating → exempt (direction is agnostic). Must NOT false-flag."""
        _HUMAN = "Q5"
        db = _db()
        cc = ConsistencyChecker(db=db)
        a = _insert(db, "spouse_of", "P26", _STANDARD, subj=[_HUMAN], obj=[_HUMAN])
        _insert(db, "married_to", "P26", _INVERSE, subj=[_HUMAN], obj=[_HUMAN])
        res = cc.check_on_write("predicate_translation", a)
        assert res.status == "pass"
        db.close()


class TestPropertyExampleSourcer:
    """Adversarial-review fix [10]: the real example-sourcer SPARQL builder,
    parser, and fixture twin (previously only mock-substituted)."""

    def test_parse_property_example_bindings_keeps_only_entity_pairs(self):
        bindings = [
            {"s": {"value": "http://www.wikidata.org/entity/Q142"},
             "v": {"value": "http://www.wikidata.org/entity/Q90"}},
            {"s": {"value": "http://www.wikidata.org/entity/Q142"},
             "v": {"value": "http://www.wikidata.org/entity/Q90"}},   # dup → dropped
            {"s": {"value": "http://www.wikidata.org/entity/Q183"},
             "v": {"value": "a literal string"}},                      # non-entity → dropped
            {"s": {"value": "http://www.wikidata.org/entity/Q183"},
             "v": {"value": "http://www.wikidata.org/entity/Q64"}},
        ]
        pairs = _parse_property_example_bindings(bindings, limit=5)
        assert pairs == [("Q142", "Q90"), ("Q183", "Q64")]   # subject->value order, deduped

    def test_build_property_example_query_validates_and_limits(self):
        q = _build_property_example_query("P36", 3)
        assert "wdt:P36" in q and "LIMIT 3" in q and "isIRI(?v)" in q
        for bad in [("P36x", 3), ("P36", 0), ("P36", 999)]:
            try:
                _build_property_example_query(*bad)
                assert False, f"expected ValueError for {bad}"
            except ValueError:
                pass

    def test_fixture_twin_returns_pairs(self):
        """sample_property_examples in fixture mode reads property_examples_P36.json
        and returns (Q142,Q90)-shaped pairs — locking the ?s=statement_subject
        orientation contract."""
        kb = WikidataAdapter(http_cache=None)  # fixture mode (RUN_LIVE_KB unset)
        assert kb.sample_property_examples("P36", 5) == [("Q142", "Q90"), ("Q183", "Q64")]
        # Missing fixture → [] (fail-open), never raises.
        assert kb.sample_property_examples("P999999", 5) == []


class TestPinnedRegenerateGate:
    """Adversarial-review fix [4]/[8]: the pre-INSERT regenerate-skip must fire on
    a true cache-miss while a pinned row exists, keyed per-PREDICATE (not per
    namespace), with NO INSERT/replace of the pinned row."""

    def test_generate_and_store_returns_pinned_row_without_regenerating(self):
        db = _db()
        # _NoLLM raises if generation is attempted.
        pt = PredicateTranslation(db=db, llm_client=LLMClient(_transport=_NoLLM()))
        rid = _insert(db, "capital", "P36", _STANDARD, pinned=1)
        # Call the GENERATE path directly (bypassing consult's _fetch short-circuit)
        # to prove the in-generation pinned gate fires.
        meta = pt._generate_and_store("capital", "wikidata")
        assert meta.id == rid                       # the SAME pinned row, not a new one
        assert meta.pinned is True
        # row id stable, still single pinned row, not retracted/replaced.
        rows = db.execute(
            "SELECT id, pinned, retracted_at FROM predicate_translation "
            "WHERE aedos_predicate='capital'"
        ).fetchall()
        assert len(rows) == 1 and rows[0]["id"] == rid and rows[0]["retracted_at"] is None
        db.close()
