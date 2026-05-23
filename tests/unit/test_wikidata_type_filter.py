"""Phase G D33: tests for the type-filter post-step on the wikidata adapter.

Covers:
  - `_extract_p31` parses Wikidata claim shapes correctly.
  - `_fetch_p31_for_candidates` batches calls and surfaces partial / total
    failure as a fail-open signal to its caller.
  - `_live_resolve` (with type filter) keeps matching candidates, drops
    non-matching ones, returns empty when the filter eliminates all, and
    fails open on wbgetentities API failure.

These tests do not hit the real Wikidata API — the httpx layer is mocked.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from aedos.config import Config
from aedos.database import open_memory_db
from aedos.layer4_sources.kb_protocol import LocalContext
from aedos.layer4_sources.kb_wikidata import (
    WikidataAdapter,
    _extract_p31,
)
from aedos.utils.http_cache import CachingHTTPClient, LRUHTTPCache


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_adapter():
    db = open_memory_db()
    config = Config()
    cache = LRUHTTPCache()
    http_client = CachingHTTPClient(cache=cache, headers={"User-Agent": config.user_agent})
    return WikidataAdapter(http_cache=http_client, db=db, config=config), db


def _make_response(body: bytes, status_code: int = 200, etag: str | None = None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = body
    resp.headers = {"ETag": etag} if etag else {}
    resp.raise_for_status = MagicMock()
    return resp


def _search_response(candidate_ids: list[str]) -> MagicMock:
    """wbsearchentities response with the given candidate Q-ids."""
    search = [
        {"id": qid, "label": f"label_{qid}", "description": f"desc_{qid}"}
        for qid in candidate_ids
    ]
    body = json.dumps({"search": search}).encode()
    return _make_response(body)


def _wbgetentities_response(p31_by_qid: dict[str, list[str]]) -> MagicMock:
    """Build a wbgetentities response. ``p31_by_qid`` maps each Q-id to the
    list of P31 Q-ids to encode for it."""
    entities = {}
    for qid, p31_list in p31_by_qid.items():
        entities[qid] = {
            "type": "item",
            "id": qid,
            "claims": {
                "P31": [
                    {
                        "mainsnak": {
                            "snaktype": "value",
                            "property": "P31",
                            "datavalue": {
                                "value": {"entity-type": "item", "id": p31_qid},
                                "type": "wikibase-entityid",
                            },
                        },
                        "type": "statement",
                        "rank": "normal",
                    }
                    for p31_qid in p31_list
                ]
            },
        }
    body = json.dumps({"entities": entities}).encode()
    return _make_response(body)


def _scripted_httpx(responses: list):
    """Return (cm, inner) pair where inner.get returns responses[i] for the
    i-th call (in order). Raises if exhausted."""
    inner = MagicMock()
    inner.get.side_effect = responses
    cm = MagicMock()
    cm.__enter__.return_value = inner
    cm.__exit__.return_value = False
    return cm, inner


# ---------------------------------------------------------------------------
# _extract_p31 unit tests
# ---------------------------------------------------------------------------

class TestExtractP31:
    def test_extracts_single_p31(self):
        entity = {
            "claims": {
                "P31": [
                    {"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}
                ]
            }
        }
        assert _extract_p31(entity) == ["Q5"]

    def test_extracts_multiple_p31(self):
        entity = {
            "claims": {
                "P31": [
                    {"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}},
                    {"mainsnak": {"datavalue": {"value": {"id": "Q21036474"}}}},
                ]
            }
        }
        assert _extract_p31(entity) == ["Q5", "Q21036474"]

    def test_no_p31_returns_empty(self):
        entity = {"claims": {}}
        assert _extract_p31(entity) == []

    def test_no_claims_returns_empty(self):
        assert _extract_p31({}) == []

    def test_malformed_claim_skipped(self):
        # One well-formed claim, one with missing datavalue — only the well-formed
        # one comes through. Defensive against future Wikidata schema additions.
        entity = {
            "claims": {
                "P31": [
                    {"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}},
                    {"mainsnak": {"snaktype": "somevalue"}},  # missing datavalue
                ]
            }
        }
        assert _extract_p31(entity) == ["Q5"]

    def test_non_qid_value_skipped(self):
        entity = {
            "claims": {
                "P31": [
                    {"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}},
                    {"mainsnak": {"datavalue": {"value": {"id": "not-a-qid"}}}},
                ]
            }
        }
        assert _extract_p31(entity) == ["Q5"]


# ---------------------------------------------------------------------------
# _fetch_p31_for_candidates tests
# ---------------------------------------------------------------------------

class TestFetchP31ForCandidates:
    def test_empty_list_returns_empty(self):
        adapter, _ = _make_adapter()
        p31, err = adapter._fetch_p31_for_candidates([])
        assert p31 == {}
        assert err is None

    def test_single_batch_returns_p31_map(self):
        adapter, _ = _make_adapter()
        resp = _wbgetentities_response(
            {"Q76": ["Q5"], "Q41773": ["Q3957"]}
        )
        cm, _inner = _scripted_httpx([resp])

        with patch("aedos.utils.http_cache.httpx.Client", return_value=cm):
            p31, err = adapter._fetch_p31_for_candidates(["Q76", "Q41773"])

        assert err is None
        assert p31["Q76"] == ["Q5"]
        assert p31["Q41773"] == ["Q3957"]

    def test_batches_when_over_size(self):
        """80 candidates with batch size 50 should yield 2 HTTP calls."""
        adapter, _ = _make_adapter()
        candidates = [f"Q{i}" for i in range(1, 81)]
        # First batch (Q1..Q50) returns trivial P31s; second batch (Q51..Q80) too.
        batch1_resp = _wbgetentities_response({qid: ["Q5"] for qid in candidates[:50]})
        batch2_resp = _wbgetentities_response({qid: ["Q5"] for qid in candidates[50:]})
        cm, inner = _scripted_httpx([batch1_resp, batch2_resp])

        with patch("aedos.utils.http_cache.httpx.Client", return_value=cm):
            p31, err = adapter._fetch_p31_for_candidates(candidates)

        assert err is None
        assert inner.get.call_count == 2
        # All 80 candidates have P31 populated
        assert all(p31[qid] == ["Q5"] for qid in candidates)

    def test_missing_entity_gets_empty_p31(self):
        """If the API response omits an entity, the candidate is still in the
        returned map but with an empty P31 list (and downstream filtering
        treats it as non-matching, correctly)."""
        adapter, _ = _make_adapter()
        # Q76 present, Q41773 omitted
        resp = _wbgetentities_response({"Q76": ["Q5"]})
        # Need to splice Q41773 out of the response — _wbgetentities_response
        # already omits anything not in the dict, so this works directly.
        cm, _inner = _scripted_httpx([resp])

        with patch("aedos.utils.http_cache.httpx.Client", return_value=cm):
            p31, err = adapter._fetch_p31_for_candidates(["Q76", "Q41773"])

        assert err is None
        assert p31["Q76"] == ["Q5"]
        assert p31["Q41773"] == []  # missing → empty

    def test_api_failure_returns_error_string(self):
        adapter, _ = _make_adapter()
        cm = MagicMock()
        inner = MagicMock()
        inner.get.side_effect = httpx.TimeoutException("simulated")
        cm.__enter__.return_value = inner
        cm.__exit__.return_value = False

        with patch("aedos.utils.http_cache.httpx.Client", return_value=cm):
            p31, err = adapter._fetch_p31_for_candidates(["Q76"])

        assert err is not None
        assert "Timeout" in err
        # Map still returns the seeded empty list for the requested Q-ids
        assert p31 == {"Q76": []}

    def test_raises_when_no_http_cache_wired(self):
        adapter = WikidataAdapter()
        with pytest.raises(RuntimeError, match="http_cache"):
            adapter._fetch_p31_for_candidates(["Q76"])
