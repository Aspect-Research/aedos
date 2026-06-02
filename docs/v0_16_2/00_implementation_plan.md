# Aedos v0.16.2 — Live Deployment (Implementation Plan)

**Goal.** Stand up Aedos as an internally-testable web application — a chat
interface (primary) plus a "run Aedos on this text" box — scoped per user, with
the build infrastructure for a real live deployment at `aspectresearch.org/aedos`
(not pushed live yet). Network-exposed, so the Python verifier must not leak API
keys, and env-var hygiene must be clean.

**Build discipline.** Same as v0.16 / v0.16.1: this plan first, then per-workstream
code→test loop (build-verify-build), commits per workstream, the existing gated
suite stays green throughout, one consolidated verification at the end. Engine
changes are minimal and justified; the live-deployment code lives in a **separate
tree** from the engine.

**Grounding.** See [01_investigation.md](01_investigation.md) for the read-only
findings this plan rests on (sandbox = subprocess + scrubbed env; app.py = no
auth/CORS/rate-limit; A+ session keying works; no Tier U reset path exists).

---

## Scope decisions (locked)

- **Internal testing posture.** Trusted testers, gated access — NOT hardened
  against an active public attacker. The honest threat model + upgrade path are
  documented; we do not overclaim a full sandbox.
- **Keys must not leak.** The env channel is already safe (scrubbed). We lock it
  in (explicit + symmetric + `-I` + regression test) and keep keys in the process
  env / out of any subprocess-readable file. This is the load-bearing security
  requirement.
- **Session model: A+.** A stable per-tester id is the `asserting_party` (Tier U
  isolation comes free from the existing keying), plus a per-party reset
  ("start fresh"). No touching the Tier U read/write keying.
- **Separation.** New top-level `deploy/` tree (backend + frontend) imports the
  `aedos` engine as a library. Engine (`src/aedos/`) gets only two small, justified
  hooks: sandbox env-hardening and a `TierU.clear_party` reset method.
- **Frontend: React + Vite**, tested locally via `npm run dev`, configured to
  point at the backend (dev: localhost; prod: the deployed origin). No staging.
- **Annotate given-assertion passes** (observability) — surface the conditional /
  `_given_assertion` verdicts prominently in the API + UI.

## Non-goals (this version)
- Full adversarial sandboxing (RestrictedPython / containers / seccomp) — documented
  as the upgrade path for true public exposure.
- Conversation-scoped Tier U (option B) — A+ uses `asserting_party` as the session
  key; `conversation_id` stays vestigial.
- Actually deploying to `aspectresearch.org` — infra is built and locally tested;
  the push is a separate operator step.
- Multi-concurrent-conversation per tester; real user accounts/OAuth.

---

## Architecture

```
src/aedos/                      # ENGINE — unchanged except two hooks below
  utils/sandbox.py              #   WS-A: env hardening + -I + (Linux) rlimits
  layer4_sources/tier_u.py      #   WS-B: clear_party(asserting_party) reset method
deploy/                         # LIVE DEPLOYMENT — new, separate from the engine
  backend/
    server.py                   #   FastAPI app: auth gate, CORS, rate limit,
                                 #   /chat, /verify, /session/reset, /health
    auth.py                     #   shared-secret access gate + per-session identity
    sessions.py                 #   session_id -> asserting_party derivation
    ratelimit.py                #   in-memory sliding-window limiter (dep-free)
    settings.py                 #   env-driven config (keys, origins, deploy key)
    __init__.py
  frontend/                     #   React + Vite (chat + verify-text-box)
    src/ ... index.html package.json vite.config.ts
  .env.example                  #   placeholders only; real .env is gitignored
  README.md                     #   run instructions (dev + prod)
tests/deploy/                   # backend + engine-hook tests (pytest)
```

The engine's existing `src/aedos/app.py` is left in place (the minimal
engine-embedded server); `deploy/backend/server.py` is the v0.16.2 service and
the one we run. It reuses `build_pipeline` + `ChatWrapper` so there is one wiring
definition.

