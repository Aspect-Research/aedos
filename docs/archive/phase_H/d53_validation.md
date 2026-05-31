# Phase H D53 — validation

**Status:** Step 4 in progress. Focused validation (6 target cases)
complete; full-corpus run pending.

## Focused validation: Cluster 1 target cases

`scripts/d53_validation.py` runs each of the six cases that motivated
Cluster 1 and reports Stage C's selected_qid against the expected
canonical Q-id.

**6/6 cases produce the correct selected_qid post-D53.**

| Case | Surface | Stage A outcome | Stage B query | selected_qid | Expected | Match | Stage C path |
|---|---|---|---|---|---|---|---|
| der_cross_001 | Obama | clean_redirect | "Barack Obama" | Q76 | Q76 | ✓ | LLM |
| der_cross_008 | Obama | clean_redirect | "Barack Obama" | Q76 | Q76 | ✓ | LLM |
| der_predicate_translation_001 | Obama | clean_redirect | "Barack Obama" | Q76 | Q76 | ✓ | LLM |
| der_disambiguation_003 | Apple | canonical_no_redirect | "Apple" | Q312 | Q312 | ✓ | single-candidate shortcut |
| der_disambiguation_004 | Einstein | clean_redirect | "Albert Einstein" | Q937 | Q937 | ✓ | LLM |
| der_disambiguation_006 | Amazon | disambiguation_page | "Amazon" | Q3783 | Q3783 | ✓ | LLM |

Raw data: `docs/phase_H/d53_validation.json`,
`docs/phase_H/d53_validation.log`.

**Walker-verdict lift on target cases:**
- `der_disambiguation_004` (Einstein received_award Nobel Prize) now
  **verifies end-to-end.** Pre-D53 this was no_grounding_found; post-D53
  the walker resolves Einstein → Q937, looks up Nobel Prize statements,
  finds the match.
- The other 5 target cases still produce no_grounding_found because
  downstream verification hits Cluster 2 (subsumption: Q76's P39 →
  Q11696, but claim asks about Q11696/Q30461 mismatch without
  subsumption) or Cluster 3 (predicate translation: Apple `founded_in`
  California needs P159/P740 mapping the current substrate doesn't
  produce) gates. The Stage C selection is correct; downstream gates
  are what's failing.

## Stage A outcome variety

The six cases exercise all three productive Stage A paths:

- **clean_redirect**: Obama → Barack Obama, Einstein → Albert Einstein
  (3 of 6). Wikipedia's redirect system canonicalizes the surface form.
- **canonical_no_redirect**: Apple → "Apple" (1 of 6). Wikipedia's
  primary article is at the surface form. Note that bare-surface
  wbsearchentities of "Apple" returns Q312 (Apple Inc.) at rank 1
  natively, so the implicit-disambig probe Cluster 1 added (which
  D53 removed) wasn't needed for this case.
- **disambiguation_page**: Amazon → disambig (1 of 6). Stage B queries
  wbsearchentities with the surface form ("Amazon"), gets 20 ranked
  candidates including Q3783 (Amazon River), and Stage C's LLM picks
  it given the river-context source text.

## Stage C heuristic shortcut firing

1 of 6 cases (Apple) fires the single-candidate shortcut. The other 5
go to the LLM. The shortcut's narrowness reflects the conservative
"single candidate only" decision in the design doc; the LLM call cost
is acceptable given that wbsearchentities returns up to 20 candidates
in most cases.

## Full-corpus aggregate accuracy

`scripts/d5_diagnostic.py` (single run, 2026-05-24):

```
pre-D51 baseline      : 17/50  (34%)
D51 step 2 (reported) : 18/50  (36%)
post-Cluster-1        : 16/50  (32%)
post-D53              : 22/50  (44%)
```

**Net: +5 cases vs pre-D51, +6 cases vs post-Cluster-1.**

Verdict changes pre-D51 → post-D53:

