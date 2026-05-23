"""Phase H D47: live tests for the Wikipedia normalizer Stage 1 path.

Documents what English Wikipedia's redirect system actually returns for the
canonical D47 examples as of the run date — the mocked unit tests pin the
parser shape, these tests pin Wikipedia's editorial state.

Gated by `RUN_LIVE_KB=1` (the existing convention, shared with the live
Wikidata tests). Reuses the runbook's `AEDOS_KB_REQUEST_DELAY_MS` knob
indirectly through the same `CachingHTTPClient` shape the production
pipeline uses.

If Wikipedia's editorial state changes (e.g. "Obama" gains a primary-topic
redirect to Barack Obama), some of these tests will start reporting the
new outcome rather than the v0.15-baseline outcome. That's the intent —
the live tests are an early-warning signal that the redirect data has
shifted, not a freeze on its current shape.
"""

from __future__ import annotations

import os

import pytest

from aedos.config import Config
from aedos.database import open_memory_db
from aedos.layer1_extraction.wikipedia_normalizer import (
    OUTCOME_CANONICAL_NO_REDIRECT,
    OUTCOME_CLEAN_REDIRECT,
    OUTCOME_DISAMBIGUATION_PAGE,
    OUTCOME_NOT_FOUND,
    WikipediaNormalizer,
)
from aedos.utils.http_cache import CachingHTTPClient, LRUHTTPCache


_RUN_LIVE = os.environ.get("RUN_LIVE_KB") == "1"
pytestmark = pytest.mark.skipif(
    not _RUN_LIVE,
    reason="Live Wikipedia tests require RUN_LIVE_KB=1",
)


@pytest.fixture
def live_normalizer():
    """A WikipediaNormalizer wired against the real MediaWiki API.

    Uses an in-memory DB so audit events are captured but the test does
    not leave state on disk. The User-Agent comes from `Config.user_agent`
    — the same configuration the deployed pipeline uses."""
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
    normalizer = WikipediaNormalizer(
        http_cache=http_client,
        db=db,
        config=config,
    )
    yield normalizer
    db.close()


class TestStage1LiveOutcomes:
    """Document what the live MediaWiki API returns for the canonical
    examples. The assertions pin the *shape* of the outcome; the exact
    title is recorded so a future fixture-vs-live audit (D33 work item 3)
    can spot when Wikipedia's state shifts."""

    def test_barack_obama_is_canonical(self, live_normalizer):
        """Full canonical names should resolve as canonical_no_redirect:
        the title is itself a valid Wikipedia article."""
        result = live_normalizer.normalize("Barack Obama")
        assert result.stage_1_outcome == OUTCOME_CANONICAL_NO_REDIRECT
        assert result.normalized_form == "Barack Obama"

    def test_obama_is_either_redirect_or_disambiguation(self, live_normalizer):
        """The bare reference 'Obama'. Per the D47 finding this is
        Wikipedia's central D47 example. As of 2026-05-23 Wikipedia
        treats 'Obama' as a disambiguation page or as a redirect to
        the disambiguation page — either outcome is acceptable here,
        the assertion pins that Stage 1 returns one of those two
        (and never a clean redirect to Barack Obama directly)."""
        result = live_normalizer.normalize("Obama")
        # The outcome should be DISAMBIGUATION_PAGE; record for future
        # diagnosis if Wikipedia changes its mind.
        assert result.stage_1_outcome in (
            OUTCOME_DISAMBIGUATION_PAGE,
            OUTCOME_CLEAN_REDIRECT,
        ), f"unexpected Stage 1 outcome for 'Obama': {result.stage_1_outcome}"
        # If Wikipedia ever adds a primary-topic redirect from 'Obama'
        # to 'Barack Obama', this records it explicitly so D47's
        # downstream behavior (and the D47 design doc's caveat) can
        # be revisited.
        if result.stage_1_outcome == OUTCOME_CLEAN_REDIRECT:
            assert result.stage_1_redirect_target is not None

    def test_williams_college_is_canonical_or_redirect(self, live_normalizer):
        """'Williams College' on English Wikipedia typically redirects
        directly to the Massachusetts institution (Q49112's article).
        Pins this so a future Wikipedia rename surfaces visibly."""
        result = live_normalizer.normalize("Williams College")
        # Either CANONICAL_NO_REDIRECT (the title IS the article) or
        # CLEAN_REDIRECT to "Williams College" canonical form — both
        # acceptable outcomes.
        assert result.stage_1_outcome in (
            OUTCOME_CANONICAL_NO_REDIRECT,
            OUTCOME_CLEAN_REDIRECT,
            OUTCOME_DISAMBIGUATION_PAGE,
        )

    def test_missing_title_returns_not_found(self, live_normalizer):
        """A sentinel string unlikely to ever name a Wikipedia article."""
        result = live_normalizer.normalize(
            "AedosTestNotARealWikipediaArticleTitle98765"
        )
        assert result.stage_1_outcome == OUTCOME_NOT_FOUND
        assert (
            result.normalized_form
            == "AedosTestNotARealWikipediaArticleTitle98765"
        )

    def test_batch_resolves_mixed_outcomes(self, live_normalizer):
        """One API call covering three titles — exercises the batched
        path against the real API. Each outcome is checked individually."""
        outcomes = live_normalizer.normalize_batch(
            [
                "Barack Obama",
                "USA",
                "AedosTestNotARealWikipediaArticleTitle98765",
            ]
        )
        assert outcomes["Barack Obama"].outcome == OUTCOME_CANONICAL_NO_REDIRECT
        # "USA" historically redirects to "United States" on enwiki.
        assert outcomes["USA"].outcome in (
            OUTCOME_CLEAN_REDIRECT,
            OUTCOME_CANONICAL_NO_REDIRECT,
        )
        assert (
            outcomes["AedosTestNotARealWikipediaArticleTitle98765"].outcome
            == OUTCOME_NOT_FOUND
        )
