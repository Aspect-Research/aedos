# Architecture

## Hypothesis

Aedos tests a specific claim: **hallucination is predominantly a failure of
verification, not of knowledge.** If you extract each factual claim a model
produces and route it to a type-matched verifier, you should be able to
suppress hallucination at the claim level without retraining the model.

The system is built to make that hypothesis easy to inspect: every
intermediate step is logged, every decision is traceable, and the vocabulary
of predicates is a small, human-editable file.

## Data flow

```
                                ┌──────────────────┐
   user message  ──────────────▶│  fact_store:     │
         │                      │    turns table   │
         ▼                      └──────────────────┘
  ┌────────────┐   claims
  │ extractor  │ ──────────┐
  │ (user)     │           │
  └────────────┘           ▼
                     ┌──────────┐    ┌─────────────┐
                     │  router  │───▶│ fact_store: │
                     └──────────┘    │ facts table │
                           │         └─────────────┘
                           │                │
                           │   pipeline_events (every stage)
                           ▼                │
                    ┌──────────────┐        │
                    │ llm_client   │◀───────┘
                    │ chat(sys+h)  │  system prompt includes all
                    └──────┬───────┘  currently-valid user facts
                           │
                           ▼ draft
                    ┌────────────┐
                    │ extractor  │
                    │ (model)    │
                    └────────────┘
                           │ claims
                           ▼
                    ┌──────────┐    python_verifier
                    │  router  │───▶ store_lookup
                    │ (model)  │    retrieval_stub
                    └──────────┘    unverifiable_flag
                           │
                   ┌───────┴──────┐
                   ▼              ▼
            no contradictions   contradictions
                   │              │
                   │              ▼
                   │       ┌────────────┐
                   │       │ corrector  │──▶ rewrites draft
                   │       └────────────┘
                   ▼              ▼
            final response to user (+ trace to UI)
```

## Component responsibilities

| Component | Responsibility | Inputs | Outputs |
|---|---|---|---|
| `fact_store` | SQLite. Owns all persistent state: facts, turns, pipeline_events. Validates rows on insert. Handles contradiction lookup and temporal close/reopen. | — | — |
| `predicate_registry` | Loads and validates `predicates.yaml`. Exposes lookups and a prompt-formatted dump. The extractor tool schema enumerates these names. | `predicates.yaml` | `Predicate` objects |
| `extractor` | Single LLM call per message, forced tool use for structured output. Validates each returned claim against the registry; drops ones that don't fit. | text, role | `ExtractionResult` |
| `router` | One code path, one decision table. Dispatches claims to the verifier named by the registry. Writes facts to the store. Emits a `Decision` per claim for logging. | claim, origin | `Decision` |
| `verifiers/python_verifiers` | Deterministic, narrow functions. Each handles exactly one predicate. Returns VERIFIED / CONTRADICTED / INCONCLUSIVE. | claim | `VerificationResult` |
| `verifiers/store_verifier` | Matches a model claim against currently-valid facts in the store. Match / contradiction / miss. | claim, store | `StoreLookupResult` |
| `verifiers/retrieval_stub` | Placeholder for v2. Always inconclusive. | claim | explanation |
| `corrector` | Single LLM call that rewrites the assistant draft given a list of corrections. | draft + corrections | rewritten text |
| `pipeline` | Orchestrator. Runs every stage of a turn in order. Writes a `pipeline_events` row for each stage so the UI can rebuild the trace. | user message | `TurnTrace` |
| `llm_client` | Anthropic SDK wrapper. Three methods: `chat`, `extract_with_tool`, `rewrite`. Applies prompt caching on stable system prefixes. | — | — |
| `app` | FastAPI backend. Endpoints for chat, trace, fact inspector, predicate inspector, reset. Serves the static UI. | HTTP | JSON / HTML |

## Key design decisions

### Bounded vocabulary

The extractor is only allowed to use predicates from the registry. Claims
with unknown predicates are dropped in `extractor._validate` — not stored,
not routed. This is load-bearing: without it, the system's verification
claims become vacuous (the extractor can always just invent a predicate
that makes the claim trivially verifiable).

Adding a predicate is intentionally low-friction (edit YAML, optionally
add a python function) so this constraint isn't painful.

### Two-extractor pattern

Every turn extracts claims twice — once from the user's message, once from
the assistant's draft. Same extractor, same registry, different binding
rules:

- User text: `I`, `my` → `user`
- Assistant text: `you`, `your` → `user`

This symmetry is deliberate. It means user-asserted facts and model-made
claims travel through the same validation and storage path; the router is
the only place where their *origin* matters.

### `pipeline_events` as the trace

Every stage writes a row to `pipeline_events` with a stage name and a JSON
blob. The UI rebuilds the trace panel by fetching those rows verbatim. This
means adding a new pipeline stage doesn't require changes to the UI plumbing
— just log the data, and it shows up.

