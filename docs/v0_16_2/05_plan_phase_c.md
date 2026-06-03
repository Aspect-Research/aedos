# v0.16.2 — Phase C Plan (parallel verification + per-claim traces)

Live use of the chat surfaced two problems: a verbose draft produced ~20 claims
verified **serially** (far too slow), and the streamed `no_grounding_found`
verdict carried **no trace** of how it was reached. Branch `v0.16.2`. Same
build-review-build discipline. SOUNDNESS IS PARAMOUNT — parallelism must not
change any verdict.

## Why this is delicate

The engine was built single-threaded (one shared SQLite connection; a sleep-based
rate limiter that "assumes single-threaded"). Naively threading the walks would
race shared mutable state — and one such race (`Walker._user_authoritative_walk`,
which gates whether KB grounding runs) could flip a verdict. So the work is:
make the shared infrastructure genuinely thread-safe, then parallelize.

Audit (decisive): the ONLY per-walk mutable instance state is three attributes —
`Walker._excluded_tier_u_row_ids`, `Walker._user_authoritative_walk`,
`Resolver._last_cache_row_id`. `KBVerifier` is stateless per `verify()`. Favorable
facts: `sqlite3.threadsafety == 3` (one connection is safe to share — sqlite
serializes), the resolver cache writes use `INSERT OR IGNORE` (no race crash), and
the RateLimiter docstring already names the fix ("wrap `_last_call` in a Lock").

## P1 — parallel verification (verdict-preserving)

1. **Isolate per-walk state via thread-local** (so a shared pipeline is safe under
   concurrent walks): back the three attrs above with `threading.local()` exposed
   through same-named properties — zero changes to the dozens of read/write sites,
   each thread sees its own walk state. (Soundness: removes the only verdict-
   affecting race.)
2. **Make shared infra thread-safe:** `RateLimiter.acquire` under a `threading.Lock`
   (keeps KB politeness — SPARQL still serializes); `LRUHTTPCache` get/put/expired
   under a lock (its OrderedDict LRU is not concurrency-safe). Verify the
   predicate-translation consult cache and the exception (nogood) cache use
   safe DB write patterns (INSERT OR IGNORE / by-id); harden if not.
3. **Parallel walk utility (engine):** `walk_claims_parallel(walker, claims,
   context, *, max_workers, on_result)` — a bounded `ThreadPoolExecutor` over the
   shared (now thread-safe) walker; returns `WalkResult`s in claim order; invokes
   `on_result(index, claim, result)` as each COMPLETES (for streaming, so verdicts
   surface out of order as they finish). Used by both chat and verify.
4. **Wire it in:** `ChatWrapper.respond` replaces its serial draft-claim loop with
   the parallel utility (emitting a per-claim `verdict` event with the trace as
   each completes). The backend `_run_verify` does the same. Worker count is
   bounded + env-configurable (`AEDOS_VERIFY_WORKERS`, default 8). Turn-level
   `engine_lock` still serializes turns; parallelism is intra-turn only.

Soundness guard: a verdict is a pure function of (claim, KB/Tier-U/Python state);
claims are verified independently and aggregated after, so isolating per-walk
state makes parallel verdicts identical to serial. Tested directly.

## P2 — detailed per-claim trace

5. Each per-claim completion event carries the **full reasoning trace**:
   `verdict`, `abstention_reason`, and `trace_human` (the row-id-free human trace
   the engine already produces via `trace_to_human` — sources tried, edges walked,
   why it abstained), plus the triple. For `no_grounding_found` this shows which
   sources/edges were attempted and the abstention reason, answering "how did it
   conclude no_grounding".
6. **Frontend:** render a card per claim, filled as its parallel verdict arrives
   (out of order), with the verdict badge + an expandable trace (trace_human +
   abstention_reason) for EVERY verdict including abstains. The live step log
   becomes a live per-claim result grid rather than a serial line log.

## Tests
- Thread-safety: RateLimiter + LRUHTTPCache under concurrent access (no lost
  updates / corruption); the thread-local walk-state is per-thread isolated.
- `walk_claims_parallel` returns results in claim order; on_result fires per claim;
  **parallel verdicts == serial verdicts** on a deterministic mock walker (the
  soundness guard).
- Backend SSE emits a per-claim trace; ordering-independent rendering.
- Full gated suite green; frontend tsc+vite build clean; live smoke (a multi-claim
  turn streams verdicts+traces concurrently, faster than serial).

## Sequencing
Engine thread-safety (thread-local + locks) → parallel utility + tests (incl.
parallel==serial) → wire chat/verify + traces → frontend → adversarial review →
patch → live smoke + results. Commits only; no tag/push.

---

## Results (Phase C — done)

**Parallel verification (verdict-preserving).** A turn's claims are walked
concurrently on one shared pipeline. The audit found the per-walk mutable state to
be exactly three attributes — `Walker._excluded_tier_u_row_ids`,
`._user_authoritative_walk`, `Resolver._last_cache_row_id` — now thread-local; the
shared OrderedDict-LRU caches + the sleep-based limiter (`RateLimiter`,
`LRUHTTPCache`, `WikidataAdapter._transitive_memo`) are lock-guarded. Claims are
independent and aggregated afterward, so isolating per-walk state makes parallel
verdicts identical to serial — pinned by a `parallel == serial` test and a
thread-local-isolation test.

**Live:** the same 5-claim text took **105.2 s serial (workers=1) → 25.3 s
parallel (workers=6) — ~4.2× faster**, with identical verdicts (4 verified, 1
budget-abstain).

**Per-claim traces.** Each `verdict` SSE event now carries the full reasoning
trace (`verdict` + `abstention_reason` + `trace_human`), rendered live as an
expandable per-claim card the moment that walk completes (out of order). The
`no_grounding_found` case that prompted this — "Mount Everest tallest mountain" —
now streams a **755-char trace** with `abstention_reason: budget_wall_clock`,
showing exactly what was explored and why it abstained.

**Build-review-build.** Adversarial review (two dimensions, find→verify) audited
the whole `walk()` call graph for missed shared mutable state. It found **one**
genuine high-severity gap (F1/C-1): `WikidataAdapter._transitive_memo` was an
unguarded OrderedDict LRU — the same race class fixed for `LRUHTTPCache`, missed
on the memo; an unguarded check-then-act could `KeyError` (swallowed by the
walker to abstain) and diverge a parallel verdict from serial. **Patched** (memo
lock). Everything else confirmed clean: KBVerifier/Substrate/TierU hold no
per-call instance state (SQLite-backed, INSERT OR IGNORE/REPLACE on the
`threadsafety==3` connection); the thread-local property pattern is correct;
`walk_claims_parallel` preserves claim order, propagates exceptions, cleans up its
pool, and runs `on_result` on the drain thread; no lock-ordering cycle
(engine_lock above the pool; rate-limiter/cache/memo locks are leaves).

**Verification.** Gated suite (unit + integration + deploy): **1661 passed**,
1 xfailed / 1 xpassed (the v0.15 sandbox boundary). +12 Phase C tests. Frontend
`tsc --noEmit` + `vite build` clean. Live re-smoke on the committed (post-F1)
code confirms parallel verdicts each carrying a trace.
