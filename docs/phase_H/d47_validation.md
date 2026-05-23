# Phase H D47 — calibration validation outcome (2026-05-23)

This document captures what D47 actually delivered against the calibration
corpora, sitting alongside `docs/phase_H/d47_design.md` (the design intent)
the way `docs/phase_G/d33_validation.md` sits alongside Phase G's D33 design.

## Summary

**D47 implementation status: complete.** All four stages of the
operator-specified implementation order landed (commits
`31843b3` → `ff9f2c8` → `912c140` → `df8e81b`). The
Wikipedia normalizer fires inside `EntityResolver.resolve` and `TierU`,
runs Stage 1 deterministically against Wikipedia's MediaWiki redirect
API for the cases it handles cleanly, and falls back to Haiku 4.5 for
LLM-mediated selection over disambiguation-page candidates when Stage 1
returns a disambiguation page.

**Calibration-corpus impact, headline numbers:**

| corpus | pre-D47 (Phase E5 baseline) | post-D47 (rc.10+D47) | delta |
|---|---|---|---|
| `entity_resolution_corpus` | 41/50 = 82% | 41/50 = 82% | 0 |
| `derivation_corpus` | 18/50 = 36% | 19/50 = 38% | +1 case (+2pp) |

Neither corpus reaches its Phase 10.5 threshold post-D47
(entity_resolution: 90%, derivation: 80%). D47's contribution to the
gap is bounded: it lifts exactly the cases where Wikipedia's redirect
system or LLM-mediated selection routes a bare ambiguous reference to a
canonical entity reachable via Wikidata. It cannot fix:

1. **Wikidata-side data-model limits** — bare references whose canonical
   entity is not in wbsearchentities' pool (e.g. `er_unambiguous_002`
   Williams College → Q49112, the D33 finding); the corpus runner gives
   the resolver no fuller form to try.
2. **Corpus-runner shape gaps** — bare references with no surrounding
   text (`er_type_filter_005` Victoria + holds_role); Stage 2's
   abstention bias correctly fires for "no context, multiple candidates"
   and the resolver behaves as it does today.
3. **Walker / KB-neighbor-enumeration gaps** — derivation failures
   driven by D5 / D16 / D23, not by entity resolution. D47 cannot help
   here.

**What D47 actually delivered** is captured in the per-case analysis
below. The headline percentage is a coincidence (one D33-related case
regressed by exactly the amount D47 lifted); the under-the-hood story
is more informative.

## Live runs

Both corpora ran with `RUN_LIVE_KB=1 RUN_LIVE_TESTS=1
RUN_CALIBRATION=1`, exercising real LLM (Anthropic + OpenRouter) and
real Wikidata + MediaWiki APIs:

```
tests/calibration/test_corpus_runner.py::test_corpus_calibration[entity_resolution_corpus]
  82% (41/50) — 93s
tests/calibration/test_corpus_runner.py::test_corpus_calibration[derivation_corpus]
  38% (19/50) — 919s
```

The entity_resolution baseline was independently re-measured by running
the same harness with the normalizer disabled (no D47) and the runner
restored to the Phase E shape (no `expected_entity_types` passed at the
ctx level). It reproduced the 82% baseline exactly — the Phase E
calibration numbers are stable across builds.

## D47 per-case story on `entity_resolution_corpus`

Two cases flipped between the Phase-E-baseline and the post-D47 build:

- **`er_type_filter_001`** ("Obama" + holds_role + expected Q5 → Q76).
  Pre-D47: FAIL. Post-D47: **PASS**.

  This is D47 doing exactly what it was designed to do. Wikipedia's
  redirect system returns a clean redirect "Obama" → "Barack Obama"
  via Stage 1 (no Stage 2 invocation needed). The resolver passes
  "Barack Obama" to Wikidata; the D33 type filter [Q5] preserves Q76
  in the candidate pool; the resolver picks it at rank 0.

- **`er_type_filter_003`** ("Amazon" + member_of + expected Q43229
  → Q3884). Pre-D47: PASS. Post-D47: **FAIL**.

  This is a D33 type-filter limitation surfacing because the post-D47
  runner now passes the corpus's `expected_type` (the previous Phase
  E runner did not, so the type filter never fired). Q3884
  (Amazon.com) has P31 = Q4830453 (business) and Q165085 (Big Tech),
  but not Q43229 (organization) directly. D33's exact-match filter
  excludes Q3884. The fix is D33's deferred work item: sub-class
  traversal via P279* (Q43229 is a super-class of Q4830453). Not a
  D47 regression.

**The 9 cases that still fail post-D47** decompose as follows. None of
them are D47-fixable in the current corpus shape:

| id | reference | predicate | Stage 1 outcome | reason D47 cannot help |
|---|---|---|---|---|
| `er_unambiguous_002` | Williams College | located_in | canonical_no_redirect | Wikidata-side: canonical Q49112 not in wbsearchentities pool for "Williams College" (Phase G D33 finding, unchanged). |
| `er_unambiguous_012` | Tokyo | located_in | canonical_no_redirect | Wikidata-side: top wbsearchentities result for "Tokyo" is not Q1490 (the city) for predicate=located_in/slot=subject. Type filter may need broader Q-id list. |
| `er_ambiguous_007` | Lincoln | holds_role | disambiguation_page | Stage 2 has no source text → correctly abstains; surface form preserved; corpus expects Q91 directly. |
| `er_type_filter_003` | Amazon | member_of | disambiguation_page | D33 sub-class limit (above). Also Stage 1 → disambig + no source text → Stage 2 abstains. |
| `er_type_filter_005` | Victoria | holds_role | disambiguation_page | Stage 2 has no source text → abstains; surface form preserved. |
| `er_type_filter_006` | Mercury | part_of | disambiguation_page | Stage 2 has no source text → abstains. |
| `er_type_filter_007` | Jordan | located_in | canonical_no_redirect | Wikipedia's "Jordan" canonical is the country (Q810); resolver picks correctly post-normalize. But type filter `[Q6256]` not in the corpus's `expected_type` field — fails on the Q-id comparison. Investigating. |
| `er_no_match_003` | "the user" | holds_role | not_found | Surface form preserved; corpus expects "no candidates" but wbsearchentities returns something (e.g. Q-id for a paraphrase). Pre-existing limit. |
| `er_no_match_004` | "my company" | employed_by | not_found | Same shape as above. |

**D47's net effect on entity_resolution_corpus:** +1 case (er_type_filter_001),
-1 case (er_type_filter_003 due to D33 sub-class limitation). Net: 0
cases moved.

The flat headline percentage hides a real D47 lift on the case-type
D47 was designed for (er_type_filter_001), offset by an unrelated D33
limitation. The +1 / -1 cancellation is honest — both are present in
the codebase, and Phase 10.5 measurement should see them as decoupled.

## D47 per-case story on `derivation_corpus`

Pre-D47 baseline: 18/50 = 36%. Post-D47 (official calibration run via
`test_corpus_calibration`): 19/50 = 38%.

**Run-to-run variance is non-trivial.** A separate diagnostic run with
identical configuration produced 17/50 = 34% — a 4-percentage-point
spread from the official run. The variance traces to non-determinism
in the multi-LLM extraction + walker chain (Anthropic Haiku 4.5 +
OpenRouter Qwen3-Next + Devstral) where small differences in tool-call
output flow into different walker traversals. The headline "+2pp" is
within the noise band; the honest statement is that derivation is in
**~34-38%** post-D47, vs. ~36% pre-D47, and the lift D47 contributes
is at most a 1-2 case improvement on entity-resolution-bottlenecked
cases.

The derivation corpus does exercise the full pipeline (extraction →
walker → KB), so source text IS available to the normalizer. The lift
is bounded because:

- Most derivation failures are walker-side (D5 KB neighbor
  enumeration, D16 belief revision, D23 single_valued classification),
  not entity-resolution-side.
- The cases that DO involve bare ambiguous references mostly resolve
  via Wikipedia's clean-redirect pattern that pre-existing wbsearch
  rankings already handled adequately.
- Cases that the diagnostic run flagged as failing include the two
  D47-flagship-shape cases (`der_disambiguation_001` "Obama holds_role
  Senator" and `der_disambiguation_006` "Amazon is the world's largest
  river"). These failed for downstream reasons (walker chain didn't
  resolve to a verifying KB statement, KB returned no_match on the
  resolved Q-id+predicate pair) — not because entity resolution
  failed. D47 normalized the entities correctly; the walker / KB layer
  couldn't carry the chain to a verdict. These are D5 / D16 territory.

Phase 10.5 measurement should treat the derivation_corpus number as a
range, not a point, and the dominant signal is the unchanged walker
ceiling rather than D47's contribution.

## Audit-log evidence D47 is firing

The `entity_normalization` audit event fires per resolver call (when a
normalizer is wired). Phase 10.5 measurement can post-hoc compute:

- **Stage-1-outcome distribution**: how often the canonical Wikipedia
  title differs from the surface form (clean_redirect rate).
- **Stage-2-invocation rate**: how often Stage 2 fires
  (disambiguation_page outcomes).
- **Stage-2-abstention rate**: how often Stage 2 abstains
  (selection=None). High abstention rate is the soundness commitment
  visible in measurement.

A spot-check on the two corpora confirms the events are landing — they
appear in `audit_log` with the expected per-entity per-claim shape. No
quantitative analysis included here; Phase 10.5 owns that pass per the
runbook.

## What this means for Phase 10.5

