# Aedos

A claim-verification and conversational-memory research prototype. Every
factual claim the assistant makes is extracted, routed to a type-matched
verifier, and either confirmed, rejected, or hedged before it reaches the
user. User-stated facts are stored as ground truth for the conversation.

The working hypothesis: hallucination is predominantly a failure of
*verification*, not of knowledge. Claude already knows that strawberry has
three Rs — what it lacks is a reflex to check before answering. Aedos adds
that reflex as an explicit pipeline.

This is a research prototype — clarity, observability, and ease of
modification matter more than performance. See `ARCHITECTURE.md` for the
full design rationale and `CHANGELOG.md` for the version-by-version
evolution.

## How a turn flows

```
user message
  ├─> extractor ────> store user-stated facts (treated as ground truth
  │                   for self-attributes; verified for world claims)
  └─> chat model ──> assistant draft
        └─> extractor ──> per-claim:
                            llm router  → python  / canonical-constants
                                          retrieval / user-authoritative
                                          unverifiable
                            tier lookup → microtheory > user-store > cache
                            verifier    → code-gen + sandbox / wikipedia
                                          retrieval + judge / store match
                            corrector   → holistic rewrite given the full
                                          per-claim verification ledger
                                          and the user's question
                          → final reply
```

Every stage emits a `pipeline_events` row. The trace UI reads from that
table — there is no special-cased rendering for stages that don't log.

## Setup

```bash
git clone https://github.com/asashepard/aedos && cd aedos
uv sync                        # or: pip install -e ".[dev]"
cp .env.example .env           # paste ANTHROPIC_API_KEY (and OPENAI_API_KEY
                               # if you want the cheap-mini-class internal calls;
                               # see "Per-purpose model routing" below)
python -m src.app              # serves http://127.0.0.1:8000
```

### What to try first

1. **`I like peanut butter.`** — the extractor stores
   `(user, likes, peanut butter)`; the trace shows the user-side claim
   landing in the user store.
2. **`Do I like peanut butter?`** — the assistant answers from the
   stored fact; the trace shows the tier-1 microtheory hit short-
   circuiting verification.
3. **`How many p's are in strawberry?`** — the chat model will
   probably say 3 (a famous confabulation). The router picks `python`,
   the code generator writes `print('strawberry'.count('p'))`, the
   sandbox runs it, the comparator says 2, and the corrector
   rewrites the response.
4. **`What time is it in Cairo right now?`** — the router picks
   `python`, the code generator reaches for `zoneinfo`, and the
   verifier confirms the hour against the system clock + IANA
   timezone database.

## Per-purpose model routing

Every internal LLM call carries a `purpose` tag (`extractor:user`,
`router`, `code_writer`, `retrieval_judge`, `corrector`,
`cache_classify`, etc.). The dispatcher (`src/llm_client.py`) resolves
each purpose to a concrete model via `DEFAULT_MODEL_BY_PURPOSE` — the
default routes the cheap mini-class GPTs to internal work and reserves
Anthropic Haiku for the chat slot:

| Purpose | Default model |
|---|---|
| `chat` | `claude-haiku-4-5` (locked; override only via `AEDOS_CHAT_MODEL`) |
| `extractor:user` / `extractor:assistant` | `gpt-4.1-mini` |
| `router` | `gpt-4.1-mini` |
| `prompt_builder` / `code_writer` / `retrieval_judge` / `corrector` | `gpt-4.1-mini` |
| `cache_classify` / `cache_scoping` / `cache_stability` | `gpt-4.1-nano` |

Override any entry per-process via `AEDOS_MODEL_<purpose>=<model_id>`.
Anthropic prompt caching (`cache_control: ephemeral` on stable system
prompts) and OpenAI automatic caching (gpt-4.1 / gpt-4o family on
prompts ≥1024 tokens) are both on; the cost ledger reads
provider-specific cache-tier token counts so per-turn cost reflects
what each provider actually bills.

## Running tests

```bash
pytest                         # ~600 fast tests, LLM calls mocked
RUN_API_TESTS=1 pytest         # also runs the live calibration tests
                               # (router / scoping / stability / wikipedia)
```

