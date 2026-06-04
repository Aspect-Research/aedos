# v0.16.1 — Autonomous Cycle-2 Results

Fully-autonomous build-verify-build cycle (no operator check-ins) addressing the
observations from the v0.16.1 final Medium Bar. Soundness-first (§3.2: never
false-verify, never false-contradict; abstention is safe). Branch `v0.16.1`.
Start marker: commit `8212f55`. Commits only — no tag, no push.

## What cycle-2 fixed

The v0.16.1 final Medium Bar (commit `8212f55` doc) passed the false-verified
gate (0) but the NEW false-contradicted gate — built this same release (WS1/WS7)
— surfaced **3 false-contradicts** the old FV-only metric was blind to. Cycle-2
closed them, then an adversarial review + a second Medium Bar surfaced and closed
a 4th (pre-existing) false-contradict.

### Targeted false-contradicts (the cycle-2 work-list)

1. **`mhd_002`** "Germany is in the European Union" → was contradicted.
   **C2-1/C2-2** (`cdd475a`): the geographic-disjoint path-b contradiction is now
   gated on the object being a subsumption-confirmed geographic PLACE
   (`_GEO_PLACE_CLASSES`, which deliberately excludes "geographic region" Q82794
   because it subsumes the EU). A union / consortium object can no longer be
   mis-read as a disjoint sub-region. Now abstains. Vatican/Rome/Thames disjoint
   + Paris/Europe verify preserved.
2. **`pt_004`** "Williams College is part of the Consortium…" → was contradicted.
   Same C2-1/C2-2 place gate (part_of/P361 is a location property). Now abstains.
3. **`pt_006`** "France was founded in 843" → was contradicted.
   **C2-3** (`2e851c0`): a single_valued predicate contradicts only on exactly ONE
   distinct mismatch value. France P571 holds {843, 1958}; a claim matching
   neither is not a functional conflict → abstain, never contradict a value the
   KB holds.

`C2-4` (`9b2eea8`) also hardened the extractor against null slots (the ed_005
crash behind the entity_disambiguation dip): a null subject/predicate/object/
source_text slot is coerced (never crashes `_build_claim`), an empty subject
abstains (subject_absent_from_source), and one malformed raw claim is skipped
rather than aborting the batch.

## Adversarial review round (read-only, on the cycle-2 changeset)

Two-dimension review (soundness of the 3 fixes + regression/completeness), each
finding adversarially verified against the code:

- **C2S-1** (real, patched — `c8911e6`): the C2-3 multi-value distinctness set
  keyed on RAW date strings, so two same-year statements at differing precision
  (a coarsening of one date) counted as multi-valued and a genuinely wrong-year
  claim over-abstained. Fix: key the set on the year-normalized value for date
  predicates. Cures an over-abstention; cannot false-contradict (the VERIFY
  match-any loop runs first) or false-verify.
- **C2S-2** (dismissed — not a bug): the geo place gate excludes sub-national
  regions, a deliberate documented soundness trade; widening would reopen the
  EU false-contradict. No change.
- **C2-COMPLETENESS-1** (real, patched — `06a9c86`): the 3 cycle-2 shapes were
  pinned only by unit tests, leaving the durable WS7 offline harness net (the CI
  gate on `false_contradicted == 0`) blind to exactly the failure class cycle-2
  closed. Added all 3 (+ the C2-FC1 shape) through the same
  `compute_metrics`/`soundness_gates` path; each pin is non-vacuous (revert →
  contradicted → trips the gate).

## Final Medium Bar round 1 → C2-FC1 (a 4th, pre-existing false-contradict)

The first final Medium Bar (`v161_c2_final`) confirmed the 3 targets fixed
(mhd_002/pt_004/pt_006 all abstain) and held false_verified == 0, but surfaced
**`csu_003`** "Asa works at a university that was founded before 1800."
(gt=verified) → contradicted (`false_contradicted = 1`).