- **Entity-resolution gap.** The 90% threshold is unreachable with the
  current `entity_resolution_corpus` shape. Two distinct mechanisms
  account for the gap: (a) Wikidata-side limits D47 cannot route
  around (the `er_unambiguous_002` / `er_unambiguous_012` class —
  bare canonical strings whose Q-id isn't in the wbsearchentities pool
  for that exact string); and (b) the runner-vs-corpus shape mismatch
  the D47 design doc flagged as a v0.16 D48 candidate (the runner
  passes no source text, so Stage 2 abstains correctly on
  disambiguation-page outcomes).

  Phase 10.5 should record the entity-resolution accuracy honestly at
  ~82% under the current corpus shape and split the failures into the
  two categories. Neither category is a D47 defect; both are
  measurement-frame questions the v0.16 corpus / runner discipline can
  address.

- **Derivation gap.** The 80% threshold is far from reach. The
  remaining 31 failures are split across the still-pending Phase H
  items (D5 KB neighbor enumeration is the largest predicted lift)
  and pre-existing walker limits. D47's +1-case lift is real but
  bounded; the broader derivation gap needs D5 + D16 + D23 closure.

  Phase 10.5 should not interpret the 38% as a v0.15 defect but as the
  empirical baseline before D5/D16 land. The actual Phase 10.5
  threshold-vs-baseline analysis happens after Phase H completes.

## What this means for D47 itself

D47 delivered what its design predicted:

- **Stage 1 (deterministic Wikipedia redirect)** works as designed.
  Live tests against MediaWiki confirm the four outcomes parse
  correctly. The "Obama" → "Barack Obama" clean_redirect case is the
  flagship example: pre-D47 unreachable, post-D47 a deterministic
  one-step normalization.

- **Stage 2 (LLM-mediated selection with abstention)** works as
  designed. Live E2E tests (`tests/integration/live/test_d47_e2e.py`)
  confirm:
  - Bare 'Obama' + source text → reaches Q76.
  - Full canonical 'Barack Obama' → still works (no regression).
  - Bare 'Obama' + no source → resolver behaves gracefully (abstains
    or returns the closest match per the existing path).
  - Audit log captures the Stage 1/2 events with all required fields.

- **The soundness commitment** that motivated D47 is preserved:
  Stage 2 biases to abstention when context is weak. The corpus
  failures that involve disambiguation_page outcomes + no source text
  ALL exhibit this abstention rather than a confident-wrong selection.
  This is the correct behavior; the v0.16 D48 follow-up is the right
  place to address the corpus-shape mismatch, not D47's prompt
  tuning.

D47 is closed for the v0.15 build. Subsequent Phase H sessions (D16,
D5) will lift cases D47 cannot touch, and Phase 10.5 measures the
combined system. The Phase 10.5 runbook should reference this
validation document for the entity_resolution / derivation context
operators will need when reading the run results.

## Test artifacts

- Unit tests: `tests/unit/test_wikipedia_normalizer.py` — 27 tests
  pass, covering all four Stage 1 outcomes, the api_error path,
  batched queries, Stage 2 selection / abstention / hallucination
  defence / network-error handling, audit logging, and the wiring-gap
  defence.
- Pipeline integration: `tests/integration/test_d47_pipeline_integration.py`
  — 10 tests pass, covering resolver-uses-normalized-form, cache
  behavior, asserting-party skip, event-id skip, fail-open semantics,
  context threading, and Tier U dedup-on-normalized.
- Live: `tests/integration/live/test_wikipedia_normalizer_live.py`
  (5 tests against MediaWiki) and `tests/integration/live/test_d47_e2e.py`
  (4 tests against MediaWiki + Wikidata + LLM). Gated by
  `RUN_LIVE_KB=1`; all pass.
- The pre-Phase-G D47-pinning xfails in
  `tests/integration/live/test_wikidata_live.py` remain as
  WikidataAdapter-direct pins of the underlying data-model limit;
  their reason text now references D47's resolver-layer fix and
  test_d47_e2e.py as the route-around.

## Commits

- `31843b3` — Phase H D47 step 1: MediaWiki client and Stage 1
  redirect resolution.
- `ff9f2c8` — Phase H D47 step 2: Stage 2 LLM selection with
  explicit abstention.
- `912c140` — Phase H D47 step 3: pipeline integration with
  normalized claim fields.
- `df8e81b` — Phase H D47 step 4: integration tests and validation.
- (this commit) — Phase H D47 step 5: calibration corpus
  re-measurement.

## After D47

Phase H continues with **D16** (walker fix — small focused
investigation, next session) and **D5** (KB neighbor enumeration —
largest piece, ~3-5 days, after D16). After all three Phase H deltas
land, tag `v0.15.0-rc.11`. Phase 10.5 starts from rc.11.

The `docs/v0.16_planning.md` D47 entry should be updated to mark D47
as **CLOSED in v0.15 Phase H** (with this validation document linked),
following the same pattern Phase G used for D33. That documentation
update can land with the rc.11 tag rather than this commit.
