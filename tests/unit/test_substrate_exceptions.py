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

    def test_leak_guard_row_exempt_from_eviction(self, tmp_path):
        # PATCH-C r2c-2: a reason='leak_guard' row is NEVER an eviction
        # candidate — excluded from BOTH the cap COUNT and the eviction
        # subquery (contract §0.11 #2). No production path records this reason
        # yet, so we write the guard row directly via the db; the test proves
        # the SQL exemption holds.
        N = 3
        db = _db(tmp_path)
        cache = SubstrateExceptionCache(db, max_rows=N)

        # The guard row is given the OLDEST created_at/last_consulted_at, so a
        # plain LRU (oldest-consulted-first) would evict it FIRST were it not
        # exempt. This is the adversarial placement that proves the exemption.
        db.execute(
            """INSERT INTO substrate_exceptions
               (exception_kind, relation_type, property_path, source_identifier,
                target_identifier, reason, created_at, last_consulted_at, used_count)
               VALUES ('transitive_path','part_of','P131','Q_GUARD','Q999',
                       'leak_guard','2000-01-01T00:00:00+00:00',
                       '2000-01-01T00:00:00+00:00',0)""",
        )
        db.commit()

        # Now insert N+1 ask_false rows (each newer than the guard row). The
        # final record_nogood triggers _evict_if_over_cap; with N+1 ask_false
        # rows and a cap of N, exactly one ask_false row must be evicted — and
        # it must be an ask_false row, never the older leak_guard row.
        for i in range(N + 1):
            cache.record_nogood(
                relation_type="part_of", source_identifier=f"Q{i}",
                target_identifier="Q999", property_path="P131", reason="ask_false",
            )

        # (a) the leak_guard row still exists and is NOT retracted.
        guard = db.execute(
            "SELECT retracted_at FROM substrate_exceptions WHERE reason='leak_guard'"
        ).fetchall()
        assert len(guard) == 1, "the leak_guard row must survive eviction"
        assert guard[0]["retracted_at"] is None, (
            "the leak_guard row must remain live (retracted_at IS NULL)"
        )

        # (b) surplus ask_false rows were deleted: N+1 inserted, cap N → exactly
        # one evicted, N live ask_false rows remain.
        live_ask_false = db.execute(
            "SELECT COUNT(*) FROM substrate_exceptions "
            "WHERE reason='ask_false' AND retracted_at IS NULL"
        ).fetchone()[0]
        assert live_ask_false == N, (
            f"surplus ask_false rows must be evicted to the cap; got "
            f"{live_ask_false} live, expected {N}"
        )

        # (c) the guard row is EXCLUDED from the cap count: the cap-relevant
        # count (the same predicate _evict_if_over_cap uses) tallies only the N
        # ask_false rows, never the guard row — so the guard does not consume
        # capacity.
        cap_count = db.execute(
            "SELECT COUNT(*) FROM substrate_exceptions WHERE retracted_at IS NULL "
            "AND reason NOT IN ('leak_guard','operator_marked')"
        ).fetchone()[0]
        assert cap_count == N, (
            f"the guard row must be excluded from the cap count; cap count is "
            f"{cap_count}, expected {N}"
        )
        # The guard row IS present in the table overall (N ask_false + 1 guard).
        total_live = db.execute(
            "SELECT COUNT(*) FROM substrate_exceptions WHERE retracted_at IS NULL"
        ).fetchone()[0]
        assert total_live == N + 1


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
