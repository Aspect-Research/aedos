# Phase 10.5 Operator Runbook

Aedos v0.15 unattended build is complete. This runbook specifies every command
the operator needs to run for Phase 10.5 (calibration pass), in the order to
run them, with expected runtimes and acceptance thresholds.

**Prerequisites before starting Phase 10.5:**
- The Phase 10.5 baseline is tagged `v0.15.0-rc.4`. Phases A, B, and C landed
  after the original Phase 10 build (belief-revision and audit-log changes in
  A/B; documentation and audit-logging hygiene in C — see `docs/phase_C_report.md`).
  `v0.15.0-rc.4` is the start point; the fallback for a calibration anomaly that
  traces past D16/D6 is `v0.15.0-rc.2`.
- `ANTHROPIC_API_KEY` is set.
- Wikidata internet access is available.
- Python 3.11+ is installed.
- All mocked tests pass: `py -m pytest tests/ -q` (no `RUN_LIVE_TESTS`).

---

## Step 0 — Confirm the fix-up baseline

**Purpose:** Verify the build + fix-up are intact before live calibration.

```bash
py -m pytest tests/ -q
```

**Expected:** All tests pass — 720 passing, 1 gated skip (the cold-start test,
deferred to Step 5). The 11 calibration corpus tests are deselected here; they
run in Step 4 under `--run-calibration`.

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

### Sourcing `.env` into the shell

If the API keys and `RUN_LIVE_*` flags are already in the project's
`.env` file (the standard convention; `.env.example` documents the
expected keys), source `.env` into the shell **before** invoking the
calibration runner — the runner reads `os.environ` directly and does
not call `load_dotenv` (D35 v0.16 candidate). The shell-built-in
approach is portable:

```bash
# Unix/macOS (bash/zsh)
set -a; source .env; set +a
```

`set -a` makes every subsequently-assigned variable exported; `source
.env` reads the file (interpreting `KEY=value` lines as assignments);
`set +a` restores normal scoping for the rest of the shell session.

```powershell
# Windows PowerShell
Get-Content .env | Where-Object { $_ -match '^[^#].*=' } | ForEach-Object {
    $name, $value = $_ -split '=', 2
    [Environment]::SetEnvironmentVariable($name, $value, 'Process')
}
```

Verify the keys are loaded:

```bash
# Unix/macOS
[ -n "$ANTHROPIC_API_KEY" ] && echo "ANTHROPIC_API_KEY: set" || echo "unset"
```

```powershell
# Windows PowerShell
if ($env:ANTHROPIC_API_KEY) { "ANTHROPIC_API_KEY: set" } else { "unset" }
```

If your shell session already had the variables set (via the explicit
`export` / `$env:` commands above), this section is unnecessary — those
take precedence over `.env` values for the current session.

### What `AEDOS_DB_PATH` affects, and what it does not

`AEDOS_DB_PATH` does **not** drive Step 4's calibration corpora. The
calibration runner (`tests/calibration/test_corpus_runner.py`) uses an
in-memory database (`open_memory_db()`) by design — calibration measures
the LLM's *cold-start* substrate-row generation, so seeded predicate
translations would mask the signal. `AEDOS_DB_PATH` is read by:

- **Step 6 medium-bar benchmark** (`tests/evaluation/benchmark.py`) —
  runs against the seeded database from Step 2 + Step 3.
- **Step 5 cold-start test** — uses its own fresh DB at the path the
  example command specifies.
- **`scripts/reset_db.py`** and **`seeds/load_seeds.py`** — operator-
  initiated db work.

So Step 2's seed-load and Step 3's Tier U assertions feed Step 6, not
Step 4. The runbook spells this out explicitly; do not be confused by
the convention that "calibration" historically implied a substrate.

(D37 v0.16 candidate: have the calibration runner honor `AEDOS_DB_PATH`
optionally, or load seeds automatically when the runner detects a
configured path — eliminates this special case. v0.15 leaves the
runner as-is and clarifies the runbook.)

---

## Step 2 — Initialize benchmark database and load seed pack

**Purpose:** Build the substrate the **Step 6 medium-bar benchmark**
will run against. Step 4 calibration does NOT use this database (see the
"What `AEDOS_DB_PATH` affects" note above) — but Step 6 does, and Step 6
needs a populated `predicate_translation` table to verify intervention-
class claims at benchmark scale.

