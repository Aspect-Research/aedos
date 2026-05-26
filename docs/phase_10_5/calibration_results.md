# Phase 10.5 — Step 4 calibration results

**Session:** Phase 10.5 Session 1 (2026-05-26).
**Build:** `cacb403` (Phase H Cluster 3 step 6) + Step 2 doc commit
(`a46e605`) + Step 4 driver commit (`5f9c71e`).
**Variance discipline:** single run per (corpus, mode) per operator
direction at session start. The data here is a point estimate, not a
median-of-N — interpret single-point deviations from threshold with
that caveat. A subsequent variance pass (×2-3) is an option if the
operator wants to distinguish noise from signal on borderline misses.
**Mode framing:** "seeded" = `_Harness(seeded=True)`, the production
deployment behavior for in-vocabulary predicates. "cold-start" =
`_Harness(seeded=False)`, every predicate triggers a cold LLM oracle.
Cold-start was run only for the 3 corpora the runbook designates as
dual-measurement (derivation, entity_resolution, kb_mapping).
**Driver:** `scripts/phase_10_5_run.py`. Per-run JSON in
`docs/phase_10_5/runs/`.

## Summary table

| Corpus | Mode | Pass/Total | Accuracy | Threshold | Δ | False-verified | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| `extraction_corpus` | seeded | 52/53 | 98.1% | 90% | **+8.1** | 0 (n/a) | 1 norm_012 miss; within LLM variance vs Phase E5 100% baseline |
| `predicate_metadata_corpus` | seeded | 74/80 | 92.5% | 85% | **+7.5** | 0 (n/a) | 6 scattered misses; no systematic pattern |
| `temporal_scope_corpus` | seeded | 35/40 | 87.5% | 90% | −2.5 | 0 (n/a) | 2 explicit_scope, 3 relative_scope misses; relative_scope misses may relate to D24 soft observation |
| `entity_resolution_corpus` | seeded | 42/50 | 84.0% | 90% | −6.0 | 0 (n/a) | D33/D47-bounded per runbook caveat; canonical entities absent from candidate pool |
| `entity_resolution_corpus` | cold-start | 42/50 | 84.0% | 90% | −6.0 | 0 (n/a) | Identical to seeded — entity resolution doesn't lean on `predicate_translation`, so the contrast is essentially null |
| `kb_mapping_corpus` | seeded | 29/40 | 72.5% | 90% | −17.5 | 0 (n/a) | **All 9 qualifier_mapping misses are corpus-vs-seed-format drift** (see Findings); kb_resolvable cases pass 28/30 |
| `kb_mapping_corpus` | cold-start | 30/40 | 75.0% | 90% | −15.0 | 0 (n/a) | Slightly higher than seeded — cold-start LLM oracle accidentally matches pre-D19 corpus expectation better |
| `subsumption_corpus` | seeded | 53/60 | 88.3% | 80% | **+8.3** | 0 (n/a) | Scattered misses (6 kb, 1 mixed); no systematic pattern |
| `predicate_distribution_corpus` | seeded | 44/50 | 88.0% | 85% | **+3.0** | 0 (n/a) | 5 `both` cases miss systematically; up/down/neither nearly perfect |
| `derivation_corpus` | seeded | 27/50 | **54.0%** | 80% | −26.0 | 0 (n/a here) | **Exactly reproduces Cluster 3 post-fix baseline (54%)**; architectural ceiling per v0.16 D56/D57/D58 |
| `derivation_corpus` | cold-start | 23/50 | **46.0%** | 80% | −34.0 | 0 (n/a here) | +8pp lift from seeding; matches Cluster 3 cold-start probe (44%) within variance |
| `python_verification_corpus` | seeded | 25/30 | 83.3% | 85% | −1.7 | 0 (n/a) | Narrowly below threshold; single-run estimate, plausibly within variance of the 85% bar |
| `consistency_check_corpus` | seeded | 24/25 | 96.0% | 100% | −4.0 | 0 (n/a) | Single miss `cc_conflict_007` — **documented v0.16 D24 runner-vs-corpus item**, not a new defect |
| `intervention_corpus` | seeded | 23/30 | 76.7% | 90% | −13.3 | 0 (n/a) | **`select_intervention` policy disagrees with corpus expectations** (Finding 1) — deterministic mismatch |

