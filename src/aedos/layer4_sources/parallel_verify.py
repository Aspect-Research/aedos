"""Parallel claim verification (v0.16.2 Phase C).

Walks many independent claims concurrently on a SHARED, thread-safe walker. Each
claim's verdict is a pure function of (claim, KB/Tier-U/Python state) — claims do
not interact during verification (they are aggregated afterward) — so verifying
them in parallel yields identical verdicts to verifying them serially, PROVIDED
the shared infrastructure is thread-safe. That precondition is met by:

  - the walker's per-walk flags being thread-local (walker.py),
  - the resolver's per-resolve state being thread-local (resolver.py),
  - the rate limiter and HTTP cache being lock-guarded (utils/),
  - sqlite3.threadsafety == 3 (one connection is safe to share; statements
    serialize) and all substrate memo writes using INSERT OR IGNORE/REPLACE.

Concurrency here is intra-turn only; the deployment's turn-level lock still
serializes turns, so the only concurrent walks are the claims of one turn.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional

DEFAULT_MAX_WORKERS = 8


def walk_claims_parallel(
    walker: Any,
    claims: list,
    context: Any,
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
    on_result: Optional[Callable[[int, Any, Any], None]] = None,
) -> list:
    """Walk `claims` concurrently and return their WalkResults IN CLAIM ORDER.

    `on_result(index, claim, result)` (optional) is invoked as each claim
    COMPLETES — in completion order, not claim order — so a caller can stream
    each verdict the moment it is ready. It runs on the calling thread (the
    as-completed drain loop), never on a worker thread, so the callback need not
    be thread-safe. A walk that raises propagates (the engine's walk() is
    designed not to raise; a raise is a real bug, surfaced not swallowed).
    """
    if not claims:
        return []
    if len(claims) == 1:
        # No thread for a single claim — keep the common case overhead-free.
        result = walker.walk(claims[0], context)
        if on_result is not None:
            on_result(0, claims[0], result)
        return [result]

    workers = max(1, min(max_workers, len(claims)))
    results: list = [None] * len(claims)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="aedos-verify") as ex:
        future_to_index = {
            ex.submit(walker.walk, claim, context): i for i, claim in enumerate(claims)
        }
        for future in as_completed(future_to_index):
            i = future_to_index[future]
            result = future.result()
            results[i] = result
            if on_result is not None:
                on_result(i, claims[i], result)
    return results
