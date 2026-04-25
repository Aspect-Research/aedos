# CLAUDE.md — guidance for Claude Code sessions in this repo

## What this is

Aedos is a claim-verification and conversational-memory research prototype.
The working hypothesis: hallucination is predominantly a failure of
verification, not of knowledge. Every factual claim the assistant makes is
extracted, routed to a type-matched verifier, and either confirmed,
rejected, or flagged. User-stated facts are stored as ground truth.

See `ARCHITECTURE.md` for the design rationale and a full data-flow diagram.

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
predicates.yaml              — ~30 typed predicates, human-editable
src/
  fact_store.py              — SQLite wrapper, all DB operations
  predicate_registry.py      — loads predicates.yaml, validates, formats for prompt
  extractor.py               — LLM → structured claims via forced tool use
  router.py                  — dispatches claims to verifiers; writes to store
  verifiers/
    python_verifiers.py      — deterministic python functions, one per predicate
    store_verifier.py        — matches model claims against user-asserted facts
    retrieval_stub.py        — placeholder; returns inconclusive
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