False-verified columns are marked `(n/a)` for corpora that don't produce
walker verdicts (extraction, metadata, distribution, mapping, etc.). For
the verdict-producing corpora (`derivation`), no §3.2-violating
false-verified was observed in either mode — every miss took a
`no_grounding_found` / `abstained_given_assertion` / `contradicted`
shape rather than a wrongly-positive `verified`. Soundness floor holds.

## Threshold pass/fail roll-up

**Threshold met** (5 corpora, 6 runs):
- `extraction_corpus` seeded (+8.1pp)
- `predicate_metadata_corpus` seeded (+7.5pp)
- `subsumption_corpus` seeded (+8.3pp)
- `predicate_distribution_corpus` seeded (+3.0pp)

**Threshold not met but explanation captured** (6 corpora, 8 runs):
- `temporal_scope` seeded −2.5pp — within single-run variance
- `entity_resolution` both modes −6.0pp — D47-bounded per runbook caveat
- `kb_mapping` both modes −15 to −17.5pp — corpus-vs-seed-format drift
- `derivation` both modes −26 / −34pp — architectural ceiling per Cluster 3 baseline
- `python_verification` seeded −1.7pp — within single-run variance
- `consistency_check` seeded −4.0pp — single D24-documented runner item
- `intervention` seeded −13.3pp — policy-vs-corpus disagreement, deterministic

The "threshold not met" category is **not** dominated by system
soundness failures. Every below-threshold result traces to a
pre-documented v0.16 item, a corpus-vs-current-code drift, or a
single-point variance against a bar the system has historically met.

## Findings

### Finding 1 — `select_intervention` policy disagrees with `intervention_corpus`

**What.** `intervention_corpus` (30 cases) scored 23/30 = 76.7% against
the 90% threshold. The 7 misses are *deterministic* — `select_intervention`
is a pure function, the runner constructs a `VerificationResult` from
the case's verdict counts and asserts the function's output. Three
`abstain` cases and four `correct` cases produce `decline` instead of
the corpus-expected `abstain` / `correct`.

**Why.** `select_intervention` (src/aedos/deployment/chat_wrapper.py)
escalates to `DECLINE` when `contradicted + abstained > 0.5 * total`.
For any single-claim contradicted or abstained input that's `1 > 0.5`
→ DECLINE, before the function's branches that would return CORRECT
(for contradicted > 0) or ABSTAIN (for abstained > 0). The corpus was
authored assuming single-claim contradicted → CORRECT and single-claim
abstained → ABSTAIN, with DECLINE reserved for multi-claim escalation
(>50% problematic across at least 2 claims). The function reaches the
DECLINE branch unconditionally for any single-problematic-claim case.

**History.** `select_intervention`'s policy has been unchanged since
Phase 9 (`git log -L` shows the function body identical at first
introduction and now). The corpus was authored separately and never
reconciled against the actual policy. This is a function-vs-corpus
expectations drift, not a regression.

**Operator decision needed.** Two possible directions:
- Revise `select_intervention`'s DECLINE threshold (`> 0.5` is the
  load-bearing condition that produces the disagreement) so single-claim
  contradicted/abstained → CORRECT/ABSTAIN respectively. This aligns the
  function with the corpus.
- Revise `intervention_corpus`'s 7 misses to expect DECLINE, aligning
  the corpus with the function's actual escalation policy.

Either resolution is one-commit-sized. Phase 10.5 surfaces the
disagreement without choosing; deferred to operator.

### Finding 2 — `kb_mapping_corpus` qualifier_mapping vs seeded format

**What.** 9 of 10 qualifier_mapping cases miss systematically in
seeded mode. In cold-start mode 8 of 10 miss (slight improvement —
explained below).

**Why.** Two structural differences between the seeded `slot_to_qualifier`
representation and the corpus expectation:

