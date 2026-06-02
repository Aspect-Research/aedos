# v0.16.2 — Live Deployment Results

Delivered on branch `v0.16.2` (off the tagged `v0.16.1`), same build-verify-build
discipline as v0.16 / v0.16.1: plan → per-workstream code+tests → adversarial
security review → patch → consolidated verification. Deployment code is kept
**separate** from the engine (`deploy/` vs `src/aedos/`); the only engine touches
are two small, justified hooks.

## What shipped

A network-facing web app — a **chat** interface (primary) plus a **"run Aedos on
this text"** box — scoped per user, with the infrastructure for a real live
deployment at `aspectresearch.org/aedos` (not pushed live).

| WS | Summary | Commit |
|---|---|---|
| WS-A | Sandbox hardened for a key-holding network deployment: env-scrub locked in as a tested invariant (keys never reach the verifier child), `-I` isolation, explicit residual docs | `6d1e5ab` |
| WS-B | `deploy/backend` FastAPI service: access gate, CORS, rate limit, per-session Tier-U scoping (A+), `/chat` + `/verify` + `/session/reset` + party-scoped `/verification/{id}`; engine hook `TierU.clear_party` | `08b7e5f` |
| WS-C | `deploy/frontend` React + Vite UI (chat + verify-text, session + reset + given-assertion badges); deploy README | `d683faa` |

## Security posture (the load-bearing piece)

**API keys cannot leak through the Python verifier.** The sandbox child is spawned
with an explicitly-built, secret-free environment (`_build_child_env`) plus `-I`
isolation; the process's API keys are structurally absent from the child, so
model-generated code cannot read them via `os.environ` even if it escapes the AST
scan. Pinned by tests (incl. an end-to-end "plant key in parent, read None inside
the sandbox" check).

**Adversarial security review** (find → verify) of the backend before commit:
- **Confirmed sound (F4/F5/F6):** `clear_party` cannot wipe the table or another
  party (falsy-guard + parameterized + namespaced); per-session isolation holds at
  the Tier-U keying layer (`:` forbidden in session ids ⇒ no escape into seed
  parties); CORS is explicit-origin + credentials-off; no secret is logged or
  returned.
- **Fixed (F1/F2/F3):** session token moved to the `X-Aedos-Session` header (off
  URL/body/logs — was a query param on `/verification`); rate-limit + verification
  dicts now evict/cap (no unbounded growth on rotated session ids); access-key
  compare on bytes so a non-ASCII key fails closed with a clean 401, not a 500.

**Residual (documented, accepted for gated internal testing):** the known
encoded-dunder sandbox escape still permits arbitrary code execution *inside the
scrubbed child* → host filesystem/network (no secrets). The access gate keeps the
surface internal; the upgrade path for public exposure is containerization /
RestrictedPython (per the sandbox docstring). Keep secrets in the process env, not
a served-dir `.env`.

## Session model (A+)
A tester's opaque session token (client UUID) IS the Tier-U `asserting_party`
(namespaced `session:<id>`); isolation comes free from the engine's existing
keying. Reset is a party-scoped hard delete. `conversation_id` stays unused (B
deferred). Verification reads are party-scoped (404 cross-party, no existence
oracle).

## Verification
- Gated suite (unit + integration + **deploy**): **1645 passed**, 1 xfailed,
  1 xpassed (the pre-existing v0.15 sandbox boundaries — now explicitly the
  documented deployment residual). 26 deploy backend tests; +4 sandbox key-leak
  pins on the engine.
- Frontend: `tsc --noEmit` + `vite build` clean.
- Live end-to-end smoke (deploy → **real engine**, live LLM + KB): PASS.
  `/health` ok; `/verify "Paris is the capital of France."` →
  `(Paris, the_capital_of, France)` **verified**; `/chat "Williams College was
  founded in 1793."` → real conversational reply + verification_id; `/session/reset`
  → `rows_cleared: 1` (the chat-promoted premise, cleared). The full path
  (build_pipeline, ChatWrapper, promote→walk→aggregate, party-scoped reset) works.
- Package `__version__` bumped to `0.16.2` (`/health` now reports it; it had been
  left at `0.16.0` through v0.16.1).

## Not done (by design)
Full adversarial sandboxing (containers / RestrictedPython); conversation-scoped
Tier-U (option B); the actual push to `aspectresearch.org`; real user accounts.
The engine's original minimal `src/aedos/app.py` is left untouched; `deploy/` is
the v0.16.2 service.
