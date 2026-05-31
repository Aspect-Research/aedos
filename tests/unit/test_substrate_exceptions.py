"""v0.16 WS3 §3D: tests for the bounded nogood cache (SubstrateExceptionCache)
and §3A provenance term, plus the §3E stale-scoping of propagate_retraction."""

from __future__ import annotations

from aedos.database import open_db
from aedos.layer3_substrate.substrate_exceptions import SubstrateExceptionCache


def _db(tmp_path):
    return open_db(str(tmp_path / "aedos.db"))


class TestSubstrateExceptionCache:
    def test_record_then_is_nogood(self, tmp_path):
        db = _db(tmp_path)
        cache = SubstrateExceptionCache(db)
        assert cache.is_nogood(
            relation_type="part_of",
            source_identifier="Q270", target_identifier="Q183",
        ) is False
        cache.record_nogood(
            relation_type="part_of",
            source_identifier="Q270", target_identifier="Q183",
            property_path="P131|P30|P17", reason="ask_false",
        )
        assert cache.is_nogood(
            relation_type="part_of",
            source_identifier="Q270", target_identifier="Q183",
        ) is True

    def test_is_nogood_path_specific_when_path_given(self, tmp_path):
        db = _db(tmp_path)
        cache = SubstrateExceptionCache(db)
        cache.record_nogood(
            relation_type="part_of", source_identifier="Q1",
            target_identifier="Q2", property_path="P131", reason="ask_false",
        )
        # Matching path → hit; different path with property_path supplied → miss.
        assert cache.is_nogood(
            relation_type="part_of", source_identifier="Q1",
            target_identifier="Q2", property_path="P131",
        ) is True
        assert cache.is_nogood(
            relation_type="part_of", source_identifier="Q1",
            target_identifier="Q2", property_path="P999",
        ) is False
        # Path-agnostic (None) → hit regardless of stored path.
        assert cache.is_nogood(
            relation_type="part_of", source_identifier="Q1",
            target_identifier="Q2",
        ) is True

    def test_record_is_idempotent(self, tmp_path):
        db = _db(tmp_path)
        cache = SubstrateExceptionCache(db)
        a = cache.record_nogood(
            relation_type="part_of", source_identifier="Q1",
            target_identifier="Q2", property_path="P131", reason="ask_false",
        )
        b = cache.record_nogood(
            relation_type="part_of", source_identifier="Q1",
            target_identifier="Q2", property_path="P131", reason="ask_false",
        )
        assert a == b
        count = db.execute(
            "SELECT COUNT(*) FROM substrate_exceptions"
        ).fetchone()[0]
        assert count == 1

    def test_retract_clears_nogood(self, tmp_path):
        db = _db(tmp_path)
        cache = SubstrateExceptionCache(db)
        row_id = cache.record_nogood(
            relation_type="part_of", source_identifier="Q1",
            target_identifier="Q2", property_path="P131", reason="ask_false",
        )
        cache.retract(row_id, reason="operator: path now holds")
        assert cache.is_nogood(
            relation_type="part_of", source_identifier="Q1", target_identifier="Q2",
        ) is False

    def test_lru_eviction_over_cap(self, tmp_path):
        db = _db(tmp_path)
        cache = SubstrateExceptionCache(db, max_rows=3)
        for i in range(5):
            cache.record_nogood(
                relation_type="part_of", source_identifier=f"Q{i}",
                target_identifier="Q999", property_path="P131", reason="ask_false",
            )
        live = db.execute(
            "SELECT COUNT(*) FROM substrate_exceptions WHERE retracted_at IS NULL"
        ).fetchone()[0]
        assert live <= 3

    def test_vetoes_binding_path(self, tmp_path):
        db = _db(tmp_path)
        cache = SubstrateExceptionCache(db)
        assert cache.vetoes("born_in", "P19", "Q7186") is False
        cache.record_nogood(
            relation_type="born_in", source_identifier="Q7186",
            target_identifier="P19", property_path="P19", reason="ask_false",
            exception_kind="subsumption",
        )
        assert cache.vetoes("born_in", "P19", "Q7186") is True


class TestProvenanceTerm:
    def test_includes_assertion_derivation(self):
        from aedos.layer5_result.trace import ProvenanceLiteral, ProvenanceTerm
        term = ProvenanceTerm()
        assert term.includes_assertion() is False
        term.add_alternative(ProvenanceTerm.lit(ProvenanceLiteral(source="kb")))
        assert term.includes_assertion() is False
        term.add_alternative(
            ProvenanceTerm.lit(ProvenanceLiteral(source="tier_u", assertion=True))
        )
        assert term.includes_assertion() is True

    def test_source_rows_distinct(self):
        from aedos.layer5_result.trace import ProvenanceLiteral, ProvenanceTerm
        term = ProvenanceTerm()
        term.add_alternative(ProvenanceTerm.lit(
            ProvenanceLiteral(source="tier_u", table="tier_u", row_id=5)))
        term.add_alternative(ProvenanceTerm.lit(
            ProvenanceLiteral(source="tier_u", table="tier_u", row_id=5)))  # dup
        term.add_alternative(ProvenanceTerm.lit(
            ProvenanceLiteral(source="kb", table="entity_resolution_cache", row_id=9)))
        term.add_alternative(ProvenanceTerm.lit(
            ProvenanceLiteral(source="python")))  # no row id → skipped
        assert term.source_rows() == [("tier_u", 5), ("entity_resolution_cache", 9)]

    def test_chain_includes_assertion_property(self):
        from aedos.layer5_result.trace import (
            JustificationTrace, ProvenanceLiteral, ProvenanceTerm, TraceNode,
        )
        trace = JustificationTrace(root=TraceNode("claim"))
        assert trace.chain_includes_assertion is False
        trace.provenance.add_alternative(
            ProvenanceTerm.lit(ProvenanceLiteral(source="tier_u", assertion=True)))
        assert trace.chain_includes_assertion is True


class TestRetractionStaleScoping:
    def test_given_assertion_verdict_marked_stale(self, tmp_path):
        from aedos.layer5_result.retraction import RetractionPropagator
        db = _db(tmp_path)
        prop = RetractionPropagator(db=db)
        prop.record_verdict_trace(
            "c1", "verified_given_assertion", [("tier_u", 7)])
        out = prop.propagate_retraction("tier_u", 7)
        assert len(out) == 1
        assert out[0].stale is True
        assert out[0].scoped_given_assertion is True
        assert prop.is_stale("c1") is True
        prop.clear_stale("c1")
        assert prop.is_stale("c1") is False

    def test_base_verdict_not_marked_stale_but_recorded(self, tmp_path):
        from aedos.layer5_result.retraction import RetractionPropagator
        db = _db(tmp_path)
        prop = RetractionPropagator(db=db)
        prop.record_verdict_trace("c2", "verified", [("tier_u", 8)])
        out = prop.propagate_retraction("tier_u", 8)
        # Dependency recorded (audit) but NOT staled (asymmetric trust).
        assert len(out) == 1
        assert out[0].stale is False
        assert out[0].scoped_given_assertion is False
        assert prop.is_stale("c2") is False
