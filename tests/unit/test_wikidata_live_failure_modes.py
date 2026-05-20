"""Mocked failure-mode tests for WikidataAdapter live methods.

These tests do NOT hit the real Wikidata API. They exercise the live
code path (`RUN_LIVE_KB=1` semantically equivalent — the adapter is
constructed with an http_cache, and the live method is called directly)
against a mocked httpx transport so failure-mode handling
(timeout/retry, malformed response, single-retry-then-give-up) is
exercised without network.

Covers Phase F2 commit 1 (`_live_resolve` failure modes).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from aedos.config import Config
from aedos.database import open_memory_db
from aedos.layer4_sources.kb_protocol import LocalContext
from aedos.layer4_sources.kb_wikidata import WikidataAdapter
from aedos.utils.http_cache import CachingHTTPClient, LRUHTTPCache


def _make_adapter():
    """Construct an adapter against a real CachingHTTPClient — only the
    httpx layer is mocked. Matches the deployed wiring shape closely."""
    db = open_memory_db()
    config = Config()
    cache = LRUHTTPCache()
    http_client = CachingHTTPClient(cache=cache, headers={"User-Agent": config.user_agent})
    adapter = WikidataAdapter(
        http_cache=http_client, db=db, config=config
    )
    return adapter, db


def _make_httpx_cm(response_or_exc):
    """Build a mock for `httpx.Client(...)` context-manager. The inner
    client's .get() either returns `response_or_exc` (if it's a response
    mock) or raises (if it's an exception class instance)."""
    inner = MagicMock()
    if isinstance(response_or_exc, Exception):
        inner.get.side_effect = response_or_exc
    else:
        inner.get.return_value = response_or_exc
    cm = MagicMock()
    cm.__enter__.return_value = inner
    cm.__exit__.return_value = False
    return cm, inner


def _make_response(body: bytes, status_code: int = 200, etag: str | None = None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = body
    resp.headers = {"ETag": etag} if etag else {}
    resp.raise_for_status = MagicMock()
    return resp


class TestLiveResolveFailureModes:
    def test_retries_on_timeout_then_succeeds(self):
        """First attempt times out, second attempt returns a real
        response. Adapter should return the candidates from the second
        attempt and the audit event should record retry_count=1."""
        adapter, db = _make_adapter()
        lc = LocalContext(predicate="holds_role", slot_position="subject")

        # First call raises TimeoutException; second returns a response.
        timeout_exc = httpx.TimeoutException("simulated timeout")
        success_resp = _make_response(
            b'{"search": [{"id": "Q76", "label": "Barack Obama", '
            b'"description": "44th president"}]}'
        )
        call_count = {"n": 0}

        def fake_get(url, params=None, headers=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise timeout_exc
            return success_resp

        inner = MagicMock()
        inner.get.side_effect = fake_get
        cm = MagicMock()
        cm.__enter__.return_value = inner
        cm.__exit__.return_value = False

        with patch("aedos.utils.http_cache.httpx.Client", return_value=cm):
            # patch time.sleep to make the test fast (the backoff is 1s)
            with patch("aedos.layer4_sources.kb_wikidata.time.sleep"):
                candidates = adapter._live_resolve("Obama", lc)

        assert len(candidates) == 1
        assert candidates[0].kb_identifier == "Q76"
        assert call_count["n"] == 2

        # Confirm audit event recorded the retry
        from aedos.audit.log import query_events
        events = query_events(db, event_type="kb_live_resolve")
        assert events[0]["event_data"]["retry_count"] == 1
        assert events[0]["event_data"]["candidate_count"] == 1

    def test_retries_on_timeout_then_gives_up_with_empty(self):
        """Both attempts time out — adapter returns [] and audit event
        records retry_count=1 plus the error."""
        adapter, db = _make_adapter()
        lc = LocalContext(predicate="holds_role", slot_position="subject")

        timeout_exc = httpx.TimeoutException("simulated timeout")
        cm, _ = _make_httpx_cm(timeout_exc)

        with patch("aedos.utils.http_cache.httpx.Client", return_value=cm):
            with patch("aedos.layer4_sources.kb_wikidata.time.sleep"):
                candidates = adapter._live_resolve("Obama", lc)

        assert candidates == []

        from aedos.audit.log import query_events
        events = query_events(db, event_type="kb_live_resolve")
        assert events[0]["event_data"]["retry_count"] == 1
        assert events[0]["event_data"]["candidate_count"] == 0
        assert "TimeoutException" in events[0]["event_data"]["error"]

    def test_malformed_response_returns_empty(self):
        """Server returns valid JSON but without the expected `search`
        key. Adapter handles gracefully — returns [] (architecture §3.1
        soundness: an absent grounding is honest abstention)."""
        adapter, db = _make_adapter()
        lc = LocalContext(predicate="holds_role", slot_position="subject")

        # Response is valid JSON but missing the "search" key.
        malformed_resp = _make_response(b'{"unexpected": "shape"}')
        cm, _ = _make_httpx_cm(malformed_resp)

        with patch("aedos.utils.http_cache.httpx.Client", return_value=cm):
            candidates = adapter._live_resolve("Obama", lc)

        assert candidates == []
        # No retry (it was a successful HTTP response, just unexpected shape)
        from aedos.audit.log import query_events
        events = query_events(db, event_type="kb_live_resolve")
        assert events[0]["event_data"]["retry_count"] == 0
        assert events[0]["event_data"]["candidate_count"] == 0

    def test_resolve_raises_when_no_http_cache_wired(self):
        """Wiring-gap defence (F1 acceptance criterion): a live resolve
        attempted without an http_cache must fail loudly. Silent
        empty-return would hide F-004-class wiring defects."""
        adapter = WikidataAdapter()  # no http_cache
        lc = LocalContext(predicate="holds_role", slot_position="subject")
        with pytest.raises(RuntimeError, match="http_cache"):
            adapter._live_resolve("Obama", lc)
