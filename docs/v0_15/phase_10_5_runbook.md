# Phase 10.5 Operator Runbook

Aedos v0.15 unattended build is complete. This runbook specifies every command
the operator needs to run for Phase 10.5 (calibration pass), in the order to
run them, with expected runtimes and acceptance thresholds.

**Prerequisites before starting Phase 10.5:**
- Phase 10 is tagged `v0.15-phase-10-complete`.
- `ANTHROPIC_API_KEY` is set.
- Wikidata internet access is available.
- Python 3.11+ is installed.
- All Phase 0-10 tests pass: `py -m pytest tests/v0_15/ -q` (no `RUN_LIVE_TESTS`).

---

## Step 0 — Confirm Phase 10 baseline

**Purpose:** Verify the unattended build completed successfully.

```bash
py -m pytest tests/v0_15/ -q
```

**Expected:** All tests pass. Count approximately 592+ passing. No failures.

**Expected runtime:** 2-5 minutes (all mocked, no live calls).

**Acceptance threshold:** 0 failures.

---

## Step 1 — Set environment variables

```bash
# Windows PowerShell
$env:RUN_LIVE_TESTS = "1"
$env:RUN_LIVE_KB = "1"
$env:RUN_CALIBRATION = "1"
$env:ANTHROPIC_API_KEY = "<your-key>"
$env:AEDOS_DB_PATH = "aedos_phase10_5.db"
```

```bash
# Unix/macOS
export RUN_LIVE_TESTS=1
export RUN_LIVE_KB=1
export RUN_CALIBRATION=1
export ANTHROPIC_API_KEY=<your-key>
export AEDOS_DB_PATH=aedos_phase10_5.db
```

---

## Step 2 — Initialize fresh database and load seed pack

**Purpose:** Start with a clean substrate for calibration.

```bash
py -c "from src.aedos_v0_15.database import open_db; open_db('aedos_phase10_5.db')"
py seeds/v0_15/load_seeds.py --db-path aedos_phase10_5.db
```

**Expected output:**
```
Seed version info:
version: 1.0.0
date_reviewed: 2026-05-17
...

Loaded 65 predicate translation seeds into aedos_phase10_5.db
```

**Expected runtime:** < 5 seconds (no LLM calls; pure DB inserts).

**Acceptance threshold:** 65 seeds loaded, 0 errors.

---

## Step 3 — Seed user-context assertions for belief_revision test cases

**Purpose:** The `belief_revision` benchmark cases require Tier U assertions about "Asa".
Insert them directly before running the benchmark.

```python
# Run this Python snippet (or adapt to your deployment harness):
from src.aedos_v0_15.database import open_db
from src.aedos_v0_15.layer4_sources.tier_u import TierU
from src.aedos_v0_15.layer3_substrate.predicate_translation import PredicateTranslation

db = open_db("aedos_phase10_5.db")
# NOTE: these assertions use routing_hint=user_authoritative; seeded directly.
assertions = [
    ("Asa", "lives_in", "Williamstown"),
    ("Asa", "educated_at", "Williams College"),
    ("user", "identity", "Asa"),
]
for subj, pred, obj in assertions:
    db.execute(
        "INSERT OR REPLACE INTO tier_u (subject, predicate, object_val, polarity, "
        "asserting_party_id, asserted_at, valid_from, valid_until, source_text) "
        "VALUES (?, ?, ?, 1, 'operator', datetime('now'), NULL, NULL, 'Phase 10.5 seed')",
        (subj, pred, obj),
    )
db.commit()
db.close()
print("Tier U assertions seeded.")
```

**Expected runtime:** < 5 seconds.

**Acceptance threshold:** 3 Tier U rows inserted, 0 errors.

---

## Step 4 — Run calibration corpora

Run each deferred calibration corpus in phase order. Each corpus is a `.jsonl`
file under `tests/v0_15/calibration/`.

