# Current State

Updated: 2026-04-28T13:00:00-0400
Updated by: autonomous instance — Session 2 (continuing)

## Status

- Branch: experiment/autonomous-v0.5.x
- Last green pytest: 2026-04-28 — 452 passed, 7 skipped (real-API gated)
- Total project coverage: 95%
- Last commit: [obs] REAL-API VALIDATION: verbatim rule works + AEDOS caught Claude's Saturn hallucination
- Active work: continuing per-operator instructions to keep producing
  improvements indefinitely. No stop condition. 100+ commits this session.

## 🎯 COMPREHENSIVE REAL-API VALIDATION 2026-04-28

All five LLM-bound components validated against Anthropic Opus 4.7
(total cost ~$1):

  - test_router_calibration_against_worked_examples PASSED ≥ 14/16
  - test_scoping_calibration_against_worked_examples PASSED ≥ 3/4
  - test_stability_calibration_against_worked_examples PASSED ≥ 3/4
  - test_real_api_extractor_does_not_substitute_values PASSED
  - Saturn moons corpus turn — AEDOS caught Claude's hallucination,
    corrector replaced 146 → 274

The verbatim rule is the load-bearing fix — without it, the
extractor poisons the verification pipeline and renders the rest
moot. With it shipped, the whole system works end-to-end.

See OBSERVATIONS.md "COMPREHENSIVE REAL-API VALIDATION 2026-04-28"
for the full table.

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

## Late-session additions (since 11:30)

  - **CRITICAL extractor bug found and fixed:** the 3 "catches"
    earlier this session were extractor-substitution false positives,
    not real catches. Extractor (Opus 4.7) was rewriting source_text
    to substitute its own world knowledge. Shipped: aggressive
    'CRITICAL: extract VERBATIM' rule + 2 worked examples (Saturn 274,
    Yellowknife 22085) + defense-in-depth substitution detector
    (source_text-not-in-input check) + UI banner +
    extractor_substitution_warning pipeline event +
    extractor_substitution_warning rendering in Detail View. Validated
    post-hoc against the existing corpus: 5 of 27 turns flagged (18%).
  - **DDG resilience:** User-Agent rotation on empty result. Denver-
    elevation case (all 3 queries returned 0) would now retry with a
    different UA. 3 new tests.
  - **Test coverage gains:** pattern_registry 91% → 100% (8 new tests),
    comparator 86% → ~99% (12 new tests), retrieval_verifier 82% → 88%
    (search providers + UA rotation).
  - **Documentation:** README v0.6 section, ARCHITECTURE.md Tier 2
    cache section, .env.example v0.6 cache knobs.

## Suggested next pickup

1. **Re-run dogfood + corpus** once Modal is healthy. With the
   extractor verbatim rule shipped, the 'catches' that were really
   substitution bugs should disappear and any REAL hallucinations GLM
   produces should now show up as legitimate catches. This is the
   payoff measurement of the whole session.
2. **Operator architectural decision** on unique-value slot metadata
   for the unique-per-entity contradiction model (birthplace,
   biological mother, native language). Would catch the 'born in MA
   in turn 24, born in VA in turn 26' adversarial pattern.
3. **Run eval harness** for raw-vs-aedos comparison data on the
   hallucination corpus. Burns Anthropic budget (~$5-10).
4. **More adversarial corpus prompts** — focus on the gaps the
   first run revealed: dynamic facts (post-2023 events), composite
   claims where ONE slot is subtly wrong, multi-turn adversarial
   sequences.
5. **Continue the loop** per the operator's prompt. Pick something
   from OBSERVATIONS, ship a small improvement, commit, push, repeat.
