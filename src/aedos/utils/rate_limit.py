"""Simple per-instance rate limiter for Aedos external-service calls.

The limiter enforces a minimum interval between `acquire` calls — the
deployed pipeline is single-threaded, so a sleep-based limiter is
sufficient. State lives as an instance attribute (the limiter is owned
by the adapter that constructs it, e.g. `WikidataAdapter._sparql_limiter`
and `WikidataAdapter._search_limiter`); this keeps "add concurrency
protection later" a small local change rather than a refactor of where
the state lives. Adding concurrency protection later wraps the
`_last_call` mutation in a `threading.Lock` and changes nothing else.
"""

from __future__ import annotations

import time
from typing import Optional


class RateLimiter:
    """Enforces a minimum interval between `acquire` calls.

    `max_per_second` sets the steady-state rate; `override_delay_ms`
    (when not None) overrides it with an explicit delay — the runbook's
    `AEDOS_KB_REQUEST_DELAY_MS` knob threads through this parameter.
    """

    def __init__(
        self,
        max_per_second: float,
        override_delay_ms: Optional[int] = None,
    ) -> None:
        if override_delay_ms is not None:
            self._interval = override_delay_ms / 1000.0
        else:
            if max_per_second <= 0:
                raise ValueError("max_per_second must be positive")
            self._interval = 1.0 / max_per_second
        self._last_call: float = 0.0

    def acquire(self) -> None:
        """Block until the minimum interval since the last acquire has elapsed."""
        now = time.monotonic()
        wait = self._interval - (now - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()