### Phase 1 — Extraction corpus

```bash
py -m pytest tests/v0_15/ -q -k "extraction_corpus" --run-calibration
```

**Expected runtime:** 10-30 minutes (LLM calls for 60 cases).

**Acceptance threshold:** ≥ 85% accuracy across all 60 cases.
- normalization (15): ≥ 85%
- decomposition (10): ≥ 80%
- temporal (15): ≥ 90%
- hard-claim discipline (7): 100%
- first-person (10): ≥ 85%

### Phase 2 — Predicate metadata corpus

```bash
py -m pytest tests/v0_15/ -q -k "predicate_metadata_corpus" --run-calibration
```

**Expected runtime:** 15-45 minutes (LLM + KB calls for 80 cases).

**Acceptance threshold:** ≥ 88% across all 80 cases.
- user_authoritative (20): ≥ 90%
- python (15): ≥ 90%
- kb_resolvable (30): ≥ 85%
- abstain (10): ≥ 90%
- ambiguous (5): ≥ 60% (lower — genuinely hard)

### Phase 3 — Temporal scope corpus

```bash
py -m pytest tests/v0_15/ -q -k "temporal_scope_corpus" --run-calibration
```

**Expected runtime:** 5-15 minutes (40 cases).

**Acceptance threshold:** ≥ 90% across all 40 cases.
- explicit_scope (10): ≥ 90%
- implicit_past (10): ≥ 90%
- relative_scope (10): ≥ 85%
- no_markers (5): ≥ 80%
- future_rejection (5): 100%

### Phase 4 — Entity resolution + KB mapping corpora

```bash
py -m pytest tests/v0_15/ -q -k "entity_resolution_corpus or kb_mapping_corpus" --run-calibration
```

**Expected runtime:** 20-60 minutes (90 cases; Wikidata API calls).

**Acceptance threshold:**
- entity_resolution_corpus (50): ≥ 80%
  - unambiguous (20): ≥ 90%
  - ambiguous (15): ≥ 70%
  - type_filter (10): ≥ 80%
  - no_match (5): ≥ 80%
- kb_mapping_corpus (40): ≥ 85%

### Phase 5 — Subsumption + predicate distribution corpora

```bash
py -m pytest tests/v0_15/ -q -k "subsumption_corpus or predicate_distribution_corpus" --run-calibration
```

**Expected runtime:** 20-45 minutes (110 cases).

**Acceptance threshold:**
- subsumption_corpus (60): ≥ 82%
- predicate_distribution_corpus (50): ≥ 80%

### Phase 6 — Derivation corpus

```bash
py -m pytest tests/v0_15/ -q -k "derivation_corpus" --run-calibration
```

**Expected runtime:** 30-90 minutes (50 cases; multi-hop walks).

**Acceptance threshold:** ≥ 80% across all 50 cases.
- multi_hop_distribution (12): ≥ 80%
- cross_source (10): ≥ 80%
- entity_disambiguation (8): ≥ 75%
- predicate_translation (8): ≥ 85%
- belief_revision (6): ≥ 85%
- abstention (6): ≥ 90%

### Phase 7 — Python verification corpus

```bash
py -m pytest tests/v0_15/ -q -k "python_verification_corpus" --run-calibration
```

**Expected runtime:** 10-20 minutes (30 cases).

**Acceptance threshold:** ≥ 90% across all 30 cases.
- date_arithmetic (10): ≥ 85%
- string_operations (8): ≥ 90%
- numerical_comparison (6): 100%
- list_set_operations (6): ≥ 90%

### Phase 8 — Consistency check corpus (regeneration sub-corpus)

```bash
py -m pytest tests/v0_15/ -q -k "consistency_check_corpus" --run-calibration
```

**Note:** Only the regeneration-convergence sub-corpus (8 cases) is deferred;
the seeded-conflict (10) and circuit-breaker (7) sub-corpora already pass.

