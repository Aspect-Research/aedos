"""Phase H D47 step 3: pipeline-integration tests for the Wikipedia
normalizer.

Exercises the wiring between Extractor → Walker → KBVerifier →
EntityResolver → WikipediaNormalizer. Live Wikipedia / Wikidata calls
are mocked via stub objects so these tests can run without
RUN_LIVE_KB=1.

Coverage:
  - The resolver routes through the normalizer; the cache + KB lookup
    use the normalized form.
  - The normalizer is skipped on the asserting party (first-person
    canonicalization output) and on synthetic event ids.
  - Tier U writes the canonical form in subject/object and the surface
    form in subject_surface/object_surface when they differ.
  - Cross-utterance dedup: writing 'USA is a country' and 'United
    States is a country' (when normalizer maps USA → United States)
    produces one Tier U row.
  - VerificationContext.source_text threads through to the LocalContext
    the resolver sees.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock

import pytest

from aedos.database import open_memory_db
from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer1_extraction.wikipedia_normalizer import (
    NormalizationResult,
    OUTCOME_CANONICAL_NO_REDIRECT,
    OUTCOME_CLEAN_REDIRECT,
)
from aedos.layer3_substrate.resolver import EntityResolver
from aedos.layer4_sources.kb_protocol import (
    LocalContext,
    ResolutionCandidate,
)
from aedos.layer4_sources.tier_u import TierU


class _StubNormalizer:
    """Minimal stub that returns a pre-canned mapping
    ``surface_form → normalized_form`` (or passes through if the form
    isn't in the table). Records the calls for assertions."""

    def __init__(self, mapping: Optional[dict] = None):
        self._mapping = mapping or {}
        self.calls: list[dict] = []

    def normalize(self, surface_form, **kwargs):
        self.calls.append({"surface_form": surface_form, **kwargs})
        normalized = self._mapping.get(surface_form, surface_form)
        outcome = (
            OUTCOME_CLEAN_REDIRECT
            if normalized != surface_form
            else OUTCOME_CANONICAL_NO_REDIRECT
        )
        return NormalizationResult(
            surface_form=surface_form,
            normalized_form=normalized,
            stage_a_outcome=outcome,
            stage_a_redirect_target=normalized if normalized != surface_form else None,
            duration_ms=0.0,
        )


class _StubKB:
    """Records the references it gets asked to resolve so tests can
    assert the resolver passed the normalized form, not the surface form.
    Returns one canned candidate per reference."""

    def __init__(self, by_reference: Optional[dict] = None):
        self._by_reference = by_reference or {}
        self.calls: list[str] = []

    def resolve_entity(self, reference, local_context):
        self.calls.append(reference)
        if reference in self._by_reference:
            qid = self._by_reference[reference]
            return [
                ResolutionCandidate(
                    kb_identifier=qid,
                    provenance={"label": reference},
                    score=1.0,
                )
            ]
        return []

    def lookup_statements(self, entity, predicate):
        return []

    def subsumption(self, a, b, relation_type):
        from aedos.layer4_sources.kb_protocol import SubsumptionResult

        return SubsumptionResult(verdict="unrelated")


# ---------------------------------------------------------------------------
# Resolver-level wiring
# ---------------------------------------------------------------------------


class TestResolverNormalizes:
    def test_resolver_uses_normalized_form_for_kb_query(self):
        db = open_memory_db()
        normalizer = _StubNormalizer({"Obama": "Barack Obama"})
        kb = _StubKB({"Barack Obama": "Q76"})
        resolver = EntityResolver(
            kb_protocol=kb, db=db, wikipedia_normalizer=normalizer
        )
        ctx = LocalContext(
            predicate="holds_role",
            slot_position="subject",
            source_text="Obama signed the bill.",
        )
        candidates = resolver.resolve("Obama", ctx)
        assert len(candidates) == 1
        assert candidates[0].kb_identifier == "Q76"
        # KB should have seen the canonical form, not the surface form.
        assert kb.calls == ["Barack Obama"]
        # The normalizer should have been invoked once.
        assert len(normalizer.calls) == 1
        assert normalizer.calls[0]["surface_form"] == "Obama"

    def test_resolver_caches_normalized_form(self):
        """Two successive resolves of the same surface form hit the
        cache; the normalizer is invoked again only because the
        resolver runs Stage 1 before the cache lookup. The KB is hit
        only on the first call."""
        db = open_memory_db()
        normalizer = _StubNormalizer({"Obama": "Barack Obama"})
        kb = _StubKB({"Barack Obama": "Q76"})
        resolver = EntityResolver(
            kb_protocol=kb, db=db, wikipedia_normalizer=normalizer
        )
        ctx = LocalContext(predicate="holds_role", slot_position="subject")
        first = resolver.resolve("Obama", ctx)
        second = resolver.resolve("Obama", ctx)
        assert first[0].kb_identifier == "Q76"
        assert second[0].kb_identifier == "Q76"
        # Second call hits the cache: KB called only once.
        assert len(kb.calls) == 1

    def test_resolver_skips_normalizer_for_asserting_party(self):
        """First-person canonicalization output: the asserting party
        identifier is not a Wikipedia article title; normalization is
        skipped to avoid silently inventing a wrong canonical."""
        db = open_memory_db()
        normalizer = _StubNormalizer({"user_42": "Some Random User Article"})
        kb = _StubKB({"user_42": "QX"})
        resolver = EntityResolver(
            kb_protocol=kb, db=db, wikipedia_normalizer=normalizer
        )
        ctx = LocalContext(
            predicate="lives_in",
            slot_position="subject",
            asserting_party="user_42",
        )
        resolver.resolve("user_42", ctx)
        # Normalizer should NOT have been invoked.
        assert normalizer.calls == []
        # KB should have seen the surface form.
        assert kb.calls == ["user_42"]

    def test_resolver_skips_normalizer_for_event_ids(self):
        db = open_memory_db()
        normalizer = _StubNormalizer({"event_42": "Wrong Article"})
        kb = _StubKB({"event_42": "QX"})
        resolver = EntityResolver(
            kb_protocol=kb, db=db, wikipedia_normalizer=normalizer
        )
        ctx = LocalContext(predicate="happened", slot_position="subject")
        resolver.resolve("event_42", ctx)
        assert normalizer.calls == []
        assert kb.calls == ["event_42"]

    def test_resolver_fails_open_when_normalizer_raises(self):
        """A normalizer outage must not abstain on every resolution.
        Surface form is used as-is on exception."""
        db = open_memory_db()
        normalizer = MagicMock()
        normalizer.normalize.side_effect = RuntimeError("outage")
        kb = _StubKB({"Obama": "Q76"})
        resolver = EntityResolver(
            kb_protocol=kb, db=db, wikipedia_normalizer=normalizer
        )
        ctx = LocalContext(predicate="holds_role", slot_position="subject")
        candidates = resolver.resolve("Obama", ctx)
        # KB saw the surface form because normalization failed.
        assert kb.calls == ["Obama"]
        assert candidates[0].kb_identifier == "Q76"

    def test_resolver_threads_local_context_to_normalizer(self):
        db = open_memory_db()
        normalizer = _StubNormalizer({"Obama": "Barack Obama"})
        kb = _StubKB({"Barack Obama": "Q76"})
        resolver = EntityResolver(
            kb_protocol=kb, db=db, wikipedia_normalizer=normalizer
        )
        ctx = LocalContext(
            predicate="holds_role",
            slot_position="subject",
            source_text="Obama signed the bill in 2010.",
            claim_subject="Obama",
            claim_predicate="signed",
            claim_object="the bill",
            claim_id="claim-1",
        )
        resolver.resolve("Obama", ctx)
        call = normalizer.calls[0]
        assert call["source_text"] == "Obama signed the bill in 2010."
        assert call["claim_subject"] == "Obama"
        assert call["claim_predicate"] == "signed"
        assert call["claim_object"] == "the bill"
        assert call["claim_id"] == "claim-1"
        assert call["slot_position"] == "subject"


# ---------------------------------------------------------------------------
# Tier U-level wiring
# ---------------------------------------------------------------------------


def _claim(
    subject: str,
    predicate: str,
    obj: str,
    polarity: int = 1,
    asserting_party: str = "user_42",
    claim_id: str = "c1",
) -> Claim:
    return Claim(
        claim_id=claim_id,
        subject=subject,
        predicate=predicate,
        object=obj,
        polarity=polarity,
        source_text=f"{subject} {predicate} {obj}",
        asserting_party=asserting_party,
        triage_decision=TriageDecision.VERIFY,
    )


class TestTierUDedupOnNormalized:
    def test_cross_utterance_writes_dedup_to_one_row(self):
        """Asserting 'USA is a country' and then 'United States is a
        country' — both normalize to 'United States' — collapses to a
        single Tier U row via the idempotency branch."""
        db = open_memory_db()
        normalizer = _StubNormalizer({"USA": "United States"})
        tier_u = TierU(db=db, wikipedia_normalizer=normalizer)

        first = tier_u.write(_claim("USA", "is_a", "country"))
        second = tier_u.write(_claim("United States", "is_a", "country"))

        # Same row: the second write is idempotent.
        assert second.was_idempotent is True
        assert second.row_id == first.row_id

        # Persisted: subject is the canonical form, surface_form is preserved
        # in subject_surface for the row whose surface differed.
        rows = db.execute("SELECT subject, subject_surface FROM tier_u").fetchall()
        assert len(rows) == 1
        assert rows[0]["subject"] == "United States"
        # First write had USA as surface_form; the surface column stores it.
        assert rows[0]["subject_surface"] == "USA"

    def test_surface_form_null_when_unchanged(self):
        """Writing the canonical form directly leaves subject_surface NULL."""
        db = open_memory_db()
        normalizer = _StubNormalizer({})  # no mappings → pass-through
        tier_u = TierU(db=db, wikipedia_normalizer=normalizer)
        tier_u.write(_claim("United States", "is_a", "country"))
        row = db.execute("SELECT subject, subject_surface FROM tier_u").fetchone()
        assert row["subject"] == "United States"
        assert row["subject_surface"] is None

    def test_lookup_keys_on_normalized_form(self):
        """A claim written with the surface form can be looked up by the
        canonical form (and vice versa) — normalization fires on both
        paths."""
        db = open_memory_db()
        normalizer = _StubNormalizer({"USA": "United States"})
        tier_u = TierU(db=db, wikipedia_normalizer=normalizer)

        # Write with the surface form.
        tier_u.write(_claim("USA", "is_a", "country"))

        # Lookup with the canonical form should find the row.
        result_canonical = tier_u.lookup(_claim("United States", "is_a", "country"))
        assert result_canonical.found is True

        # Lookup with the surface form should also find it (normalization
        # fires on the lookup side too).
        result_surface = tier_u.lookup(_claim("USA", "is_a", "country"))
        assert result_surface.found is True

    def test_tier_u_without_normalizer_unchanged(self):
        """When no normalizer is wired, behavior matches pre-D47:
        subject/object are keyed on the surface form directly and the
        surface columns stay NULL."""
        db = open_memory_db()
        tier_u = TierU(db=db)  # no normalizer
        tier_u.write(_claim("USA", "is_a", "country"))
        tier_u.write(_claim("United States", "is_a", "country"))
        rows = db.execute("SELECT subject FROM tier_u").fetchall()
        # Two distinct rows, one per surface form.
        assert sorted(r["subject"] for r in rows) == ["USA", "United States"]