### User-authoritative claims from the model

A subtle case the spec doesn't spell out: when the *model* asserts a
user-authoritative fact (e.g. "you like peanut butter"), we route it to
store lookup, not user-assertion storage. If the user has said it before,
great — boost the confidence. If they haven't, we store the model's claim
as unverified with low confidence and flag it. We never let the model
fabricate user preferences.

### One primary path

There's exactly one way to run a turn through the pipeline. No
configuration options, no mode flags, no alternate code paths. Tests
exercise that one path thoroughly. When something changes, it's visible.

## Verification status semantics (v0.2)

Every stored fact and every routing `Decision` carries one of these
verification statuses. The corrector reads the status to decide what
intervention (if any) to apply.

| Status | Assigned when | Typical confidence | Corrector behavior |
|---|---|---|---|
| `verified` | Python verifier said VERIFIED, retrieval judge said SUPPORTED, or store lookup matched a user-asserted fact | 0.95–0.99 | noop |
| `contradicted` | A verifier returned CONTRADICTED (python, retrieval, or store) | 0.95–0.99 | **replace**: rewrite using the verified value |
| `user_asserted` | User stated this directly; ground truth for `user_authoritative` predicates | 0.95+ | noop |
| `unverifiable_in_principle` | Predicate's `verification_method` is `unverifiable` (`will_happen`, `might`, `believed_by_many`) | 0.3 | **soften**: use hedging language for definite framing |
| `unverifiable_pending_implementation` | Retrieval errored / returned no results / judge said INSUFFICIENT_EVIDENCE; or a python verifier was inconclusive; or a user-authoritative store lookup missed | 0.4 | **hedge** when confidence < 0.5: insert a verification disclaimer near the claim |
| `routing_anomaly` | A `user_authoritative` predicate was asserted by the model about a non-user subject (e.g. `(Donald Trump, likes, peanut butter)`) | 0.2 | noop **at the content level** — but the pipeline emits a `routing_anomaly_detected` event so the trace UI shows a prominent banner, since this almost always indicates an extractor bug |

The point of distinguishing `unverifiable_in_principle` from
`unverifiable_pending_implementation` is that the first is a property of
the predicate itself (we cannot ever verify a future-tense claim), while
the second is a property of *this run* (the search was rate-limited, the
snippets were thin, the verifier hasn't been written yet). The corrector
applies different prose treatments to each.

## Known limitations

- **Retrieval is v1 — snippet-based, judge-LLM only.** The retrieval
  verifier issues one search (DuckDuckGo, Tavily, or SerpAPI), takes the
  top 1–3 result snippets, and asks an LLM to judge support. It does not
  fetch full pages, does not cross-corroborate across multiple sources,
  and does not pull from structured sources (Wikidata, Wikipedia
  infoboxes). DDG scraping in particular is rate-limited and flaky.
  Failures surface as `unverifiable_pending_implementation` with an
  explicit `error_flag` (`retrieval_error`, `no_results`,
  `judge_parse_error`, `judge_error`) so they're visible in the trace,
  not silently passed through. v2 would add full-page fetch where
  needed, a structured-fact path for high-confidence claims (Wikidata
  for capitals, founders, birth years), and source-ranking + multi-snippet
  corroboration before invoking the LLM judge.

- **Python verifiers are narrow.** `verify_has_count` works only when the
  extractor successfully encodes the count claim as JSON. Natural
  variations ("there are three p-sounds in strawberry") will fall through
  to INCONCLUSIVE. That's acceptable — the system fails loud, not silent.

- **Single conversation.** The `facts`, `turns`, and `pipeline_events`
  tables have no conversation id. Everything is one long conversation
  until `reset_db.py` wipes the file. Adding multi-conversation scoping
  is a schema-level change and deliberately deferred.

- **No streaming.** The chat endpoint blocks on the full turn. Fine for a
  debugging UI; not what you'd ship.

- **No re-verification on edit.** If you change a predicate's
  `verification_method` in the registry while facts already exist,
  historical facts keep their old verification status. `reset_db.py` is
  the intended workflow for this; production would need a re-verify pass.

## What v2 would add

- A retrieval layer that fetches full pages on demand, ranks sources,
  and corroborates across at least two sources before judging
  (replacing the snippet-only v1 path).
- Structured-fact retrieval against Wikidata / Wikipedia infoboxes for
  predicates with stable answers (capitals, birth years, founders).
- Multi-conversation scoping on the core tables.
- Streaming assistant responses with incremental trace updates.
- A policy layer that decides *when* to correct silently vs surface the
  conflict to the user.
- Confidence calibration based on repeated verification outcomes (an
  observed contradiction from a python verifier is much more decisive
  than a retrieval miss).
