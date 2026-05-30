"""Phase H D47 step 1: tests for the Wikipedia normalizer's Stage 1 logic.

Covers the four Stage 1 outcomes (canonical_no_redirect, clean_redirect,
disambiguation_page, not_found) plus the api_error path. The httpx layer is
mocked — no live calls. Live tests for the actual MediaWiki API behavior
live in `tests/integration/live/test_wikipedia_normalizer_live.py`.

Step 2 of D47 adds Stage 2 (LLM-mediated selection) tests in this file;
step 1 only verifies that a disambiguation_page outcome leaves the surface
form unchanged (Stage 2 is a no-op at step 1).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from aedos.audit.log import query_events
from aedos.config import Config
from aedos.database import open_memory_db
from aedos.layer1_extraction.wikipedia_normalizer import (
    OUTCOME_API_ERROR,
    OUTCOME_CANONICAL_NO_REDIRECT,
    OUTCOME_CLEAN_REDIRECT,
    OUTCOME_DISAMBIGUATION_PAGE,
    OUTCOME_NOT_FOUND,
    OUTCOME_SKIPPED_KB_IDENTIFIER,
    Stage1Outcome,
    WikipediaNormalizer,
)
from aedos.utils.http_cache import CachingHTTPClient, LRUHTTPCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeKBAdapter:
    """Phase H D53: stub KB adapter for normalizer unit tests.

    Returns predetermined wbsearchentities candidates and P31 type maps.
    The normalizer's Stage B calls `wbsearchentities()`; Stage C may call
    `_fetch_p31_for_candidates()` for the D33 type filter.
    """

    def __init__(self, candidates=None, p31_by_qid=None):
        self._candidates = list(candidates or [])
        self._p31 = dict(p31_by_qid or {})
        self.wbsearch_calls: list[tuple[str, int]] = []  # (query, limit)
        self.p31_calls: list[list[str]] = []

    def wbsearchentities(self, query, limit=None):
        self.wbsearch_calls.append((query, limit))
        return list(self._candidates)

    def _fetch_p31_for_candidates(self, qids):
        self.p31_calls.append(list(qids))
        return ({q: list(self._p31.get(q, [])) for q in qids}, None)


def _wb_candidate(qid, label, description=None, aliases=None, rank=1, match_type="label"):
    """Build a `WBSearchCandidate` for tests without importing it inline."""
    from aedos.layer4_sources.kb_wikidata import WBSearchCandidate
    return WBSearchCandidate(
        qid=qid, label=label, description=description,
        aliases=list(aliases or []), match_type=match_type, rank=rank,
    )


def _make_normalizer(kb_candidates=None, p31_by_qid=None):
    db = open_memory_db()
    config = Config()
    cache = LRUHTTPCache()
    http_client = CachingHTTPClient(
        cache=cache, headers={"User-Agent": config.user_agent}
    )
    kb = _FakeKBAdapter(candidates=kb_candidates, p31_by_qid=p31_by_qid)
    return (
        WikipediaNormalizer(
            http_cache=http_client, db=db, config=config, kb_adapter=kb
        ),
        db,
    )


def _make_response(body: bytes, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = body
    resp.headers = {}
    resp.raise_for_status = MagicMock()
    return resp


def _api_response(query_dict: dict) -> MagicMock:
    """Build a MediaWiki query response with the supplied `query` block."""
    body = json.dumps({"batchcomplete": True, "query": query_dict}).encode()
    return _make_response(body)


# ---------------------------------------------------------------------------
# Stage 1 outcome tests (single-title path)
# ---------------------------------------------------------------------------


class TestStage1CanonicalNoRedirect:
    """The title is itself a valid Wikipedia article. No redirect follows
    and pageprops doesn't flag disambiguation."""

    def test_basic_canonical(self):
        normalizer, _ = _make_normalizer()
        response = _api_response(
            {
                "pages": [
                    {
                        "title": "Barack Obama",
                        "pageid": 534366,
                        "pageprops": {"wikibase_item": "Q76"},
                    }
                ]
            }
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = response
            result = normalizer.normalize("Barack Obama")
        assert result.stage_a_outcome == OUTCOME_CANONICAL_NO_REDIRECT
        assert result.normalized_form == "Barack Obama"
        assert result.surface_form == "Barack Obama"
        assert result.stage_a_redirect_target is None
        assert result.stage_c_llm_invoked is False


class TestStage1CleanRedirect:
    """MediaWiki followed a redirect to a single canonical article."""

    def test_basic_redirect(self):
        normalizer, _ = _make_normalizer()
        # The classic test pattern: input "United States" → canonical
        # "United States" already, but model a redirect with input
        # "USA" → "United States".
        response = _api_response(
            {
                "redirects": [{"from": "USA", "to": "United States"}],
                "pages": [
                    {
                        "title": "United States",
                        "pageid": 3434750,
                        "pageprops": {"wikibase_item": "Q30"},
                    }
                ],
            }
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = response
            result = normalizer.normalize("USA")
        assert result.stage_a_outcome == OUTCOME_CLEAN_REDIRECT
        assert result.normalized_form == "United States"
        assert result.surface_form == "USA"
        assert result.stage_a_redirect_target == "United States"

    def test_redirect_with_normalize(self):
        """MediaWiki may also normalize the input title (case fixes etc.)
        before processing — verify the parse handles both normalize +
        redirect chains in one response."""
        normalizer, _ = _make_normalizer()
        response = _api_response(
            {
                "normalized": [{"from": "usa", "to": "Usa"}],
                "redirects": [{"from": "Usa", "to": "United States"}],
                "pages": [
                    {
                        "title": "United States",
                        "pageid": 3434750,
                        "pageprops": {"wikibase_item": "Q30"},
                    }
                ],
            }
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = response
            result = normalizer.normalize("usa")
        assert result.stage_a_outcome == OUTCOME_CLEAN_REDIRECT
        assert result.normalized_form == "United States"


class TestStage1DisambiguationPage:
    """MediaWiki returned a disambiguation page. pageprops contains
    the 'disambiguation' key. D53 step 2: the disambig outcome drives
    Stage B (wbsearchentities with the surface form) — no more scraping
    the disambig page's link list. These tests pin Stage A's detection
    behavior; Stage B+C tests live in TestStageBQuery / TestStageCSelection."""

    def test_basic_disambiguation_with_empty_kb_abstains(self):
        """Stage A returns disambig. Stage B's fake kb_adapter returns
        no candidates → flow abstains with `no_stage_b_candidates`."""
        normalizer, _ = _make_normalizer()  # default kb returns []
        stage_1_response = _api_response(
            {
                "pages": [
                    {
                        "title": "Smith",
                        "pageid": 12345,
                        "pageprops": {"disambiguation": ""},
                    }
                ]
            }
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = stage_1_response
            result = normalizer.normalize("Smith")
        assert result.stage_a_outcome == OUTCOME_DISAMBIGUATION_PAGE
        # Stage B's surface-form query produced no candidates.
        assert result.normalized_form == "Smith"
        assert result.error == "no_stage_b_candidates"
        assert result.stage_c_llm_invoked is False

    def test_redirect_then_disambiguation(self):
        """Redirect followed to a page that turns out to be a disambig
        page. Outcome is disambiguation_page; redirect target recorded
        as the disambig title."""
        normalizer, _ = _make_normalizer()
        stage_1_response = _api_response(
            {
                "redirects": [{"from": "Obama", "to": "Obama (disambiguation)"}],
                "pages": [
                    {
                        "title": "Obama (disambiguation)",
                        "pageid": 12345,
                        "pageprops": {"disambiguation": ""},
                    }
                ],
            }
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = stage_1_response
            result = normalizer.normalize("Obama")
        assert result.stage_a_outcome == OUTCOME_DISAMBIGUATION_PAGE
        assert result.stage_a_redirect_target == "Obama (disambiguation)"
        # Surface form preserved when Stage B has nothing.
        assert result.normalized_form == "Obama"


class TestStage1NotFound:
    """No Wikipedia article exists at this title."""

    def test_basic_missing(self):
        normalizer, _ = _make_normalizer()
        response = _api_response(
            {
                "pages": [
                    {
                        "title": "ThisIsNotARealWikipediaArticleTitle12345",
                        "missing": True,
                    }
                ]
            }
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = response
            result = normalizer.normalize("ThisIsNotARealWikipediaArticleTitle12345")
        assert result.stage_a_outcome == OUTCOME_NOT_FOUND
        assert result.normalized_form == "ThisIsNotARealWikipediaArticleTitle12345"

    def test_empty_title_returns_not_found(self):
        """Empty / whitespace titles short-circuit to not_found without
        hitting the API — querying empty strings against MediaWiki
        returns garbage."""
        normalizer, _ = _make_normalizer()
        with patch("httpx.Client") as MockClient:
            result = normalizer.normalize("")
            # No HTTP call should have been made.
            MockClient.return_value.__enter__.return_value.get.assert_not_called()
        assert result.stage_a_outcome == OUTCOME_NOT_FOUND
        assert result.normalized_form == ""


class TestStage1APIError:
    """Transient HTTP failure: retry once, then surface as api_error."""

    def test_network_error(self):
        normalizer, _ = _make_normalizer()
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = (
                httpx.NetworkError("connection refused")
            )
            result = normalizer.normalize("Smith")
        assert result.stage_a_outcome == OUTCOME_API_ERROR
        # Surface form preserved on api_error.
        assert result.normalized_form == "Smith"
        assert result.error is not None
        assert "NetworkError" in result.error

    def test_timeout(self):
        normalizer, _ = _make_normalizer()
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = (
                httpx.TimeoutException("read timeout")
            )
            result = normalizer.normalize("Smith")
        assert result.stage_a_outcome == OUTCOME_API_ERROR
        assert result.normalized_form == "Smith"

    def test_malformed_json(self):
        """Non-dict response body → api_error rather than crashing."""
        normalizer, _ = _make_normalizer()
        # A bare-string response decodes to a string, not a dict — the
        # parser must surface this as api_error.
        response = _make_response(json.dumps("not_a_dict").encode())
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = response
            result = normalizer.normalize("Smith")
        assert result.stage_a_outcome == OUTCOME_API_ERROR
        assert result.normalized_form == "Smith"

    def test_no_query_block(self):
        """Response has no `query` block at all → every title is not_found."""
        normalizer, _ = _make_normalizer()
        response = _make_response(json.dumps({"batchcomplete": True}).encode())
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = response
            result = normalizer.normalize("Smith")
        # No page in the response → not_found (the parse helper treats
        # missing pages defensively as not_found, not api_error).
        assert result.stage_a_outcome == OUTCOME_NOT_FOUND


# ---------------------------------------------------------------------------
# Batched query
# ---------------------------------------------------------------------------


class TestNormalizeBatch:
    def test_batch_returns_per_reference_outcomes(self):
        """One API call, three titles, three different outcomes."""
        normalizer, _ = _make_normalizer()
        response = _api_response(
            {
                "redirects": [{"from": "USA", "to": "United States"}],
                "pages": [
                    {
                        "title": "Barack Obama",
                        "pageid": 1,
                        "pageprops": {"wikibase_item": "Q76"},
                    },
                    {
                        "title": "United States",
                        "pageid": 2,
                        "pageprops": {"wikibase_item": "Q30"},
                    },
                    {
                        "title": "ThisIsMissing",
                        "missing": True,
                    },
                ],
            }
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = response
            outcomes = normalizer.normalize_batch(
                ["Barack Obama", "USA", "ThisIsMissing"]
            )

        assert outcomes["Barack Obama"].outcome == OUTCOME_CANONICAL_NO_REDIRECT
        assert outcomes["Barack Obama"].canonical_title == "Barack Obama"
        assert outcomes["USA"].outcome == OUTCOME_CLEAN_REDIRECT
        assert outcomes["USA"].canonical_title == "United States"
        assert outcomes["ThisIsMissing"].outcome == OUTCOME_NOT_FOUND

    def test_batch_dedupes_input(self):
        """Duplicate inputs collapse to one query and still produce one
        result per input."""
        normalizer, _ = _make_normalizer()
        response = _api_response(
            {
                "pages": [
                    {
                        "title": "Smith",
                        "pageid": 1,
                        "pageprops": {"disambiguation": ""},
                    }
                ]
            }
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = response
            outcomes = normalizer.normalize_batch(["Smith", "Smith"])
        assert len(outcomes) == 1
        assert outcomes["Smith"].outcome == OUTCOME_DISAMBIGUATION_PAGE

    def test_batch_empty_input(self):
        normalizer, _ = _make_normalizer()
        outcomes = normalizer.normalize_batch([])
        assert outcomes == {}

    def test_batch_splits_above_50(self):
        """MediaWiki accepts up to 50 titles per query; verify the batcher
        issues two calls for 60 inputs."""
        normalizer, _ = _make_normalizer()
        # Sixty distinct titles.
        titles = [f"Title{i}" for i in range(60)]
        # Each batch's response: a page-per-title with canonical_no_redirect.
        response_batch_1 = _api_response(
            {"pages": [{"title": t, "pageid": i, "pageprops": {}} for i, t in enumerate(titles[:50])]}
        )
        response_batch_2 = _api_response(
            {"pages": [{"title": t, "pageid": 100 + i, "pageprops": {}} for i, t in enumerate(titles[50:])]}
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = [
                response_batch_1,
                response_batch_2,
            ]
            outcomes = normalizer.normalize_batch(titles)
            # Verify two calls were made.
            assert MockClient.return_value.__enter__.return_value.get.call_count == 2

        assert len(outcomes) == 60
        # All canonical_no_redirect (no redirect block in either response).
        for t in titles:
            assert outcomes[t].outcome == OUTCOME_CANONICAL_NO_REDIRECT


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


class TestAuditLogging:
    def test_normalize_emits_audit_event(self):
        normalizer, db = _make_normalizer()
        response = _api_response(
            {
                "redirects": [{"from": "USA", "to": "United States"}],
                "pages": [
                    {
                        "title": "United States",
                        "pageid": 1,
                        "pageprops": {"wikibase_item": "Q30"},
                    }
                ],
            }
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = response
            normalizer.normalize(
                "USA",
                claim_subject="USA",
                claim_predicate="member_of",
                claim_object="NATO",
                source_text="The USA is a member of NATO.",
                slot_position="subject",
                claim_id="test-claim-1",
            )
        events = query_events(db, event_type="entity_normalization", limit=5)
        assert len(events) == 1
        evt = events[0]
        assert evt["event_subject"] == "USA"
        data = evt["event_data"]
        assert data["surface_form"] == "USA"
        assert data["normalized_form"] == "United States"
        assert data["stage_a_outcome"] == OUTCOME_CLEAN_REDIRECT
        assert data["claim_id"] == "test-claim-1"
        assert data["slot_position"] == "subject"
        assert data["source_text_present"] is True

    def test_audit_logging_failure_does_not_break_normalize(self):
        """If the audit log write raises, the normalization result is
        still returned correctly."""
        normalizer, db = _make_normalizer()
        # Close the DB so a write raises.
        db.close()
        response = _api_response(
            {
                "pages": [
                    {
                        "title": "Barack Obama",
                        "pageid": 1,
                        "pageprops": {"wikibase_item": "Q76"},
                    }
                ]
            }
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = response
            # Should not raise.
            result = normalizer.normalize("Barack Obama")
        assert result.stage_a_outcome == OUTCOME_CANONICAL_NO_REDIRECT


# ---------------------------------------------------------------------------
# Wiring-gap defence
# ---------------------------------------------------------------------------


class TestWiringGapDefence:
    def test_no_http_cache_raises_on_query(self):
        """A normalizer constructed without an http_cache must raise
        when asked to normalize, not silently return garbage."""
        normalizer = WikipediaNormalizer(http_cache=None, db=None, config=Config())
        with pytest.raises(RuntimeError, match="requires an http_cache"):
            normalizer.normalize("Barack Obama")




# ---------------------------------------------------------------------------
# Phase H Cluster 1 step 1 — Q-id short-circuit + per-instance memo
# ---------------------------------------------------------------------------


class _StubLLM:
    """Stub LLM that returns a pre-canned `extract_with_tool` response.

    Records the most recent call's user_message + system + tool for
    assertions about prompt construction."""

    def __init__(self, response=None):
        self._response = response
        self.last_system = None
        self.last_user_message = None
        self.last_tool = None
        self.last_purpose = None
        self.call_count = 0

    def extract_with_tool(self, system, user_message, tool, purpose=None, **kwargs):
        self.call_count += 1
        self.last_system = system
        self.last_user_message = user_message
        self.last_tool = tool
        self.last_purpose = purpose
        if isinstance(self._response, Exception):
            raise self._response
        return self._response or {}


def _make_normalizer_with_llm(llm_response, kb_candidates=None, p31_by_qid=None):
    db = open_memory_db()
    config = Config()
    cache = LRUHTTPCache()
    http_client = CachingHTTPClient(
        cache=cache, headers={"User-Agent": config.user_agent}
    )
    stub = _StubLLM(llm_response)
    kb = _FakeKBAdapter(candidates=kb_candidates, p31_by_qid=p31_by_qid)
    normalizer = WikipediaNormalizer(
        http_cache=http_client, llm_client=stub, db=db, config=config,
        kb_adapter=kb,
    )
    return normalizer, db, stub, kb


def _stage_a_canonical_response(title: str):
    """A Stage A response indicating the surface form is itself the
    canonical Wikipedia article title (canonical_no_redirect)."""
    return _api_response(
        {
            "pages": [
                {
                    "title": title,
                    "pageid": 12345,
                    "pageprops": {"wikibase_item": "Q1"},
                }
            ]
        }
    )


class TestQIdShortCircuit:
    """Wikidata Q-id surface forms bypass Stage A/B/C entirely. The
    walker's D5 KB neighbor enumeration substitutes Q-ids into claims
    that then re-enter the resolver; sending those through the normalizer
    is wasted cost — they're already canonical KB identifiers."""

    def test_qid_surface_form_skips_stages(self):
        normalizer, _ = _make_normalizer()
        with patch("httpx.Client") as MockClient:
            result = normalizer.normalize("Q937")
            MockClient.return_value.__enter__.return_value.get.assert_not_called()
        assert result.stage_a_outcome == OUTCOME_SKIPPED_KB_IDENTIFIER
        assert result.normalized_form == "Q937"
        assert result.selected_qid == "Q937"
        assert result.stage_c_llm_invoked is False

    def test_qid_surface_form_logs_audit_event(self):
        normalizer, db = _make_normalizer()
        with patch("httpx.Client"):
            normalizer.normalize("Q5", claim_predicate="is_a")
        events = query_events(db, event_type="entity_normalization", limit=5)
        assert len(events) == 1
        data = events[0]["event_data"]
        assert data["stage_a_outcome"] == OUTCOME_SKIPPED_KB_IDENTIFIER
        assert data["normalized_form"] == "Q5"
        assert data["selected_qid"] == "Q5"

    def test_non_qid_falls_through(self):
        """'Q5x' (Q followed by non-digit) is NOT a Q-id — must hit Stage A."""
        normalizer, _ = _make_normalizer()
        response = _api_response(
            {"pages": [{"title": "Q5x", "missing": True}]}
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = response
            result = normalizer.normalize("Q5x")
        assert result.stage_a_outcome == OUTCOME_NOT_FOUND


# ---------------------------------------------------------------------------
# Phase H D53 step 2 — Stage B (wbsearchentities) + Stage C (filter/heuristic/LLM)
# ---------------------------------------------------------------------------


class TestStageBQuery:
    """Stage B's wbsearchentities query string depends on Stage A's outcome."""

    def test_clean_redirect_uses_redirect_target(self):
        cand = _wb_candidate("Q76", "Barack Obama", "44th US president")
        normalizer, _, _, kb = _make_normalizer_with_llm(
            None, kb_candidates=[cand]
        )
        response = _api_response(
            {
                "redirects": [{"from": "Obama", "to": "Barack Obama"}],
                "pages": [
                    {
                        "title": "Barack Obama", "pageid": 1,
                        "pageprops": {"wikibase_item": "Q76"},
                    }
                ],
            }
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = response
            result = normalizer.normalize("Obama", source_text="Obama signed it")
        assert result.stage_a_outcome == OUTCOME_CLEAN_REDIRECT
        assert result.stage_b_query == "Barack Obama"
        assert result.stage_c_shortcut_fired is True
        assert result.selected_qid == "Q76"
        assert result.normalized_form == "Barack Obama"
        assert kb.wbsearch_calls and kb.wbsearch_calls[0][0] == "Barack Obama"

    def test_canonical_no_redirect_uses_canonical_title(self):
        cand = _wb_candidate("Q312", "Apple Inc.", "American tech company")
        normalizer, _, _, kb = _make_normalizer_with_llm(
            None, kb_candidates=[cand]
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = (
                _stage_a_canonical_response("Apple")
            )
            result = normalizer.normalize(
                "Apple", source_text="Apple was founded in California"
            )
        assert result.stage_a_outcome == OUTCOME_CANONICAL_NO_REDIRECT
        assert result.stage_b_query == "Apple"
        assert result.selected_qid == "Q312"

    def test_disambiguation_page_uses_surface_form_for_stage_b(self):
        cands = [
            _wb_candidate("Q11696", "President of the United States", rank=1),
            _wb_candidate("Q30461", "president", rank=2),
        ]
        normalizer, _, llm, kb = _make_normalizer_with_llm(
            {"selection": "Q11696", "reasoning": "Obama context indicates US president"},
            kb_candidates=cands,
        )
        resp = _api_response(
            {
                "pages": [
                    {
                        "title": "President", "pageid": 1,
                        "pageprops": {"disambiguation": ""},
                    }
                ]
            }
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = resp
            result = normalizer.normalize(
                "President",
                claim_subject="Obama", claim_predicate="holds_role",
                source_text="Obama holds_role President",
            )
        assert result.stage_a_outcome == OUTCOME_DISAMBIGUATION_PAGE
        assert result.stage_b_query == "President"
        assert result.stage_c_llm_invoked is True
        assert result.selected_qid == "Q11696"
        assert kb.wbsearch_calls[0][0] == "President"

    def test_not_found_still_runs_stage_b(self):
        cand = _wb_candidate("Q1", "Some entity")
        normalizer, _, _, kb = _make_normalizer_with_llm(None, kb_candidates=[cand])
        resp = _api_response(
            {"pages": [{"title": "Obscure", "missing": True}]}
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = resp
            result = normalizer.normalize("Obscure", source_text="ctx")
        assert result.stage_a_outcome == OUTCOME_NOT_FOUND
        assert result.stage_b_query == "Obscure"
        assert result.selected_qid == "Q1"

    def test_api_error_short_circuits_skips_stage_b(self):
        normalizer, _, _, kb = _make_normalizer_with_llm(None)
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = (
                httpx.NetworkError("down")
            )
            result = normalizer.normalize("Anything", source_text="ctx")
        assert result.stage_a_outcome == OUTCOME_API_ERROR
        assert kb.wbsearch_calls == []
        assert result.selected_qid is None

    def test_stage_b_no_candidates_abstains(self):
        normalizer, _, _, kb = _make_normalizer_with_llm(None, kb_candidates=[])
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = (
                _stage_a_canonical_response("Foo")
            )
            result = normalizer.normalize("Foo", source_text="ctx")
        assert result.stage_b_candidate_count == 0
        assert result.error == "no_stage_b_candidates"
        assert result.selected_qid is None


class TestStageCSelection:
    """Stage C: type filter (D33) + single-candidate shortcut + LLM."""

    def test_single_candidate_shortcut_skips_llm(self):
        cand = _wb_candidate("Q76", "Barack Obama")
        normalizer, _, llm, _ = _make_normalizer_with_llm(
            None, kb_candidates=[cand]
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = (
                _stage_a_canonical_response("Barack Obama")
            )
            result = normalizer.normalize("Barack Obama", source_text="ctx")
        assert result.stage_c_shortcut_fired is True
        assert result.stage_c_llm_invoked is False
        assert result.selected_qid == "Q76"
        assert llm.call_count == 0

    def test_llm_picks_qid_from_multi_candidate(self):
        cands = [
            _wb_candidate("Q1", "first"),
            _wb_candidate("Q2", "second"),
            _wb_candidate("Q3", "third"),
        ]
        normalizer, _, llm, _ = _make_normalizer_with_llm(
            {"selection": "Q2", "reasoning": "context matches second"},
            kb_candidates=cands,
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = (
                _stage_a_canonical_response("ambig")
            )
            result = normalizer.normalize("ambig", source_text="ctx supports second")
        assert result.stage_c_llm_invoked is True
        assert result.selected_qid == "Q2"
        assert result.stage_c_reasoning == "context matches second"
        assert "Q1" in llm.last_user_message
        assert "Q2" in llm.last_user_message

    def test_llm_abstain_preserves_fallback_label(self):
        cands = [_wb_candidate("Q1", "a"), _wb_candidate("Q2", "b")]
        normalizer, _, _, _ = _make_normalizer_with_llm(
            {"selection": "ABSTAIN", "reasoning": "context ambiguous"},
            kb_candidates=cands,
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = (
                _stage_a_canonical_response("ambig")
            )
            result = normalizer.normalize("ambig", source_text="t")
        assert result.selected_qid is None
        assert result.stage_c_selection is None
        assert result.normalized_form == "ambig"

    def test_hallucinated_qid_treated_as_abstention(self):
        """Defence-in-depth: an LLM Q-id not in the candidate set is
        treated as abstention."""
        cands = [_wb_candidate("Q1", "a"), _wb_candidate("Q2", "b")]
        normalizer, _, _, _ = _make_normalizer_with_llm(
            {"selection": "Q999999", "reasoning": "made up"},
            kb_candidates=cands,
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = (
                _stage_a_canonical_response("ambig")
            )
            result = normalizer.normalize("ambig", source_text="t")
        assert result.selected_qid is None
        assert "selection_not_in_candidates" in (result.error or "")

    def test_type_filter_keeps_matching_candidates(self):
        cands = [
            _wb_candidate("Q76", "Barack Obama"),
            _wb_candidate("Q41773", "Obama, Japan"),
        ]
        p31 = {"Q76": ["Q5"], "Q41773": ["Q515"]}
        normalizer, _, _, kb = _make_normalizer_with_llm(
            None, kb_candidates=cands, p31_by_qid=p31,
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = (
                _stage_a_canonical_response("Obama")
            )
            result = normalizer.normalize(
                "Obama", source_text="ctx",
                expected_entity_types=["Q5"],
            )
        assert result.stage_c_type_filter_applied is True
        assert result.stage_c_filtered_count == 1
        assert result.stage_c_shortcut_fired is True
        assert result.selected_qid == "Q76"

    def test_type_filter_fail_open_when_all_eliminated(self):
        """D33's fail-open: if the filter removes every candidate, pass
        the unfiltered list to the LLM rather than abstain silently."""
        cands = [_wb_candidate("Q41773", "Obama, Japan")]
        p31 = {"Q41773": ["Q515"]}
        normalizer, _, _, _ = _make_normalizer_with_llm(
            None, kb_candidates=cands, p31_by_qid=p31,
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = (
                _stage_a_canonical_response("Obama")
            )
            result = normalizer.normalize(
                "Obama", source_text="ctx", expected_entity_types=["Q5"],
            )
        assert result.stage_c_type_filter_applied is True
        # With one candidate (after fail-open fallback), shortcut fires.
        assert result.selected_qid == "Q41773"


class TestNormalizeMemo:
    """The per-instance memo collapses repeat normalize() calls with
    identical inputs into one Stage A/B/C run."""

    def test_repeat_call_serves_from_memo(self):
        cands = [_wb_candidate("Q1", "a"), _wb_candidate("Q2", "b")]
        normalizer, _, llm, kb = _make_normalizer_with_llm(
            {"selection": "Q1", "reasoning": "matches"},
            kb_candidates=cands,
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = (
                _stage_a_canonical_response("foo")
            )
            first = normalizer.normalize("foo", source_text="ctx", slot_position="subject")
            second = normalizer.normalize("foo", source_text="ctx", slot_position="subject")
        assert first.selected_qid == "Q1"
        assert second.selected_qid == "Q1"
        assert first.from_memo is False
        assert second.from_memo is True
        assert llm.call_count == 1
        assert len(kb.wbsearch_calls) == 1

    def test_memo_keyed_on_context(self):
        cands = [_wb_candidate("Q1", "a"), _wb_candidate("Q2", "b")]
        normalizer, _, llm, kb = _make_normalizer_with_llm(
            {"selection": "Q1", "reasoning": "matches"},
            kb_candidates=cands,
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = (
                _stage_a_canonical_response("foo")
            )
            normalizer.normalize("foo", source_text="text A")
            normalizer.normalize("foo", source_text="text B")
        assert llm.call_count == 2

    def test_memo_keyed_on_expected_entity_types(self):
        cands = [_wb_candidate("Q1", "a"), _wb_candidate("Q2", "b")]
        normalizer, _, llm, _ = _make_normalizer_with_llm(
            {"selection": "Q1", "reasoning": "matches"},
            kb_candidates=cands,
            p31_by_qid={"Q1": ["Q5"], "Q2": ["Q5"]},
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = (
                _stage_a_canonical_response("foo")
            )
            normalizer.normalize("foo", source_text="ctx", expected_entity_types=["Q5"])
            normalizer.normalize("foo", source_text="ctx", expected_entity_types=["Q515"])
        assert llm.call_count == 2

    def test_memo_hit_logs_audit_event(self):
        cands = [_wb_candidate("Q1", "a")]
        normalizer, db, _, _ = _make_normalizer_with_llm(
            None, kb_candidates=cands,
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = (
                _stage_a_canonical_response("foo")
            )
            normalizer.normalize("foo", source_text="t")
            normalizer.normalize("foo", source_text="t")
        events = query_events(db, event_type="entity_normalization", limit=10)
        assert len(events) == 2
        assert events[0]["event_data"]["from_memo"] is True
        assert events[1]["event_data"]["from_memo"] is False


def _stage_a_canonical_with_qid(title: str, qid: str):
    """Stage A canonical_no_redirect response whose pageprops carry a chosen
    wikibase_item Q-id (vs the placeholder Q1 in _stage_a_canonical_response)."""
    return _api_response(
        {"pages": [{"title": title, "pageid": 999, "pageprops": {"wikibase_item": qid}}]}
    )


class TestCanonicalArticleRescue:
    """S1a: the general replacement for the hand-curated _KNOWN_ROLE_TITLES
    map. When wbsearchentities misses a title's canonical Q-id and Stage C
    abstains, the Stage 1 pageprops wikibase_item rescues the resolution —
    type-gated, and only when the Q-id was not a candidate Stage C declined."""

    def test_rescue_when_wbsearch_misses_canonical_qid(self):
        cands = [_wb_candidate("Q1", "a"), _wb_candidate("Q2", "b")]
        normalizer, _, _, _ = _make_normalizer_with_llm(
            {"selection": "ABSTAIN", "reasoning": "ambiguous"}, kb_candidates=cands,
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = (
                _stage_a_canonical_with_qid("President of the United States", "Q11696")
            )
            result = normalizer.normalize(
                "President of the United States", source_text="t"
            )
        assert result.selected_qid == "Q11696"

    def test_no_rescue_when_qid_already_a_candidate(self):
        # Stage C abstained among candidates that INCLUDE the canonical Q-id —
        # a deliberate ambiguity abstention, not a wbsearch miss. No override.
        cands = [_wb_candidate("Q11696", "a"), _wb_candidate("Q2", "b")]
        normalizer, _, _, _ = _make_normalizer_with_llm(
            {"selection": "ABSTAIN", "reasoning": "ambiguous"}, kb_candidates=cands,
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = (
                _stage_a_canonical_with_qid("X", "Q11696")
            )
            result = normalizer.normalize("X", source_text="t")
        assert result.selected_qid is None

    def test_rescue_blocked_by_type_mismatch(self):
        cands = [_wb_candidate("Q1", "a"), _wb_candidate("Q2", "b")]
        normalizer, _, _, _ = _make_normalizer_with_llm(
            {"selection": "ABSTAIN", "reasoning": "ambiguous"},
            kb_candidates=cands, p31_by_qid={"Q11696": ["Q5"]},
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = (
                _stage_a_canonical_with_qid("Title", "Q11696")
            )
            result = normalizer.normalize(
                "Title", source_text="t", expected_entity_types=["Q4164871"],
            )
        assert result.selected_qid is None

    def test_rescue_passes_matching_type(self):
        cands = [_wb_candidate("Q1", "a"), _wb_candidate("Q2", "b")]
        normalizer, _, _, _ = _make_normalizer_with_llm(
            {"selection": "ABSTAIN", "reasoning": "ambiguous"},
            kb_candidates=cands, p31_by_qid={"Q11696": ["Q4164871"]},
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = (
                _stage_a_canonical_with_qid("President of the United States", "Q11696")
            )
            result = normalizer.normalize(
                "President of the United States", source_text="t",
                expected_entity_types=["Q4164871"],
            )
        assert result.selected_qid == "Q11696"
