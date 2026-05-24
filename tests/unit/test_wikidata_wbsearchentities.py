"""Phase H D53 step 1: tests for `WikidataAdapter.wbsearchentities`.

The new method is a raw wbsearchentities wrapper that returns
`WBSearchCandidate` objects with full label/description/aliases/
match-info for downstream LLM-mediated disambiguation. Distinct from
the existing `_live_resolve` which wraps results in `ResolutionCandidate`
for the KBProtocol interface and applies the D33 type filter.

httpx is mocked — no live calls. Live tests for actual Wikidata
behavior live in `tests/integration/live/test_wikidata_wbsearchentities_live.py`.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from aedos.audit.log import query_events
from aedos.config import Config
from aedos.database import open_memory_db
from aedos.layer4_sources.kb_wikidata import WBSearchCandidate, WikidataAdapter
from aedos.utils.http_cache import CachingHTTPClient, LRUHTTPCache


def _make_adapter():
    db = open_memory_db()
    config = Config()
    cache = LRUHTTPCache()
    http = CachingHTTPClient(cache=cache, headers={"User-Agent": config.user_agent})
    return WikidataAdapter(http_cache=http, db=db, config=config), db


def _make_response(body: bytes, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = body
    resp.headers = {}
    resp.raise_for_status = MagicMock()
    return resp


def _search_response(items: list[dict]):
    return _make_response(json.dumps({"search": items}).encode())


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestWBSearchEntities:
    def test_single_candidate_returns_one_row(self):
        adapter, _ = _make_adapter()
        body = _search_response([
            {
                "id": "Q76",
                "label": "Barack Obama",
                "description": "44th president of the United States",
                "aliases": ["Obama", "Barack Hussein Obama II"],
                "match": {"type": "label", "text": "Barack Obama"},
            }
        ])
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = body
            results = adapter.wbsearchentities("Barack Obama")
        assert len(results) == 1
        c = results[0]
        assert isinstance(c, WBSearchCandidate)
        assert c.qid == "Q76"
        assert c.label == "Barack Obama"
        assert "44th president" in c.description
        assert "Obama" in c.aliases
        assert c.match_type == "label"
        assert c.rank == 1

    def test_multi_candidate_preserves_rank_order(self):
        adapter, _ = _make_adapter()
        body = _search_response([
            {"id": "Q1", "label": "first", "match": {"type": "label", "text": "first"}},
            {"id": "Q2", "label": "second", "match": {"type": "alias", "text": "s"}},
            {"id": "Q3", "label": "third", "description": "desc3"},
        ])
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = body
            results = adapter.wbsearchentities("anything")
        assert [r.qid for r in results] == ["Q1", "Q2", "Q3"]
        assert [r.rank for r in results] == [1, 2, 3]
        assert results[0].match_type == "label"
        assert results[1].match_type == "alias"
        # Description defaults to None when absent.
        assert results[0].description is None
        assert results[2].description == "desc3"
        # Aliases default to empty list.
        assert results[0].aliases == []

    def test_empty_results_returns_empty_list(self):
        adapter, _ = _make_adapter()
        body = _search_response([])
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = body
            results = adapter.wbsearchentities("XyzNotARealEntity")
        assert results == []

    def test_invalid_qid_is_skipped(self):
        """Defence-in-depth: an item with a non-Q-id `id` is dropped."""
        adapter, _ = _make_adapter()
        body = _search_response([
            {"id": "Q1", "label": "ok"},
            {"id": "notaqid", "label": "bad"},
            {"id": "Q42", "label": "ok"},
        ])
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = body
            results = adapter.wbsearchentities("x")
        assert [r.qid for r in results] == ["Q1", "Q42"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_query_returns_empty_without_http_call(self):
        adapter, _ = _make_adapter()
        with patch("httpx.Client") as MockClient:
            results = adapter.wbsearchentities("")
            MockClient.return_value.__enter__.return_value.get.assert_not_called()
        assert results == []

    def test_whitespace_query_returns_empty_without_http_call(self):
        adapter, _ = _make_adapter()
        with patch("httpx.Client") as MockClient:
            results = adapter.wbsearchentities("   ")
            MockClient.return_value.__enter__.return_value.get.assert_not_called()
        assert results == []

    def test_no_http_cache_raises(self):
        config = Config()
        db = open_memory_db()
        adapter = WikidataAdapter(http_cache=None, db=db, config=config)
        with pytest.raises(RuntimeError, match="requires an http_cache"):
            adapter.wbsearchentities("foo")

    def test_malformed_response_returns_empty(self):
        """Non-dict / missing `search` key → empty list, no crash."""
        adapter, _ = _make_adapter()
        body = _make_response(json.dumps("not a dict").encode())
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = body
            results = adapter.wbsearchentities("foo")
        assert results == []

    def test_non_list_aliases_handled(self):
        """If the API returns aliases as a non-list (shouldn't happen but
        defence-in-depth), the candidate's aliases list is empty."""
        adapter, _ = _make_adapter()
        body = _search_response([
            {"id": "Q1", "label": "x", "aliases": "not_a_list"},
        ])
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = body
            results = adapter.wbsearchentities("x")
        assert len(results) == 1
        assert results[0].aliases == []