```bash
py -c "from aedos.database import open_db; open_db('aedos_phase10_5.db')"
py seeds/load_seeds.py --db-path aedos_phase10_5.db
```

**Expected output:**
```
Seed version info:
version: 1.0.0
date_reviewed: 2026-05-17
...

Loaded 61 predicate translation seeds into aedos_phase10_5.db
```

**Expected runtime:** < 5 seconds (no LLM calls; pure DB inserts).

**Acceptance threshold:** 61 seeds loaded, 0 errors.

---

## Step 3 — Seed user-context assertions for belief_revision benchmark cases

**Purpose:** The medium-bar benchmark's `belief_revision` cases (Step 6)
require Tier U assertions about "Asa" in the benchmark database. Insert
them directly before running the benchmark. As with Step 2, **Step 4
calibration does not read these rows** — they feed Step 6 only.

```python
# Run this Python snippet (or adapt to your deployment harness):
from aedos.database import open_db
from aedos.layer4_sources.tier_u import TierU
from aedos.layer3_substrate.predicate_translation import PredicateTranslation

db = open_db("aedos_phase10_5.db")
# NOTE: these assertions use routing_hint=user_authoritative; seeded directly.
# Column names match the tier_u schema (architecture 6.1): `object` (not
# `object_val`) and `asserting_party` (not `asserting_party_id`).
assertions = [
    ("Asa", "lives_in", "Williamstown"),
    ("Asa", "educated_at", "Williams College"),
    ("user", "identity", "Asa"),
]
for subj, pred, obj in assertions:
    db.execute(
        "INSERT OR REPLACE INTO tier_u (asserting_party, subject, predicate, object, "
        "polarity, asserted_at, valid_from, valid_until, source_text) "
        "VALUES ('operator', ?, ?, ?, 1, datetime('now'), NULL, NULL, 'Phase 10.5 seed')",
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

The calibration runner is `tests/calibration/test_corpus_runner.py`. It
loads each corpus, runs every case through the responsible component, computes
per-corpus accuracy, and asserts it against the threshold below — the runner
fails the test if accuracy is under threshold, so no manual grading is needed.

**Substrate state.** The runner uses an in-memory database
(`open_memory_db()`) per case-suite — **NOT** the `$AEDOS_DB_PATH`
database populated by Steps 2 and 3. This is intentional: calibration
measures the LLM's *cold-start* substrate-row generation quality, so
seeded rows would mask the signal. Every predicate the corpus references
triggers an inline `predicate_translation` LLM call; that latency is the
"cold-start expensive" path the runbook's runtime estimates account for.
Step 2 and Step 3's substrate populate the Step 6 benchmark, not this
step (see "What `AEDOS_DB_PATH` affects" in Step 1).

Gating:
- The runner is collected only with `--run-calibration`.
- With `--run-calibration` but no `RUN_CALIBRATION=1`, it does a harness
  dry-run (loads + validates the corpus, skips with a count) — useful to
  confirm the harness works without spending on live calls.
- With `RUN_CALIBRATION=1` set (Step 1) it evaluates live. `RUN_LIVE_KB=1` and
  `RUN_LIVE_TESTS=1` (also set in Step 1) make the KB and LLM calls live.

Each test id is `test_corpus_calibration[<corpus>]`, so `-k "<corpus>"` selects
it. The acceptance thresholds below are reproduced verbatim from the
implementation plan's "Calibration deferral policy" table.

**Threshold summary.** This table is the canonical runbook copy of the
calibration thresholds. It is kept in lock-step with the runner's `THRESHOLDS`
dict (`tests/calibration/test_corpus_runner.py` — the single source of
truth) by `tests/unit/test_runbook_thresholds.py`, which fails CI if the
two diverge. The per-Phase sub-sections below restate the same thresholds as
operator narrative.

| Corpus (`-k` filter) | Runner threshold | Plan bar |
|---|---|---|
| `extraction_corpus` | 90% | ≥ 90% |
| `predicate_metadata_corpus` | 85% | ≥ 85% |
| `temporal_scope_corpus` | 90% | extraction ≥ 90%, lookup 100% |
| `entity_resolution_corpus` | 90% | ≥ 90% (live KB) |
| `kb_mapping_corpus` | 90% | ≥ 90% (live KB) |
| `subsumption_corpus` | 80% | ≥ 90% KB-mediated, ≥ 80% substrate |
| `predicate_distribution_corpus` | 85% | ≥ 85% |
| `derivation_corpus` | 80% | ≥ 80% (live KB) |
| `python_verification_corpus` | 85% | ≥ 85% |
| `consistency_check_corpus` | 100% | 100% detection + circuit breaker |
| `intervention_corpus` | 90% | ≥ 90% |

### Phase 1 — Extraction corpus

```bash
py -m pytest tests/calibration/test_corpus_runner.py -q --run-calibration -k "extraction_corpus"
```

**Expected runtime:** 10-30 minutes (LLM calls for 53 cases).

**Acceptance threshold:** ≥ 90% (against the cleaned 53-case corpus).

**Corpus note.** Phase E3's per-case extraction triage (2026-05-23) removed 4
unscoreable cases from the original 57-case corpus and modified 1 input
text; the original 57-case version is preserved at
`tests/calibration/extraction_corpus_v0.jsonl` for historical reference.
The cleanup rationale and the v0.16 work items the removed cases imply are
documented in `docs/v0.16_planning.md` D44. Phase E's Haiku 4.5 + v5 prompt
configuration achieved 53/53 = 100% on the cleaned corpus, so the ≥ 90%
threshold expects substantial headroom; if Phase 10.5 sees < 90% the gap
is unlikely to be the extractor and likely the LLM-routed substrate or
runtime conditions.

### Phase 2 — Predicate metadata corpus

```bash
py -m pytest tests/calibration/test_corpus_runner.py -q --run-calibration -k "predicate_metadata_corpus"
```

**Expected runtime:** 15-45 minutes (LLM calls for 80 cases).

**Acceptance threshold:** ≥ 85%.

### Phase 3 — Temporal scope corpus

```bash
py -m pytest tests/calibration/test_corpus_runner.py -q --run-calibration -k "temporal_scope_corpus"
```

**Expected runtime:** 5-15 minutes (40 cases).

**Acceptance threshold:** extraction ≥ 90%, lookup 100%. (The runner asserts the
extraction accuracy ≥ 90%; the lookup-100% bar is verified by the Phase 3
mocked unit suite.)

### Phase 4 — Entity resolution + KB mapping corpora

```bash
py -m pytest tests/calibration/test_corpus_runner.py -q --run-calibration -k "entity_resolution_corpus or kb_mapping_corpus"
```

**Expected runtime:** 20-60 minutes (90 cases; Wikidata API calls).

**Acceptance threshold:**
- entity_resolution_corpus: ≥ 90% (live KB).
- kb_mapping_corpus: ≥ 90% (live KB).

**Phase G D47 caveat (added 2026-05-23).** Live validation of D33's type
filter surfaced that some canonical entities (Q76 for Barack Obama,
Q49112 for Williams College) are not reachable from their bare
ambiguous string forms via Wikidata — the bare strings are not in
Wikidata's label or altLabel index for the canonical entities. Cases
in this corpus that use such bare ambiguous references will produce
abstentions in v0.15. Interpret missed cases as honest measurement of
v0.15's known upstream-disambiguation constraint, not as a defect; the
v0.16 D47 work item addresses extraction-time normalization and oracle
context enhancement. The acceptance threshold above remains the ship
gate; if it is not met and the gap traces to D47 cases, that data
informs v0.16 sequencing rather than blocking the v0.15 release.

### Phase 5 — Subsumption + predicate distribution corpora

```bash
py -m pytest tests/calibration/test_corpus_runner.py -q --run-calibration -k "subsumption_corpus or predicate_distribution_corpus"
```

**Expected runtime:** 20-45 minutes (110 cases).

**Acceptance threshold:**
- subsumption_corpus: ≥ 90% KB-mediated, ≥ 80% substrate-generation. (The runner
  asserts the overall corpus accuracy ≥ 80%; inspect the KB-mediated subset for
  the ≥ 90% bar.)
- predicate_distribution_corpus: ≥ 85%.

### Phase 6 — Derivation corpus

```bash
py -m pytest tests/calibration/test_corpus_runner.py -q --run-calibration -k "derivation_corpus"
```

**Expected runtime:** 30-90 minutes (50 cases; multi-hop walks).

**Acceptance threshold:** ≥ 80% (live KB).

### Phase 7 — Python verification corpus

```bash
py -m pytest tests/calibration/test_corpus_runner.py -q --run-calibration -k "python_verification_corpus"
```

**Expected runtime:** 10-20 minutes (30 cases).

**Acceptance threshold:** ≥ 85%.

### Phase 8 — Consistency check corpus

```bash
py -m pytest tests/calibration/test_corpus_runner.py -q --run-calibration -k "consistency_check_corpus"
```

**Note:** detection is deterministic; the runner evaluates the
seeded-conflict-detection sub-corpus. The regeneration-convergence sub-corpus
involves live LLM regeneration and is inspected separately.

**Expected runtime:** 10-20 minutes.

**Acceptance threshold:** 100% detection, 100% circuit breaker correctness.

### Phase 9 — Intervention corpus

```bash
py -m pytest tests/calibration/test_corpus_runner.py -q --run-calibration -k "intervention_corpus"
```

**Expected runtime:** 20-45 minutes (30 cases; full pipeline).

**Acceptance threshold:** ≥ 90% intervention-type classification.

---

## Step 5 — Cold-start zero-seed test

**Purpose:** Verify correctness on a fresh substrate with no seeds loaded.

```bash
# Initialize a separate zero-seed database
py -c "from aedos.database import open_db; open_db('aedos_zero_seed.db')"
AEDOS_DB_PATH=aedos_zero_seed.db py -m pytest tests/cold_start/ -v
```

**Expected runtime:** 5-15 minutes (10 claims; first-claim cold-start is expensive).

**Acceptance thresholds:**
- All 10 cases produce expected verdict.
- First-claim latency ≤ 30s.
- Tenth-claim latency ≤ 5s.

---

## Step 6 — Medium-bar evaluation

**Purpose:** Compare Aedos v0.15 against an LLM-only baseline on the 122-case
curated test set. `benchmark.py`'s live runner is implemented (fix-up 2).

**Pre-flight (optional, no API cost):** confirm the harness wiring before
spending on live calls —

```bash
py -m tests.evaluation.benchmark --validate-harness
```

Expected: `Harness validation: PASS`.

**Run the evaluation.** Requires `RUN_LIVE_TESTS=1` and `RUN_LIVE_KB=1` (set in
Step 1) — the runner exits with an error if they are unset and never silently
falls back to mocks. It evaluates against the seeded `$AEDOS_DB_PATH` database
from Step 2.

```bash
py -m tests.evaluation.benchmark \
    --test-set tests/evaluation/medium_bar_test_set.jsonl \
    --output docs/evaluation_results.md