Diagnosis (live, root-caused; **pre-existing, not a cycle-2 regression** — the
verdict path predates `8212f55` and no cycle-2 change affects it): the extractor
sometimes maps "founded **before** 1800" to a `founded_in_year` claim whose
literal object is the comparison phrase "before 1800", and the vague subject
"a university" resolves (via multi-hop discovery) to one arbitrary specific
entity — founded 2001 — whose date the single_valued comparison then refutes,
losing the inequality and the intended referent (Asa's employer, Williams College
1793, for which the claim is TRUE).

Two complementary §3.2 guards (`c1b744a` + `31bf439`):

- **Verifier guard** (`_compare_positive`, `object_not_a_parseable_date`): a
  single_valued DATE predicate may contradict only when the claim's object parses
  to a comparable year; a comparison phrase / non-year object is a PARSE failure,
  not falsity → abstain. The date analog of the entity `value_unresolved` (N1)
  guard. Covers the in-statements path for any subject.
- **Walk-level guard** (`walk()` verdict chokepoint,
  `vague_subject_existential`): never emit CONTRADICTED for a claim whose SUBJECT
  is a vague/indefinite reference. Such a subject is an EXISTENTIAL: resolving it
  to one arbitrary entity cannot soundly REFUTE it ("a university founded before
  1800" is true — such universities exist). Symmetric with the vague-OBJECT
  object-conflict guard; covers BOTH the direct and the multi-hop-discovery
  contradiction paths (the live failure arrived via discovery, bypassing the
  verifier guard). Only contradiction is suppressed; an existentially-true
  verified is left intact.

Live confirmation: csu_003 re-sampled 16× post-fix → **0 contradicted** (was
6/16), with the contradicting extraction variant seen 9× — all now abstain.

## Final Medium Bar round 2 (the soundness-gate confirmation)

`v161_c2_final2` (live, warm `aedos_phase10_5.db`). **BOTH HARD GATES PASS.**

| Metric | v0.16.1 final (`8212f55`) | C2 final R1 (`v161_c2_final`) | **C2 final R2 (`v161_c2_final2`)** |
|---|---|---|---|
| **false_verified** | 0 | 0 | **0 — PASS** |
| **false_contradicted** | 3 (GATE FAIL) | 1 (GATE FAIL: csu_003) | **0 — PASS** |
| OVERALL SOUNDNESS | FAIL | FAIL | **PASS** |
| Accuracy | 60.7% | 59.8% | 62.3% |
| false-abstain | 46.4% | 48.8% | 47.6% (40) |
| principled_abstention | 100% | 100% | 100% |

All 5 surfaced cases now land in the §3.2-safe direction (no contradiction):
mhd_002 / pt_004 / pt_006 / csu_003 / ed_005 → all `no_grounding_found`. Zero
false-contradicts anywhere in the run. (The medium bar is non-deterministic —
LLM extraction + live SPARQL + codegen — so per-case accuracy fluctuates run to
run; the soundness GATES are the acceptance criterion and both hold. Mode-level
accuracy dips (entity_disambiguation, multi_hop, predicate_translation) are
dominated by cold-start budget abstains and eval noise, in the §3.2-safe
abstain direction, not new false verdicts.)

**Cycle-2 outcome: false_contradicted 3 → 0 and false_verified held at 0 — the
soundness invariant is restored and both hard gates pass.**

## Cycle-2 commits (on `8212f55`)

| Commit | Change |
|---|---|
| `cdd475a` | C2-1/C2-2 geo-disjoint place gate |
| `2e851c0` | C2-3 multi-value single_valued |
| `9b2eea8` | C2-4 extractor null-slot guard |
| `c8911e6` | C2S-1 review patch (date-normalized distinctness set) |
| `06a9c86` | C2-COMPLETENESS-1 review patch (offline-net pins) |
| `c1b744a` | C2-FC1 verifier object-parse guard |
| `31bf439` | C2-FC1 walk-level vague-subject contradiction guard |

Gated suite green throughout (1615 passed, 1 xfailed, 1 xpassed — the
pre-existing v0.15 sandbox boundaries).
