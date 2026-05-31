# Phase 10 Plan — Hardening + Seeds + Cold-Start Docs + Evaluation Scaffolding

## Goal

Produce the optional seed pack, cold-start test scaffolding, audit-log query endpoints (already partially present in app.py), cold-start documentation, and medium-bar evaluation scaffolding. No live LLM or live KB calls. No calibration execution. Phase ends with tag `v0.15-phase-10-complete`.

## Deliverables

### Seed pack
- `seeds/v0_15/predicate_translation.json` — ≥60 hand-curated predicate translation mappings covering roles, locations, kinship, categorical, mereological, quantitative, event predicates
- `seeds/v0_15/SEED_VERSION.txt` — version stamp + date
- `seeds/v0_15/load_seeds.py` — CLI loader; idempotent INSERT OR REPLACE
- `tests/v0_15/unit/test_seed_loader.py` — parses, validates, loads into in-memory DB (no LLM/KB)

### Cold-start test scaffolding
- `tests/v0_15/cold_start/test_zero_seed_correctness.py` — 10 representative claims, mocked LLM + fixture KB; structural execution only (live execution deferred to Phase 10.5)

### Audit-log query endpoints
- `app.py` already has all four endpoints (`/audit/substrate-rows`, `/audit/consistency-checks`, `/audit/circuit-breakers`, `/audit/retractions`) from Phase 9
- `tests/v0_15/integration/test_audit_endpoints.py` — exercises all four against synthetic audit data (no LLM/KB needed)

### Cold-start documentation
- `docs/v0_15/cold_start.md`

### Evaluation scaffolding
- `tests/v0_15/evaluation/medium_bar_test_set.jsonl` — 100-150 cases across six failure modes (not executed in this phase)
- `tests/v0_15/evaluation/benchmark.py` — runner + baseline driver + metrics; written but not invoked
- `docs/v0_15/evaluation_methodology.md`

### Runbook (end condition #3)
- `docs/v0_15/phase_10_5_runbook.md`

## Test target

~40 new tests; cumulative ~592+.

## Ambiguities pre-resolved

1. **app.py audit endpoints**: Already present from Phase 9. Only `test_audit_endpoints.py` needs to be added.
2. **cold-start test execution**: Uses mocked LLM (purpose-dispatch) + StubKB; tests confirm structural execution, not live accuracy.
3. **benchmark.py execution**: Written with structural mock-execution test; actual results deferred to Phase 10.5.

## Order of operations

1. Write seed pack + loader
2. Write unit test for seed loader
3. Write cold-start test scaffolding
4. Write audit endpoint tests
5. Write evaluation scaffolding (test set + benchmark.py)
6. Write documentation (cold_start.md, evaluation_methodology.md, phase_10_5_runbook.md)
7. Run pytest
8. Commit + tag
9. Append to run_log
