# CLAUDE.md — guidance for Claude Code sessions in this repo

## What this is

Aedos is a claim-verification and conversational-memory research prototype.
The working hypothesis: hallucination is predominantly a failure of
verification, not of knowledge. Every factual claim the assistant makes is
extracted, routed to a type-matched verifier, and either confirmed,
rejected, or flagged. User-stated facts are stored as ground truth.

See `ARCHITECTURE.md` for the design rationale and a full data-flow diagram.

## v0.5 changes (read before touching anything)

> **The pattern-driven verification dispatch is gone.** Old `aedos.db`
> files from v0.3/v0.4 are still compatible (the schema didn't change),
> but `pattern.verification_method`, `predicate_overrides`,
> `verification_rules`, and `flag_non_user_as_anomaly` no longer exist
> on patterns. Routing is decided by an LLM call per claim.

### The big shift: pattern-routed → LLM-routed verification

v0.4's router walked `pattern.verification_method` rules plus a
`predicate_overrides` map to pick a verifier. That didn't scale — every
new computable claim type required editing YAML, and entire categories
of python-verifiable claims (date arithmetic, internal consistency
checks, structural string properties) defaulted to retrieval or
unverifiable because they didn't match a pre-declared category.

v0.5 replaces the rule walk with `src/llm_router.py`: a single LLM call
per claim that picks one of:

  - `python`                            — code resolves the claim from its inputs
  - `python_with_canonical_constants`   — same plus a stable reference (list of US states, primes under 100, etc.); triggers a cross-check
  - `retrieval`                         — search + judge
  - `user_authoritative`                — claim is about the user
  - `unverifiable`                      — no method applies

The router LLM (Sonnet 4.6, via `extract_with_tool`) is prompted with
worked examples for each method plus boundary cases — most importantly
the multi-claim convention ("Marie Curie was born in 1867 and died in
1934, so she lived 67 years" routes to python on the arithmetic, taking
the dates as given) and the external-string boundary ("the Gettysburg
Address opens with X" looks like a string operation but actually needs
retrieval). When in doubt, the router prefers earlier methods —
python > python_with_canonical_constants > retrieval > user_authoritative
> unverifiable.

### Triage is gone

`src/verifiers/code_generation/triage.py` is deleted. The LLM router
has already decided python-verifiability before code generation runs.
False positives surface as `code_execution_failed` (sandbox
non-zero / timeout) or `comparison_error` (comparator can't parse) —
not as a fall-through to retrieval. If you find the router routes
something to python that the code can't actually answer, the fix is in
the router prompt or worked examples, not by reinstating triage.

### Canonical-constants cross-check (§5)

`python_with_canonical_constants` is for claims that need a small,
stable, widely-known reference the code can emit literally — list of
US states, days of the week, ASCII tables. The cross-check runs the
code-generation pipeline twice at different temperatures (0.0 and 0.3)
and compares the two outputs. Agreement → accept. Disagreement → log
`canonical_constants_disagreement` and return that status (the
dispatcher treats it as pending). This guards against the LLM emitting
a subtly wrong canonical reference; pure-python claims don't need this
because their inputs are all in the claim itself.

### Routing anomaly is now hardcoded

The pattern-level `flag_non_user_as_anomaly` flag is gone. The router's
sanity check is now a hardcoded map (`_USER_SUBJECT_PATTERNS` in
`src/router.py`) listing patterns whose subject must be the user
(preference, propositional_attitude). A non-user agent on those
patterns flags as `routing_anomaly` — the LLM router would route the
claim to unverifiable anyway, but the anomaly banner alerts the
operator that the extractor mis-bound the slot.

### How to add a new computable claim type (no YAML edit)

You don't add anything. Use the predicate label that fits the pattern;
the router will recognize it as python-verifiable from content. If the
router occasionally misroutes something you expected to be python:

1. Look at the trace UI — the routing block shows the method and reason.
2. If it's a calibration drift, add or adjust a worked example in
   `_ROUTER_SYSTEM` (`src/llm_router.py`).
3. Run the calibration test: `RUN_API_TESTS=1 pytest tests/test_routing_calibration.py`.

Do NOT add fallback heuristics in `router.py`. The router is the only
place routing decisions are made.

### Where to look in the trace UI

Per model claim, the verification decision now leads with a routing
block (method, reason, confidence). Confidence < 0.7 renders a yellow
warning. For canonical-constants claims, both code generations show
side-by-side. The triage section in the code-gen block is gone.

## v0.4 changes (read before touching anything)

> **The hand-written python verifier registry is gone.** Old `aedos.db`
> files from v0.3 are still compatible (the schema didn't change), but
> any code referencing `src.verifiers.python_verifiers` will fail —
> that module is deleted.

### The big shift: hand-written verifiers → code-generated verification

v0.3's python path called a per-predicate hand-written function:
`verify_has_count`, `verify_is_anagram_of`, etc. That doesn't scale —
every new property the model invents (prime counts, words containing
specific letters, anything you didn't anticipate) silently returned
wrong-but-conveniently-zero results because `has_count` only knew how
to count single characters.

v0.4 replaces that with a four-stage code-generation pipeline. When a
claim is python-routed:

1. **Triage** decides if the claim is python-resolvable from its slots
   alone (no external data, no human judgment).
2. **Prompt builder** articulates a NEUTRAL question about the claim
   that does NOT reveal the asserted answer.
3. **Code writer** receives ONLY the neutral prompt — never the claim,
   never the asserted value — and writes a python script.
4. **Sandbox** runs the script in a subprocess with strict limits.
5. **Comparator** parses stdout and compares to the asserted value.

The deliberate firewall (Stages 1, 2, 3 are separate LLM calls; Stage
3 sees only the neutral prompt) exists to keep confirmation bias out
of code generation. If the code writer saw "the model claimed 25,
write code to check 25", it would be biased toward producing code
that confirms 25. Stripping the asserted value before code generation
forces code that answers the question, not validates a hypothesis.

### Verification statuses (v0.4 additions)

`unverifiable_pending_implementation` now covers:

- `comparison_error` from the comparator (couldn't parse stdout, or
  couldn't extract a claimed value for this pattern/predicate).
- `code_execution_failed` from the sandbox (timeout, exit non-zero).

When triage says **`not_python_verifiable`**, the router falls through
to the pattern's first non-python rule. So `quantitative.born_in_year`
(triage will say "external data") falls back to retrieval; predicates
in `relational.predicate_overrides` whose pattern default is retrieval
also get the same fallback path.

### Patterns vs predicate_overrides

`patterns.yaml` now supports `predicate_overrides: {predicate: method}`.
This is how `relational.reverse_of` gets routed to python despite the
relational pattern's retrieval default. Override values are checked
BEFORE the verification_method rule list. Use this only for predicates
where the override is structural — i.e. a computable string/number
relation that fits the pattern but needs different verification.

### How to add a new python-verifiable predicate (no code change)

You don't write a verifier function — code generation handles it.

- **In an existing pattern that already routes to python** (e.g.
  `quantitative` with predicates like `has_count`, `prime_count`,
  `sum_equals`): just use the predicate. Triage decides if it's
  resolvable; if yes, the pipeline runs.
- **Inside `relational` for a new computable predicate** (e.g.
  `palindrome_of`): add it to `predicate_overrides` in `patterns.yaml`
  with `python` as the value. Done.

If triage occasionally rejects something you expected to be verifiable,
look at the trace UI — the triage stage emits its `reason` so you can
see why. Don't add fallback hand-written verifiers; that recreates the
v0.3 maintenance problem we're solving.

### Where to look in the trace UI

Per python-routed claim, the decision shows a code-generation block
with a collapsed-by-default details panel. Inside:

- Triage verdict + reason
- Generated neutral prompt (with all retry attempts; leakage flagged)
- Generated code (with the model that wrote it)
- Execution output (stdout, stderr, duration_ms, timed_out)
- Comparison verdict with claimed_value vs. computed_value

If `code_prompt_leakage_detected` was emitted, the block shows a
warning. If execution was slow (>1s) or wrote stderr, the block flags
that too. These warnings don't change the verdict — they're for the
operator to investigate later.

### Sandbox is for correctness, not security

`src/verifiers/code_generation/sandbox.py` runs generated code in a
subprocess with closed stdin, empty cwd, and a minimal env. It's not a
security boundary — the code comes from our own LLM and runs locally.
The limits keep accidental interactions out, not malicious ones.

## v0.3 changes (read before touching anything)

> **The schema and representation changed.** Old `aedos.db` files are
> incompatible with v0.3 — run `python scripts/reset_db.py` before first
> use. Old `predicates.yaml` is gone; `patterns.yaml` replaces it.

### The big shift: closed vocabulary → patterns

v0.1/v0.2 had a closed predicate vocabulary (~37 predicates) and
verification methods declared per predicate. v0.3 replaces that with
**8 patterns** (`patterns.yaml`) and **free-form predicates** within
each pattern. Verification semantics come from the pattern. New
predicates within an existing pattern require no code change — they're
just used in extraction and inherit the pattern's routing.

Why this scales where the predicate approach didn't: every conversation
introduces relations that don't fit a curated list. Open vocabulary
breaks canonicalization. Patterns let predicate labels grow organically
while keeping verification predictable, because the verification method
is determined by the structural type, not the surface label.

### How to add a new predicate (common, no code change)

You don't add anything. The extractor produces predicates as part of
fact extraction; if it picks a label not yet seen in the codebase, that's
fine. The router dispatches by pattern. The python verifier registry is
keyed by predicate name, so if you want a *python* verifier for a new
predicate, register it (see below); otherwise the pattern's default
verification method handles it.

### How to add a new python verifier within an existing pattern

1. Write a `verify_<predicate>(claim) -> VerificationResult` function in
   `src/verifiers/python_verifiers.py`. The claim has `slots`; read the
   slots you need.
2. Register in the `VERIFIERS` dict at the bottom of the file, keyed by
   predicate name (NOT function name).
3. Add tests.

The pattern's verification rules use `python_when_predicate_supported`
(see `quantitative` for the example) — the router checks `VERIFIERS` for
a registered verifier and falls through to the next rule (typically
retrieval) if none is registered.

### How to add a new pattern (rare, requires discussion)

This is an architectural decision, not a routine change. The pattern set
is bounded for a reason: each pattern needs declared slots,
discriminating examples in the prompt, key-slot identity for store
lookups, and a query strategy if it uses retrieval. The corrector also
needs to know how to interpret the verification status it produces.

If you genuinely need a new pattern:

1. Append the entry to `patterns.yaml` with all required fields:
   `description`, `slots`, `verification_method`, `example_predicates`,
   `example_extractions`, `disambiguation_notes`, `query_strategy` (if
   retrieval), `flag_non_user_as_anomaly` (if user-authoritative branch).
2. Add the pattern's key slots to `KEY_SLOTS_BY_PATTERN` in `src/router.py`.
3. If the flat view in `src/fact_store.py` should project the pattern's
   subject/object slots, update the COALESCE in the `facts_flat` view.
4. Update extractor few-shot examples in `src/extractor.py` to teach
   the pattern's discriminators.
5. Tests at every layer.

### Granular verification statuses

Six values now (was five in v0.2):

- `verified`, `contradicted`, `user_asserted` — same as before
- `unverifiable_in_principle` — pattern's resolved method is `unverifiable`
- `retrieval_inconclusive` — **NEW v0.3** — verifier ran, judge said
  INSUFFICIENT_EVIDENCE. The corrector hedges these.
- `retrieval_failed` — **NEW v0.3** — verifier got no useful signal
  (network error / no_results / judge unparseable). The corrector does
  **NOT** hedge — adding "I think" to a possibly-true claim is worse
  than leaving it. The pipeline logs `verifier_failure` events instead.
- `unverifiable_pending_implementation` — catch-all for python verifier
  inconclusive / store-lookup miss
- `routing_anomaly` — pattern with `flag_non_user_as_anomaly` got a
  non-user agent. Corrector noops; pipeline logs the offending slot.

The `retrieval_inconclusive` vs `retrieval_failed` split is the v0.3 fix
for a v0.2 bug: hedging on verifier failure made the system *more*
wrong. Hedge only when there's positive evidence of uncertainty.

### Slots-aware retrieval queries

Each retrieval pattern declares a `query_strategy` — an ordered list of
templates with `{slot}` placeholders. The verifier tries them in order
and uses the first attempt that returns ≥ 2 results. Each attempt logs
to `pipeline_events` as `retrieval_query_attempt`.

Critical: never prepend "current" to a query. Temporal scope comes from
the slots' `valid_from` / `valid_until`, not from query string magic.
The judge prompt has two variants — one for currently-held claims (no
`valid_until` slot) and one for historical claims (with explicit
period). The verifier picks the right prompt based on slot values.

## v0.2 changes (read before touching anything)

> **The schema enum widened.** Old `aedos.db` files are incompatible
> with v0.2 — run `python scripts/reset_db.py` (or click "Reset DB" in
> the UI) before first use.

- **New role predicates** in `predicates.yaml`: `holds_role`, `is_a`,
  `headed_by`, `member_of`, `succeeded_by`, `preceded_by`. All retrieval-
  verifiable. They close the role-claim gap that previously caused the
  extractor to misuse `believes` for sentences like "Donald Trump is
  the US President".
- **`retrieval_query_template`** is a new optional field on retrieval
  predicates. The retrieval verifier formats it with `{subject}` and
  `{object}` to build a search query. Without one, the verifier falls
  back to `"{subject} {object}"`.
- **`verification_status` enum** expanded:
  - `verified`, `contradicted`, `user_asserted` — same as v0.1
  - `unverifiable_in_principle` — predicate's `verification_method` is
    `unverifiable` (`will_happen`, `might`, `believed_by_many`)
  - `unverifiable_pending_implementation` — retrieval failed, judge said
    insufficient evidence, python verifier inconclusive, or store lookup
    missed for a user-authoritative predicate. Indicates the run failed
    rather than the claim being unfalsifiable
  - `routing_anomaly` — model asserted a `user_authoritative` predicate
    about a non-user subject. Strong signal of upstream extractor error
  - The full mapping (status → confidence → corrector action) lives in
    `ARCHITECTURE.md` under "Verification status semantics"
- **Real retrieval verifier** (`src/verifiers/retrieval_verifier.py`).
  Uses Tavily / SerpAPI / DuckDuckGo (in that preference order) to
  fetch snippets, then an LLM judge for SUPPORTED / CONTRADICTED /
  INSUFFICIENT_EVIDENCE. Results cache in a new `retrieval_cache` table
  with a default TTL of 24 hours (configurable via
  `AEDOS_RETRIEVAL_CACHE_TTL_HOURS`). All failure modes
  (`retrieval_error`, `no_results`, `judge_parse_error`, `judge_error`)
  are explicit and surface in `pipeline_events` plus the trace UI.
- **Aggressive corrector** (`src/corrector.py`). Now plans interventions
  per claim:
  - `verified` / `user_asserted` → noop
  - `contradicted` → REPLACE with the verified value
  - `unverifiable_pending_implementation` (conf < 0.5) → HEDGE
  - `unverifiable_in_principle` → SOFTEN
  - `routing_anomaly` → noop at content level; logged separately as a
    `routing_anomaly_detected` pipeline event
  Multiple interventions in one response are batched into a single LLM
  rewrite call.
- **Updated guidance for adding predicates**: now requires considering
  `verification_method` (especially for new role-type or world-fact
  predicates that should go to retrieval) and supplying a
  `retrieval_query_template` if the verification method is `retrieval`.
  See "How to add a new predicate" below for the step-by-step.

## Priorities

In this order:

1. **Clarity.** Every function readable in one sitting.
2. **Observability.** Every pipeline stage writes a `pipeline_events` row.
   No silent failures.
3. **Ease of modification.** Predicates live in YAML. Verifiers are
   one-function-per-file-section. Tests are narrow.

Explicit non-goals: performance, scale, cross-conversation state, general
commonsense reasoning, migration tooling.

## Layout

```
patterns.yaml                — 8 structural patterns; free-form predicates within each
src/
  fact_store.py              — SQLite wrapper, all DB operations
  pattern_registry.py        — loads patterns.yaml, validates, formats for prompt
  extractor.py               — LLM → structured claims via forced tool use
  llm_router.py              — v0.5 LLM-based per-claim verification routing
  router.py                  — dispatches claims to verifiers; writes to store
  verifiers/
    types.py                 — shared VerificationOutcome / VerificationResult
    store_verifier.py        — matches model claims against user-asserted facts
    retrieval_verifier.py    — slots-aware multi-attempt retrieval + judge
    code_generation/         — v0.4/v0.5: prompt → code → sandbox → compare
      prompt_builder.py      — NEUTRAL prompt + leak detection
      code_writer.py         — code generated from prompt only
      sandbox.py             — subprocess execution with strict limits
      comparator.py          — deterministic stdout vs claim comparison
      pipeline.py            — orchestrator + CodeGenerationVerifier
                               (with verify_with_cross_check for
                               python_with_canonical_constants)
  corrector.py               — rewrites assistant draft given corrections
  pipeline.py                — orchestrator for a full turn
  llm_client.py              — Anthropic SDK wrapper
  app.py                     — FastAPI backend + static file serving
static/                      — vanilla-JS UI (index.html + app.js + style.css)
tests/                       — pytest, one file per component + integration
scripts/reset_db.py          — wipe & recreate schema
```

Run the app with `python -m src.app` (serves at `http://127.0.0.1:8000`).
Run tests with `pytest` (real-API tests gated behind `RUN_API_TESTS=1`).

## Do NOT change without discussion

These are load-bearing invariants:

- **The core schema** (`facts`, `turns`, `pipeline_events`). Changes here
  ripple through every component.
- **The two-extractor pattern.** User messages and assistant drafts both
  go through the extractor, with the same registry. Don't collapse them.
- **The "every stage observable" UI constraint.** Every pipeline stage
  writes a `pipeline_events` row. The UI reads from that table. New stages
  must log; the UI should not be special-cased for a stage that doesn't
  emit events.
- **Bounded predicate vocabulary.** The extractor is forbidden from
  inventing predicates. If a claim doesn't fit the registry, it's dropped.
  Never loosen this.
- **One primary code path per flow.** No mode flags, no alternate routes.
  If a new behavior is needed, add it to the existing path or ask first.

## (v0.2) How to add a new predicate — superseded by v0.3

In v0.3, predicates are free-form within a pattern. See
"v0.3 changes" → "How to add a new predicate" above. The v0.2
guidance below is preserved for context.

1. Append an entry to `predicates.yaml`:

   ```yaml
   my_new_predicate:
     object_type: int           # int | string | bool | entity | count
     verification_method: python  # user_authoritative | python | store_lookup | retrieval | unverifiable
     python_verifier: verify_my_new_predicate  # required iff verification_method == python
     retrieval_query_template: "{subject} {object}"  # only when verification_method == retrieval; optional
     description: One-sentence description for the extractor LLM.
     example: "Natural language example → (subject, my_new_predicate, object)"
   ```

2. If python-verifiable, add the function in
   `src/verifiers/python_verifiers.py`:

   ```python
   def verify_my_new_predicate(claim):
       # ... compute ground truth from claim["subject"] and claim["object"] ...
       positive_is_true = ...
       outcome = _apply_polarity(positive_is_true, int(claim["polarity"]))
       return VerificationResult(outcome, actual_value=..., explanation="...")
   ```

   Register it in the `VERIFIERS` dict at the bottom of the same file.

3. Add tests in `tests/test_verifiers.py` covering verified / contradicted
   / inconclusive cases.

4. Add an extractor test in `tests/test_extractor.py` with a mocked LLM
   response using the new predicate — makes sure validation passes it.

5. Restart the app. The registry is cached, so restart is required for
   the new entry to appear in the extractor prompt.

## How to add a new python verifier

See step 2 above. A few conventions:

- Return `VerificationResult(VerificationOutcome.INCONCLUSIVE, explanation=...)`
  when the claim shape doesn't match — don't guess.
- Use `_apply_polarity(positive_is_true, polarity)` to factor in negation.
  Never forget polarity.
- Keep the function narrow. If it's tempting to branch on subject type or
  guess formats, split into multiple predicates instead.
- `actual_value` should be the *corrected* value if the claim is
  contradicted — the router uses this verbatim as the correction object.

## How to debug a turn

1. Send the message through the UI.
2. Watch the right panel (Pipeline Trace). Every stage is visible:
   extraction → routing → verification → correction.
3. If something's wrong, check the `pipeline_events` table directly:

   ```python
   from src.fact_store import FactStore
   store = FactStore("aedos.db")
   for e in store.get_pipeline_events(turn_id=3):
       print(e["stage"], e["data"])
   ```

4. For LLM issues, check the `data` blob of the `user_extraction` /
   `assistant_extraction` events — they include both `valid_claims` and
   `rejected_claims` with rejection reasons.

## Testing conventions

- One test file per source module (`test_<module>.py`).
- LLM calls are mocked by default. A single integration test per scenario
  exercises the full pipeline with a `MockLLM` that queues canned
  responses.
- Real-API tests are gated behind `RUN_API_TESTS=1` and live inside the
  mocked tests with a `@pytest.mark.skipif` guard. Keep them cheap — one
  request per test, max.
- `_reset_registry` autouse fixture clears the registry cache between
  tests. Add it to any test module that touches the registry.
- Use `tmp_path` for the SQLite file in tests so runs are hermetic.

## Running and debugging the chat interface

```bash
# One-time
cp .env.example .env           # paste ANTHROPIC_API_KEY
uv sync  # or: pip install -e ".[dev]"

# Dev loop
python -m src.app              # starts at http://127.0.0.1:8000
python scripts/reset_db.py     # wipe the DB between runs

# Run a specific scenario
pytest tests/test_integration.py::test_model_hallucinated_count_gets_corrected -v
```

The UI's **Reset DB** button calls `/api/reset`, which is the same
operation as `scripts/reset_db.py` — use whichever is handy.

## When you're stuck

- The pipeline is a straight line. If behavior is wrong, walk the stages
  in order: extraction → routing → verification → correction. One of
  them has the bug.
- The `source_text` field on every claim tells you what span of the
  message was extracted. If extraction went wrong, start there.
- If the LLM keeps returning malformed tool inputs, the fix is almost
  always to tighten the tool's `input_schema` or the prompt in
  `_build_system_prompt` — not to add parsing fallbacks in `_validate`.
  Fail loudly.
