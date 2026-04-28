# Aedos

A claim-verification and conversational-memory research prototype. Every
factual claim the assistant makes is extracted, routed to a type-matched
verifier, and either confirmed, rejected, or flagged before it reaches the
user. User-stated facts are stored as ground truth for the conversation.

This is a research prototype — clarity, observability, and ease of
modification matter more than performance. See `ARCHITECTURE.md` for the
design rationale.

## What's new in v0.6 (experimental — autonomous-v0.5.x branch)

- **Pluggable chat-model backend.** `AEDOS_CHAT_MODEL_PROVIDER` selects
  the model under test for hallucination catching: `anthropic` (default,
  any Claude model) or `modal` (GLM-5.1-FP8 served via Modal). The
  infrastructure LLMs (extractor, router, code-writer, judge, corrector)
  always use Anthropic. New `chat_model_call` pipeline event captures
  provider/model/latency uniformly across providers. See
  `src/llm_clients/`.
- **Tier 1 cross-session user store.** `facts.user_id` and `turns.user_id`
  columns. Routing, store lookups, and inserts are all user-scoped. Old
  DBs migrate transparently via ALTER TABLE on first open. Default
  `default_user` for solo dogfooding; multi-user deployments can thread
  any `user_id` through `Pipeline(user_id=...)`.
- **Tier 2 verification cache (`src/cache/`).** Three-stage observation-
  to-action sequence:
    1. Scoping classifier per claim → `user_specific` /
       `session_specific` / `world_fact`. Only world_fact is cache-eligible.
    2. Stability classifier for cache-eligible claims → six TTL bins
       (immutable / decade_stable / years_stable / months_stable /
       days_stable / volatile).
    3. Cache lookup BEFORE retrieval (short-circuits on hit). Cache
       writes AFTER retrieval verdicts (verified / contradicted / 
       inconclusive). Canonical key is case/whitespace/slot-order
       independent, polarity-distinguishing.
  Cache is OFF by default. Enable with `AEDOS_CACHE_TIER2=1` (single
  knob). The granular flags `AEDOS_CACHE_SCOPING` /
  `AEDOS_CACHE_STABILITY` / `AEDOS_CACHE_WRITES` are still available
  as overrides — e.g. `AEDOS_CACHE_TIER2=1 AEDOS_CACHE_WRITES=0`
  enables observation mode (classifiers run, no writes). The Cache
  tab in the trace UI shows live hit rate from pipeline_events plus
  per-stability-class breakdown.
- **Cost telemetry (`src/cost.py`).** Every Anthropic API call records
  tokens + USD cost. End-of-turn `turn_cost` event aggregates by model.
  Pricing table for opus/sonnet/haiku 4.x line + GLM free tier; unknown
  models report pricing_known=False without poisoning the total.
- **Flow View tab.** Single-turn vertical SVG flowchart: chat → extract →
  router (branches per claim) → corrector → final. Color-coded edges by
  outcome. Click any node → switch to Detail View at that stage.
- **Eval harness (`scripts/eval_harness.py`).** Run a corpus through both
  raw chat and full AEDOS pipeline; classify each turn as caught /
  preserved / broken / missed / uncertain. Save to `eval_results/`.
- **Hallucination corpus (`scripts/dogfood_hallucination_corpus.py`).**
  28 adversarial prompts across counting traps, numerical claims about
  obscure entities, composite claims with one wrong detail, fake
  inventions, multi-turn user_authoritative recall, long-tail trivia.
- **PROTOTYPE: unique-value-slot detection.**
  `AEDOS_UNIQUE_VALUE_SLOTS=1` enables a hardcoded check that catches
  the 'user said born in MA in turn N, then said born in VA in turn M'
  adversarial pattern. Currently covers `spatial_temporal.was_born_in`
  only — operator decides whether to extend. New `RoutingOutcome.
  USER_CONTRADICTED_SELF` value when triggered.

> **Real-API validation 2026-04-28** (Anthropic Opus 4.7, ~$1 total):
> all five LLM components (router, scoping classifier, stability
> classifier, extractor verbatim rule, end-to-end Saturn-moons
> hallucination catch) PASSED. The whole v0.5/v0.6 pipeline works
> on real LLM calls. See `autonomous_state/OBSERVATIONS.md`
> "COMPREHENSIVE REAL-API VALIDATION 2026-04-28" for the full table.

