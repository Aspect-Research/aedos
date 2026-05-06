# Architecture

## Hypothesis

Aedos tests one claim: **hallucination is predominantly a failure of
verification, not of knowledge.** If you extract every factual claim a
model produces and route it to a type-matched verifier, you should be
able to suppress hallucination at the claim level without retraining
the model.

The system is built to make that hypothesis easy to inspect: every
intermediate step is logged, every decision is traceable, and the
vocabulary of patterns is a small human-editable file.

For the v0.1→v0.6 evolution see [`CHANGELOG.md`](CHANGELOG.md). This
document describes the system as it stands.

## Data flow

```
user message
    │
    ▼
extract user claims ──► route ──► fact_store (user-asserted)
    │
    ▼
build chat context (history + user-asserted facts)
    │
    ▼
chat backend (Anthropic, operator-selectable model) → assistant draft
    │
    ▼
extract assistant claims
    │
    ▼
CacheGate.classify (per claim: scope + stability + cache eligibility)
    │
    ▼
router.route (per claim) ──► verifier (code-gen / retrieval / store)
    │                              │
    │                              ▼
    │                        Decision (verified/contradicted/...)
    ▼
CacheGate.maybe_write (cacheable retrieval verdicts)
    │
    ▼
corrector (replace / hedge / soften based on Decisions)
    │
    ▼
final response (+ all events written to pipeline_events)
```

## Component map

| Component | Responsibility |
|---|---|
| `fact_store` | SQLite. Owns facts, turns, pipeline_events, verification_cache. Subscriber registry for SSE event push. |
| `pattern_registry` | Loads `patterns.yaml` (8 structural patterns; predicates free-form within). |
| `extractor` | One LLM call per message, forced tool use. Validates each claim against the registry; flags substitutions where source_text isn't in the input. |
| `llm_router` | One LLM call per assistant claim. Returns the verification method: `python` / `python_with_canonical_constants` / `retrieval` / `user_authoritative` / `unverifiable`. |
| `router` | Dispatches each claim to the verifier the LLM router picked. Cache-eligible claims hit the cache before retrieval. Builds the `Decision`. |
| `verifiers/code_generation` | prompt builder → code writer → sandbox → comparator. Cross-check at temp 0.0 / 0.3 for canonical-constants claims. |
| `verifiers/retrieval_verifier` | Slots-aware Wikipedia search; walks the pattern's query strategy on INSUFFICIENT_EVIDENCE and runs one LLM-reformulated query as a fallback. Tolerant LLM judge. |
| `verifiers/store_verifier` | Matches a model claim against user-asserted facts (per-user). |
| `cache` | `VerificationCache` (table I/O), `CacheGate` (single owner of scoping + stability + lookup + write), `canonicalize_claim_key` with stem normalization, `semantic_lookup` for shape-based fallback. |
| `corrector` | One LLM call. Plans interventions (replace / hedge / soften / remove) per Decision and applies them all in one rewrite, demanding internal consistency with verified values. |
| `pipeline` | Orchestrator. Six clearly-named stage methods inside `_run_turn_inner`. |
| `llm_client` | Anthropic SDK wrapper + per-purpose dispatcher. Chat model is locked to `DEFAULT_MODEL_BY_PURPOSE['chat']` (Haiku 4.5) — operators override via `AEDOS_CHAT_MODEL`, not via the UI. Drops `temperature` for opus-4-7. |
| `openai_client` | OpenAI SDK wrapper used when any purpose resolves to a `gpt-*` model. Cost lands on the same per-instance ledger. |
| `llm_clients/` | Chat-slot backend (currently `AnthropicChatBackend` only — the Modal/GLM backend was removed in v0.7.15). |
| `cost` | Per-million-token pricing constants + per-call recording + end-of-turn aggregation. Reads provider-specific cache-tier token counts (Anthropic `cache_creation_input_tokens` / `cache_read_input_tokens`, OpenAI `prompt_tokens_details.cached_tokens`) so per-turn cost reflects what each provider actually bills. |
| `app` | FastAPI. `POST /api/chat`, `POST /api/chat/stream` (SSE), `/api/turns`, `/api/trace/{id}`, `/api/facts`, `/api/patterns`, `/api/cache`, `/api/health`, `/api/reset`. Serves the static UI. |