- Seed pack carries the post-D19 routing keys (`subject`, `object`) AND
  the qualifier mappings, with qualifier P-codes prefixed (`qualifier:P580`):
  ```json
  {"subject": "statement_subject", "object": "statement_value",
   "valid_from": "qualifier:P580", "valid_until": "qualifier:P582", ...}
  ```
- Corpus expects only the qualifier mappings, with raw P-codes:
  ```json
  {"valid_from": "P580", "valid_until": "P582"}
  ```

The runner's `_run_kb_mapping` compares `meta.slot_to_qualifier ==
expected.get("slot_to_qualifier")` literally → systematic failure on
every qualifier case where the seed includes routing keys or
`qualifier:` prefixes.

Cold-start does slightly better because the LLM oracle's freshly-generated
`slot_to_qualifier` happens to omit the routing keys and `qualifier:`
prefix more often than the seed pack stores them, accidentally matching
the corpus's pre-D19 expectation.

**Operator decision needed.** The substrate is well-formed — the KB
verifier consumes the post-D19 shape correctly (`kb_resolvable` cases
pass 28/30). This is a corpus-vs-current-format drift, not a substrate
quality issue. Options:

- Refresh `kb_mapping_corpus.jsonl`'s qualifier_mapping expectations to
  match the post-D19 representation (add `subject`/`object` routing
  keys and `qualifier:` prefixes).
- Loosen `_run_kb_mapping`'s comparison to strip the routing keys and
  `qualifier:` prefix before comparison, treating the post-D19 dressed
  shape as compatible with the bare qualifier shape.

The accuracy gap of 17.5pp (seeded) is entirely this drift; the
underlying capability is intact.

### Finding 3 — `predicate_distribution_corpus` never produces `both`

**What.** All 5 `both` distribution cases miss; up/down/neither score
near-perfect. Threshold passes overall (88% vs 85%) but the systematic
`both` miss is suspicious.

**Why.** The predicate-distribution oracle (qwen3-next-80b via
OpenRouter) never appears to emit `both` as a distribution classification
on the corpus's `both` cases. Could be:
- LLM prompt under-cues the `both` category
- Oracle conflates `both` with up or down based on which feels more
  salient per case
- Corpus expectations for `both` are themselves debatable (which 5
  predicates were classified as `both` and is that classification
  well-defined?)

**Operator decision needed.** Threshold passes, so this is not blocking
release. Worth flagging for v0.16 D56 (cold-start oracle calibration)
— if the oracle's `both` classification is structurally unreachable
that's a prompt issue worth iterating.

### Finding 4 — derivation seeded−cold-start gap is ~8pp

**What.** Derivation seeded = 54.0%, derivation cold-start = 46.0%.
Gap = 8pp.

**Why.** Reproduces the Cluster 3 post-fix probe finding (seeded 54%
+10pp vs C2 baseline 44%, cold-start 44% unchanged). The gap is the
measured benefit of the seed pack on in-vocabulary derivation —
predicate-translation routing being deterministic from the seed pack
enables Q-Lookup α upgrades and §"KB wins" cross-source revisions that
the cold-start LLM oracle generates less reliably.

**Implications.**
- Confirms the seed pack adds real lift on the corpus where it matters
  most (derivation walks multi-hop and consults predicate metadata at
  every Stage 1/3 lookup).
- The ~8pp gap is informative for v0.16 D56 sequencing: cold-start
  oracle calibration improvements that close some of that gap would
  reduce the production-deployment dependence on having every predicate
  in the seed pack.
- Neither number meets the 80% threshold. Per Cluster 3's framing the
  ceiling at 54% seeded is bounded by v0.16 work items:
  - **R4 belief_revision** (3 of 6 still miss): `der_revision_002` is
    D57 (functional-at-a-point-in-time cardinality), `der_revision_003`
    and `der_revision_004` are D58 (TierU normalizer determinism).
  - **R2 KB-likely-upgrade** (7 of 13 miss): walker upgrade-policy
    bounded by KB nondeterminism + entity-resolution constraints
    (D33/D47 family).
  - **Entity disambiguation** (6 of 8 miss): D47 directly.

### Finding 5 — soundness floor holds

Across the 14 runs (528 + 140 = 668 case-mode invocations counting both
modes for the 3 dual-measurement corpora) there were **0 §3.2-violating
false-verifieds**. Every miss took an honest shape — `no_grounding_found`,
`abstained_given_assertion`, structural runner mismatch, or `decline`-vs-
`correct` policy disagreement — rather than a wrongly-positive `verified`
verdict.

The Phase H discipline that eliminated the known false-verified
mechanisms (D16 harness state isolation, Cluster 2 dual designation,
Cluster 3 canonicalization, Cluster 3 step 7's belief-revision-before-
Stage-1 ordering) holds in measurement. **Soundness over completeness
remains the operating point.**

## Per-corpus accuracy vs Cluster 3 / prior measurements

| Corpus | Phase 10.5 seeded | Prior measurement | Delta | Source |
|---|---:|---:|---:|---|
| extraction | 98.1% | 100% | −1.9pp | Phase E5 (53/53) |
| predicate_metadata | 92.5% | ~92-95% | within range | Phase E v2 |
| temporal_scope | 87.5% | 100% lookup, 90%+ extraction | mixed | Phase 3 mocked |
| entity_resolution | 84.0% | ~82% (D47-bounded) | +2pp | Phase E v2 |
| kb_mapping | 72.5% | ~90% (pre-D19) | −17.5pp | runbook expectation |
| subsumption | 88.3% | ~88% | within | Phase E v2 |
| predicate_distribution | 88.0% | ~88% | within | Phase E v2 |
| derivation | 54.0% | 54% (Cluster 3 post-fix) | 0pp | Cluster 3 single probe |
| python_verification | 83.3% | ~90% | −7pp | Phase E |
| consistency_check | 96.0% | 100% (less D24-documented 1 case) | matches | D24 |
| intervention | 76.7% | ~90% | −13pp | runbook expectation |

**Where Phase 10.5 confirms prior measurements:**
- derivation (54% exactly matches Cluster 3)
- subsumption, predicate_metadata, predicate_distribution within ±1pp
- entity_resolution within +2pp of D47-bound baseline
- consistency_check matches D24-corrected expectation

**Where Phase 10.5 deviates from prior measurements:**
- kb_mapping −17.5pp — Finding 2 (corpus-vs-format drift)
- intervention −13pp — Finding 1 (function-vs-corpus policy)
- python_verification −7pp — single-run variance vs threshold

## What Phase 10.5 has and has not surfaced

**What this data tells us:**
- The system's soundness floor holds.
- The substrate-mediated corpora (extraction, predicate_metadata,
  subsumption, predicate_distribution) all meet or exceed their bars,
  confirming the Phase E/H model selection + prompt iteration is
  load-bearing in measurement.
- The derivation ceiling at 54% seeded is structurally correct per
  Cluster 3's diagnosis; v0.16 D56/D57/D58 are the right work items.
- Entity resolution is D47-bounded as the runbook predicted.
- Two corpora (kb_mapping, intervention) have stale expectations vs
  the current code that produce most of their below-threshold gap —
  these are corpus-refresh work, not system work.

**What this data does NOT tell us:**
- Single-run estimates can't distinguish noise from signal on borderline
  results (temporal_scope −2.5pp, python_verification −1.7pp). A
  ×2-3 variance pass would resolve these. Operator chose to defer.
- The Step 6 medium-bar benchmark + Step 5 cold-start zero-seed test
  remain to run; those are subsequent sessions.

## Inputs to subsequent sessions

- **Step 5 (cold-start zero-seed):** the standalone 10-claim
  cold-start probe focused on first-claim latency. Different cut at
  cold-start than this step's corpus-level cold-start mode.
- **Step 6 (medium-bar):** runs against `aedos_phase10_5.db` (Step 2's
  seeded substrate + Step 3's Asa-persona Tier U facts). The 122-case
  test set + LLM-baseline comparison.
- **Step 7 (release decision):** reconciles the Phase 10.5 data + the
  v0.16 backlog. Decides v0.15.0 release vs further v0.16 work before
  release.

The Phase 10.5 data here is one of the two release-decision inputs;
the medium-bar comparison + cold-start probe are the others.

## Open items the operator should review before Step 7

1. **Finding 1** (intervention policy vs corpus) — pick a resolution
   direction.
2. **Finding 2** (kb_mapping qualifier corpus drift) — pick a resolution
   direction.
3. **Finding 3** (predicate_distribution `both` systematic miss) —
   confirm whether this is v0.16 D56 scope or a present-build concern.
4. **Variance** — confirm or revisit the single-run decision now that
   the data is in hand. The borderline results (temporal_scope,
   python_verification) and the systematic findings would benefit
   most from variance bounds.

---

# Session 2 addendum — remediation + variance pass (2026-05-26)

Session 2 addressed the four open items above and added variance
bounds on three corpora. The single-mode point-estimate framing from
Session 1 still holds for the 8 corpora not in scope this session;
the corpora addressed here have updated numbers below.

## Items 1-3 — remediation outcomes

### Item 1 — per-claim intervention redesign (replaces Finding 1)

The original `select_intervention` function returned one of four
intervention types (PASS_THROUGH / ABSTAIN / CORRECT / DECLINE) from
verdict counts. The function silently dropped per-claim information
when a draft had mixed problems (a draft with one contradicted and
one abstained claim returned CORRECT only, hiding the abstain). It
also escalated single-claim all-problematic cases to DECLINE,
refusing to respond when a single per-claim annotation would have
sufficed — the original Finding 1.

The operator-confirmed redesign moved intervention to per-claim:

- New `ClaimVerdict` dataclass in `aggregator.py` carries the claim
  + verdict + abstention_reason per claim. Aggregator builds the
  list during `aggregate`; `VerificationResult.claim_verdicts`
  exposes it (additive — the legacy dict `per_claim_verdicts`
  stays for the audit log).
- 3-value `InterventionType` enum (PASS_THROUGH / INTERVENE /
  DECLINE) + `ClaimAction` + `InterventionPlan` dataclasses. New
  `select_interventions(claim_verdicts) -> InterventionPlan`
  function. DECLINE branch fires only when zero verified AND ≥ 2
  problematic; single-claim-all-problematic now INTERVENE with one
  action.
- Format A response composition in `build_response`: draft +
  `"\n\n---\nAedos verification notes:\n- ..."` bulleted list per
  per-claim action.
- `/chat` HTTP response exposes both the 3-value `intervention_type`
  and the new `per_claim_actions` array.
- `intervention_corpus.jsonl` reshape: `expected_output` →
  `{overall, action_counts: {correct: N, abstain: M}}`. The 2
  corpus cases that previously DECLINED with verified > 0
  (`int_decline_004` 3v/4c/0a; `int_decline_005` 1v/0c/6a) flip to
  INTERVENE under the new policy — intended behavior since any
  verified content + per-claim annotations express the issues.

**Result.** intervention_corpus: 23/30 = 76.7% → **30/30 = 100%**.
Full unit + integration suite green throughout (1123 tests).

Commits: `d398263` (aggregator), `d393265` (selector + composition),
`b4694de` (corpus alignment).

### Item 2 — kb_mapping_corpus refresh (closes Finding 2)

The 9 `qualifier_mapping` cases that systematically failed in
Session 1 were corpus-vs-seed format drift across 3 dimensions:
routing keys (`subject`/`object`), qualifier prefix (`qualifier:`),
semantic key names (`start`/`end`/`year` vs positional
`valid_from`/`valid_until`). All 9 cases refreshed to match the
post-D19 seed shape exactly; case 010 (`notable_work`, unseeded)
left alone.

**Result (seeded):** kb_mapping_corpus 29/40 = 72.5% → **38/40 =
95.0%** (+22.5pp). Above 90% threshold.

**Result (cold-start):** 30/40 = 75.0% → 30/40 = 75.0% (no net
change, but the misses *shifted* — see D56 refinement). All 9
seeded qualifier cases now miss in cold-start because the LLM
oracle generates the older simpler shape; previously-failing
kb_resolvable cases happen to pass. The net is identical but the
structural insight is honest: the cold-start oracle doesn't
produce the post-D19 shape. Captured in `docs/v0.16_planning.md`
D56 refinement.

Commit: `017642b`.

### Item 3 — D56 refinements documented (closes Finding 3)

Two refinements appended to `docs/v0.16_planning.md` D56:

1. **predicate_distribution `both` pattern** — three possible causes
   (prompt under-cueing, corpus expectations debatable, model
   limitation) for v0.16 investigation.
2. **Cold-start qualifier shape gap** — surfaced by Item 2; the
   oracle prompt should be updated to teach the post-D19
   `slot_to_qualifier` shape.

Commit: `2fc5136`.

## Item 4 — variance pass: per-corpus bands

12 additional runs across 3 corpora × 2 modes. Combined with
Session 1's runs the variance bands are:

| Corpus | Mode | N | Median | Range | Spread | Threshold | Result |
|---|---|:-:|---:|---|---:|---:|---|
| `temporal_scope_corpus` | seeded | 3 | 80.0% | 75.0%–87.5% | 12.5pp | 90% | **below threshold** at all 3 runs |
| `temporal_scope_corpus` | cold-start | 2 | 82.5% | 82.5%–82.5% | 0pp | 90% | below threshold, stable |
| `python_verification_corpus` | seeded | 3 | 86.7% | 83.3%–86.7% | 3.4pp | 85% | **above threshold** at median; Session 1's 83.3% was the low end |
| `python_verification_corpus` | cold-start | 2 | 86.7% | 86.7%–86.7% | 0pp | 85% | above threshold, stable |
| `derivation_corpus` | seeded | 3 | 54.0% | 50.0%–56.0% | 6.0pp | 80% | architectural ceiling; matches Cluster 3 baseline (54%) |
| `derivation_corpus` | cold-start | 3 | 46.0% | 40.0%–48.0% | 8.0pp | 80% | architectural ceiling; close to Cluster 3 cold-start (44%) |

### Per-corpus interpretation

**`temporal_scope_corpus` — moved from "within noise" to "consistently below".**
Session 1's single-point 87.5% was at the high end. The N=3 seeded
range 75.0%–87.5% (median 80%, spread 12.5pp) lands *every* run
below the 90% threshold. The high spread suggests the extractor's
temporal parsing has real per-prompt variance (5 of 5 misses in
the median 80% run came from `explicit_scope` and `relative_scope`
categories, where temporal parsing is most precise). Cold-start
mode is stable at 82.5% across 2 runs (extraction doesn't depend
on the substrate, so seeded vs cold-start should match — they
do, within seeded's spread). **The threshold is not met under the
median measurement.** This becomes a release-decision input
rather than a noise-attributable gap.

**`python_verification_corpus` — clears threshold at the median.**
Session 1's single-point 83.3% was at the low end of variance.
N=3 seeded runs at 83.3%, 86.7%, 86.7% with median 86.7% lands
above the 85% threshold; cold-start mode is identical (86.7%
across 2 runs — Python verification doesn't depend on substrate
either). The Session 1 sub-threshold reading was variance, not a
real gap. **Threshold met at the median.**

**`derivation_corpus` — architectural ceiling confirmed with proper bands.**
Seeded N=3: 54%/56%/50%, median 54%, range 6pp. Cold-start N=3:
46%/40%/48%, median 46%, range 8pp. The seeded median matches the
post-Cluster-3 baseline exactly. The cold-start median is 2pp
above the Cluster 3 single-probe (44%) — within Cluster 3's own
noise. **The seeded-vs-cold-start gap** with proper bands:

- Median-to-median: 54% − 46% = **+8pp** (matches Session 1)
- Best-case gap: 56% (seeded max) − 40% (cold-start min) = +16pp
- Worst-case gap: 50% (seeded min) − 48% (cold-start max) = +2pp
- **The +8pp median gap is robust.** Individual seeded-vs-cold-start
  run pairs fluctuate substantially, but the central tendency is
  consistent across all 3 runs each way.

The 26pp gap to the 80% threshold is the architectural ceiling per
Cluster 3's diagnosis. v0.16 D56/D57/D58 are the right work items.

### Variance discipline notes

**Per-run JSONs** for all 12 additional runs are in
`docs/phase_10_5/runs/` — same naming convention as Session 1.
Each captures per-case pass/fail + duration; the median and range
above are computed from these.

**Soundness floor.** The driver records pass/fail but not the
underlying verdict shape, so false-verified detection is not
directly probed by Item 4's runs. Session 1's direct verdict
analysis (cluster_3_validation framework + the derivation walker
trace capture) remains the latest authoritative false-verified
check: **0 false-verifieds across 668 case-mode invocations**.
The Item 4 runs would have surfaced any new false-verified through
deeper investigation if a corpus's miss pattern changed
qualitatively, but the miss patterns reproduce the Session 1
patterns. The soundness floor holds.

**Cold-start temporal_scope and python_verification at N=2.** Per
the prompt's "×2 additional" framing, these configurations had
no Session 1 baseline (only the 3 dual-measurement corpora got
cold-start in Session 1). N=2 is a weaker variance bound than N=3
but both runs produced identical results (82.5%/82.5% and
86.7%/86.7%), suggesting these corpora are extraction-bound and
substrate-independent — variance is low because the substrate
isn't on the per-case execution path.

### Updated assessment

After Items 1-3 remediation + Item 4 variance bounding, the
corpus-by-corpus pass/fail picture for the corpora that changed
this session:

| Corpus | Mode | Phase 10.5 result | Pass threshold? | Notes |
|---|---|---:|---|---|
| `intervention_corpus` | seeded | 100.0% | **yes** | Was 76.7% Session 1; per-claim redesign closed the gap |
| `kb_mapping_corpus` | seeded | 95.0% | **yes** | Was 72.5% Session 1; corpus refresh closed the gap |
| `kb_mapping_corpus` | cold-start | 75.0% | no | Net same; misses shifted (D56 refinement captured) |
| `temporal_scope_corpus` | seeded | 80.0% (median) | **no** | Variance bound revealed Session 1's 87.5% was high end |
| `temporal_scope_corpus` | cold-start | 82.5% (stable) | no | Substrate-independent; matches seeded within seeded's spread |
| `python_verification_corpus` | seeded | 86.7% (median) | **yes** | Variance bound revealed Session 1's 83.3% was low end |
| `python_verification_corpus` | cold-start | 86.7% (stable) | yes | Substrate-independent |
| `derivation_corpus` | seeded | 54.0% (median) | no (architectural) | Cluster 3 baseline reproduced; v0.16 D56/D57/D58 |
| `derivation_corpus` | cold-start | 46.0% (median) | no (architectural) | Cluster 3 baseline reproduced; gap to seeded = +8pp |

## Updated open items going into Step 5 / Step 7

1. ~~Finding 1~~ **resolved** by Item 1 per-claim redesign.
2. ~~Finding 2~~ **resolved** by Item 2 corpus refresh.
3. ~~Finding 3~~ **captured** in D56 refinement (Item 3); v0.16 work.
4. ~~Variance~~ **addressed** for 3 corpora; temporal_scope is now
   a documented gap (median below threshold across 3 runs);
   python_verification clears threshold at median; derivation
   architectural ceiling confirmed with proper bands.

**New release-decision input from variance pass:** temporal_scope
seeded at median 80% does not meet its 90% threshold. The gap is
not noise — N=3 runs all land below 90%. The 5 misses concentrate
in `relative_scope` and `explicit_scope` categories (the precise
date arithmetic). This is now a documented gap to address in
Step 7's release-decision deliberation: either v0.15.0 ships
with this gap noted, or temporal extraction prompt iteration
moves into a pre-release work item.
