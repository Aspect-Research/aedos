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
    the 'disambiguation' key. With Step 2 wired, the disambiguation
    outcome drives Stage 2; these tests cover the Stage 1 detection
    (without an LLM, Stage 2 abstains visibly via the no-LLM path)."""

    def test_basic_disambiguation_without_llm_abstains(self):
        """No LLM client wired → Stage 2 can't run → abstain with the
        surface form preserved and an error noting the wiring gap."""
        normalizer, _ = _make_normalizer()
        # Stage 1 returns disambiguation; Stage 2 tries to fetch
        # candidates and would invoke the LLM. Both calls go through
        # httpx — the second is the parse call. Mock both, then
        # confirm that without an LLM Stage 2 records the gap honestly.
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
        parse_response = _make_response(
            json.dumps(
                {
                    "parse": {
                        "title": "Smith",
                        "links": [
                            {"ns": 0, "title": "John Smith", "exists": True},
                        ],
                    }
                }
            ).encode()
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = [
                stage_1_response,
                parse_response,
            ]
            result = normalizer.normalize("Smith")
        assert result.stage_1_outcome == OUTCOME_DISAMBIGUATION_PAGE
        # Wiring-gap defence: no LLM → abstain, surface form preserved.
        assert result.normalized_form == "Smith"
        assert result.stage_2_invoked is False
        assert result.error == "no_llm_client_for_stage_2"

    def test_redirect_then_disambiguation(self):
        """Redirect followed to a page that turns out to be a disambiguation
        page. Outcome is disambiguation_page; the redirect target is
        recorded as the disambig title."""
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
        # Empty links payload — exercises the no-candidates abstention.
        parse_response = _make_response(
            json.dumps({"parse": {"title": "Obama (disambiguation)", "links": []}}).encode()
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = [
                stage_1_response,
                parse_response,
            ]
            result = normalizer.normalize("Obama")
        assert result.stage_1_outcome == OUTCOME_DISAMBIGUATION_PAGE
        assert result.stage_1_redirect_target == "Obama (disambiguation)"
        # No candidates → abstain on surface form.
        assert result.normalized_form == "Obama"
        assert result.error == "no_candidates_on_disambiguation_page"


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


# ---------------------------------------------------------------------------
# Stage 2 — LLM-mediated selection over disambiguation candidates (Step 2)
# ---------------------------------------------------------------------------


class _StubLLM:
    """Stub LLM that returns a pre-canned `extract_with_tool` response.

    Records the most recent call's user_message + system + tool for
    assertions about prompt construction."""

    def __init__(self, response: dict | Exception | None = None):
        self._response = response
        self.last_system: str | None = None
        self.last_user_message: str | None = None
        self.last_tool: dict | None = None
        self.last_purpose: str | None = None
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


def _disambig_stage_1_response(
    disambig_title: str = "Smith", input_title: str | None = None
):
    """Build a Stage 1 response indicating the input resolves to a
    disambiguation page. When `input_title` differs from `disambig_title`,
    a redirects block is included so the parser maps input → page
    correctly."""
    query: dict = {
        "pages": [
            {
                "title": disambig_title,
                "pageid": 12345,
                "pageprops": {"disambiguation": ""},
            }
        ]
    }
    if input_title is not None and input_title != disambig_title:
        query["redirects"] = [{"from": input_title, "to": disambig_title}]
    return _api_response(query)


def _disambig_parse_response(candidates: list[str], disambig_title: str = "Smith"):
    """Build a `action=parse` response containing the given candidates as
    namespace-0 article links."""
    links = [{"ns": 0, "title": c, "exists": True} for c in candidates]
    return _make_response(
        json.dumps(
            {
                "parse": {
                    "title": disambig_title,
                    "links": links,
                }
            }
        ).encode()
    )


def _make_normalizer_with_llm(llm_response):
    db = open_memory_db()
    config = Config()
    cache = LRUHTTPCache()
    http_client = CachingHTTPClient(
        cache=cache, headers={"User-Agent": config.user_agent}
    )
    stub = _StubLLM(llm_response)
    normalizer = WikipediaNormalizer(
        http_cache=http_client, llm_client=stub, db=db, config=config
    )
    return normalizer, db, stub


class TestStage2Selection:
    def test_clear_context_selects_candidate(self):
        """Bare 'Obama' + source text mentioning Barack signing a bill
        → LLM picks 'Barack Obama' from candidates."""
        normalizer, db, llm = _make_normalizer_with_llm(
            {"selection": "Barack Obama", "reasoning": "Source text mentions the President signing a bill."}
        )
        candidates = ["Barack Obama", "Michelle Obama", "Obama, Fukui"]
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = [
                _disambig_stage_1_response("Obama (disambiguation)", input_title="Obama"),
                _disambig_parse_response(candidates, "Obama (disambiguation)"),
            ]
            result = normalizer.normalize(
                "Obama",
                claim_subject="Obama",
                claim_predicate="signed",
                claim_object="the bill",
                source_text="Barack Obama signed the bill. Obama said it was historic.",
                slot_position="subject",
                claim_id="test-1",
            )

        assert result.stage_1_outcome == OUTCOME_DISAMBIGUATION_PAGE
        assert result.stage_2_invoked is True
        assert result.stage_2_candidates == candidates
        assert result.stage_2_selection == "Barack Obama"
        assert result.normalized_form == "Barack Obama"
        assert result.error is None

        # The prompt should include the source text and candidates.
        assert "Barack Obama signed the bill" in llm.last_user_message
        for c in candidates:
            assert c in llm.last_user_message

    def test_abstain_when_no_context(self):
        """'Smith proved the theorem' with no further context — the LLM
        emits ABSTAIN; the surface form is preserved unchanged."""
        normalizer, _, _ = _make_normalizer_with_llm(
            {"selection": "ABSTAIN", "reasoning": "Surrounding text gives no clue which Smith."}
        )
        candidates = ["Adam Smith", "Will Smith", "John Smith"]
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = [
                _disambig_stage_1_response("Smith"),
                _disambig_parse_response(candidates, "Smith"),
            ]
            result = normalizer.normalize(
                "Smith",
                claim_subject="Smith",
                claim_predicate="proved",
                claim_object="the theorem",
                source_text="Smith proved the theorem.",
            )

        assert result.stage_1_outcome == OUTCOME_DISAMBIGUATION_PAGE
        assert result.stage_2_invoked is True
        assert result.stage_2_candidates == candidates
        assert result.stage_2_selection is None  # abstained
        assert result.normalized_form == "Smith"  # surface form preserved
        assert "no clue" in result.stage_2_reasoning.lower()

    def test_abstain_via_empty_string(self):
        """The model returns an empty selection string — treated as abstention."""
        normalizer, _, _ = _make_normalizer_with_llm(
            {"selection": "", "reasoning": "uncertain"}
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = [
                _disambig_stage_1_response("Smith"),
                _disambig_parse_response(["Adam Smith"], "Smith"),
            ]
            result = normalizer.normalize(
                "Smith", source_text="Smith said yes."
            )
        assert result.stage_2_invoked is True
        assert result.stage_2_selection is None
        assert result.normalized_form == "Smith"

    def test_selection_not_in_candidates_treated_as_abstention(self):
        """Defence-in-depth: a model that invents a title outside the
        candidate set is treated as abstention. A stray hallucination
        must not drive a wrong KB query downstream."""
        normalizer, _, _ = _make_normalizer_with_llm(
            {"selection": "Barack Hussein Obama", "reasoning": "guess"}
        )
        candidates = ["Barack Obama", "Michelle Obama"]
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = [
                _disambig_stage_1_response("Obama (disambiguation)", input_title="Obama"),
                _disambig_parse_response(candidates, "Obama (disambiguation)"),
            ]
            result = normalizer.normalize(
                "Obama", source_text="Obama signed the bill."
            )
        assert result.stage_2_invoked is True
        assert result.stage_2_selection is None
        assert result.normalized_form == "Obama"
        assert "selection_not_in_candidates" in (result.error or "")

    def test_llm_exception_abstains(self):
        """LLM call raises → recorded as abstention, no crash."""
        normalizer, _, _ = _make_normalizer_with_llm(RuntimeError("rate limited"))
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = [
                _disambig_stage_1_response("Smith"),
                _disambig_parse_response(["Adam Smith"], "Smith"),
            ]
            result = normalizer.normalize(
                "Smith", source_text="Smith proved it."
            )
        assert result.stage_2_invoked is True
        assert result.stage_2_selection is None
        assert result.normalized_form == "Smith"
        assert "RuntimeError" in (result.error or "")

    def test_candidate_fetch_failure_abstains(self):
        """The disambiguation page fetch fails → no candidates → abstain,
        no LLM call attempted."""
        normalizer, _, llm = _make_normalizer_with_llm(None)
        with patch("httpx.Client") as MockClient:
            stage_1 = _disambig_stage_1_response("Smith")
            # Second call (the parse) raises a network error.
            mock_get = MockClient.return_value.__enter__.return_value.get
            mock_get.side_effect = [
                stage_1,
                httpx.NetworkError("timeout"),
                httpx.NetworkError("timeout"),  # retry
            ]
            result = normalizer.normalize(
                "Smith", source_text="Smith proved it."
            )
        assert result.stage_1_outcome == OUTCOME_DISAMBIGUATION_PAGE
        assert result.stage_2_invoked is False  # never reached
        assert result.normalized_form == "Smith"
        assert "NetworkError" in (result.error or "")
        # No LLM call should have been attempted.
        assert llm.call_count == 0

    def test_candidate_truncation_respects_max(self):
        """A disambiguation page with many links is truncated to
        Config.wikipedia_stage_2_max_candidates (default 20)."""
        normalizer, _, llm = _make_normalizer_with_llm(
            {"selection": "Cand0", "reasoning": "first"}
        )
        # 50 candidates → should be truncated to 20.
        candidates = [f"Cand{i}" for i in range(50)]
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = [
                _disambig_stage_1_response("ManyCands"),
                _disambig_parse_response(candidates, "ManyCands"),
            ]
            result = normalizer.normalize("ManyCands", source_text="t")
        assert len(result.stage_2_candidates) == 20
        # First 20 in original order preserved.
        assert result.stage_2_candidates == [f"Cand{i}" for i in range(20)]

    def test_non_namespace_0_links_filtered(self):
        """Disambiguation pages have many non-article links (categories,
        meta-pages); only ns=0 article links are eligible candidates."""
        normalizer, _, llm = _make_normalizer_with_llm(
            {"selection": "Adam Smith", "reasoning": "ok"}
        )
        # Mix ns=0 (article), ns=14 (category), ns=10 (template), red link.
        mixed_links = [
            {"ns": 0, "title": "Adam Smith", "exists": True},
            {"ns": 14, "title": "Category:Smiths", "exists": True},
            {"ns": 10, "title": "Template:Disambig", "exists": True},
            {"ns": 0, "title": "Smith (red link)", "exists": False},
            {"ns": 0, "title": "Will Smith", "exists": True},
        ]
        parse_resp = _make_response(
            json.dumps(
                {"parse": {"title": "Smith", "links": mixed_links}}
            ).encode()
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = [
                _disambig_stage_1_response("Smith"),
                parse_resp,
            ]
            result = normalizer.normalize("Smith", source_text="t")
        # Only the two ns=0 + exists links should remain.
        assert result.stage_2_candidates == ["Adam Smith", "Will Smith"]

    def test_audit_event_includes_stage_2_fields(self):
        normalizer, db, _ = _make_normalizer_with_llm(
            {"selection": "Barack Obama", "reasoning": "Context matches."}
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = [
                _disambig_stage_1_response("Obama (disambiguation)", input_title="Obama"),
                _disambig_parse_response(
                    ["Barack Obama", "Michelle Obama"], "Obama (disambiguation)"
                ),
            ]
            normalizer.normalize(
                "Obama",
                claim_id="c1",
                source_text="Barack Obama signed it.",
            )
        events = query_events(db, event_type="entity_normalization", limit=5)
        assert len(events) == 1
        data = events[0]["event_data"]
        assert data["stage_1_outcome"] == OUTCOME_DISAMBIGUATION_PAGE
        assert data["stage_2_invoked"] is True
        assert data["stage_2_selection"] == "Barack Obama"
        assert data["stage_2_candidates"] == ["Barack Obama", "Michelle Obama"]
        assert "Context" in data["stage_2_reasoning"]
        assert data["normalized_form"] == "Barack Obama"