## Adding a new predicate

You don't. Predicates are free-form within a pattern; the extractor
emits whatever predicate label fits, and the LLM router classifies the
claim's structure to pick a verification method. If the router
misroutes a predicate, the fix is in the router prompt
(`src/llm_router.py`) — add a worked example and a calibration case in
`tests/test_routing_calibration.py`.

For new patterns (rare and load-bearing), see `CLAUDE.md`.

## Resetting state

```bash
python scripts/reset_db.py     # wipes the SQLite file and recreates schema
```

Or click **Reset DB** in the UI header — it calls the same endpoint.

## Optional environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | Anthropic API key |
| `OPENAI_API_KEY` | — | Required when any purpose routes to a `gpt-*` model (the default config does) |
| `AEDOS_DB_PATH` | `aedos.db` | SQLite file location |
| `AEDOS_CHAT_MODEL` | `claude-haiku-4-5` | Chat-slot model. The UI has no model dropdown; this env var is the only override |
| `AEDOS_MODEL_<purpose>` | — | Override any per-purpose model — e.g. `AEDOS_MODEL_router=claude-haiku-4-5` |
| `AEDOS_RETRIEVAL_CACHE_TTL_HOURS` | `24` | TTL for the per-query Wikipedia snippet cache. `0` disables |

## Layout

```
patterns.yaml           — 8 structural patterns; predicates free-form within each
src/
  app.py                — FastAPI backend + static-file serving
  pipeline.py           — orchestrates a full turn; emits every pipeline_event
  fact_store.py         — SQLite wrapper; facts / turns / pipeline_events /
                          retrieval_cache / verification_cache
  pattern_registry.py   — loads patterns.yaml, formats the extractor prompt
  extractor.py          — LLM claim extraction via forced tool use
  llm_router.py         — per-claim verification routing (the LLM picks
                          python / canonical-constants / retrieval /
                          user-authoritative / unverifiable)
  router/               — Decision dispatcher (4-file package)
  llm_client.py         — Anthropic SDK wrapper + per-purpose dispatcher
  openai_client.py      — OpenAI SDK wrapper (cost lands on the same ledger)
  llm_clients/          — chat-slot backends (currently anthropic_chat only)
  cost.py               — per-call cost accounting; reads Anthropic cache-tier
                          + OpenAI cached_tokens for accurate cross-provider $
  corrector.py          — holistic rewrite of the assistant draft given the
                          full per-claim verification ledger
  session_markers.py    — conversation-scoped microtheory markers
  cache/                — Tier 2 verification cache
    gate.py                     — single owner of scoping + lookup + write
    classify_combined.py        — single-call scope+stability classifier
    scoping_classifier.py       — user / session / world classifier (legacy)
    stability_classifier.py     — TTL bin classifier (legacy)
    verification_cache.py       — canonicalize + lookup + write
  verifiers/
    types.py                    — VerificationOutcome / VerificationResult
    store_verifier.py           — match against user-asserted facts
    retrieval_verifier.py       — slots-aware Wikipedia retrieval + judge,
                                  with retry-on-INSUFFICIENT_EVIDENCE +
                                  LLM query reformulation
    comparative.py              — superlative-claim detector + query templates
    code_generation/            — prompt → code → sandbox → compare
    scrapers/                   — Wikipedia MediaWiki API client
static/                — single-page UI (vanilla JS, vanilla CSS, no build step)
tests/                 — one test file per component + integration scenarios
scripts/
  reset_db.py            — wipe + recreate schema
  eval_harness.py        — raw vs aedos comparison across a corpus
  analyze_cache.py       — cache hit rate + most-reused canonical keys
  analyze_costs.py       — per-turn LLM cost breakdown by model + purpose
```

## Pointers

- `ARCHITECTURE.md` — design rationale + full data-flow diagram
- `CLAUDE.md` — guidance for sessions working in this repo (load-bearing
  invariants, how to add a predicate / pattern, how to debug a turn)
- `CHANGELOG.md` — version-by-version evolution from v0.1 to v0.12
