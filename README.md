# Aedos

A claim-verification and conversational-memory research prototype. Every
factual claim the assistant makes is extracted, routed to a type-matched
verifier, and either confirmed, rejected, or flagged before it reaches the
user. User-stated facts are stored as ground truth for the conversation.

This is a research prototype — clarity, observability, and ease of
modification matter more than performance. See `ARCHITECTURE.md` for the
design rationale.

## What's new in v0.4

- **Code-generated python verification.** The hand-written verifier
  registry (`has_count`, `is_anagram_of`, etc.) is gone. When a claim
  is python-routed, three LLM calls produce code that resolves it: a
  triage call decides if it's resolvable, a prompt-builder articulates
  a NEUTRAL question (omitting the asserted value), and a code-writer
  generates a python script that the sandbox runs and the comparator
  evaluates. The firewall — separating the LLM that decides what to
  verify, the LLM that articulates the question, and the LLM that
  writes the code — keeps confirmation bias out of code generation.
- **Predicate overrides on patterns.** `relational.reverse_of`,
  `is_anagram_of`, `contains_substring`, `equals`, `greater_than` now
  route to python via `predicate_overrides` while the rest of
  `relational` keeps its retrieval default.
- **`patterns.yaml` simplification.** `python_when_predicate_supported`
  is gone; patterns just use `python` (with the triage stage as the
  gate).
- **New trace UI block.** Per python-routed claim, a code-generation
  block shows triage → prompt → code → execution → comparison, with a
  collapsed-by-default details panel and warnings for prompt leakage,
  slow runs, and stderr.

> **v0.4 is schema-compatible with v0.3.** No DB reset is required.
> Code that imported from `src.verifiers.python_verifiers` will need
> updating — that module is removed.

## What's new in v0.3

- **Pattern-based representation.** Closed predicate vocabulary is gone.
  Facts are now `(pattern, predicate, slots)` where pattern is one of
  eight bounded structural types and predicate labels are free-form
  within a pattern. New relations like `is_obsessed_with` or
  `was_succeeded_by_in_office` work without code changes — they inherit
  the pattern's verification semantics.
- **Slots-aware retrieval queries.** Multi-attempt query strategies per
  pattern, with explicit fallbacks. The verifier never prepends "current"
  to queries — temporal scope comes from `valid_from` / `valid_until`
  slots. Each attempt logs to `pipeline_events` for visibility.
- **Verifier-failure vs verifier-inconclusive split.** The v0.2 corrector
  hedged true claims when retrieval failed. v0.3 distinguishes
  `retrieval_inconclusive` (judge said insufficient evidence — hedge) from
  `retrieval_failed` (search errored or returned nothing — do NOT hedge,
  just log a warning). Verifier failure is not evidence of uncertainty.
- **Schema change.** `facts.subject/predicate/object/object_type` →
  `facts.pattern/predicate/slots(JSON)`. Plus a `facts_flat` view that
  projects subject/object for the UI.

> **v0.3 requires a fresh DB.** The schema changed — old SQLite files are
> not compatible. Run `python scripts/reset_db.py` (or click "Reset DB"
> in the UI) before first use.

## What's new in v0.2

- **Real retrieval verifier.** The retrieval stub is replaced with a
  working DuckDuckGo / Tavily / SerpAPI backed verifier that fetches
  search snippets, asks an LLM judge, and caches results in SQLite.
- **Role predicates.** `holds_role`, `is_a`, `headed_by`, `member_of`,
  `succeeded_by`, `preceded_by` covered the role-claim gap. (In v0.3
  these are now expressed via the `role_assignment`, `categorical`,
  and `relational` patterns.)
- **Granular `verification_status`.** Distinguishes
  `unverifiable_in_principle` from `unverifiable_pending_implementation`,
  and adds `routing_anomaly` for caught extractor errors.
- **Aggressive corrector.** Hedges unverified claims, softens unverifiable
  predictions, and replaces contradicted facts.

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

In v0.4, predicates are free-form within a pattern. There's no per-
predicate code to write:

- **Inside a pattern that already routes to python** (e.g.
  `quantitative` for new properties like `prime_count` or
  `digit_sum`): just use the predicate label. Triage decides if the
  claim is python-resolvable; if yes, code generation runs; if no, it
  falls back per the pattern's rules.
- **A computable relational predicate** (e.g. `palindrome_of`): add it
  to `relational.predicate_overrides` in `patterns.yaml` with `python`
  as the value. The rest of `relational` keeps retrieval as default.

For new patterns (rare), see `CLAUDE.md` — that requires more changes
than just YAML.

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
patterns.yaml       — eight structural patterns; predicates free-form within each
src/
  fact_store.py     — SQLite wrapper (facts, turns, pipeline_events)
  pattern_registry.py
  extractor.py      — LLM claim extraction via forced tool use
  router.py         — dispatches claims to verifiers; writes to store
  verifiers/
    types.py            — shared VerificationOutcome / VerificationResult
    store_verifier.py   — matches model claims against user-asserted facts
    retrieval_verifier.py
    code_generation/    — v0.4 triage → prompt → code → sandbox → compare
  corrector.py      — rewrites assistant responses to reflect corrections
  pipeline.py       — orchestrates a full turn; logs pipeline_events
  llm_client.py     — Anthropic SDK wrapper with prompt caching
  app.py            — FastAPI backend
static/             — single-page UI (vanilla JS)
tests/              — one test file per component + integration scenarios
scripts/reset_db.py
```
