"""Phase H D47 step 4: end-to-end live validation of the normalizer path.

Exercises the full path Extractor (skipped here for focus) → Walker →
KBVerifier → EntityResolver (with normalizer) → Wikidata, against live
Wikipedia + Wikidata APIs. The previous Phase G D47-pinning xfails
(in test_wikidata_live.py) tested the adapter directly and pinned the
Wikidata data-model limit; these tests exercise the layer above and
verify that D47 routes around the limit when the input provides
disambiguating source-text context.

Three scenarios:

  1. Bare ambiguous reference + source text that disambiguates
     ('Obama' + 'Barack Obama signed the bill...') → Stage 2 picks
     'Barack Obama' → resolver finds Q76.

  2. Unambiguous full name (no D47 lift expected) → resolver finds
     Q76 directly (regression guard: D47 does not break the working
     path).

  3. Bare ambiguous reference with no disambiguating context → Stage 2
     abstains → resolver behaves as it does today (likely no
     resolution).

Gated by `RUN_LIVE_KB=1` (same gate as the Wikidata live tests; the
normalizer uses the same MediaWiki / Wikidata networks).
"""

from __future__ import annotations

import os

import pytest

from aedos.config import Config
from aedos.database import open_memory_db
from aedos.layer1_extraction.wikipedia_normalizer import WikipediaNormalizer
from aedos.layer3_substrate.resolver import EntityResolver
from aedos.layer4_sources.kb_protocol import LocalContext
from aedos.layer4_sources.kb_wikidata import WikidataAdapter
from aedos.llm.client import LLMClient
from aedos.utils.http_cache import CachingHTTPClient, LRUHTTPCache


_RUN_LIVE = os.environ.get("RUN_LIVE_KB") == "1"
pytestmark = pytest.mark.skipif(
    not _RUN_LIVE,
    reason="Live D47 E2E tests require RUN_LIVE_KB=1",
)


@pytest.fixture
def live_resolver():
    """Build a real EntityResolver wired with: live WikidataAdapter,
    live WikipediaNormalizer, real LLMClient, in-memory DB. Mirrors how
    build_pipeline wires the resolver."""
    db = open_memory_db()
    config = Config()
    cache = LRUHTTPCache(
        max_size=config.http_cache_lru_size,
        default_ttl_seconds=config.http_cache_entity_ttl_seconds,
    )
    http_client = CachingHTTPClient(
        cache=cache,
        default_ttl_seconds=config.http_cache_entity_ttl_seconds,
        headers={"User-Agent": config.user_agent},
    )
    llm = LLMClient()
    kb = WikidataAdapter(
        http_cache=http_client, llm_client=llm, db=db, config=config
    )
    normalizer = WikipediaNormalizer(
        http_cache=http_client, llm_client=llm, db=db, config=config
    )
    resolver = EntityResolver(
        kb_protocol=kb, db=db, llm_client=llm, wikipedia_normalizer=normalizer
    )
    yield resolver, db
    db.close()


class TestD47EndToEnd:
    def test_bare_obama_with_disambiguating_context_reaches_q76(self, live_resolver):
        """The flagship D47 scenario: bare 'Obama' as a reference, with
        source text mentioning 'Barack Obama' earlier in the sentence,
        should resolve to Q76 via:

          Stage 1 → disambiguation_page (Wikipedia's 'Obama' is a
                    disambiguation page).
          Stage 2 → LLM sees 'Barack Obama signed the bill...' in
                    source_text → picks 'Barack Obama' from candidates.
          Resolver → KB query for 'Barack Obama' → wbsearchentities
                     returns Q76 at rank 0 → type filter [Q5] keeps it.
        """
        resolver, _ = live_resolver
        lc = LocalContext(
            predicate="holds_role",
            slot_position="subject",
            expected_entity_types=["Q5"],
            source_text=(
                "Barack Obama signed the bill on Tuesday. Obama said it "
                "was historic legislation that would benefit millions of "
                "Americans."
            ),
            claim_subject="Obama",
            claim_predicate="said",
            claim_object="it was historic legislation",
        )
        candidates = resolver.resolve("Obama", lc)
        selected = resolver.select(candidates, lc)
        assert selected == "Q76", (
            f"D47 path should disambiguate 'Obama' to 'Barack Obama' via "
            f"Stage 2 and resolve to Q76; got selected={selected}, "
            f"candidates={[c.kb_identifier for c in candidates]}"
        )

    def test_full_name_unambiguous_still_reaches_q76(self, live_resolver):
        """Regression guard: D47 must not break the working path. A full
        canonical reference like 'Barack Obama' hits Stage 1's
        canonical_no_redirect outcome — no Stage 2 invoked, and the
        resolver finds Q76 directly."""
        resolver, _ = live_resolver
        lc = LocalContext(
            predicate="holds_role",
            slot_position="subject",
            expected_entity_types=["Q5"],
            source_text="Barack Obama was the 44th US President.",
            claim_subject="Barack Obama",
            claim_predicate="was",
            claim_object="the 44th US President",
        )
        candidates = resolver.resolve("Barack Obama", lc)
        selected = resolver.select(candidates, lc)
        assert selected == "Q76"

    def test_bare_reference_no_context_falls_through_gracefully(self, live_resolver):
        """No disambiguating context → Stage 2 should abstain (the
        prompt's bias) → surface form preserved → downstream KB query
        sees 'Obama' → may or may not resolve to Q76 (depending on
        wbsearchentities' current ranking + the type filter). The test
        asserts only that the resolver doesn't crash and the call path
        survives; success or abstention are both acceptable outcomes.

        This documents D47's soundness commitment: when the user wasn't
        precise enough, the system honestly abstains rather than guess.
        """
        resolver, _ = live_resolver
        lc = LocalContext(
            predicate="holds_role",
            slot_position="subject",
            expected_entity_types=["Q5"],
            source_text=None,  # No source text — the corpus-runner scenario.
        )
        # Must not raise; selected may be None (abstention) or some
        # human Q-id; both are valid outcomes for this scenario.
        candidates = resolver.resolve("Obama", lc)
        selected = resolver.select(candidates, lc)
        assert selected is None or selected.startswith("Q"), (
            f"resolver should return None or a Q-id, got {selected!r}"
        )

    def test_audit_log_captures_normalization_event(self, live_resolver):
        """Phase 10.5 instrumentation: an `entity_normalization` audit
        event fires per resolution that hits the normalizer, with the
        Stage 1/2 outcome fields populated."""
        from aedos.audit.log import query_events

        resolver, db = live_resolver
        lc = LocalContext(
            predicate="holds_role",
            slot_position="subject",
            expected_entity_types=["Q5"],
            source_text="Barack Obama said it was historic.",
            claim_id="test-audit-1",
        )
        resolver.resolve("Obama", lc)

        events = query_events(db, event_type="entity_normalization", limit=5)
        assert len(events) >= 1
        evt = events[0]
        assert evt["event_subject"] == "Obama"
        data = evt["event_data"]
        # Stage A must have run.
        assert data["stage_a_outcome"] in (
            "canonical_no_redirect",
            "clean_redirect",
            "disambiguation_page",
            "not_found",
            "api_error",
        )
        # D53: every successful flow records the Stage C invocation
        # flag (True when LLM ran, False when shortcut/skip).
        assert "stage_c_llm_invoked" in data
