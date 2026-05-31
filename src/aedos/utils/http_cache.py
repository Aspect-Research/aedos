"""HTTP client with ETag-conditional requests and in-process LRU cache.

Used by the Wikidata adapter for entity resolution and SPARQL queries.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

import httpx


@dataclass
class CacheEntry:
    response_body: bytes
    etag: Optional[str]
    status_code: int
    headers: dict[str, str]
    cached_at: float
    ttl_seconds: int

    def is_expired(self) -> bool:
        return (time.monotonic() - self.cached_at) > self.ttl_seconds


class LRUHTTPCache:
    def __init__(self, max_size: int = 256, default_ttl_seconds: int = 3600):
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._max_size = max_size
        self._default_ttl = default_ttl_seconds

    def _key(self, url: str, params: Optional[dict[str, Any]] = None) -> str:
        raw = url
        if params:
            raw += "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, url: str, params: Optional[dict[str, Any]] = None) -> Optional[CacheEntry]:
        key = self._key(url, params)
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.is_expired():
            return None  # Expired; keep in dict for conditional-GET ETag reuse
        self._cache.move_to_end(key)
        return entry

    def put(
        self,
        url: str,
        entry: CacheEntry,
        params: Optional[dict[str, Any]] = None,
    ) -> None:
        key = self._key(url, params)
        self._cache[key] = entry
        self._cache.move_to_end(key)
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def get_expired(self, url: str, params: Optional[dict[str, Any]] = None) -> Optional[CacheEntry]:
        """Return an expired entry (without removing it) for conditional GET."""
        key = self._key(url, params)
        return self._cache.get(key)

    def invalidate(self, url: str, params: Optional[dict[str, Any]] = None) -> None:
        key = self._key(url, params)
        self._cache.pop(key, None)

    def __len__(self) -> int:
        return len(self._cache)


class CachingHTTPClient:
    """httpx-based HTTP client with ETag conditional requests and LRU caching."""

    def __init__(
        self,
        cache: Optional[LRUHTTPCache] = None,
        default_ttl_seconds: int = 3600,
        timeout_seconds: float = 30.0,
        headers: Optional[dict[str, str]] = None,
    ):
        self._cache = cache if cache is not None else LRUHTTPCache(default_ttl_seconds=default_ttl_seconds)
        self._default_ttl = default_ttl_seconds
        self._timeout = timeout_seconds
        self._base_headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "Aedos/0.15 (claim-verification research)",
        }
        if headers:
            self._base_headers.update(headers)

    def get(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        ttl_seconds: Optional[int] = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """GET request with TTL caching. Returns parsed JSON.

        Hit path (non-expired entry): return immediately, no HTTP call.
        Miss or expired: make a fresh GET, cache the response.
        ETag is stored in cache entries and sent as If-None-Match when
        revalidating an expired entry; 304 refreshes the TTL.
        """
        import json

        cached = self._cache.get(url, params)

        # Fast path: non-expired cache hit
        if cached is not None:
            return json.loads(cached.response_body)

        req_headers = dict(self._base_headers)
        if extra_headers:
            req_headers.update(extra_headers)

        # Check for an expired entry to send conditional GET
        expired = self._cache.get_expired(url, params)
        if expired is not None and expired.etag:
            req_headers["If-None-Match"] = expired.etag

        with httpx.Client(timeout=self._timeout) as client:
            response = client.get(url, params=params, headers=req_headers)

        if response.status_code == 304 and expired is not None:
            # Not modified — refresh TTL
            refreshed = CacheEntry(
                response_body=expired.response_body,
                etag=expired.etag,
                status_code=200,
                headers=expired.headers,
                cached_at=time.monotonic(),
                ttl_seconds=ttl_seconds or self._default_ttl,
            )
            self._cache.put(url, refreshed, params)
            return json.loads(expired.response_body)

        response.raise_for_status()
        body = response.content
        etag = response.headers.get("ETag")
        entry = CacheEntry(
            response_body=body,
            etag=etag,
            status_code=response.status_code,
            headers=dict(response.headers),
            cached_at=time.monotonic(),
            ttl_seconds=ttl_seconds or self._default_ttl,
        )
        self._cache.put(url, entry, params)
        return json.loads(body)

    def get_raw(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> httpx.Response:
        """GET without caching — for queries where the response shouldn't be cached."""
        req_headers = dict(self._base_headers)
        if extra_headers:
            req_headers.update(extra_headers)
        with httpx.Client(timeout=self._timeout) as client:
            return client.get(url, params=params, headers=req_headers)
