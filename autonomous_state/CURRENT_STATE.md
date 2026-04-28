# Current State

Updated: 2026-04-28T11:30:00-0400
Updated by: autonomous instance — Session 2

## Status

- Branch: experiment/autonomous-v0.5.x
- Last green pytest: 2026-04-28 — 399 passed, 5 skipped (real-API gated)
- Total project coverage: 94%
- Last commit: [p7] cost telemetry: Modal usage flows into per-turn cost ledger
- Active work: continuing per-operator instructions to keep producing
  improvements indefinitely. No stop condition.

## What shipped this session (Session 2 — 49 commits so far)

### Phase 6 — Tier 2 verification cache (FULLY SHIPPED)

Six commits, in observation→action order per spec:
  1. schema (verification_cache table + 4 cache pipeline event stages)
  2. scoping classifier in observation mode
  3. stability classifier in observation mode
  4. VerificationCache + canonicalize_claim_key (storage layer)
  5. cache writes wired (fill the cache from successful retrievals)
  6. cache lookups wired (short-circuit retrieval on hit)
  7. /api/cache + Cache tab in trace UI

Three env vars to enable: AEDOS_CACHE_SCOPING=1 +
AEDOS_CACHE_STABILITY=1 + AEDOS_CACHE_WRITES=1. OFF by default.

### Cost telemetry (NEW)

src/cost.py — pricing constants, cost_for_call, aggregate_costs.
LLMClient records every API call's cost; Pipeline emits a turn_cost
event at end-of-turn with by-model breakdown. Modal/GLM usage also
flows through (prompt_tokens / completion_tokens / reasoning_tokens
captured from the GLM response). Trace UI shows per-turn cost line.

### Hallucination corpus (NEW)

scripts/dogfood_hallucination_corpus.py — 28 adversarial prompts.
Ran end-to-end against GLM-5.1-FP8. Results:

  - **3 real catches** (contradicted + corrected): yellowknife
    population, saturn moons (post-2023 update), marie curie composite
  - **6 inconclusive** (verifier hedged appropriately)
  - **1 retrieval_failed** (denver elevation — DDG returned 0 results)
  - **27 verified** verdicts overall
  - **5 pipeline errors** (Modal cold-start timeouts, 1 content=null
    on the floccinaucinihilipilification spell-backwards prompt)

**Big architectural finding** (turn 26): user said "born in
Williamstown MA" in turn 24, then asked "I think I told you born in
Williamsburg VA. Is that right?" — extractor pulled the second as a
new assertion, router didn't see it as contradicting (per-key-slot
exact match misses different-value-same-entity), model confabulated
"yes both!", AEDOS verified both. Documented in OBSERVATIONS as "THE
BIG MISS"; partial fix shipped (extractor calibration for
interrogative-meta forms), full fix needs operator architectural call
on unique-value slot metadata.

### Eval harness (NEW)

scripts/eval_harness.py runs each prompt twice (raw chat + AEDOS),
classifies each turn as caught/preserved/broken/missed/uncertain.
Output to eval_results/. Loose substring matching; not a substitute
for human review but the JSON dump preserves full traces.

### Robustness improvements

  - bounded retry on 502/503 in modal_glm (transient upstream)
  - bounded retry on 429 (already shipped session 1)
  - judge parser accepts SUPPORT/CONTRADICT/INCONCLUSIVE abbreviations
    (real bug fix from session 1)
  - chat max_tokens capped at 1024 (cold-start tractability)
  - stdout reconfigure to UTF-8 in all dogfood/eval scripts (Windows
    cp1252 was crashing on math-output GLM responses)
  - Modal warm-up ping with 429 retry in dogfood scripts

### Test coverage improvements

  - Tier 1 cross-session: 8 tests (commit b49be45 from session 1)
  - Modal/GLM: 22 tests (session 1) + 3 more session 2
  - Cost: 13 unit + 8 integration
  - Cache: 5 schema + 10 scoping + 12 stability + 15 storage + 10
    writes + 5 lookups + 4 API
  - Inspector endpoints: 11 tests (app.py 75% → 89%)
  - Comparator: 12 new edge-case tests (86% → 99%)
  - Verifier types: 6 (83% → 100%)
  - Retrieval providers: tavily/serpapi parsing + default_search
    dispatcher
  - Eval harness helpers: 12 tests for substring/classification logic

Total tests: 229 (start of session 1) → 399 (current). +170 new tests.

## Initial Hypothesis (still relevant)

v0.5 routing logic was calibrated against Claude as chat model. GLM-5.1
has different hallucination patterns. The router's worked examples may
not cover GLM's failure modes. Initial work was empirical: dogfood
with GLM, observe what breaks, adapt.

**Update from session 2 hallucination corpus:** GLM produces ~10%
hallucination rate on adversarial prompts (3 of 28 caught + 6 hedged).
The verification pipeline DOES catch real wrong claims when retrieval
returns useful signal. The biggest gap is in user-self-contradiction
detection (architectural — see THE BIG MISS).

## Suggested next pickup

1. **Validate the extractor calibration** for "I think I told you X"
   with a real-API run. Either re-run dogfood turn 26 specifically or
   add a new test_extractor.py case with RUN_API_TESTS=1.
2. **Operator architectural decision** on unique-value slot metadata
   (would need a patterns.yaml change). Documented in
   OBSERVATIONS / NEXT_STEPS for review.
3. **Run eval harness** end-to-end against the hallucination corpus
   to get raw-vs-aedos comparison data. Burns Anthropic budget;
   probably ~$5-10. Operator should approve.
4. **More adversarial corpus prompts** — the current 28 caught only
   3 hallucinations. Better coverage would be: more dynamic facts
   (post-2023 events), more lesser-known entities, more multi-turn
   adversarial setups, more "almost right" cases where one slot in a
   composite claim is subtly wrong.
5. **Phase 7 continuous improvement** — pick the most interesting
   thread from OBSERVATIONS and explore it.
