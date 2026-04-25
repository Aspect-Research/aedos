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

## Representation: patterns vs predicates (v0.3)

Aedos's earlier releases used a closed predicate vocabulary. v0.3 moves
to a pattern-based representation. The motivation is straightforward.

### The two failure modes we wanted to avoid

**Closed vocabulary doesn't scale.** Every conversation introduces
relations that don't fit the existing list. Adding predicates is human
work; the rate of new conversational claim-types vastly exceeds the rate
at which a curated vocabulary can grow. v0.2 had ~37 predicates and
already routinely encountered claims with no good fit, leading to one of
two bad outcomes: forcing a poor fit (the `(Donald Trump, believes,
"is the U.S. president")` case from the v0.2 trace) or abstaining when a
real fact was being stated.

**Open vocabulary doesn't work either.** Letting the extractor invent
predicates reproduces the canonicalization problem that broke the
semantic web — three conversations later you have `likes`, `loves`,
`enjoys`, `is_fond_of`, and `has_positive_attitude_toward` all
representing the same relation, none of which can be queried as a unit
or routed consistently.

### Why patterns are the middle path

A **pattern** is a bounded structural type with declared verification
semantics. There are eight: `role_assignment`, `preference`,
`quantitative`, `spatial_temporal`, `categorical`, `relational`,
`event`, `propositional_attitude`. A fact is a pattern instance
populated with slot values, plus a free-form predicate label that names
the relation descriptively.

The verification method comes from the pattern, not the predicate. So
`adores`, `is_obsessed_with`, and `tolerates` are all preference
predicates and inherit preference's semantics — user-authoritative when
the agent is the user, unverifiable otherwise. No code change is needed
to support a new predicate; the structural type carries the meaning.

This lets vocabulary grow organically per conversation while keeping
verification predictable.

### Domains covered

| Pattern | Domain | Example |
|---|---|---|
| `role_assignment` | named offices/positions, time-bounded | "Trump is the 47th President" |
| `preference` | user affinities | "I love peanut butter" |
| `quantitative` | numeric measurements | "Strawberry has 3 r's" |
| `spatial_temporal` | location, residence, employment | "I live in Williamstown" |
| `categorical` | profession / kind / class | "Marie Curie was a physicist" |
| `relational` | binary directed relations | "Trump defeated Harris in 2024" |
| `event` | discrete occurrences | "Trump was inaugurated on Jan 20, 2025" |
| `propositional_attitude` | beliefs, plans, hopes, feelings | "I think the Fed will cut rates" |

### Known scope limits

The pattern set is deliberately bounded. It represents factual claims
with well-defined verification procedures. Things outside that scope
are flagged as out-of-scope by the extractor, not represented:

- **Aesthetic judgments** ("the sunset was beautiful"). Not verifiable,
  not a preference of a specific agent, not a category. Abstained.
- **Counterfactuals** ("if Biden had run, he would have lost"). No
  pattern represents counterfactual conditionals; out of scope for v0.3.
- **Complex causal claims** ("X caused Y because of Z"). Multi-step
  causation isn't captured. Such claims abstain unless they reduce
  cleanly to one of the eight patterns.
- **Process descriptions** ("photosynthesis converts sunlight"). Generic
  scientific process descriptions don't fit the patterns; abstained.

This is a defensible scope. We are not trying to represent everything
sayable — we are representing the kinds of claims for which an
automated verification procedure makes sense.

## Verification status semantics (v0.2/v0.3)

Every stored fact and every routing `Decision` carries one of these
verification statuses. The corrector reads the status to decide what
intervention (if any) to apply.

| Status | Assigned when | Typical confidence | Corrector behavior |
|---|---|---|---|
| `verified` | Python verifier said VERIFIED, retrieval judge said SUPPORTED, or store lookup matched a user-asserted fact | 0.95–0.99 | noop |
| `contradicted` | A verifier returned CONTRADICTED | 0.95–0.99 | **replace**: rewrite using the verified value |
| `user_asserted` | User stated this directly | 0.95+ | noop |
| `unverifiable_in_principle` | Pattern's resolved method is `unverifiable` (e.g. `propositional_attitude` for non-user agent) | 0.3 | **soften** definite framing |
| `retrieval_inconclusive` | **v0.3 split:** verifier RAN, search returned snippets, judge said INSUFFICIENT_EVIDENCE | 0.4 | **hedge** — there's positive evidence of uncertainty |
| `retrieval_failed` | **v0.3 split:** verifier didn't get useful signal — network error, no results, judge unparseable | 0.4 | **noop** — verifier failure is not evidence of uncertainty about the *claim*; the pipeline emits a `verifier_failure` event instead |
| `unverifiable_pending_implementation` | Python verifier inconclusive, store-lookup miss, etc. | 0.4 | **hedge** when confidence < 0.5 |
| `routing_anomaly` | Pattern with `flag_non_user_as_anomaly` got a non-user agent | 0.2 | noop **at content level** — pipeline emits a `routing_anomaly_detected` event with the offending slot |

The `retrieval_inconclusive` / `retrieval_failed` split is the v0.3 fix
for a v0.2 bug: hedging on retrieval failure was making the system more
wrong. Adding "I think" to a possibly-true claim is worse than leaving
it as-is.

_(Verification status semantics moved to the v0.3 section above.)_

## Known limitations

- **Retrieval is v1 — snippet-based, judge-LLM only.** The retrieval
  verifier issues one or more searches (DuckDuckGo, Tavily, or SerpAPI),
  takes the top 1–3 result snippets, and asks an LLM to judge support.
  It does not fetch full pages, does not cross-corroborate across
  multiple sources, and does not pull from structured sources (Wikidata,
  Wikipedia infoboxes). v0.3 added a multi-attempt query strategy and the
  `retrieval_inconclusive` / `retrieval_failed` split, but the underlying
  retrieval pipeline is still snippet-based. v2 would add full-page fetch
  where needed, a structured-fact path for high-confidence claims, and
  source-ranking + multi-snippet corroboration before the judge.

- **Scope of representation.** The eight patterns capture factual claims
  with well-defined verification procedures. Aesthetic judgments,
  counterfactuals, and complex causal claims are out of scope and will
  abstain (see "Representation: patterns vs predicates" → Known scope
  limits above). This is a deliberate design decision, not a TODO.

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
