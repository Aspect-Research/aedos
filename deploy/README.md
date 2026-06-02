# Aedos v0.16.2 — Live Deployment

A network-facing web app for Aedos: a **chat** interface (primary) and a
**“run Aedos on this text”** box, scoped per user. Kept separate from the engine
(`src/aedos/`), which it imports as a library.

> **Posture:** internal testing behind a shared access gate. Sound against
> LLM-generated-wrong-code and key leakage (the sandbox child never receives API
> keys); NOT hardened against an active attacker crafting input to escape the
> sandbox — see "Security" below and `src/aedos/utils/sandbox.py`.

## Layout
- `backend/` — FastAPI service (`deploy.backend.server:create_app`): access gate,
  CORS, rate limiting, per-session Tier-U scoping (A+), `/chat`, `/verify`,
  `/session/reset`, party-scoped `/verification/{id}`.
- `frontend/` — React + Vite UI (chat + verify-text).
- `.env.example` — backend env template (copy to the process env; never commit a
  populated `.env`).

## Run (local dev)

**1. Backend** — from the repo root, with the engine installed (`pip install -e .`):

```bash
# secrets + config in the PROCESS ENV (not a served-dir file)
export ANTHROPIC_API_KEY=sk-ant-...           # provider key (engine reads it)
export AEDOS_DEPLOY_KEY=$(openssl rand -hex 24) # shared access secret
export AEDOS_ALLOWED_ORIGINS=http://localhost:5173
export AEDOS_DB_PATH=aedos_phase10_5.db        # the seeded substrate
uvicorn "deploy.backend.server:create_app" --factory --port 8000
```

(For purely-local dev with no network exposure you may set `AEDOS_REQUIRE_AUTH=0`
to drop the access gate; never do this on a networked host.)

**2. Frontend:**

```bash
cd deploy/frontend
npm install
cp .env.example .env.local        # VITE_API_BASE=http://localhost:8000
npm run dev                        # http://localhost:5173
```

In the UI, paste the `AEDOS_DEPLOY_KEY` into the "access key" field (stored in
`localStorage`). A `session_id` (your Tier-U party) is generated and persisted
automatically; "Start fresh" clears your session context, "New session" rotates
the id.

## API (all but `/health` require the `X-Aedos-Key` header)
| Method | Path | Body | Purpose |
|---|---|---|---|
| GET | `/health` | — | liveness |
| POST | `/chat` | `{session_id, message}` | conversational turn |
| POST | `/verify` | `{session_id, text}` | run Aedos on raw text → per-claim verdicts |
| POST | `/session/reset` | `{session_id}` | clear this session's Tier-U context |
| GET | `/verification/{id}?session_id=` | — | verbose audit view (party-scoped) |

`observability[].conditional` is the **given-assertion** flag; the response also
rolls up `given_assertion: {count, claim_ids}`.

## Sessions (A+ model)
A tester's opaque `session_id` IS the Tier-U `asserting_party` (namespaced
`session:<id>`). Isolation between sessions comes from the engine's existing
Tier-U keying (`WHERE asserting_party=?`); reset is a party-scoped delete.

## Security (what holds / what doesn't)
- **API keys:** never enter the Python-verifier sandbox child (scrubbed env, `-I`
  isolation; pinned by tests). Keep keys in the process env, **not** a `.env`
  readable from the serving directory.
- **Access gate:** shared `X-Aedos-Key`, constant-time compare, fails closed.
- **Residual:** a sandbox-escape (the documented encoded-dunder boundary) gets
  arbitrary code execution *inside the scrubbed child* → host filesystem/network
  (no secrets). Accepted for gated internal testing; for public exposure,
  containerize / RestrictedPython (per the sandbox docstring).

## Production (`aspectresearch.org/aedos`, when ready)
Build the frontend (`npm run build`; set Vite `base: "/aedos/"` for sub-path
hosting), serve `dist/` behind the reverse proxy, run the backend behind it with
`AEDOS_ALLOWED_ORIGINS` set to the real origin and secrets in the process env.
No staging — dev points at prod via `VITE_API_BASE`.