**Expected runtime:** 10-20 minutes.

**Acceptance threshold:** ≥ 85% on regeneration-convergence sub-corpus.

### Phase 9 — Intervention corpus

```bash
py -m pytest tests/v0_15/ -q -k "intervention_corpus" --run-calibration
```

**Expected runtime:** 20-45 minutes (30 cases; full pipeline).

**Acceptance threshold:** ≥ 90% intervention-type-classification correctness end-to-end.

---

## Step 5 — Cold-start zero-seed test

**Purpose:** Verify correctness on a fresh substrate with no seeds loaded.

```bash
# Initialize a separate zero-seed database
py -c "from src.aedos_v0_15.database import open_db; open_db('aedos_zero_seed.db')"
AEDOS_DB_PATH=aedos_zero_seed.db py -m pytest tests/v0_15/cold_start/ -v
```

**Expected runtime:** 5-15 minutes (10 claims; first-claim cold-start is expensive).

**Acceptance thresholds:**
- All 10 cases produce expected verdict.
- First-claim latency ≤ 30s.
- Tenth-claim latency ≤ 5s.

---

## Step 6 — Medium-bar evaluation

**Purpose:** Compare Aedos v0.15 against LLM-only baseline on 122-case curated test set.

```bash
py -m tests.v0_15.evaluation.benchmark \
    --test-set tests/v0_15/evaluation/medium_bar_test_set.jsonl \
    --output docs/v0_15/evaluation_results.md
```

**Expected runtime:** 60-120 minutes (122 cases × Aedos pipeline + baseline LLM calls).

**Acceptance thresholds:**
1. Aedos false-verified rate ≤ 5%.
2. Aedos overall accuracy ≥ baseline + 15 percentage points.
3. Aedos accuracy ≥ baseline on every failure mode (no regression).
4. Aedos accuracy ≥ baseline + 20pp on at least 4 of 6 failure modes.

Results written to `docs/v0_15/evaluation_results.md`.

Run 3 times and report median to account for LLM non-determinism.

---

## Step 7 — Tag v0.15.0

If all Phase 10.5 acceptance thresholds pass:

```bash
git add docs/v0_15/evaluation_results.md
git commit -m "v0.15 Phase 10.5: calibration pass + evaluation results"
git tag v0.15.0
```

**Do NOT tag v0.15.0 if any acceptance threshold fails.** Investigate the
failure, fix the root cause, re-run the relevant sub-step, and then tag.

---

## Estimated total Phase 10.5 runtime

| Step | Estimated time |
|---|---|
| Steps 0-3 (setup) | < 15 minutes |
| Phase 1-3 calibration | 30-90 minutes |
| Phase 4-5 calibration (Wikidata) | 40-105 minutes |
| Phase 6-9 calibration | 70-170 minutes |
| Cold-start test | 5-15 minutes |
| Medium-bar evaluation (×3 runs) | 3-6 hours |
| **Total** | **~6-9 hours** |

Budget one business day. Wikidata API rate limits are the main variable.

---

## Troubleshooting

**Circuit breaker fires during calibration:** A predicate is generating conflicting
translations. Add a hand-curated entry to `seeds/v0_15/predicate_translation.json`
for that predicate, reload seeds, and re-run the failing corpus sub-category.

**Entity resolver returns no candidates:** Wikidata API may be throttling. Add a
30s sleep between entity resolution calls (`AEDOS_KB_REQUEST_DELAY_MS=30000`).

**LLM returns malformed tool output:** Increase temperature slightly
(`AEDOS_LLM_TEMPERATURE=0.1`) for the predicate translation oracle; the default
is 0.0.

**Calibration accuracy below threshold:** Check the specific sub-category.
Systematic failures in one sub-category indicate a bug in the corresponding
oracle; check the oracle's prompt and output parsing. Failures spread across
sub-categories are likely LLM API variability; re-run.