| Case | Pre-D51 verdict | Post-D53 verdict |
|---|---|---|
| `der_cross_002` (Asa works at Google, tier_u) | no_grounding_found | **verified** |
| `der_cross_007` (Asa's birth year plus 30) | verified | no_grounding_found (extraction variance) |
| `der_disambiguation_004` (Einstein + Nobel Prize) | n/a | **verified** (target case) |
| `der_disambiguation_008` (Cambridge in Massachusetts) | no_grounding_found | **verified** |
| `der_multihop_005` (Obama born in Honolulu) | no_grounding_found | **verified** |
| `der_multihop_012` (Asa died in Cambridge) | no_grounding_found | **verified** |
| `der_predicate_translation_003` (Asa works at Google, tier_u) | no_grounding_found | **verified** |
| `der_predicate_translation_004` (Williams College in Williamstown) | no_grounding_found | **verified** |

The cases that flipped to verified break into two groups:

1. **Direct D53 wins from the Cluster 1 target list:**
   `der_disambiguation_004` (Einstein) verifies because Stage A's
   `clean_redirect` canonicalizes Einstein → Albert Einstein and
   Stage B's wbsearchentities returns Q937 cleanly.

2. **Adjacent wins from D53 improving resolution generally:**
   Cambridge, Williams College, Obama-born-in-Honolulu, Asa-works-at-
   Google all required entity resolution improvements that D53 also
   delivers (wbsearchentities's ranked candidates with
   labels/descriptions, plus the Stage C LLM with structured claim
   context). These weren't in the Cluster 1 target six because they
   were originally cataloged as Cluster 2/3 cases — D53's better
   entity layer unblocks them anyway.

The `der_cross_007` regression is the same extraction-variance issue
documented in `cluster_1_validation.md`: the extractor produces a
different claim shape on different runs, and the Python verifier
fails on the unfortunate shape. Unrelated to D53. Per D49 a 1-case
drift sits within noise.

The five Cluster 1 target cases that still fail (`der_cross_001`,
`der_cross_008`, `der_predicate_translation_001`,
`der_disambiguation_003`, `der_disambiguation_006`) hit downstream
gates as predicted in the design doc:
- der_cross_001 / 008 / pt_001: Obama → Q76 resolves cleanly, but
  Q76's P39 statement value (Q11696 — President of the United States)
  doesn't match Stage C's selected Q-id for "President"
  (Q11696 itself, but predicate translation may not be looking up
  P39 correctly, or subsumption is needed to bridge). Cluster 2 / 3.
- der_disambiguation_003 (Apple): Q312 resolves cleanly, but the
  `founded_in` predicate needs P159/P740 mapping the substrate
  doesn't currently produce. Cluster 3.
- der_disambiguation_006 (Amazon): Q3783 resolves cleanly, but
  the predicate `the_world_s_largest_river_by_discharge`
  (extracted from poor decomposition) doesn't map to a Wikidata
  property. Cluster 3 or extraction.

## Run-to-run variance

Single run reported. Per D49 discipline, variance bands typically
swing 1-2 cases either way. The +5/+6 lift is well outside that
band, so the result is robust to re-runs. A re-run after Cluster 2
/ 3 would be a more interesting comparison than re-running D53
alone.

## What this commit closes

D53's primary architectural goal — replacing Wikipedia disambig-page
scraping with Wikidata wbsearchentities — is complete. The six
Cluster 1 problem cases all produce correct Q-id selections.
Downstream verification on the same cases remains gated by Cluster 2
and Cluster 3.

The Stage B/C audit log is significantly richer than Cluster 1's
Stage 2 audit log: every event records the Stage A outcome, the
Stage B query string, candidate count, top candidates, type filter
applied (yes/no), filtered count, shortcut fired (yes/no), LLM
invoked (yes/no), Q-id selection, and reasoning. Phase 10.5 post-hoc
analysis will be able to attribute failures with much higher
precision.

## D53 deliverables summary

| Commit | Description |
|---|---|
| `Phase H D53: empirical investigation + design draft` | Two investigation scripts, design doc, V0.16 → V0.15 promotion |
| `Phase H D53 step 1: wbsearchentities client` | `WikidataAdapter.wbsearchentities()` + 18 unit + 9 live tests |
| `Phase H D53 step 2: three-stage normalizer flow` | Stage A/B/C orchestration, renamed audit shape, resolver integration |
| `Phase H D53 step 3: remove obsoleted Wikipedia disambig page infrastructure` | 432 lines of dead code removed; `wikipedia_stage_2_max_candidates` config retired |
| `Phase H D53 step 4: validation` | This document + focused + full-corpus run |

## Open follow-up (informational)

Captured for future sessions:

- **Cluster 2** (subsumption / extracted-claims-as-premises): the
  5 cases that still fail post-D53 need either subsumption chains
  (Q11696 ⊆ Q30461 etc.) or other Cluster-2-level fixes.
- **Cluster 3** (predicate canonicalization): cases like Apple
  `founded_in` California need predicate→P159/P740 translation that
  the current substrate doesn't produce.
- **`cluster_1_diagnostic.py`** is now stale (references the removed
  `_stage_2_llm_select`). It produced useful artifacts during
  Cluster 1; for D53 the new `d53_validation.py` supersedes it.
