"""Phase H D53 step 1: live tests for `WikidataAdapter.wbsearchentities`.

Pins Wikidata's actual wbsearchentities behavior for the six Cluster 1
problem cases plus their canonicalized-form counterparts. The empirical
investigation found:

  - Bare "Obama" does NOT return Q76 in the top 20.
  - Canonicalized "Barack Obama" returns Q76 at rank 1.
  - Similar patterns for Einstein, President.
  - Apple, Amazon, Williams College: bare query returns canonical at top.

These tests don't enforce rank-1 strictness for the Obama-class cases
(Wikidata's search ranking is not a stable contract); they enforce that
the hybrid-canonicalized form returns the expected Q-id within the top
N results, where N is small.

Gated by RUN_LIVE_KB=1.
"""

from __future__ import annotations

import os

import pytest

from aedos.config import Config
from aedos.database import open_memory_db
from aedos.layer4_sources.kb_wikidata import WBSearchCandidate, WikidataAdapter
from aedos.utils.http_cache import CachingHTTPClient, LRUHTTPCache


_RUN_LIVE = os.environ.get("RUN_LIVE_KB") == "1"
pytestmark = pytest.mark.skipif(
    not _RUN_LIVE,
    reason="Live Wikidata tests require RUN_LIVE_KB=1",
)


@pytest.fixture
def live_adapter():
    db = open_memory_db()
    config = Config()
    cache = LRUHTTPCache(
        max_size=config.http_cache_lru_size,
        default_ttl_seconds=config.http_cache_entity_ttl_seconds,
    )
    http = CachingHTTPClient(
        cache=cache,
        default_ttl_seconds=config.http_cache_entity_ttl_seconds,
        headers={"User-Agent": config.user_agent},
    )
    return WikidataAdapter(http_cache=http, db=db, config=config)


def _rank_of(results: list[WBSearchCandidate], qid: str) -> int:
    """1-based rank of `qid` in `results`; 0 if not present."""
    for r in results:
        if r.qid == qid:
            return r.rank
    return 0


# ---------------------------------------------------------------------------
# Canonical-form queries (hybrid path) — these are what D53 actually relies on
# ---------------------------------------------------------------------------


class TestCanonicalFormQueries:
    """The hybrid D53 flow canonicalizes via Wikipedia redirect before
    querying wbsearchentities. These tests pin the canonicalized
    behavior for the six investigation cases. They're the path D53
    relies on; the bare-query tests below are documentation of the
    *limitation* the hybrid addresses."""

    def test_barack_obama_returns_q76(self, live_adapter):
        results = live_adapter.wbsearchentities("Barack Obama", limit=20)
        assert _rank_of(results, "Q76") == 1, (
            f"Expected Q76 (Barack Obama) at rank 1; got "
            f"{[(r.rank, r.qid, r.label) for r in results[:5]]}"
        )

    def test_apple_inc_returns_q312(self, live_adapter):
        results = live_adapter.wbsearchentities("Apple Inc.", limit=20)
        rank = _rank_of(results, "Q312")
        assert rank == 1, (
            f"Expected Q312 (Apple Inc.) at rank 1; got "
            f"{[(r.rank, r.qid, r.label) for r in results[:5]]}"
        )

    def test_albert_einstein_returns_q937(self, live_adapter):
        results = live_adapter.wbsearchentities("Albert Einstein", limit=20)
        assert _rank_of(results, "Q937") == 1

    def test_president_of_the_united_states_returns_q11696(self, live_adapter):
        results = live_adapter.wbsearchentities(
            "President of the United States", limit=20
        )
        assert _rank_of(results, "Q11696") == 1

    def test_williams_college_returns_q49166(self, live_adapter):
        results = live_adapter.wbsearchentities("Williams College", limit=20)
        assert _rank_of(results, "Q49166") == 1


# ---------------------------------------------------------------------------
# Bare-query behavior — documents the limitation the hybrid addresses
# ---------------------------------------------------------------------------


class TestBareQueryBehavior:
    """Documents what wbsearchentities returns for short ambiguous queries
    without Wikipedia canonicalization. The 'Obama' case is the smoking
    gun that motivated the hybrid design — Q76 is not in the top 20.

    These tests don't assert that Q76 is absent (that'd freeze a
    Wikidata behavior that might improve); they pin that the canonical
    is *not* at rank 1, which is the actual decision point for the
    hybrid path. If a future Wikidata change brings Q76 to rank 1,
    the test fails and we re-evaluate the hybrid's necessity."""

    def test_bare_apple_returns_q312_at_top(self, live_adapter):
        """Apple is the easy case — wbsearchentities directly puts
        Apple Inc. at rank 1 even for bare 'Apple'. The hybrid still
        runs through Wikipedia first (because canonical_no_redirect for
        'Apple' → 'Apple' the fruit's article), but this confirms
        wbsearchentities itself ranks the company first."""
        results = live_adapter.wbsearchentities("Apple", limit=20)
        assert _rank_of(results, "Q312") == 1

    def test_bare_obama_does_not_return_q76_at_rank_1(self, live_adapter):
        """The smoking-gun case. Bare 'Obama' returns Obama-the-town
        etc. before Barack Obama (whose label is 'Barack Obama' with
        'Obama' as an alias). The Wikipedia-redirect canonicalization
        step in the hybrid resolves this — Wikipedia redirects 'Obama'
        → 'Barack Obama', and a wbsearchentities query on
        'Barack Obama' returns Q76 at rank 1."""
        results = live_adapter.wbsearchentities("Obama", limit=20)
        # The hybrid path's reason for existing: assert canonical is
        # NOT at rank 1 here. (If Wikidata ranking improves and Q76
        # does land at rank 1, the assertion fails and we reconsider.)
        rank_q76 = _rank_of(results, "Q76")
        assert rank_q76 != 1, (
            f"Wikidata ranking has changed — Q76 is at rank 1 for bare 'Obama'. "
            f"The D53 hybrid path may no longer be necessary. Top 5: "
            f"{[(r.rank, r.qid, r.label) for r in results[:5]]}"
        )


# ---------------------------------------------------------------------------
# Sanity / shape
# ---------------------------------------------------------------------------


class TestCandidateShape:
    def test_each_candidate_has_required_fields(self, live_adapter):
        results = live_adapter.wbsearchentities("Albert Einstein", limit=5)
        assert results
        for r in results:
            assert r.qid.startswith("Q")
            assert isinstance(r.label, str) and r.label
            # description is Optional[str]; aliases is list (possibly empty)
            assert r.description is None or isinstance(r.description, str)
            assert isinstance(r.aliases, list)
            assert r.match_type in ("label", "alias", "")
            assert r.rank >= 1

    def test_limit_caps_result_count(self, live_adapter):
        results = live_adapter.wbsearchentities("President", limit=5)
        assert len(results) <= 5