## Key design choices

### Bounded pattern set, free-form predicates

8 patterns (`patterns.yaml`): `preference`, `propositional_attitude`,
`spatial_temporal`, `categorical`, `role_assignment`, `relational`,
`quantitative`, `event`. Each declares slots + a query strategy.

Within a pattern, predicates are free-form — the extractor can produce
`is_child_of`, `child_of`, `son_of`, all valid as long as they sit
under the right pattern. Verification semantics come from the pattern,
not the predicate label. This is what made v0.3 scale where v0.1's
closed predicate vocabulary couldn't.

### Two-extractor symmetry

Same extractor runs over the user message and the assistant draft.
Same registry, same validation. The router is the only place origin
matters: user-origin claims update the fact store as ground truth;
model-origin claims get verified.

### `pipeline_events` is the trace

Every stage writes a row. The trace UI fetches those rows verbatim.
Adding a new event type is one constant in `PIPELINE_STAGES` + one
optional UI renderer; the plumbing stays the same.

The 30 stages currently in the enum cover the main pipeline plus
fine-grained code-generation, cache, routing, cost, and warning
events. SSE pushes them live during a turn.

### Verification routing (LLM, not rules)

v0.4 used a YAML rule walk per claim. v0.5 replaced it with a single
LLM call (`src/llm_router.py`) that picks the method. Worked examples
in the system prompt cover boundary cases (multi-claim arithmetic
around retrieved values, external-string operations that look like
python but need retrieval, canonical references that need
cross-check).

### Code-generated python verification

Four-stage pipeline. The deliberate firewall: the **code writer never
sees the asserted value**. It only sees a NEUTRAL prompt that asks the
question. Without this, the model writes code that confirms the
assertion rather than computes the answer.

For canonical-constants claims (US states, days of the week, ASCII
tables), `verify_with_cross_check` runs the pipeline twice at
temperatures 0.0 and 0.3. Disagreement surfaces as
`canonical_constants_disagreement` — guards against the LLM emitting a
subtly wrong reference.

### Tier 2 verification cache (always on)

`CacheGate` is the single owner of the cache lifecycle:

  1. **Classify** (per claim, before routing): scoping classifier
     decides `user_specific` / `session_specific` / `world_fact`
     (only world_fact is cache-eligible); stability classifier picks
     a TTL bucket from immutable down to volatile.
  2. **Lookup** (during routing, before retrieval): exact key match,
     then semantic-shape fallback (Jaccard predicate-token overlap
     anchored on identity slots) to catch spelling-variant predicates
     like `child_of` ↔ `is_child_of`.
  3. **Write** (after verification): cacheable retrieval verdicts
     (verified / contradicted / inconclusive) get persisted with the
     stability TTL.

`canonicalize_claim_key` strips common stems (`is_`, `was_`, `has_`,
…) before keying so equivalent predicates collide deterministically.

Hit short-circuits retrieval entirely. The Cache tab in the inspector
drawer shows live hit-rate from `pipeline_events` plus the entry
table.

### Single model selection (per turn)

The chat model is **locked to Haiku 4.5** at construction time
(`DEFAULT_MODEL_BY_PURPOSE['chat']`). The chat UI no longer offers a
model dropdown — operators who need to swap the chat model do so via
`AEDOS_CHAT_MODEL` in `.env`, not via the UI. Internal calls
(extractor, router, code-writer, judge, corrector, scoping, stability,
canonical-constants cross-check) follow `DEFAULT_MODEL_BY_PURPOSE`
independently — the default routing puts cheap mini-class GPTs on all
internal work. Override any entry per-process via
`AEDOS_MODEL_<purpose>=<model_id>`.

### Live progressive UI

`POST /api/chat/stream` is SSE. `FactStore.register_event_subscriber`
+ a 2KB padded preamble defeats Chrome's initial chunk buffering.

The UI shows a 5-step progress chart in the chat panel (chat model →
extraction → verification → correction → final). Each step renders
when its event lands, with an animated "thinking" placeholder for the
next expected step. Click any landed step to expand its full detail
inline.

