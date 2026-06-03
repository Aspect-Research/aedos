"""v0.16.2 Phase C: parallel claim verification + the thread-safety it rests on.

The load-bearing property: verifying claims concurrently yields the SAME verdicts
(in claim order) as serial verification, and one walk's per-walk state never leaks
into another's. Claims are independent (aggregated afterward), so this holds iff
the per-walk mutable state is thread-isolated — pinned here directly.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from aedos.layer4_sources.parallel_verify import walk_claims_parallel


def _claims(n):
    return [
        SimpleNamespace(claim_id=f"c{i}", subject=f"S{i}", predicate="p",
                        object=f"O{i}", polarity=1)
        for i in range(n)
    ]


class _DeterministicWalker:
    """walk() is a pure function of the claim — verdict encodes the subject — with
    a small sleep to force real overlap when run concurrently."""

    def __init__(self):
        self.threads: set[str] = set()

    def walk(self, claim, context):
        self.threads.add(threading.current_thread().name)
        time.sleep(0.01)
        return SimpleNamespace(verdict=f"v:{claim.subject}", trace=None,
                               abstention_reason=None)


class TestWalkClaimsParallel:
    def test_results_in_claim_order(self):
        w = _DeterministicWalker()
        claims = _claims(20)
        results = walk_claims_parallel(w, claims, None, max_workers=8)
        assert [r.verdict for r in results] == [f"v:{c.subject}" for c in claims]

    def test_parallel_verdicts_equal_serial(self):
        # The soundness guard: same verdicts as a serial walk, same order.
        claims = _claims(25)
        serial = [_DeterministicWalker().walk(c, None).verdict for c in claims]
        parallel = [r.verdict for r in
                    walk_claims_parallel(_DeterministicWalker(), claims, None, max_workers=8)]
        assert parallel == serial

    def test_actually_runs_concurrently(self):
        w = _DeterministicWalker()
        walk_claims_parallel(w, _claims(12), None, max_workers=6)
        assert len(w.threads) > 1  # genuinely used multiple threads

    def test_on_result_fires_once_per_claim(self):
        seen = []
        lock = threading.Lock()

        def on_result(index, claim, result):
            with lock:
                seen.append(index)

        walk_claims_parallel(_DeterministicWalker(), _claims(10), None,
                             max_workers=4, on_result=on_result)
        assert sorted(seen) == list(range(10))

    def test_single_claim_no_thread(self):
        w = _DeterministicWalker()
        got = []
        results = walk_claims_parallel(w, _claims(1), None,
                                       on_result=lambda i, c, r: got.append(i))
        assert len(results) == 1 and got == [0]
        assert w.threads == {threading.current_thread().name}  # ran inline

    def test_empty(self):
        assert walk_claims_parallel(_DeterministicWalker(), [], None) == []


class TestWalkerPerWalkStateThreadIsolation:
    """The walker's per-walk flags are thread-local, so concurrent walks on one
    shared Walker cannot clobber each other's state — especially
    `_user_authoritative_walk`, which gates whether KB grounding runs (a cross-
    walk race there would be a §3.2 hazard)."""

    def test_user_authoritative_and_excluded_are_thread_local(self):
        from aedos.layer4_sources.walker import Walker

        w = Walker(tier_u=MagicMock(), kb_verifier=MagicMock(),
                   python_verifier=None, substrate=MagicMock())
        results: dict[str, tuple] = {}
        barrier = threading.Barrier(2)

        def worker(name: str, auth: bool, excl: int):
            w._user_authoritative_walk = auth
            w._excluded_tier_u_row_ids = {excl}
            barrier.wait()  # both threads have written before either reads
            results[name] = (w._user_authoritative_walk,
                             set(w._excluded_tier_u_row_ids))

        t1 = threading.Thread(target=worker, args=("a", True, 1))
        t2 = threading.Thread(target=worker, args=("b", False, 2))
        t1.start(); t2.start(); t1.join(); t2.join()

        # Each thread reads back ITS OWN values, not the other's last write.
        assert results["a"] == (True, {1})
        assert results["b"] == (False, {2})

    def test_defaults_when_unset_in_thread(self):
        from aedos.layer4_sources.walker import Walker

        w = Walker(tier_u=MagicMock(), kb_verifier=MagicMock(),
                   python_verifier=None, substrate=MagicMock())
        # A fresh thread that never set the flags sees the safe defaults.
        out = {}

        def worker():
            out["auth"] = w._user_authoritative_walk
            out["excl"] = w._excluded_tier_u_row_ids

        t = threading.Thread(target=worker); t.start(); t.join()
        assert out["auth"] is False and out["excl"] == set()


class TestSharedInfraThreadSafety:
    def test_rate_limiter_concurrent_no_crash(self):
        from aedos.utils.rate_limit import RateLimiter

        lim = RateLimiter(max_per_second=2000)  # 0.5ms interval

        def hammer():
            for _ in range(40):
                lim.acquire()

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # No exception / no corrupted _last_call = pass (the lock makes the
        # read-wait-update atomic).

    def test_http_cache_concurrent_no_corruption(self):
        from aedos.utils.http_cache import CacheEntry, LRUHTTPCache

        cache = LRUHTTPCache(max_size=50)

        def entry():
            return CacheEntry(response_body=b"x", etag=None, status_code=200,
                              headers={}, cached_at=time.monotonic(), ttl_seconds=3600)

        def hammer(base):
            for i in range(200):
                url = f"http://x/{(base + i) % 80}"
                cache.put(url, entry())
                cache.get(url)

        threads = [threading.Thread(target=hammer, args=(b * 1000,)) for b in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # The OrderedDict LRU survived concurrent move_to_end/popitem and stayed
        # bounded — no corruption / RuntimeError.
        assert len(cache) <= 50
