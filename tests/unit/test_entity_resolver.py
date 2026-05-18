"""Tests for EntityResolver — cache cold/warm, selection, retraction."""

from __future__ import annotations

import pytest

from aedos.database import open_memory_db
from aedos.layer4_sources.kb_protocol import LocalContext, ResolutionCandidate
from aedos.layer3_substrate.resolver import EntityResolver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockKB:
    def __init__(self, candidates: list[ResolutionCandidate]):
        self._candidates = candidates
        self.call_count = 0

    def resolve_entity(self, reference, local_context):
        self.call_count += 1
        return list(self._candidates)

    def lookup_statements(self, entity, predicate):
        return []

    def subsumption(self, entity_a, entity_b, relation_type):
        from aedos.layer4_sources.kb_protocol import SubsumptionResult
        return SubsumptionResult(verdict="unrelated")


def _resolver(candidates=None):
    db = open_memory_db()
    kb = MockKB(candidates or [ResolutionCandidate(kb_identifier="Q76", score=0.9)])
    return EntityResolver(kb_protocol=kb, db=db), kb, db


def _lc(predicate="holds_role", slot="subject"):
    return LocalContext(predicate=predicate, slot_position=slot)


# ---------------------------------------------------------------------------
# TestEntityResolverCache
# ---------------------------------------------------------------------------

class TestEntityResolverCache:
    def test_cold_cache_calls_kb(self):
        resolver, kb, _ = _resolver()
        resolver.resolve("Obama", _lc())
        assert kb.call_count == 1

    def test_cold_cache_returns_candidates(self):
        resolver, _, _ = _resolver()
        results = resolver.resolve("Obama", _lc())
        assert len(results) == 1
        assert results[0].kb_identifier == "Q76"

    def test_warm_cache_no_kb_call(self):
        resolver, kb, _ = _resolver()
        resolver.resolve("Obama", _lc())
        resolver.resolve("Obama", _lc())
        assert kb.call_count == 1  # second call hits cache

    def test_warm_cache_returns_cache_hit_provenance(self):
        resolver, _, _ = _resolver()
        resolver.resolve("Obama", _lc())
        results = resolver.resolve("Obama", _lc())
        assert results[0].provenance.get("cache_hit") is True

    def test_cache_written_to_db(self):
        resolver, _, db = _resolver()
        resolver.resolve("Obama", _lc())
        count = db.execute("SELECT count(*) FROM entity_resolution_cache").fetchone()[0]
        assert count == 1

    def test_cache_stores_kb_identifier(self):
        resolver, _, db = _resolver()
        resolver.resolve("Obama", _lc())
        row = db.execute("SELECT resolved_kb_identifier FROM entity_resolution_cache LIMIT 1").fetchone()
        assert row["resolved_kb_identifier"] == "Q76"

    def test_different_predicates_separate_cache_keys(self):
        resolver, kb, _ = _resolver()
        resolver.resolve("Obama", _lc(predicate="holds_role"))
        resolver.resolve("Obama", _lc(predicate="employed_by"))
        assert kb.call_count == 2


# ---------------------------------------------------------------------------
# TestEntityResolverSelect
# ---------------------------------------------------------------------------

class TestEntityResolverSelect:
    def test_select_top_candidate(self):
        resolver, _, _ = _resolver([
            ResolutionCandidate(kb_identifier="Q76", score=0.9),
            ResolutionCandidate(kb_identifier="Q842926", score=0.4),
        ])
        result = resolver.select(
            [ResolutionCandidate(kb_identifier="Q76", score=0.9),
             ResolutionCandidate(kb_identifier="Q842926", score=0.4)],
            _lc(),
        )
        assert result == "Q76"

    def test_select_none_when_empty(self):
        resolver, _, _ = _resolver()
        assert resolver.select([], _lc()) is None

    def test_select_none_when_score_below_threshold(self):
        resolver, _, _ = _resolver()
        result = resolver.select(
            [ResolutionCandidate(kb_identifier="Q76", score=0.5)],
            _lc(),
        )
        assert result is None

    def test_select_passes_threshold(self):
        resolver, _, _ = _resolver()
        result = resolver.select(
            [ResolutionCandidate(kb_identifier="Q76", score=0.61)],
            _lc(),
        )
        assert result == "Q76"


# ---------------------------------------------------------------------------
# TestEntityResolverRetraction
# ---------------------------------------------------------------------------

class TestEntityResolverRetraction:
    def test_retract_sets_retracted_at(self):
        resolver, _, db = _resolver()
        resolver.resolve("Obama", _lc())
        row = db.execute("SELECT id FROM entity_resolution_cache LIMIT 1").fetchone()
        resolver.retract_cache_entry(row["id"], "test retraction")
        retracted = db.execute(
            "SELECT retracted_at FROM entity_resolution_cache WHERE id=?", (row["id"],)
        ).fetchone()
        assert retracted["retracted_at"] is not None

    def test_retracted_entry_not_returned_on_next_lookup(self):
        resolver, kb, db = _resolver()
        resolver.resolve("Obama", _lc())
        row = db.execute("SELECT id FROM entity_resolution_cache LIMIT 1").fetchone()
        resolver.retract_cache_entry(row["id"], "stale")
        # Next lookup should miss cache and call KB again
        resolver.resolve("Obama", _lc())
        assert kb.call_count == 2
