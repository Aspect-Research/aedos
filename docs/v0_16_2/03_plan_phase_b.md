# v0.16.2 — Phase B Plan (get it working end-to-end + visibility)

Continuation within v0.16.2 after first live use surfaced a hard failure and two
visibility gaps. Branch `v0.16.2`. Same build-review-build discipline.

## Diagnosis (reproduced against a REAL uvicorn socket, not TestClient)

`scripts` diag (now removed) over real uvicorn + httpx showed:
- CORS/preflight **correct** (ACAO = the configured origin) — NOT the cause.
- **`HEALTH-DURING-CHAT` → ReadTimeout: the event loop is BLOCKED.** The endpoints
  are `async def` but call synchronous engine code (`respond`/`walk`/...), so a
  single request freezes uvicorn's whole loop; concurrent requests can't even get
  `/health`. (TestClient ran the app via a portal and masked this.)
- **`/chat` did not return within 240 s.** The chat flow generates a draft (LLM)
  then verifies *every* draft claim serially against live KB; at the default 30 s
  walker wall-clock per claim, a multi-claim draft ≈ minutes. The browser
  connection goes idle and the fetch fails → "network error" (no server log,
  because nothing errored — it was still churning).

So this is three problems, not one: (1) loop-blocking, (2) no keep-alive during
long work, (3) unbounded interactive latency. Fixes converge on streaming.

## Priority 1 — works end-to-end

1. **Offload engine work off the event loop.** Run every blocking engine call
   (`respond`, `extract`/`walk`/`aggregate`, `clear_party`, context read) via
   `starlette.concurrency.run_in_threadpool` / `asyncio.to_thread`. The loop stays
   responsive; concurrent requests and keep-alive work.
2. **Stream the turn over SSE** (`POST /chat/stream`, `POST /verify/stream`,
   `text/event-stream`). The blocking work runs in a thread and pushes progress
   events onto an `asyncio.Queue` (via `loop.call_soon_threadsafe`); the async
   generator yields SSE frames as they arrive, ending with a `result` event (or an
   `error` event). The connection is never idle → no idle "network error", and the
   user sees steps in real time (P2's live-steps requirement, same mechanism).
   Keep non-streaming `/chat` + `/verify` working (threadpool-offloaded) as a
   fallback / for non-SSE clients.
3. **Bound interactive latency.** Deployment builds the engine `Config` with an
   interactive walker budget (env `AEDOS_WALKER_WALL_CLOCK_SECONDS`, default ~12 s;
   `AEDOS_WALKER_MAX_LLM_CALLS`). Lower budget ⇒ a few more abstains, but bounded,
   responsive turns; documented tradeoff (soundness is unaffected — abstain is
   safe). Streaming makes even a multi-claim turn tolerable.
4. **Surface real errors.** Engine exceptions become a clean SSE `error` event /
   JSON 500 with a readable message (and a server log), never a silent
   network-error. Add `http://127.0.0.1:5173` to the default allowed origins.

**Engine hook:** `ChatWrapper.respond(..., progress: Optional[Callable[[dict],
None]] = None)` — invoked at phase boundaries (user-claims extracted, premises
promoted, draft generated, verifying claim i/N + its verdict, composed). Pure
addition; `progress=None` reproduces today's behavior exactly. (chat_wrapper lives
in `src/aedos/deployment/`, the deployment-facing engine surface.)

## Priority 2 — visibility (fully completed this phase)

5. **Live step log.** The SSE events from #2/#hook render in the chat UI as a
   real-time "thinking" trace: extracting → promoted N premises → verifying
   `<claim>` → `<verdict>` → composing. Transparent process, as Aedos intends.
6. **Context inspector.** `GET /session/context` (party via `X-Aedos-Session`)
   returns what Tier-U the session has retained. Engine hook
   `TierU.rows_for_party(asserting_party) -> list[dict]` (current non-retracted
   rows: subject/predicate/object/polarity/status/valid_from/valid_until). A UI
   "Inspector" panel lists the retained premises, refreshed after each turn + on
   demand. Answers "what has Aedos retained from the conversation?"
7. **UI servicing the whole turn.** Working/streaming indicator, the live step log
   inline in the turn, explicit error display (the real message), and the
   inspector. The UI reflects everything happening in the chat.

## Engine hooks (minimal, justified)
- `ChatWrapper.respond(progress=...)` — optional progress callback (P1/#hook).
- `TierU.rows_for_party(party)` — read-only party dump (P2/#6); mirrors
  `clear_party`, parameterized, party-scoped.

Both are small additions; no verdict logic changes; the gated suite stays green.

## Sequencing & verification
- **Build 1 (P1):** threadpool + SSE streaming + budget + error events + progress
  hook → reproduce against real uvicorn (loop stays responsive; a turn streams to
  completion) → adversarial review → patch.
- **Build 2 (P2):** context inspector + UI live-log/inspector/error polish →
  live smoke (stream a turn, read the inspector) → review → patch.
- Gated suite (incl. tests/deploy) green throughout; frontend `tsc + vite build`
  clean; live end-to-end smoke. Commits only; no tag/push.

---

## Results (Phase B — done)

**P1 — works end-to-end.** Diagnosed on a real uvicorn socket: the async handlers
froze the event loop and a chat turn ran >240 s. Fixed by offloading engine work
to a threadpool serialized by one `engine_lock` (loop stays free; the engine's
single-threaded/one-connection assumption preserved), streaming turns over SSE,
and an interactive walker budget (12 s/claim). Reproduced fixed: `/health` during
a live stream returns in ~0.4–1.1 s (RESPONSIVE; was a ReadTimeout/BLOCKED), and
turns stream incrementally instead of hanging.

**P2 — visibility.** Live step log (extracting → verifying `<claim>` → `<verdict>`
→ composing) streams into the chat as it happens; a Session-context Inspector
shows the Tier-U premises the session has retained, refreshed each turn / on
reset. Engine hooks `respond(progress=...)` and `rows_for_party` reviewed safe.

**Build-review-build.** Adversarial review B1–B8: **B1 (high)** `/verification/{id}`
ran a synchronous re-walk on the loop unlocked — patched to threadpool+lock
(closes **B8**); **B4** guarded frontend SSE parse + always-clear busy state;
**B5** reset map-rebind race → prune in place under the lock; **B7** SSE error
frame now discloses only the exception class (matches the buffered routes).
**B2/B3** (global one-engine-call serialization; an abandoned stream holds the
slot to completion) are **accepted, documented limitations** for a single-instance
internal-testing service (latency is not an Aedos goal; correctness preserved) —
revisit with a per-call DB connection if real concurrency is needed. **B6**
confirmed the engine hooks leak nothing and can't break verification.

**Verification.** Gated suite (unit + integration + deploy) **1651 passed**,
1 xfailed / 1 xpassed (the v0.15 sandbox boundary). Frontend `tsc --noEmit` +
`vite build` clean. Final live smoke (post-patch, real engine): loop RESPONSIVE
during a stream; chat streamed; the promoted premise appeared in the inspector;
`/verification/{id}` returned its verbose trace under the new lock+threadpool and
404'd cross-session; reset cleared the context.
