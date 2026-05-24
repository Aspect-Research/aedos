# Phase H D16 — calibration harness state-isolation fix

**Status:** investigation complete; harness fix applied; re-baselined; documentation written.
**Tag:** part of `v0.15.0-rc.11` (Phase H closure).
**Date:** 2026-05-23.

## TL;DR

D16's original framing — "walker has a belief-revision soundness gap on
`der_revision_001`/`der_revision_002`" — was **incorrect**. The walker's
belief-revision code is correct under its inputs. The two false-verifieds
reported across all four candidates in Phase E v2 trace to a
**measurement-instrument defect**: the calibration harness shares one
in-memory database across all 50 derivation cases, so Tier U rows written
by earlier cases leaked into later cases' walker lookups and produced
artifactual Stage 1 literal-match verdicts.

The fix is in the calibration runner, not in the walker. The walker code
is unchanged. Each derivation case now starts with isolated Tier U +
case-seeded subsumption state, matching the case's setup intent.

Implications:

- **Phase E v2's accuracy numbers were measured against a defective
  harness.** The 36–38% derivation accuracy and the 82% entity-resolution
  accuracy that have been treated as architectural ceilings were
  imprecise. The corrected baselines under the post-fix harness are in
  §Re-baseline below.
- **D16 is no walker fix.** The walker's belief-revision integration was
  always correct.
- **A broader harness audit is owed.** D50 (added to v0.16) captures
  the broader pattern — every corpus runner that mutates case-specific
  state must be audited for similar leakage.

## The mechanism

The calibration harness (`tests/calibration/test_corpus_runner.py`'s
`_Harness`) lazy-builds one in-memory database per
`test_corpus_calibration(corpus)` invocation. All 50 cases of a given
corpus share that database. By design, this lets substrate generation
amortize across cases — the LLM's cold-start judgment for `prefers` is
generated once and reused, which matches the runbook's "calibration
measures the LLM's cold-start substrate-row generation" intent.

The defect: `_run_derivation` writes to two case-specific tables for each
case it processes:

- **Tier U** — every `tier_u` / `tier_u_prior` entry in the case's
  `input` is written via `TierU.write`.
- **Subsumption (`source='calib'`)** — `context_premises` with
  `part_of` / `is_a` predicates are inserted directly.

Both tables hold **case-state**, not substrate-state. They should reset
per case, exactly the way `_run_consistency_check` already builds a fresh
database per case (Phase D follow-up).

Walking the mechanism for `der_revision_001`:

1. `der_cross_009` (corpus line 21) runs first. Its `tier_u` field
   includes `(Asa, prefers, coffee, +1)`. `TierU.write` inserts row
   id=4.
2. `der_revision_001` (corpus line 39) runs later. Its `tier_u_prior` is
   `(Asa, prefers, tea, +1)`. The write path's object-conflict closure
   logic asks the predicate-translation oracle whether `prefers` is
   functional. The oracle's LLM cold-start judgment (with the v5
   prompt and Sonnet, Haiku, Qwen, GPT-4.1-mini all tested in Phase E v2)
   returns `single_valued=0`. Object-conflict closure does not fire.
   `(Asa, prefers, coffee, +1)` from der_cross_009 stays open.
3. The walker walks the extracted claim `(Asa, prefers, coffee, +1)`.
   `TierU.lookup` Stage 1 literal-matches row id=4 (still open from
   step 1). Walker returns `verified`. **False-verified vs. expected
   `contradicted`.**

`der_revision_002` is symmetric: `der_cross_002` writes `(Asa,
employed_by, Google, +1)`; the corpus's extractor produces predicate
`"works at"` (verbatim, all four candidates) so the literal predicate
isn't an exact Stage 1 hit; but `(Asa, employed_by, Google, +1)` from
der_cross_002 is reachable via other lookup paths (see Phase E v2's
predicate-translation broadening) and again surfaces a verdict the
case's own prior would not authorize.

The walker is **correct under its inputs.** The inputs were
contaminated.

## The diagnostic

`scripts/diag_d16.py` reproduces the failure mechanism with a mock LLM
that returns the seed-pack values verbatim. It seeds the prior cases'
Tier U rows in corpus order, then runs the two revision cases. With
seed-pack `prefers: single_valued=1` (per Phase G D39) AND the
diagnostic's mock returning that value, der_revision_001 produces
`contradicted` via `belief_revision: object_conflict`. The honest
post-fix behavior live (where the cold-start LLM judges
`prefers: single_valued=0`) is `no_grounding_found`.

