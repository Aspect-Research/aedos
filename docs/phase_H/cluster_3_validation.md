# Phase H Cluster 3 — validation (dual-measurement)

**Status:** DIAGNOSTIC + REMEDIATION COMPLETE. The first probe pair
(seeded 48%, cold-start 44%) surfaced a smaller-than-predicted +4pp
lift; the diagnostic ([cluster_3_diagnostic.md](cluster_3_diagnostic.md))
classified the remaining ceiling cases by tunability. Cluster 3 step 7
addresses the promotion-shadow-prior walker pattern and step 8 adds
article stripping; both landed with unit-test coverage. Post-step-7+8
probe data is collected once the validation re-run completes.

The variance-bound runs (2 additional per mode per D49 discipline)
are deferred to Phase 10.5 — Phase H closes with the single-probe
data + diagnostic + remediation pattern documented, and Phase 10.5
inherits the dual-measurement framework for its release-decision data.

Harness: `scripts/cluster_3_validation.py`. Per-run JSON in
`docs/phase_H/cluster_3_validation_run_*.json`.

## What Cluster 3 closes (audit framing)

The cluster_2 validation surfaced two upstream bottlenecks
([cluster_2_validation.md](cluster_2_validation.md) Findings 1-2):

- The LLM predicate-translation oracle was mis-classifying
  KB-mappable predicates (`holds_role`, `capital_of`, `located_in`,
  `received_award`, `has_capital`) as `user_authoritative` because
  the corpus runner's in-memory DB never carried the seed pack's
  hand-curated metadata; every consultation cold-started.
- Belief-revision (R4) cases miss universally because the extractor's
  surface predicates (`works_at`, `joined`, `ended`) don't match the
  seeded canonical forms (`employed_by`, `status`) at walker's Stage 1
  literal lookup.

Cluster 3 attacks both by:

1. **Step 1.** `database.create_schema(load_seeds=True)` auto-loads
   the seed pack at DB-open time; production deployments and the
   corpus runner's seeded mode get the 83-entry seed pack populating
   `predicate_translation` before any case fires. `open_db()` defaults
   to True (production); `open_memory_db()` defaults to False (test
   convention); the corpus runner explicitly opts in via
   `_Harness(seeded=True)`. An audit `seeds_loaded` event records the
   path + entry count for trace reconstruction.
2. **Step 2.** 19 seed-pack alias rows added (`works_at` → P108,
   `received_award` → P166, `birthplace_is` → P19, etc.) so the
   walker's Stage 3 predicate-translation broadening bridges
   extractor surface forms to the canonical KB property without
   requiring extraction-time rewriting. The `normalize_predicate`
   function now accepts underscored input equivalently to space-
   separated, so `works_at` and `works at` produce the same canonical
   predicate. The `_KNOWN_DRIFT` allowlist shrinks from 29 entries
   to 6 — the residual entries map to Wikidata properties not yet
   seeded (P37 official_language, P576 dissolved, P749
   parent_organization, P800 notable_work, plus the multi-author
   `co_founded` variant).
3. **Step 3.** v5 extractor prompt extended with rules 12-14 for
   verb-shape variants: `joined`/`was hired by`/`started at` →
   `employed_by` with valid_from; `left`/`quit`/`resigned from` →
   `employed_by` with valid_until; `ended`/`began` on state-bearing
   subjects → `status` with valid_until/valid_from. Explicit
   non-trigger conditions (per D45) prevent over-application to
   non-employment groups, physical departures, and one-time
   historical events.