---

## WS-A — Sandbox hardening + env hygiene (security-critical)

**Files:** `src/aedos/utils/sandbox.py` (engine), `tests/unit/test_sandbox.py`,
`deploy/.env.example`.

1. **Lock in env scrubbing (the key-leak channel).** Make `minimal_env` an
   explicit, platform-symmetric allow-list of *non-secret* infra vars
   (`PATH`/`SYSTEMROOT`/`PATHEXT`/`TEMP`/`TMP`/`LANG`/`LC_ALL`/`SystemDrive` as
   present), built the same way on every OS, and provably never containing
   `*_API_KEY` / `*_TOKEN` / `*_SECRET`. Add an assertion/filter that strips any
   key matching a secret-name pattern even if it sneaks into the infra list.
2. **Add `-I` (isolated mode)** to the subprocess argv (`[sys.executable, "-I",
   "-c", code]`) — ignores `PYTHON*` env, user site-packages, and cwd on
   `sys.path`. Defense-in-depth on top of the scrub.
3. **(Linux) resource caps** via `preexec_fn` using `resource.setrlimit`
   (RLIMIT_AS memory, RLIMIT_CPU) — guarded `if sys.platform != "win32" and
   resource available`; a no-op on Windows dev. Bounds a successful escape.
4. **Regression tests** (the proof, not the intent):
   - Inject a fake `ANTHROPIC_API_KEY`/`OPENROUTER_API_KEY` into the *parent* env,
     run sandbox code that prints `os.environ.get('ANTHROPIC_API_KEY')`, assert the
     output is `None`/empty — i.e. **keys are unreadable from inside the sandbox**.
   - Assert the same holds *through* the encoded-dunder bypass path (escape ⇒ still
     no keys in env).
   - Keep the existing xfail/xpass boundary tests intact (don't claim to close the
     bypass).
5. **Env hygiene + docs.** `deploy/.env.example` (placeholders only); document that
   production keys live in the **process environment**, never in a `.env` file
   readable from the served/working directory; reaffirm the docstring's
   "public exposure ⇒ containerize" upgrade path with the now-explicit residuals.

**Acceptance:** new key-leak tests pass; existing sandbox suite still green (incl.
the two known-boundary tests unchanged); engine behavior otherwise unchanged.

## WS-B — Backend service (`deploy/backend/`)

**Reuses** `build_pipeline(_db, config)` + `ChatWrapper`. Adds the network concerns.

1. **Settings** (`settings.py`): env-driven — `AEDOS_DEPLOY_KEY` (shared access
   secret), `AEDOS_ALLOWED_ORIGINS` (CSV), provider keys (passed through to the
   engine config), `AEDOS_DB_PATH`, rate-limit params. Fail fast if
   `AEDOS_DEPLOY_KEY` is unset in a non-local run.
2. **Access gate** (`auth.py`): a FastAPI dependency requiring header
   `X-Aedos-Key == AEDOS_DEPLOY_KEY` on all endpoints except `/health`. 401/403
   otherwise. Makes the service internal-only. (Expandable later to per-tester
   tokens / OAuth.)
3. **Sessions** (`sessions.py`): the client supplies a stable opaque `session_id`
   (UUID, generated + persisted client-side). Server derives
   `asserting_party = "session:" + session_id` — the A+ key. Distinct sessions ⇒
   isolated Tier U partitions (free from the keying). A `session_id` is required
   (no shared default partition).