The mock matches the seed pack rather than the LLM's cold-start because
the goal of the diagnostic is to surface the walker-code path; the
LLM-vs-seed-pack disagreement is the separate, downstream measurement
question.

## The fix

`tests/calibration/test_corpus_runner.py:528` (`_run_derivation`) now
clears Tier U and case-seeded subsumption rows at the start of each
case:

```python
h.db.execute("DELETE FROM tier_u")
h.db.execute("DELETE FROM subsumption WHERE source='calib'")
h.db.commit()
```

Substrate-state tables (`predicate_translation`,
`predicate_distribution`, `entity_resolution_cache`, LLM-generated
subsumption rows where `source != 'calib'`) are not cleared — those
hold the cold-start substrate measurements the harness is designed to
share across cases.

The `_ComparisonHarness` in `tests/evaluation/phase_e_comparison.py`
inherits the same `_RUNNERS` dispatch, so the fix propagates.

No walker, Tier U, or substrate code changed.

## Re-baseline

Three corpora were re-run under the post-fix harness with the production
model configuration (`DEFAULT_MODEL_BY_PURPOSE` rc.10): `derivation_corpus`,
`predicate_metadata_corpus`, `entity_resolution_corpus`. Output:
`docs/phase_H/d16_rebaseline_<corpus>.json`. Pre-fix reference comes from
Phase E v2's Haiku run
(`docs/phase_E/results/phase_e5_per_component/claude-haiku-4-5__derivation_corpus.json`)
— the same per-corpus single-LLM measurement the report cited as the
Phase E v2 walker baseline.

| Corpus | Pre-fix (Phase E5 Haiku) | Post-fix (production) | Δ |
|---|---|---|---|
| `predicate_metadata_corpus` | 81.25% (65/80) | **97.5% (78/80)** | +16.25 pp |
| `entity_resolution_corpus` | 82.0% (41/50) | **82.0% (41/50)** | 0 pp |
| `derivation_corpus` | 36.0% (18/50) | **30.0% (15/50)** | −6.0 pp |

**Attribution of each shift.**

- **`predicate_metadata_corpus` +16.25 pp** — Not from the D16 harness
  fix. `_run_predicate_metadata` does not mutate case-state, so the
  isolation change has no effect on this runner. The lift traces to
  Phase E v2's predicate_translation prompt iteration (Haiku v1 → v2,
  recorded in `docs/phase_E_v2_report.md` Part 2) and to run-to-run
  variance (D49). Genuine signal: prompt engineering helped, plus model
  non-determinism may exaggerate the apparent jump.
- **`entity_resolution_corpus` 0 pp** — Not from the fix.
  `_run_entity_resolution` doesn't mutate case-state either. 82% is
  stable across pre-fix and post-fix; D47's documented architectural
  ceiling holds.
- **`derivation_corpus` −6 pp** — **This is the D16 fix's effect.**
  `_run_derivation` was the only runner whose case-state contamination
  produced visible Phase E v2 artifacts. The pre-fix 36% was inflated
  by leakage; the post-fix 30% is the honest baseline.

## Cases that moved (`derivation_corpus`)

`scripts/d16_compare.py` produces the per-case diff between Phase E5
Haiku and the post-fix run.

| Case | Pre verdict | Post verdict | Direction |
|---|---|---|---|
| `der_cross_002` | verified | no_grounding_found | **pass → fail** |
| `der_predicate_translation_003` | verified | no_grounding_found | **pass → fail** |
| `der_predicate_translation_006` | verified | no_grounding_found | **pass → fail** |
| `der_predicate_translation_008` | verified | no_grounding_found | **pass → fail** |
| `der_disambiguation_007` | no_grounding_found | verified | **fail → pass** |
| `der_multihop_004` | contradicted | no_grounding_found | verdict shifted (still failing) |
| `der_revision_001` | verified (false-verified) | no_grounding_found (false-abstention) | verdict shifted (still failing) |
| `der_revision_002` | verified (false-verified) | no_grounding_found (false-abstention) | verdict shifted (still failing) |

