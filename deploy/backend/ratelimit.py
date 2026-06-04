"""Dependency-free in-memory sliding-window rate limiter.

Per-instance (single process), keyed by session party (falling back to client
IP). Bounds runaway LLM/KB cost from a single tester. For a multi-process or
multi-instance deployment this would move to a shared store; documented as the
upgrade path.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Callable, Optional


class SlidingWindowLimiter:
    def __init__(
        self,
        max_requests: int,
        window_seconds: float,
        *,
        clock: Optional[Callable[[], float]] = None,
        max_keys: int = 50_000,
    ) -> None:
        self._max = max(1, int(max_requests))
        self._window = float(window_seconds)
        self._clock = clock or time.monotonic
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()
        # F2: bound the number of tracked keys. Caller-supplied session ids key
        # this table, so a rotate-every-request pattern must not grow it without
        # limit. Empty deques are evicted on touch; a full GC runs past the cap.
        self._max_keys = max(1, int(max_keys))

    def _gc(self, cutoff: float) -> None:
        """Drop keys whose entire window has expired. Caller holds the lock."""
        stale = [k for k, q in self._hits.items()
                 if not q or q[-1] <= cutoff]
        for k in stale:
            del self._hits[k]

    def allow(self, key: str) -> bool:
        """Return True if a request under `key` is permitted now, recording it;
        False if it would exceed the window budget."""
        now = self._clock()
        cutoff = now - self._window
        with self._lock:
            if len(self._hits) >= self._max_keys:
                self._gc(cutoff)
            q = self._hits[key]
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= self._max:
                return False
            q.append(now)
            # Evict the key entirely if it ended up empty (cannot happen right
            # after an append, but keeps the invariant if logic changes).
            if not q:
                del self._hits[key]
            return True

    def retry_after_seconds(self, key: str) -> float:
        """Seconds until the oldest in-window hit for `key` expires (for the
        Retry-After header). 0 when not currently limited."""
        now = self._clock()
        with self._lock:
            q = self._hits.get(key)
            if not q or len(q) < self._max:
                return 0.0
            return max(0.0, self._window - (now - q[0]))