4. **Endpoints:**
   - `POST /chat` — `{session_id, message}` → conversational `ChatWrapper.respond`
     with `asserting_party` = the session party. Returns `final_message`,
     `intervention_type`, `per_claim_actions`, `verification_id`, `observability`
     (incl. **given-assertion annotation**, see §B6).
   - `POST /verify` — `{session_id, text}` → the "run Aedos on this text" box:
     extract → walk each claim → aggregate, returning per-claim verdicts +
     observability **without** the chat draft/persona loop. (Uses the
     extractor/walker/aggregator directly, party-scoped so promoted premises from
     the tester's chat session still apply.)
   - `POST /session/reset` — `{session_id}` → `TierU.clear_party(party)`; returns
     count cleared. "Start fresh."
   - `GET /verification/{id}` — verbose audit view, **party-scoped** (404 unless it
     belongs to the requesting session).
   - `GET /health` — unauthenticated liveness.
5. **CORS** (`CORSMiddleware`): origins from `AEDOS_ALLOWED_ORIGINS` (dev
   `http://localhost:5173`; prod the deployed origin). Allow `X-Aedos-Key` +
   `Content-Type`; methods GET/POST.
6. **Given-assertion annotation:** the engine already emits
   `verified_given_assertion` / `contradicted_given_assertion` /
   `abstained_given_assertion` + a `conditional` flag via `claim_observability`.
   Surface a normalized `given_assertion: bool` + the base vs final verdict per
   claim in the API so the UI can badge "conditional on your assertion." (Backend
   surfacing only; no verdict-logic change.)
7. **Rate limiting** (`ratelimit.py`): dependency-free in-memory sliding-window
   keyed by `(session_id or client IP)`; configurable (e.g. N req / 60 s). 429 on
   exceed. Protects against runaway LLM cost. (Single-process; documented as
   per-instance.)

**Engine hook:** `TierU.clear_party(asserting_party) -> int` —
`DELETE FROM tier_u WHERE asserting_party=?`, commit, return rowcount. Hard delete
(reset = start fresh; avoids per-row retraction-propagation fan-out). Unit-tested
for party isolation (clearing party A leaves party B intact).

**Acceptance:** backend unit/integration tests (auth gate blocks/admits; CORS
header present; session isolation — party A can't read/reset party B; reset clears
only the caller's party; /verify returns per-claim verdicts; rate limit returns
429; given-assertion annotation present). Engine `clear_party` test green. Full
gated suite still green.

## WS-C — Frontend (`deploy/frontend/`, React + Vite)

1. **Scaffold:** Vite + React + TypeScript. `VITE_API_BASE` (dev
   `http://localhost:8000`; prod the deployed API origin) and the access key
   entered once + stored in `localStorage`.
2. **Session:** generate a UUID `session_id` on first load, persist in
   `localStorage`; send it on every request. "Start fresh" button → `/session/reset`.
3. **Two modes** (tab/toggle):
   - **Chat** (primary): message list, input box, streaming-free request/response;
     renders `final_message` + per-claim action badges + given-assertion annotation
     + an expandable observability/trace panel (verdict, base verdict, conditional,
     abstention reason, human trace; link to `/verification/{id}`).
   - **Verify text** (the box): a textarea → `/verify` → per-claim verdict cards.
4. **Build check:** `npm run build` succeeds; a thin smoke (component renders,
   API client points at `VITE_API_BASE`). Heavy logic stays server-side.

**Acceptance:** `npm run build` clean; dev server talks to the backend locally;
both modes render a real response end-to-end against a locally-running backend.

---

## Threat model & residual risks (honest)

- **API keys:** not in the sandbox child env (WS-A locks this + tests it). Keys in
  process env, never in a served-dir `.env`.
- **Residual (documented):** the encoded-dunder bypass still permits arbitrary code
  execution *inside the scrubbed child* → host filesystem read + outbound network
  (bounded by `-I`, cwd=tempdir, 5 s, and Linux rlimits). For **internal testing
  behind the access gate** this is accepted; for **true public exposure** the
  upgrade path is containerization / RestrictedPython (unchanged from the v0.15
  docstring, now with explicit residuals). The access gate keeps the surface
  internal.
- **Multi-tenant isolation:** per-session `asserting_party` + the existing Tier U
  keying; verification reads are party-scoped.

## Sequencing
WS-A (sandbox/keys — the gate on everything else) → WS-B (backend + engine reset
hook) → WS-C (frontend) → consolidated verification + results doc. Commits per
workstream; no tag/push of v0.16.2 until the operator says so.
