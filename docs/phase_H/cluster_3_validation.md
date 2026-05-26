# Phase H Cluster 3 — validation (dual-measurement)

**Status:** PROBE PHASE — the harness ran a single seeded probe and a
single cold-start probe. Both modes are wired and produce results.
The variance-bound runs (2 additional per mode, totaling 3 per mode
per D49 discipline) are pending operator decision based on the
probe results. Full numbers and per-case tables land here once the
3×2 schedule completes.

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

*(filled after the probe runs complete)*

### Seeded probe (run 1)

*pending*

### Cold-start probe (run 1)

*pending*

### Comparison and triage

*pending*

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
