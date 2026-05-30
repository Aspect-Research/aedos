# Phase 10.5 — corpus integrity snapshot

**Captured:** 2026-05-26 (Phase 10.5 Session 1, Step 2).
**Build state:** `cacb403` (Phase H Cluster 3 step 6).
**Methodology:** Each calibration corpus is parsed line-by-line (JSONL),
its case count tallied, and the last commit that touched it recorded. The
dry-run corpus pass (D24 mechanism — `pytest tests/calibration/ -q
--run-calibration` with the stub harness) was exercised end-to-end against
all 11 corpora and emitted zero structural errors.

This is the as-measured corpus state Phase 10.5's measurements run against.
Any change to the corpora after this snapshot's commit invalidates the
snapshot and requires a re-run.

## Inventory

| Corpus | Cases | Bad rows | Top-level keys | Last-modifying commit |
|---|---:|---:|---|---|
| `extraction_corpus.jsonl` (cleaned) | 53 | 0 | `category`, `expected_predicate`, `id`, `input`, `notes` | `1194509` 2026-05-23 — Phase E5 |
| `extraction_corpus_v0.jsonl` (raw reference) | 57 | 0 | same as cleaned | `1194509` 2026-05-23 — Phase E5 |
| `predicate_metadata_corpus.jsonl` | 80 | 0 | `aedos_predicate`, `expected_metadata`, `id`, `notes` | `bc47c4e` 2026-05-17 |
| `temporal_scope_corpus.jsonl` | 40 | 0 | `category`, `expected_scope`, `id`, `notes`, `text` | `bc47c4e` 2026-05-17 |
| `entity_resolution_corpus.jsonl` | 50 | 0 | `category`, `expected_output`, `id`, `input`, `notes` | `bc47c4e` 2026-05-17 |
| `kb_mapping_corpus.jsonl` | 40 | 0 | `category`, `expected_output`, `id`, `notes`, `predicate` | `bc47c4e` 2026-05-17 |
| `subsumption_corpus.jsonl` | 60 | 0 | `category`, `expected_output`, `id`, `input`, `notes` | `bc47c4e` 2026-05-17 |
| `predicate_distribution_corpus.jsonl` | 50 | 0 | `category`, `expected_output`, `id`, `input`, `notes` | `bc47c4e` 2026-05-17 |
| `derivation_corpus.jsonl` | 50 | 0 | `category`, `expected_output`, `id`, `input`, `notes` | `750b22c` 2026-05-24 — Phase H Cluster 2 step 5 |
| `python_verification_corpus.jsonl` | 30 | 0 | `category`, `expected_output`, `id`, `input`, `notes` | `bc47c4e` 2026-05-17 |
| `consistency_check_corpus.jsonl` | 25 | 0 | `category`, `expected_output`, `id`, `input`, `notes` | `bc47c4e` 2026-05-17 |
| `intervention_corpus.jsonl` | 30 | 0 | `category`, `expected_output`, `id`, `input`, `notes` | `bc47c4e` 2026-05-17 |

**Total scored corpora:** 11 (the raw `extraction_corpus_v0.jsonl` is a
historical reference, not measured).
**Total cases:** 528 across the 11 scored corpora.

## Drift relative to Cluster 3 validation

- `derivation_corpus.jsonl` was last modified by Cluster 2 step 5
  (`750b22c`, 2026-05-24). The post-Cluster-3 single-probe data
  (`cluster_3_validation.md`: seeded 54%, cold-start 44%) ran against
  this corpus version. Phase 10.5's derivation measurements run against
  the same version — no drift.
- All other corpora are at `bc47c4e` (2026-05-17), pre-dating every Phase
  E / F / G / H change. No drift.
- The `extraction_corpus.jsonl` (cleaned 53-case) is the configuration
  Phase E5 calibrated against (1194509). `extraction_corpus_v0.jsonl`
  (raw 57-case) is preserved for reference; the 4 removed cases'
  rationale is in `docs/v0.16_planning.md` D44.

## Seed-pack predicate coverage

`tests/unit/test_seed_pack_predicate_coverage.py` (4 tests) and
`tests/unit/test_runbook_thresholds.py` (3 tests) are green at the
snapshot commit. The seed-pack-vs-corpus invariants Cluster 3 step 2 and
step 4 established hold:

- Every reference-corpus predicate is either seeded or documented in the
  `_KNOWN_DRIFT` allowlist (the post-Cluster-3 allowlist has 6 entries:
  P37, P576, P749, P800, plus the `co_founded` multi-author variant, plus
  one other; full enumeration in the test fixture).
- Every `_KNOWN_DRIFT` entry actually appears in a corpus (no dead
  allowlist entries).
- Phase G D39 seed additions are present.
- Phase H Cluster 3 step 4 semantic corrections (`located_in` P276 →
  P131; `occurred_in` P585 → P276) are present.

## Structural runner-vs-corpus check (D24 dry-run)

`py -m pytest tests/calibration/ -q --run-calibration` (no `RUN_CALIBRATION`)
exercises every runner against every case of its corpus via the stub
harness. Result at snapshot commit: **11 skipped, 0 structural errors.**
Each runner's skip message confirms case count: extraction 53,
predicate_metadata 80, temporal_scope 40, entity_resolution 50,
kb_mapping 40, subsumption 60, predicate_distribution 50, derivation 50,
python_verification 30, consistency_check 25, intervention 30.

The D24 stub harness does not catch every possible runner-vs-corpus
mismatch (D24 records the limitation: a universal `_Stub` masks
attribute-missing errors that a static key audit would catch). The static
runner-vs-corpus key audit (D24 work item (b)) is v0.16-scope. For Phase
10.5, the D24 mechanism is sufficient: it caught every prior
runner-vs-corpus defect in the audit lineage that the prior reactive
audits found late.

## Documented cold-start exceptions

The 6 `_KNOWN_DRIFT` predicates (above) correspond to predicates whose
seed-pack entry is intentionally absent (Wikidata properties not yet
seeded, or multi-author variants the seed pack does not model). Cases
referencing those predicates will trigger cold-start LLM consultation on
the seeded mode as well as on the cold-start mode — they are documented
exceptions to the seeded-mode assumption that every predicate hits the
seed pack at Stage 1.

## Snapshot summary

- 11 corpora, 528 cases total.
- 0 parse errors.
- 0 structural runner errors.
- Field shapes match what `tests/calibration/test_corpus_runner.py`'s
  per-corpus runners read.
- Seed coverage invariants hold.
- Last corpus modification at `750b22c` (2026-05-24); no drift since
  Cluster 3 step 5's single-probe baseline.

Phase 10.5 Step 4 measurements proceed against this snapshot.