4. **Step 4.** Two seed-pack semantic corrections: `located_in`
   P276 → P131 (administrative territorial; matches all three
   primary corpora's expected mapping); `occurred_in` P585 → P276
   (P585 was internally inconsistent — a qualifier property paired
   with object_type=entity; corpus uses occurred_in for
   event-in-location semantics).
5. **Step 5 (this doc).** Dual-measurement validation framework
   in place — corpus runner's `_Harness(seeded: bool)` controls
   which mode runs; `scripts/cluster_3_validation.py --mode {seeded
   | cold-start | both}` exercises either. Phase 10.5 inherits the
   framework for its release-decision data.

## Dual-measurement framing

The two modes measure different system properties; neither is "the"
Aedos number alone:

- **Seeded (primary).** Production deployments behave this way for
  predicates in the seeded vocabulary. The measurement reflects "what
  Aedos actually does for its designed vocabulary." Per-case
  variance is bounded by KB nondeterminism and the LLM walker's path
  selection — `predicate_translation` is fixed substrate.
- **Cold-start (secondary).** Production deployments behave this way
  the first time they see a novel predicate; subsequent calls cache
  the LLM oracle's judgment, so cold-start is essentially "first
  encounter" behavior. Per-case variance is higher (LLM oracle
  output is non-deterministic per D49). This measures the system's
  robustness on vocabulary the seed pack doesn't anticipate.
- **Hybrid (tertiary, v0.16).** Mixed-vocabulary realistic deployment
  — some predicates seeded, some novel. Out of scope for v0.15;
  captured as a v0.16 candidate.

The relationship between the seeded and cold-start numbers is itself
informative. A large gap suggests the LLM oracle has room for
improvement on cold-start cases (v0.16 work); a small gap suggests
the LLM oracle is already doing well on novel predicates and the
seed pack is mostly a performance / determinism optimization.

## Probe results

### Seeded probe (run 1)

**Result: 24/50 = 48%.** Per-run JSON in
`cluster_3_validation_run_20260526T182317Z.json`. Walltime ~9
minutes.

| rule | C3 seeded | C2 baseline (avg of 3) | delta |
|---|---|---|---|
| NON_STANDARD | 1/4 | 1/4 | 0 |
| OVERRIDE | 1/1 | 1/1 | 0 |
| R1 (KB/Python explicit) | 1/5 | 1/5 | 0 |
| R2 (KB-likely-upgrade) | 6/13 | 3.67/13 | **+2-3** |
| R3 (fictional → asserted) | 12/19 | 12.67/19 | -1 |
| R4 (belief revision) | 1/6 | 0/6 | **+1** |
| R6 (future tense) | 2/2 | 2/2 | 0 |
| **total** | **24/50** | **~22/50 (44%)** | **+2 (48% vs 44%)** |

**Cluster 3 audit events fired (one run):**
- `tier_u_status_upgraded`: 6 cases — Q-Lookup α upgrades firing
  correctly with the seeded `kb_resolvable` routing (the Q-UserAuth
  short-circuit no longer mis-classifies these as user-authoritative).
- `cross_source_contradiction`: 1 case — `der_revision_001`
  ("Asa prefers coffee" vs prior `prefers tea`) now correctly fires
  the §"KB wins" cross-source mechanism. With `prefers` properly
  seeded as user_authoritative + single_valued=1, the promotion-time
  contradiction is detected and the walker is skipped.
- `walker_skipped_due_to_pre_verdict`: 1 case — the
  cross_source_contradiction case's pre-verdict path.

**What lifted vs Cluster 2:**

1. **Pattern A (Q-UserAuth) cases unstuck (+2 R2).** Cluster 2's
   Finding 1 identified 6 R2 cases where walker hit its own promoted
   row and the LLM-driven predicate-translation oracle mis-routed
   KB-mappable predicates (`holds_role`, `capital_of`) to
   `user_authoritative`. With seeded routing, `der_cross_001` (Obama
   President 2009-2017) and `der_multihop_011` (Obama President +
   distribution) now hit the KB-resolvable path correctly and emit
   `verified` instead of `verified_given_assertion`.