```

**Expected runtime:** 60-120 minutes (122 cases × Aedos pipeline + baseline LLM calls).

**Expected output:** the report is printed and written to the `--output` path,
ending with `Results written to docs/evaluation_results.md`.

**Acceptance thresholds** (the runner's report prints PASS/FAIL for each):
1. Aedos false-verified rate ≤ 5%.
2. Aedos overall accuracy ≥ baseline + 15 percentage points.
3. Aedos accuracy ≥ baseline on every failure mode (no regression).
4. Aedos accuracy ≥ baseline + 20pp on at least 4 of 6 failure modes — the
   runbook's operationalization of the plan's "significantly higher".

Results written to `docs/evaluation_results.md`.

Run 3 times and report median to account for LLM non-determinism. `--baseline-only`
and `--aedos-only` re-run a single runner during development.

---

## Step 7 — Tag v0.15.0

If all Phase 10.5 acceptance thresholds pass:

```bash
git add docs/evaluation_results.md
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
translations. Add a hand-curated entry to `seeds/predicate_translation.json`
for that predicate, reload seeds, and re-run the failing corpus sub-category.

**Entity resolver returns no candidates:** Wikidata API may be throttling. Add a
30s sleep between entity resolution calls (`AEDOS_KB_REQUEST_DELAY_MS=30000`).

**LLM returns malformed tool output:** Capture the raw response from the
audit log (the `LLMClient._attach_raw_response` path preserves the SDK
response on failed parses). If a specific model produces persistent
malformed tool output, the model is likely incompatible with the tool
schema — see `docs/v0.16_planning.md` D25 for the DeepSeek precedent.
Tuning options (temperature, retry, prompt restructuring) are model-
specific; v0.16 may add a per-purpose temperature knob if calibration
data shows it's needed. (Phase F2 removed the prior `AEDOS_LLM_TEMPERATURE`
reference here — the env var was never read by any code.)

**Calibration accuracy below threshold:** Check the specific sub-category.
Systematic failures in one sub-category indicate a bug in the corresponding
oracle; check the oracle's prompt and output parsing. Failures spread across
sub-categories are likely LLM API variability; re-run.