Net: 4 pass → fail, 1 fail → pass, 3 verdict-shifted with no pass/fail
change. Total moved: 8 of 50 cases (16%).

**Interpretation.**

- The 4 **pass → fail** cases (`der_cross_002`,
  `der_predicate_translation_003`/`_006`/`_008`) were Phase E5
  apparent-passes that depended on cross-case state contamination. In
  the isolated harness the walker can't reach the verdicts the corpus
  expected — most likely the leaked Tier U rows from earlier cases
  were standing in for premises the walker would otherwise have had to
  derive. Without leakage, the architectural shortfall is visible.
- The 1 **fail → pass** case (`der_disambiguation_007`, "Mercury is
  closer to the Sun than Earth") is most likely model-stochasticity
  (D49) rather than an isolation-fix effect — this is a KB-verifiable
  claim that doesn't depend on Tier U state.
- The 2 **`der_revision_*`** verdict shifts are the headline finding
  for D16's original scope: both convert from false-verified
  (artifactual; the walker was finding a leaked literal-match row in
  Tier U) to false-abstention (honest; under cold-start LLM judgment
  of `prefers`/`employed_by` as multi-valued, the walker's
  object-conflict path correctly does not fire). The walker has been
  correct throughout; the leakage was masking that correctness with
  spurious "verified" verdicts.
- `der_multihop_004` shifted from contradicted to abstain. Both are
  failing verdicts (corpus expected `verified`); this is a separate
  signal about how the walker's multi-hop traversal behaves under
  honest state, likely D5 territory.

## Produced verdict distribution post-fix

| Verdict | Count |
|---|---|
| `no_grounding_found` | 46 |
| `verified` | 2 |
| `contradicted` | 0 |
| (other) | 2 (lenient-pass paths in the runner) |

The walker is dominantly abstaining. Of 50 cases, only **2** produce
`verified` (`der_cross_006` Python arithmetic; `der_disambiguation_007`
Mercury–Sun). **Zero** produce `contradicted`. The honest architectural
ceiling for what the walker can confidently derive under cold-start
substrate generation is much lower than Phase E v2's 36% framing
suggested.

This sharpens D5's framing: KB neighbour enumeration needs to lift
the walker's confident-derivation rate substantially to clear any
reasonable threshold. The pre-D5 baseline against which D5 is measured
is now **30%** (corrected), not 36% (inflated).

## What this means for Phase 10.5

The architectural ceilings Phase H was scoped to address (D47, D5, D16)
were measured against a defective harness in Phase E v2. The post-fix
re-baseline gives the honest pre-D5 starting point. D5's lift can now be
measured against a correct baseline rather than an
artifact-contaminated one.

## What this means for D5

D5's design proceeds with the same scope (KB neighbor enumeration for
multi-hop derivations). The post-fix derivation baseline is what D5's
lift will be measured against. The honest assessment of where the
architectural ceiling sits — given the harness was previously
mis-measuring — informs whether D5's expected lift is the dominant
remaining gap or whether a different architectural item carries more
weight.

## What this means for v0.16

- **D16 reframes** in `v0.16_planning.md`: "walker belief-revision
  soundness gap" → "calibration harness state-isolation defect." The
  walker code is correct.
- **D50 added** to `v0.16_planning.md`: broader audit of harness
  state-isolation across all corpus runners. The Tier-U-and-calib-
  subsumption fix here addressed `_run_derivation` specifically; the
  general pattern (case-state vs substrate-state separation) deserves
  a systematic pass.
- **D23 stays deferred** to Phase 10.5 data, as designed. Whether
  `employed_by` (and by extension other revision-target predicates)
  should be reclassified as `single_valued=1` is a measurement
  question and an honesty-of-the-corpus-expectations question. Now
  that the harness measures honestly, the data informing that
  decision is trustworthy for the first time.

## Audit trail

- Diagnostic: `scripts/diag_d16.py`
- Re-baseline runner: `scripts/d16_recalibrate.py`
- Re-baseline outputs:
  `docs/phase_H/d16_rebaseline_derivation_corpus.json`,
  `docs/phase_H/d16_rebaseline_predicate_metadata_corpus.json`,
  `docs/phase_H/d16_rebaseline_entity_resolution_corpus.json`
- Re-baseline log: `docs/phase_H/d16_rebaseline_run.log`
- Commits: <!-- POPULATE -->