2. **R4 cross-source revision case (+1 R4).** `der_revision_001`
   ("Asa prefers coffee" + prior "Asa prefers tea") now produces
   `contradicted` via cross_source_contradiction. The walker is
   skipped at promotion time because the §"KB wins" mechanism
   detects the contradiction directly. Pre-Cluster-3, `prefers` was
   absent from the empty `predicate_translation` table, so the
   walker's lookup_object_conflict path couldn't broaden via Stage 3
   to bridge the prior and new claims.

**Where the lift was smaller than predicted:**

The operator's brief predicted +10-20pp; the actual seeded lift is
+2-4pp (48% vs 44%). Three observed reasons:

a. **4 of 6 Pattern A cases still miss** (`der_disambiguation_002`
   Paris-capital-France, `der_disambiguation_008` Cambridge-in-MA,
   `der_predicate_translation_005` Marie Curie Nobel,
   `der_predicate_translation_006` France has_capital Paris). For
   these, even with seeded routing, the walker matches its own
   promoted row at Stage 1 and emits `verified_given_assertion`
   rather than KB-upgrading. The seed pack fix is necessary but
   not sufficient for Q-Lookup α to fire reliably on these. Likely
   walker policy issue (when to upgrade vs when to short-circuit
   on the promoted row); v0.16 candidate.

b. **R4 belief-revision cases beyond `prefers`.** `der_revision_002`
   (Asa works_at Google vs prior employed_by Microsoft) — the
   extractor now produces `employed_by` via the `works_at` alias
   (Step 2), but the walker's Stage 1 lookup finds the freshly-
   promoted asserted_unverified row, not the prior `employed_by
   Microsoft` row. The two `employed_by` rows coexist; single_valued
   conflict detection at Tier U write time would need to fire here
   but doesn't because the new claim doesn't trigger the
   lookup_object_conflict path for the prior. Likely a Tier U write
   ordering issue or single-valued-conflict detection gap; v0.16.

c. **`der_revision_005` (project status).** Got
   `abstained_given_assertion`. Step 3's Rule 14 fires correctly:
   extractor produces `predicate=status, object=ended,
   valid_until=2024`. But the prior is `subject="project"`, the
   extracted is `subject="The project"` (with article). Subject
   normalization between text-extracted and seeded Tier U is
   inconsistent for non-named-entity subjects. The Wikipedia
   normalizer doesn't apply here because "the project" isn't a
   Wikidata entity. v0.16 candidate — subject normalization for
   common nouns.

d. **`der_revision_006` (Asa joined Google in 2020).** Got
   `verified` (KB upgraded — not the expected `contradicted`
   scope-conflict). Step 3's Rule 12 produces `employed_by` with
   `valid_from=2020`. The walker upgrades to KB (Wikidata says
   Asa is not in KB, but the predicate routing succeeds enough to
   produce `verified`). Scope-conflict detection (same employer,
   different valid_from) is not currently in the walker's
   belief-revision check. v0.16 candidate.

### Cold-start probe (run 1)

**Result: 22/50 = 44%.** Per-run JSON in
`cluster_3_validation_run_20260526T183428Z.json`. Walltime ~8.5
minutes.

The cold-start probe is essentially flat with the Cluster 2
baseline (44%). Every predicate consultation triggered an LLM
oracle call; the seed pack was absent. This confirms that the
Cluster 3 prompt extensions (step 3 rules 12-14), seed-pack
expansion (step 2 aliases), and semantic corrections (step 4) did
not regress the cold-start path — the LLM oracle continues to
generate similarly-accurate metadata for novel predicates as it did
pre-Cluster-3.

The dual-measurement framework's design intent is reflected in this
data: cold-start measures "what the LLM oracle can do unaided" and
the metric is stable across the Cluster 3 changes.

### Comparison and pre-remediation triage

The +4pp seeded-vs-cold-start gap (48% vs 44%) was smaller than
the operator brief's conservative +10-20pp prediction. The
diagnostic ([cluster_3_diagnostic.md](cluster_3_diagnostic.md))
classified the remaining ceiling cases by tunability:

- **Category 1 (4 R2 cases):** Walker upgrade policy reaches
  `_try_external_grounding` but KB returns non-verified for
  inverted-direction predicates (capital_of), ambiguous subjects
  (Cambridge), or qualifier-shape mismatches (Marie Curie Nobel
  year). Bounded code work in KB verifier / entity resolver. v0.16
  candidates (discrete D-items per area).
- **Category 2 (3 R4 cases):** Walker promotion-shadow-prior
  pattern — the promote-then-walk introduced by Cluster 2 means
  the walker matches its own promoted asserted_unverified row at
  Stage 1 before the belief-revision checks fire. Step 7 addresses
  this by restructuring `_direct_lookup` to check polarity-conflict
  and object-conflict against PRIORS first (with own promotion
  excluded from the flipped lookup), then fall through to Stage 1
  (no exclusion) so R3 cases still match own promotion as the
  in-vocabulary grounding source.
- **Category 3 (1 R4 case):** Subject normalization for common
  nouns. Step 8 adds article stripping in `TierU._normalize_slot`
  so "The project" and "project" canonicalize identically.
- **Category 4 (1 R4 case):** `employed_by` single_valued semantic
  question. Deferred to v0.16 D57 (functional-at-a-point-in-time
  cardinality).

## Post-step-7+8 probe

**Result: pending** — second seeded probe in flight; data lands once
complete. Expected post-step-7+8 gains (per the diagnostic's
predictions):

- **der_revision_003** (polarity_conflict against externally_verified
  prior): walker now reaches flipped lookup before Stage 1 → finds
  prior → returns `contradicted` (plain, not _given_assertion
  because prior is externally_verified). Expected PASS.
- **der_revision_004** (idempotent "Asa is still a student"):
  bypass_normalizer on the seed write (Step 7) means seed and
  promotion canonicalize identically → single row, status
  externally_verified. Walker's flipped check misses; Stage 1 hits
  externally_verified prior → returns `verified`. Expected PASS.
- **der_revision_005** ("The project ended in 2024" + prior status
  ongoing): Step 8 strips "The" from "The project" → subject
  matches prior "project" → walker's object_conflict check fires
  on `status` (functional, single_valued=1) → returns
  `contradicted` (plain, prior externally_verified). Expected PASS.
- **der_revision_006** (scope_conflict): TierU.write detects same
  key + different valid_from + externally_verified prior → §"KB
  wins" fires at promotion time → pre_verdict=contradicted →
  walker skipped → returns `contradicted`. Expected PASS.

Projected post-fix accuracy: 28/50 = 56% (+4 R4 cases relative to
the first seeded probe's 24/50). Phase 10.5's 3x2 variance runs
confirm.

## Full variance-bound results (3 runs per mode)

*pending operator decision on whether to commit to the full schedule
after the probe data is in*

## What Cluster 3 closes vs. v0.16 deltas

- D46 (calibration corpus vs seed-pack normalization) is largely
  closed by Step 2's seed-pack aliases. Residual entries (P37, P576,
  P749, P800, `co_founded`) are captured as v0.16 candidates in the
  updated D46 entry.
- Tertiary measurement (hybrid mode) is a new v0.16 candidate.
- Step 3's prompt rules may surface extractor regressions on edge
  cases — Phase 10.5's extraction_corpus run validates this.

## What's NOT closed

- The cold-start LLM oracle's calibration quality on novel predicates
  is unchanged by Cluster 3. Cold-start measurements still reflect
  the same LLM-driven generation behavior we measured throughout
  Phase H. v0.16 may iterate on the predicate-translation oracle's
  prompt if the cold-start measurement is materially below the
  seeded number.
- `der_cross_008` (Cluster 2's verdict-family flake) and other
  KB-nondeterminism artifacts are not addressed by Cluster 3 — they
  affect both modes equally.
- Phase H is closed by Cluster 3 from a *capability* standpoint, but
  the rc.11 tag waits for the Phase 10.5 calibration pass per the
  operator's standard discipline.
