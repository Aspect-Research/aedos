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
    Stage1Outcome,
    WikipediaNormalizer,
)
from aedos.utils.http_cache import CachingHTTPClient, LRUHTTPCache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_normalizer():
    db = open_memory_db()
    config = Config()
    cache = LRUHTTPCache()
    http_client = CachingHTTPClient(
        cache=cache, headers={"User-Agent": config.user_agent}
    )
    return (
        WikipediaNormalizer(http_cache=http_client, db=db, config=config),
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
        assert result.stage_1_outcome == OUTCOME_CANONICAL_NO_REDIRECT
        assert result.normalized_form == "Barack Obama"
        assert result.surface_form == "Barack Obama"
        assert result.stage_1_redirect_target is None
        assert result.stage_2_invoked is False


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
        assert result.stage_1_outcome == OUTCOME_CLEAN_REDIRECT
        assert result.normalized_form == "United States"
        assert result.surface_form == "USA"
        assert result.stage_1_redirect_target == "United States"

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
        assert result.stage_1_outcome == OUTCOME_CLEAN_REDIRECT
        assert result.normalized_form == "United States"


class TestStage1DisambiguationPage:
    """MediaWiki returned a disambiguation page. pageprops contains
    the 'disambiguation' key. Step 1's compose_result leaves the surface
    form unchanged (Stage 2 is a no-op at this commit)."""

    def test_basic_disambiguation(self):
        normalizer, _ = _make_normalizer()
        response = _api_response(
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
            MockClient.return_value.__enter__.return_value.get.return_value = response
            result = normalizer.normalize("Smith")
        assert result.stage_1_outcome == OUTCOME_DISAMBIGUATION_PAGE
        # Step 1 compose: stage 2 not yet wired, surface form preserved.
        assert result.normalized_form == "Smith"
        assert result.stage_1_redirect_target == "Smith"
        assert result.stage_2_invoked is False

    def test_redirect_then_disambiguation(self):
        """Redirect followed to a page that turns out to be a disambiguation
        page. Outcome is disambiguation_page; the redirect target is
        recorded as the disambig title."""
        normalizer, _ = _make_normalizer()
        response = _api_response(
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
            MockClient.return_value.__enter__.return_value.get.return_value = response
            result = normalizer.normalize("Obama")
        assert result.stage_1_outcome == OUTCOME_DISAMBIGUATION_PAGE
        assert result.stage_1_redirect_target == "Obama (disambiguation)"
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
        assert result.stage_1_outcome == OUTCOME_NOT_FOUND
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
        assert result.stage_1_outcome == OUTCOME_NOT_FOUND
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
        assert result.stage_1_outcome == OUTCOME_API_ERROR
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
        assert result.stage_1_outcome == OUTCOME_API_ERROR
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
        assert result.stage_1_outcome == OUTCOME_API_ERROR
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
        assert result.stage_1_outcome == OUTCOME_NOT_FOUND


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
        assert data["stage_1_outcome"] == OUTCOME_CLEAN_REDIRECT
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
        assert result.stage_1_outcome == OUTCOME_CANONICAL_NO_REDIRECT


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