## What's new in v0.5

- **LLM-based verification routing.** Patterns no longer determine the
  verification method. A single LLM router (`src/llm_router.py`) decides
  per-claim which method applies: `python`, `python_with_canonical_constants`,
  `retrieval`, `user_authoritative`, or `unverifiable`. Date arithmetic,
  internal-consistency checks, structural string properties, and many
  other claim types that used to default to retrieval (or be silently
  unverifiable) now route to python.
- **Predicate overrides removed.** `relational.predicate_overrides` is
  gone, along with the `verification_method` field on every pattern.
  The router reasons about claims directly, not about labels.
- **Triage merged into routing.** The v0.4 Stage 1 triage is gone — the
  LLM router has already decided python-verifiability before code
  generation runs. Code-generation false positives surface as
  `code_execution_failed` or `comparison_error` rather than triggering
  a fall-through.
- **Canonical-constants cross-check.** When the router routes to
  `python_with_canonical_constants` (the model needs a small stable
  reference like the list of US states), the code-generation pipeline
  runs twice at different temperatures and compares. Disagreement
  surfaces a warning and falls back; agreement carries through.
- **Trace UI shows the routing decision.** Each verification block now
  leads with the routing method, reason, and confidence; sub-0.7
  confidence renders a yellow warning. Cross-check disagreements show
  both code generations side-by-side.

> **v0.5 is schema-compatible with v0.4 / v0.3.** No DB reset is required.
> Code that read `pattern.verification_method`, `predicate_overrides`,
> `verification_rules`, or `flag_non_user_as_anomaly` will need updates —
> those fields are gone.

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

In v0.5, predicates are free-form within a pattern and the LLM router
decides verification per claim. There's nothing to add for a new
predicate — the extractor will produce it under whichever pattern fits,
and the router will pick a method based on the claim's content. If the
router doesn't classify the predicate the way you expected, the fix is
in the router prompt (`src/llm_router.py`), not in YAML.

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
patterns.yaml         — 8 structural patterns; predicates free-form within each
src/
  fact_store.py       — SQLite wrapper (facts + user_id, turns + user_id,
                        pipeline_events, retrieval_cache, verification_cache)
  pattern_registry.py
  extractor.py        — LLM claim extraction via forced tool use
                        (with verbatim rule + substitution detector)
  llm_router.py       — v0.5 LLM-based per-claim routing classifier
  router.py           — dispatches claims to verifiers; writes to store
  pipeline.py         — orchestrates a full turn; logs pipeline_events
  corrector.py        — rewrites assistant responses to reflect corrections
  llm_client.py       — Anthropic SDK wrapper + cost ledger
  cost.py             — per-call cost accounting (Anthropic + Modal pricing)
  app.py              — FastAPI backend (chat + inspector + cache endpoints)
  llm_clients/        — chat-model backends (anthropic, modal/GLM-5.1-FP8)
  cache/              — v0.6 Tier 2 verification cache
    scoping_classifier.py    — user / session / world classifier
    stability_classifier.py  — TTL bin classifier
    verification_cache.py    — canonicalize + lookup + write
  verifiers/
    types.py                  — VerificationOutcome / VerificationResult
    store_verifier.py         — match against user-asserted facts
    retrieval_verifier.py     — slots-aware retrieval + judge (DDG/Tavily/SerpAPI)
    code_generation/          — v0.5 prompt → code → sandbox → compare
static/                — single-page UI (vanilla JS, vanilla CSS, no build step)
tests/                 — one test file per component + integration scenarios
scripts/
  reset_db.py                   — wipe + recreate schema
  smoke_test_glm.py             — 3-prompt smoke test against GLM
  dogfood_glm.py                — 17-prompt dogfood (provider-flagged)
  dogfood_hallucination_corpus.py — 28 adversarial prompts
  eval_harness.py               — raw vs aedos comparison
  summarize_corpus_run.py       — analyze a run's catches/hedges
  analyze_substitutions.py      — extractor substitution rate measurement
  analyze_costs.py              — per-turn LLM cost breakdown by model
  analyze_cache.py              — cache hit rate + most-reused canonical keys
```
