# Aedos

A claim-verification and conversational-memory research prototype. Every
factual claim the assistant makes is extracted, routed to a type-matched
verifier, and either confirmed, rejected, or flagged before it reaches the
user. User-stated facts are stored as ground truth for the conversation.

This is a research prototype — clarity, observability, and ease of
modification matter more than performance. See `ARCHITECTURE.md` for the
design rationale.

## What's new in v0.2

- **Real retrieval verifier.** The retrieval stub is replaced with a
  working DuckDuckGo / Tavily / SerpAPI backed verifier that fetches
  search snippets, asks an LLM judge, and caches results in SQLite.
- **Role predicates.** `holds_role`, `is_a`, `headed_by`, `member_of`,
  `succeeded_by`, `preceded_by` cover the role-claim gap that previously
  caused the extractor to misuse `believes`.
- **Granular `verification_status`.** Distinguishes
  `unverifiable_in_principle` from `unverifiable_pending_implementation`,
  and adds `routing_anomaly` for caught extractor errors.
- **Aggressive corrector.** Hedges unverified claims, softens unverifiable
  predictions, and replaces contradicted facts. Decides per claim;
  batches all interventions into one rewrite call.

> **v0.2 requires a fresh DB.** The schema enum widened — old SQLite
> files are not compatible. Run `python scripts/reset_db.py` (or click
> "Reset DB" in the UI) before first use.

## Setup

```bash
git clone <this-repo> aedos && cd aedos
uv sync                        # or: pip install -e ".[dev]"
cp .env.example .env           # then paste your ANTHROPIC_API_KEY
python -m src.app              # serves http://127.0.0.1:8000
```

Open `http://127.0.0.1:8000` in a browser.

### What to try first

1. Type **"I like peanut butter."** — watch the trace panel show the
   extracted `(user, likes, peanut butter)` claim being stored.
2. Type **"Do I like peanut butter?"** — the assistant answers from the
   stored fact; the trace shows a store lookup boosting the existing fact.
3. Type **"How many p's are in strawberry?"** — the assistant will likely
   say 3 (the model's favorite confabulation). The python verifier
   contradicts it, the corrector rewrites the response to 0, and the UI
   shows both versions.

## Running tests

```bash
pytest                         # fast; LLM calls are mocked
RUN_API_TESTS=1 pytest         # also hit the real Anthropic API once
```

## Adding a new predicate

1. Append an entry to `predicates.yaml` (see existing entries for the
   field shape).
2. If the new predicate is python-verifiable, add a `verify_<name>`
   function in `src/verifiers/python_verifiers.py`, register it in the
   `VERIFIERS` dict, and add tests in `tests/test_verifiers.py`.
3. Restart the app. The extractor's tool schema is rebuilt from the
   registry at startup, so the LLM will immediately know about the new
   predicate.

See `CLAUDE.md` for step-by-step guidance.

## Optional environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | Anthropic API key for chat / extraction / corrector / judge calls |
| `AEDOS_DB_PATH` | `aedos.db` | SQLite file location |
| `AEDOS_EXTRACTOR_MODEL` | `claude-opus-4-7` | Model for the claim extractor |
| `AEDOS_CHAT_MODEL` | `claude-opus-4-7` | Model that generates assistant drafts |
| `AEDOS_CORRECTOR_MODEL` | `claude-opus-4-7` | Model for both corrector rewrites and the retrieval judge. Haiku 4.5 is a good cost-saver here |
| `TAVILY_API_KEY` | (none) | If set, retrieval uses Tavily — usually higher-quality results than the DDG fallback |
| `SERPAPI_KEY` | (none) | If set and Tavily isn't, retrieval uses SerpAPI |
| `AEDOS_RETRIEVAL_CACHE_TTL_HOURS` | `24` | TTL for the SQLite-backed retrieval cache. Set to `0` to disable caching |

When neither `TAVILY_API_KEY` nor `SERPAPI_KEY` is set, retrieval scrapes
DuckDuckGo's HTML endpoint. This works without keys but is rate-limited
and historically flaky — failures surface as
`unverifiable_pending_implementation` with a `retrieval_error` flag in
the trace.

## Resetting state

```bash
python scripts/reset_db.py     # wipes the SQLite file and recreates schema
```

Or click **Reset DB** in the UI header.

## Layout

```
predicates.yaml     — human-editable vocabulary of ~30 predicates
src/
  fact_store.py     — SQLite wrapper (facts, turns, pipeline_events)
  predicate_registry.py
  extractor.py      — LLM claim extraction via forced tool use
  router.py         — dispatches claims to verifiers; writes to store
  verifiers/        — python verifiers, store lookup, retrieval stub
  corrector.py      — rewrites assistant responses to reflect corrections
  pipeline.py       — orchestrates a full turn; logs pipeline_events
  llm_client.py     — Anthropic SDK wrapper with prompt caching
  app.py            — FastAPI backend
static/             — single-page UI (vanilla JS)
tests/              — one test file per component + integration scenarios
scripts/reset_db.py
```