# ---------------------------------------------------------------------------
# Failure modes (fail-open)
# ---------------------------------------------------------------------------


class TestFailureModes:
    def test_network_error_returns_empty(self):
        adapter, _ = _make_adapter()
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = (
                httpx.NetworkError("conn refused")
            )
            results = adapter.wbsearchentities("foo")
        assert results == []

    def test_timeout_returns_empty(self):
        adapter, _ = _make_adapter()
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = (
                httpx.TimeoutException("timed out")
            )
            results = adapter.wbsearchentities("foo")
        assert results == []

    def test_retries_once_then_returns_empty(self):
        """First call fails transiently; retry also fails; fail open."""
        adapter, _ = _make_adapter()
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = (
                httpx.NetworkError("transient"),
                httpx.NetworkError("transient again"),
            )
            results = adapter.wbsearchentities("foo")
        assert results == []

    def test_retry_succeeds_on_second_attempt(self):
        adapter, _ = _make_adapter()
        body = _search_response([{"id": "Q1", "label": "x"}])
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = [
                httpx.NetworkError("transient"),
                body,
            ]
            results = adapter.wbsearchentities("foo")
        assert len(results) == 1
        assert results[0].qid == "Q1"


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


class TestAuditLogging:
    def test_audit_event_recorded_on_success(self):
        adapter, db = _make_adapter()
        body = _search_response([
            {"id": "Q1", "label": "a"},
            {"id": "Q2", "label": "b"},
        ])
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.return_value = body
            adapter.wbsearchentities("query text")
        events = query_events(db, event_type="wbsearchentities_query", limit=5)
        assert len(events) == 1
        d = events[0]["event_data"]
        assert d["query"] == "query text"
        assert d["candidate_count"] == 2
        assert d["top_qids"] == ["Q1", "Q2"]
        assert d["error"] is None
        assert d["retry_count"] == 0
        assert events[0]["event_subject"] == "query text"

    def test_audit_event_recorded_on_error(self):
        adapter, db = _make_adapter()
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value.get.side_effect = (
                httpx.NetworkError("boom")
            )
            adapter.wbsearchentities("anything")
        events = query_events(db, event_type="wbsearchentities_query", limit=5)
        assert len(events) == 1
        d = events[0]["event_data"]
        assert d["candidate_count"] == 0
        assert "NetworkError" in (d["error"] or "")
        # Retry counter incremented once.
        assert d["retry_count"] == 1


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


class TestConfigIntegration:
    def test_limit_from_config(self):
        """Default limit comes from Config.wikidata_wbsearch_limit (20)."""
        config = Config()
        db = open_memory_db()
        cache = LRUHTTPCache()
        http = CachingHTTPClient(cache=cache, headers={"User-Agent": config.user_agent})
        adapter = WikidataAdapter(http_cache=http, db=db, config=config)
        captured: dict = {}

        def fake_get(url, params, ttl_seconds):
            captured["params"] = params
            return _search_response([]).content and {"search": []}

        with patch.object(adapter._http, "get", side_effect=fake_get):
            adapter.wbsearchentities("x")
        assert captured["params"]["limit"] == 20

    def test_explicit_limit_overrides_config(self):
        adapter, _ = _make_adapter()
        captured: dict = {}

        def fake_get(url, params, ttl_seconds):
            captured["params"] = params
            return {"search": []}

        with patch.object(adapter._http, "get", side_effect=fake_get):
            adapter.wbsearchentities("x", limit=5)
        assert captured["params"]["limit"] == 5

    def test_invalid_config_limit_rejected_at_construction(self):
        with pytest.raises(ValueError, match="wikidata_wbsearch_limit"):
            Config(wikidata_wbsearch_limit=0)
        with pytest.raises(ValueError, match="wikidata_wbsearch_limit"):
            Config(wikidata_wbsearch_limit=51)