The Inspector drawer (button top-right) houses Facts / Patterns /
Cache as a tabbed slide-out.

## Verification status (8 internal, 4 user-facing)

The router emits one of 8 internal `verification_status` values:

| Status | Meaning | Corrector action |
|---|---|---|
| `verified` | confirmed by any path | noop |
| `user_asserted` | user said it; treat as ground truth | noop |
| `contradicted` | disproven; correction object provided | REPLACE |
| `retrieval_inconclusive` | judge said insufficient_evidence | HEDGE |
| `retrieval_failed` | verifier got no useful signal at all | noop (failure isn't evidence) |
| `unverifiable_in_principle` | no method applies | SOFTEN |
| `unverifiable_pending_implementation` | python verifier inconclusive / store-lookup miss / code-execution failed | HEDGE if conf<0.5 |
| `routing_anomaly` | extractor bound a user-subject pattern to a non-user agent | noop (logged separately) |

The UI projects these to 4 buckets via
`Decision.display_status_for(status)`:

  * `verified` (green)
  * `contradicted` (red)
  * `inconclusive` (amber)
  * `not_applicable` (grey)

Routing logic still keys off the 8-status fine grain; the projection
is pure UI sugar.

## Pipeline events

30 named stages in `PIPELINE_STAGES`. The main 5 the UI renders as
explicit progress steps:

  * `chat_model_call` — backend dispatch result
  * `assistant_extraction` — extracted claim list
  * `verification` — Decision per claim
  * `correction` — interventions applied
  * `final` — final response text

Annotation events render under their parent step in the inline detail:

  * `routing_decision` — per claim, before each verification
  * `routing_anomaly_detected`, `verifier_failure`,
    `extractor_substitution_warning` — warnings
  * `retrieval_query_attempt` — one per multi-attempt retrieval query
  * `code_prompt_built`, `code_prompt_leakage_detected`,
    `code_generated`, `code_executed`, `code_unusual_behavior`,
    `code_comparison` — code-gen sub-stages
  * `canonical_constants_cross_check`,
    `canonical_constants_disagreement` — cross-check
  * `cache_scoping_decision`, `cache_stability_decision`,
    `cache_lookup`, `cache_write` — cache lifecycle
  * `turn_cost` — end-of-turn aggregate

## Retrieval providers (priority order)

  1. Wikipedia (MediaWiki API; no key, no meaningful rate limit, highest
     quality for the bulk of factual queries)
  2. Tavily (paid; via `TAVILY_API_KEY`)
  3. SerpAPI (paid; via `SERPAPI_KEY`)
  4. DuckDuckGo HTML scrape (free fallback; brittle)

Wikipedia errors fall through silently. Each provider returns
`Snippet(title, snippet, url)` for the judge.

The judge uses a tolerant parser that handles markdown bolds (`**SUPPORTED**`),
"Verdict:" prefixes, preambles, and `NOT SUPPORTED` (negation flips
SUPPORTED ↔ CONTRADICTED).

## What's deliberately not here

  * **No mode flags / configuration options** for the pipeline. One
    primary path. Adding a feature means changing the path.
  * **No cross-conversation entity resolution.** The Tier 2 cache hits
    on canonical key + semantic shape; equivalent claims with
    materially different surface form (e.g. "Marie Curie" vs "Madame
    Curie") miss. Future work; not v0.7.
  * **No retraining feedback loop.** Aedos verifies each turn in
    isolation. The cache accumulates verdicts but they aren't fed back
    to the chat model.
  * **No multi-source corroboration.** A retrieval verdict comes from
    one provider's snippets + one judge call. v2 would require ≥2
    independent sources.

## Module sizes (for grokking the codebase)

| File | Lines |
|---|---|
| `src/router/router.py` | 620 |
| `src/pipeline.py` | 600 |
| `src/verifiers/retrieval_verifier.py` | 670 |
| `src/fact_store.py` | 640 |
| `src/extractor.py` | 440 |
| `src/app.py` | 530 |
| `src/cache/gate.py` | 280 |
| `src/cache/verification_cache.py` | 420 |
| `static/app.js` | 1030 |
| `static/style.css` | 770 |
| `patterns.yaml` | 440 |

Each is sized to be readable in one sitting.
