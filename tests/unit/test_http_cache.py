"""Tests for the v0.15 HTTP cache layer."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from aedos.utils.http_cache import CacheEntry, CachingHTTPClient, LRUHTTPCache


class TestLRUHTTPCache:
    def test_put_and_get(self):
        cache = LRUHTTPCache(max_size=10)
        entry = CacheEntry(
            response_body=b'{"data": 1}',
            etag='"abc"',
            status_code=200,
            headers={},
            cached_at=time.monotonic(),
            ttl_seconds=3600,
        )
        cache.put("https://example.com/api", entry)
        retrieved = cache.get("https://example.com/api")
        assert retrieved is not None
        assert retrieved.response_body == b'{"data": 1}'

    def test_cache_miss_returns_none(self):
        cache = LRUHTTPCache()
        assert cache.get("https://example.com/missing") is None

    def test_expired_entry_returns_none(self):
        cache = LRUHTTPCache()
        entry = CacheEntry(
            response_body=b"{}",
            etag=None,
            status_code=200,
            headers={},
            cached_at=time.monotonic() - 7200,
            ttl_seconds=3600,
        )
        cache.put("https://example.com/expired", entry)
        assert cache.get("https://example.com/expired") is None

    def test_lru_eviction(self):
        cache = LRUHTTPCache(max_size=2)
        for i in range(3):
            entry = CacheEntry(
                response_body=f"body{i}".encode(),
                etag=None,
                status_code=200,
                headers={},
                cached_at=time.monotonic(),
                ttl_seconds=3600,
            )
            cache.put(f"https://example.com/{i}", entry)
        # First entry should have been evicted
        assert cache.get("https://example.com/0") is None
        assert cache.get("https://example.com/2") is not None

    def test_invalidate(self):
        cache = LRUHTTPCache()
        entry = CacheEntry(
            response_body=b"{}",
            etag='"v1"',
            status_code=200,
            headers={},
            cached_at=time.monotonic(),
            ttl_seconds=3600,
        )
        cache.put("https://example.com/item", entry)
        cache.invalidate("https://example.com/item")
        assert cache.get("https://example.com/item") is None

    def test_params_differentiate_cache_keys(self):
        cache = LRUHTTPCache()
        entry1 = CacheEntry(b"result1", None, 200, {}, time.monotonic(), 3600)
        entry2 = CacheEntry(b"result2", None, 200, {}, time.monotonic(), 3600)
        cache.put("https://example.com/search", entry1, params={"q": "Paris"})
        cache.put("https://example.com/search", entry2, params={"q": "London"})

        r1 = cache.get("https://example.com/search", params={"q": "Paris"})
        r2 = cache.get("https://example.com/search", params={"q": "London"})
        assert r1.response_body == b"result1"
        assert r2.response_body == b"result2"

    def test_len(self):
        cache = LRUHTTPCache(max_size=10)
        assert len(cache) == 0
        cache.put("https://a.com", CacheEntry(b"{}", None, 200, {}, time.monotonic(), 3600))
        assert len(cache) == 1


class TestCachingHTTPClient:
    def _make_mock_http_client(self, body: bytes, etag: str | None = None):
        """Create a mock httpx.Client context manager that returns a response."""
        resp = MagicMock()
        resp.status_code = 200
        resp.content = body
        resp.headers = {"ETag": etag} if etag else {}
        resp.raise_for_status = MagicMock()

        mock_inner = MagicMock()
        mock_inner.get.return_value = resp

        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_inner
        mock_cm.__exit__.return_value = False
        return mock_cm, mock_inner, resp

    def test_cache_populated_after_first_request(self):
        cache = LRUHTTPCache()
        client = CachingHTTPClient(cache=cache)
        mock_cm, mock_inner, _ = self._make_mock_http_client(b'{"ok": true}', etag='"v1"')

        with patch("aedos.utils.http_cache.httpx.Client", return_value=mock_cm):
            result = client.get("https://example.com/api")

        assert result == {"ok": True}
        assert len(cache) == 1

    def test_cache_hit_avoids_second_request(self):
        cache = LRUHTTPCache()
        client = CachingHTTPClient(cache=cache)
        mock_cm, mock_inner, _ = self._make_mock_http_client(b'{"ok": true}', etag='"v1"')

        with patch("aedos.utils.http_cache.httpx.Client", return_value=mock_cm):
            client.get("https://example.com/api")
            client.get("https://example.com/api")

        # httpx inner client.get only called once
        assert mock_inner.get.call_count == 1

    def test_etag_sent_when_revalidating_expired_entry(self):
        cache = LRUHTTPCache()
        client = CachingHTTPClient(cache=cache)
        # Seed cache with an ALREADY-EXPIRED entry that has an ETag
        # (cached_at is set far in the past so it's expired)
        expired_entry = CacheEntry(
            b'{"x": 1}', '"etag_val"', 200, {},
            cached_at=time.monotonic() - 7200,  # 2 hours ago
            ttl_seconds=3600,
        )
        cache.put("https://example.com/api", expired_entry)

        call_headers: list[dict] = []

        def fake_get(url, params=None, headers=None):
            call_headers.append(dict(headers or {}))
            resp = MagicMock()
            resp.status_code = 200
            resp.content = b'{"x": 2}'
            resp.headers = {"ETag": '"etag_val_v2"'}
            resp.raise_for_status = MagicMock()
            return resp

        mock_inner = MagicMock()
        mock_inner.get.side_effect = fake_get
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_inner
        mock_cm.__exit__.return_value = False

        with patch("aedos.utils.http_cache.httpx.Client", return_value=mock_cm):
            client.get("https://example.com/api")

        assert "If-None-Match" in call_headers[0]
        assert call_headers[0]["If-None-Match"] == '"etag_val"'
