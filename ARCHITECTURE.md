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
| `pattern_registry` | Loads and validates `patterns.yaml` (8 patterns; predicates free-form within). Exposes lookups and a prompt-formatted dump. | `patterns.yaml` | `Pattern` objects |
| `extractor` | Single LLM call per message, forced tool use. Validates each returned claim against the registry; drops malformed; flags substitutions (source_text-not-in-input, value-not-in-source-text). | text, role | `ExtractionResult` |
| `llm_router` | (v0.5) Per-claim LLM routing classifier. Returns one of `python`, `python_with_canonical_constants`, `retrieval`, `user_authoritative`, `unverifiable`. | claim | `RoutingDecision` |
| `router` | Dispatches claims to the verifier the LLM router picked. Writes facts to the store. Cache-eligible claims hit the v0.6 cache before retrieval. | claim, origin | `Decision` |
| `verifiers/code_generation` | (v0.5) prompt builder → code writer → sandbox → comparator. Cross-check at temp 0.0/0.3 for canonical-constants claims (forces Sonnet 4.6 since Opus 4.7 deprecated temperature). | claim | `CodeGenVerificationResult` |
| `verifiers/store_verifier` | Matches a model claim against user-asserted facts. Scoped by `user_id`. | claim, store, user_id | `StoreLookupResult` |
| `verifiers/retrieval_verifier` | Slots-aware multi-attempt retrieval (DDG with UA rotation, Tavily, SerpAPI) + LLM judge. Result-cached in `retrieval_cache` table. | claim | `RetrievalResult` |
| `cache` | (v0.6 Tier 2) Scoping + stability classifiers + verification cache. Frame: cache, not knowledge base. | claim | `CachedVerdict` or None |
| `corrector` | Single LLM call that rewrites the assistant draft given a list of interventions (replace / hedge / soften / remove). | draft + interventions | rewritten text |
| `pipeline` | Orchestrator. Runs every stage in order. Writes `pipeline_events` rows. Aggregates per-turn cost. | user message | `TurnTrace` |
| `llm_client` | Anthropic SDK wrapper. Three methods: `chat`, `extract_with_tool`, `rewrite`. Records cost per call. Drops `temperature` for opus-4-7. | — | — |
| `llm_clients/` | Pluggable chat-model backends (anthropic / modal-glm). Selected by `AEDOS_CHAT_MODEL_PROVIDER`. | system + messages | response text |
| `cost` | (v0.6) Per-million-token pricing constants + `cost_for_call` + `aggregate_costs`. | model + tokens | `CallCost` |
| `app` | FastAPI backend. Endpoints for chat, trace, fact inspector, pattern inspector, cache inspector, reset. Serves the static UI. | HTTP | JSON / HTML |

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
| `routing_anomaly` | A `_USER_SUBJECT_PATTERNS` pattern (`preference`, `propositional_attitude`) got a non-user agent — almost always an upstream extractor error | 0.2 | noop **at content level** — pipeline emits a `routing_anomaly_detected` event with the offending slot |

The `retrieval_inconclusive` / `retrieval_failed` split is the v0.3 fix
for a v0.2 bug: hedging on retrieval failure was making the system more
wrong. Adding "I think" to a possibly-true claim is worse than leaving
it as-is.

_(Verification status semantics moved to the v0.3 section above.)_

## Code-generated verification (v0.4 / v0.5)

The python verification path used to be a hand-written registry: one
function per predicate in `src/verifiers/python_verifiers.py`, dispatched
by predicate name. That worked for the small set of predicates we
seeded, but it didn't scale. Every new property a model invents
(`prime_count_in_range`, `words_containing_letter_e`, sum of digits,
…) needed a new hand-written function. The cost wasn't the function
itself — it was the long tail. Predicates we hadn't anticipated fell
through to the registered `has_count` and silently returned 0, which
the comparator then accepted as "verified" against any claim of 0.

v0.4 replaced the registry with code generation. v0.5 removed the
triage stage (the LLM router decides python-verifiability up front).
A python-routed claim is now resolved by two LLM calls plus a sandbox
execution and a deterministic comparator:

```
            claim
              │
              ▼
        ┌──────────┐
        │ Stage 1  │  build a NEUTRAL question
        │ extractor│  sees: full claim INCLUDING asserted value
        │   model  │  returns: {prompt, expected_output_type}
        └────┬─────┘  prompt MUST NOT contain asserted value
             │
             │  leak detector scans prompt for stringifications
             │  of the asserted value; retries once on detection
             │
             ▼
        ┌──────────┐
        │ Stage 2  │  write python script
        │ corrector│  sees: ONLY the neutral prompt + expected_type
        │   model  │  returns: source code
        └────┬─────┘
             │
             ▼
        ┌──────────┐
        │ sandbox  │  subprocess, strict limits, no env, empty cwd
        └────┬─────┘
             │  stdout / stderr / duration / timed_out
             ▼
        ┌──────────┐
        │comparator│  parse stdout per expected_type;
        │ (python) │  apply polarity; equal vs not equal
        └────┬─────┘
             │
             ▼
   verified | contradicted | comparison_error | code_execution_failed
```

For `python_with_canonical_constants` claims the pipeline runs twice
(temperatures 0.0 and 0.3) and the cross-check compares the computed
values; disagreement → `canonical_constants_disagreement`.

### The firewall

The system avoids confirmation bias in code-generated verification by
separating the LLM that decides what to verify, the LLM that
articulates the question, and the LLM that writes the code. The
code-writing LLM never sees the model's claimed answer; it only sees
a neutral question. This means the code is written to compute a
value, not to validate a hypothesis. Comparison happens
deterministically outside the LLM, in `comparator.py`.

The firewall is enforced structurally, not by convention:

- `write_code()`'s function signature takes only `(neutral_prompt,
  expected_output_type, llm)`. It cannot accept the original claim;
  passing one is a `TypeError`.
- `compare()` is a pure-python function, not an LLM call. It cannot
  rationalize.

### Known limitation: leak detection is heuristic

The firewall depends on Stage 2 producing a leak-free prompt. Stage 2
is constrained by its prompt and few-shot examples to omit the
asserted value, and a substring-based leak detector retries once on
detection. But sophisticated leakage — the asserted value reformulated
semantically ("the answer is the same as the number of fingers on a
typical hand"), or as a synonym, or in a different base — can slip
through. Hardening this is a target for future work; the current
heuristic catches the obvious cases (the literal value as a digit
string or as a substring of the claim's distinctive subject).

### What this is NOT

- **Not a security sandbox.** The code runs in our own environment;
  the limits in `sandbox.py` keep accidental interactions away from
  AEDOS state. They do not stop a determined attacker.
- **Not a fallback for unfamiliar predicates.** When the LLM router
  decides retrieval, the retrieval verifier handles it; when it
  decides unverifiable, the corrector softens. There is no
  hand-written verifier registry as a backup. The point of v0.4/v0.5
  is to stop maintaining that registry.

## Verification routing (v0.5)

Verification routing is decided per claim by an LLM-based classifier,
not by the claim's structural pattern. `src/llm_router.py` makes a
single call to Sonnet 4.6, which picks one of five methods:

| Method | When |
|---|---|
| `python` | The claim's truth value is computable from its own inputs alone — no external data, no canonical reference. Letter counts, arithmetic, string reversal, primality, palindromes, day-of-week from a date in the claim, duration between two dates in the claim, internal consistency. |
| `python_with_canonical_constants` | Same, but the code may reference small stable canonical references the LLM can emit literally (lists of US states, months, primes under 100, ASCII tables). Triggers a cross-check (two generations at different temperatures). |
| `retrieval` | External data needed — specific people, places, current world state, populations, geographic facts, historical event dates. Anything not computable from the claim's own slots. |
| `user_authoritative` | The claim's subject is the user. Verified by store lookup against user-asserted facts. |
| `unverifiable` | Aesthetic judgments, third-party internal states, future events, model-state claims, probabilistic claims about non-users. |

### Why we removed predicate overrides

v0.4's `predicate_overrides` map (`relational.reverse_of → python`,
`relational.is_anagram_of → python`, …) didn't scale. Every new
computable claim type required editing YAML, and entire categories of
python-verifiable claims (date arithmetic, structural text properties,
internal consistency checks) silently routed to retrieval or
unverifiable because they didn't match a pre-declared category.

The LLM router recognizes python-verifiability from the claim's
content directly. It can route a `quantitative.term_duration` claim
with `valid_from=2017, valid_until=2021, value=4` to python (it's an
arithmetic check on stated values), without anyone editing YAML.

### Multi-claim convention

A claim like "Marie Curie was born in 1867 and died in 1934, so she
lived 67 years" packages an arithmetic check around two retrievable
dates. The router routes this to **python** — the arithmetic is what's
being asserted; the dates are inputs the claim takes as given. If the
dates themselves are wrong, that surfaces as a separate retrieval-class
claim that the extractor would emit independently. The router doesn't
try to split a single claim.

### Canonical-constants cross-check

For `python_with_canonical_constants`, the verifier runs the code-
generation pipeline twice at different temperatures (0.0 and 0.3) and
compares the two computed values. Agreement → accept. Disagreement →
log `canonical_constants_disagreement` and return that status (the
dispatcher treats it as pending). Two generations is a cheap guard
against the LLM emitting a subtly wrong canonical reference.

A more rigorous version would use two different models. Temperature
variation is the v0.5 compromise — it surfaces most divergent
generations without requiring two API surfaces.

### What this routing layer is NOT

- **Not a rule engine.** No pattern→method tables, no predicate
  overrides, no fallback heuristics. The router decides; if it gets it
  wrong, fix the prompt or worked examples.
- **Not heuristic.** The router asks the model to apply judgment, not
  to match labels. The 15 worked examples in `_ROUTER_SYSTEM` cover
  the boundary cases we've found; the calibration test
  (`tests/test_routing_calibration.py`) catches drift.

### Triage is gone

v0.4 had a triage stage as the first call in code generation, deciding
whether the claim was python-resolvable. v0.5 removed it: the LLM
router has already decided python-verifiability before code generation
runs, and a redundant triage call would just risk drift between two
prompts. False positives surface at the sandbox stage as
`code_execution_failed` or at the comparator as `comparison_error`,
which the dispatcher treats as pending (low confidence, hedged by the
corrector if needed).

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

- **Python verification depends on the LLM router + leak detection.**
  v0.5 generates python on demand: the router decides python-eligibility,
  the prompt builder articulates a neutral question, and the code writer
  produces a script the sandbox runs. Every step has limits: the router
  may occasionally over- or under-route; the leak detector is heuristic
  (literal substrings only); the sandbox runs in a subprocess so
  startup cost is non-trivial (~tens to hundreds of ms per claim).
  The system fails loud — `comparison_error`, `code_execution_failed` —
  rather than silently passing.

- **Single conversation.** The `facts`, `turns`, and `pipeline_events`
  tables have no conversation id. Everything is one long conversation
  until `reset_db.py` wipes the file. Adding multi-conversation scoping
  is a schema-level change and deliberately deferred.

- **No streaming.** The chat endpoint blocks on the full turn. Fine for a
  debugging UI; not what you'd ship.

- **No re-verification on edit.** If you change the router's prompt or
  worked examples while facts already exist, historical facts keep
  their old verification status (they were verified by the previous
  routing logic). `reset_db.py` is the intended workflow for this;
  production would need a re-verify pass.

## Verification cache (v0.6 / Tier 2)

The cache is a performance optimization for retrieval, not a knowledge
base. **Cached entries can be wrong, can go stale, and are subject to
eviction. Every cached verdict is provisional.** The framing is load-
bearing: hosting the cache as "fast lookups for things we already
verified" lets us be aggressive about TTLs (cache miss = 1 extra
retrieval call); hosting it as "things AEDOS knows" would push us to
trust stale entries.

### Pipeline

For each model-asserted claim:

1. **Scoping classifier** (one LLM call per claim) → one of
   `user_specific`, `session_specific`, `world_fact`. Only world_fact
   is cache-eligible. user_specific is Tier 1's job; session_specific
   ("this sentence has 7 words", "the previous turn said X") is true-
   only-in-context.
2. **Stability classifier** (only for world_fact) → TTL bin: immutable
   (math, definitions), decade_stable (geography), years_stable
   (political offices), months_stable (recent pop culture), days_stable
   (current events), volatile (prices, weather — don't cache).
3. **Cache lookup** before retrieval. Canonical key is case/whitespace/
   slot-order independent and polarity-distinguishing. Hit → serve the
   cached verdict, skip retrieval. Miss → fall through.
4. **Cache write** after retrieval. verified / contradicted /
   inconclusive verdicts get cached with the stability TTL. Volatile
   doesn't write. Python verdicts don't write (cheap to redo).

### What's deliberately not cached

- User-specific claims (preferences, biography, opinion). Tier 1 is the
  per-user store; the Tier 2 cache is shared.
- Session-specific claims (literal-text counts, "now" / "today" claims).
- Volatile claims (prices, weather).
- Python-routed verdicts. The cost of a cache miss is one DDG search +
  one judge call (~$0.005-0.02). Re-running python verification is a
  subprocess invocation + LLM code-write call (~$0.005). The savings
  from caching python don't justify the cache-management overhead.

### Failure modes

- **Stale entries serving wrong answers.** Bias toward shorter TTLs
  when uncertain — wrong-and-confident is worse than slow-and-correct.
  Stability classifier prompt explicitly biases toward tighter bins.
- **Canonical-key collisions across non-equivalent claims.** Polarity
  is in the key so positive/negative don't collide. But "Tokyo is
  in Japan" and "Tokyo, Japan exists" hash differently — the first
  cached lookup misses the second. Acceptable for v0.6; entity-
  alias resolution is future work.
- **Canonical-key MISSES across semantically-equivalent claims.** Same
  as above. A miss costs one extra retrieval; a wrong-key hit serves
  a wrong answer for the entire TTL window. The asymmetry justifies
  the v0.6 conservative posture.

### Off by default

Single shortcut to enable the whole stack: `AEDOS_CACHE_TIER2=1`
turns on scoping + stability + writes at once. For partial / observation
modes, the granular env vars override the shortcut: e.g.
`AEDOS_CACHE_TIER2=1 AEDOS_CACHE_WRITES=0` enables the classifiers but
not the actual cache reads/writes — useful for measuring what the
classifiers PRODUCE on real claims before risking served verdicts. The
granular vars (`AEDOS_CACHE_SCOPING`, `AEDOS_CACHE_STABILITY`,
`AEDOS_CACHE_WRITES`) can also be set independently in the original
ratcheting order.

Each enabled layer adds one LLM call per assistant claim — opt-in for a
reason. Calibrate the classifier prompts on real claims first, then
enable writes.

The Cache tab in the trace UI shows live hit rate, per-stability hit
breakdown, and the most-recently-cached entries with verdict, stability
class, hit count, and expiry status. Cached claims also surface as a
green ↺ CACHED badge on the corresponding decision in the Detail View
and a dashed border in the Flow View, so the operator can see at a
glance which verdicts cost zero LLM calls.

## What v2 would add

- A retrieval layer that fetches full pages on demand, ranks sources,
  and corroborates across at least two sources before judging
  (replacing the snippet-only v1 path).
- Structured-fact retrieval against Wikidata / Wikipedia infoboxes for
  predicates with stable answers (capitals, birth years, founders).
- ~~Multi-conversation scoping on the core tables.~~ **(v0.5.x done):
  facts/turns now have user_id; default 'default_user' for solo
  dogfooding; multi-user scoping ready.**
- Streaming assistant responses with incremental trace updates.
- A policy layer that decides *when* to correct silently vs surface the
  conflict to the user.
- Confidence calibration based on repeated verification outcomes (an
  observed contradiction from a python verifier is much more decisive
  than a retrieval miss).
- Generalize the v0.6 unique-value-slot prototype (currently
  spatial_temporal.was_born_in only) to a curated set of definitionally-
  unique slots, OR move to YAML metadata (`unique_per_entity: true`
  on slot definitions).
- Entity-name canonicalization beyond the cache's case-folding +
  whitespace normalization: alias tables, fuzzy match, embedding
  similarity. Currently the cache misses semantically-equivalent
  claims with different wording.
