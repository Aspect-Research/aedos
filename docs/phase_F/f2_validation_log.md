# Phase F2 — Validation Log

*Output of F2 commit #7 (validation). Records what F2 verified before
landing and what awaits the operator's live-LLM run.*

---

## Environment

- Date: 2026-05-20
- Python: 3.11.9
- Wikidata: live (`RUN_LIVE_KB=1`) — egress confirmed working
- LLM keys: **unset in this environment** (per Phase E plan ambient
  state) — full derivation-corpus run blocked here, awaits operator.

## What F2 verified directly

### Live API integration (real Wikidata, RUN_LIVE_KB=1)

Per-method live tests (`tests/integration/live/test_wikidata_live.py`):

| Capability | Live tests | Status |
|---|---|---|
| `_live_resolve` | 4 protocol tests + 2 D33 xfail | 4 pass, 2 xfail (expected) |
| `_live_lookup` | 5 protocol/semantic tests | 5 pass |
| `_live_subsumption` | 4 protocol/semantic tests | 4 pass |

Pipeline-reaches-Wikidata end-to-end (`tests/integration/live/test_pipeline_reaches_wikidata.py`):

| Wiring assertion | Status |
|---|---|
| Assembled pipeline's resolver reaches live `wbsearchentities` | pass |
| Configured User-Agent reaches HTTP requests (F-007 closure) | pass |
| Assembled pipeline's `kb.lookup_statements` reaches live SPARQL | pass |
| Assembled pipeline's `kb.subsumption` reaches live SPARQL | pass |

**Total live calls during F2 verification:** ~25 (each test isolated;
HTTP cache reused within a single fixture instance).

### Mocked failure-mode coverage

`tests/unit/test_wikidata_live_failure_modes.py`:

| Failure mode | Tests | Status |
|---|---|---|
| Resolve: timeout retry, give-up, malformed, wiring-gap | 4 | pass |
| Lookup: timeout retry, give-up, deprecated-rank filter, invalid IDs, wiring-gap | 6 | pass |
| Subsumption: equivalent, timeout, invalid relation type, wiring-gap, chain direction | 5 | pass |

### Wiring (F-004 / F-005 / F-006 / F-007 / F-022)

`tests/integration/test_build_pipeline_config.py`:

| Wiring property | Status |
|---|---|
| Default `build_pipeline(db)` constructs `WikidataAdapter` with full deps | pass |
| Explicit `kb` arg overrides default construction | pass |
| `Config.wikidata_*_endpoint` reaches the adapter | pass |
| `Config.user_agent` reaches HTTP headers | pass |
| Rate limiters live as adapter instance attributes (Q3 design) | pass |
| `AEDOS_KB_REQUEST_DELAY_MS` overrides rate-limiter interval (F-022) | pass |

### F-009 closure

`tests/unit/test_purpose_table_completeness.py`:

| Check | Status |
|---|---|
| Every `purpose=` literal in `src/aedos/` is a key in `DEFAULT_MODEL_BY_PURPOSE` | pass |
| Every table key is either used or explicitly reserved | pass |

This is D26's CI-runnable purpose audit landed.

### Mocked regression

```
$ py -m pytest tests/ -q --ignore=tests/cold_start --ignore=tests/calibration --ignore=tests/integration/live
783 passed in 13.43s
```

No regressions from F2's changes. The 5 Python-verifier integration
tests that initially failed (mock-transport routed by old purpose names)
were updated as part of F-009 commit.

---

## What awaits the operator

The F2 design specified a **derivation corpus validation run** as the
acceptance gate (F1 §3, commit #7). This requires LLM API keys
(ANTHROPIC_API_KEY at minimum; OPENAI_API_KEY by default for the
substrate purposes), which are unset in the build environment. The
operator runs this with their credentials.

### Procedure

```bash
# Same setup as Phase 10.5 Step 1, scoped to the derivation corpus only.
$env:RUN_LIVE_TESTS = "1"
$env:RUN_LIVE_KB = "1"
$env:RUN_CALIBRATION = "1"
$env:ANTHROPIC_API_KEY = "<your-key>"
$env:OPENAI_API_KEY = "<your-key>"

# The derivation corpus only (skips other Phase 10.5 corpora).
py -m pytest tests/calibration/test_corpus_runner.py -q --run-calibration -k "derivation_corpus"
```

**Substrate note (per F-041, captured during F2 follow-up).** The
calibration corpus runs against an unseeded in-memory database by
design — calibration measures the LLM's *cold-start* substrate-row
generation. Seeds are loaded separately for the Step 6 medium-bar
benchmark (and `AEDOS_DB_PATH` is consumed there), **not** for the
Step 4 calibration corpora. The seeded path used for the F2-internal
single-case sanity check (below) was a deliberate departure from the
calibration default to exercise F2's KB wiring end-to-end; the operator-
facing acceptance run follows the Phase 10.5 convention (unseeded).
See `docs/phase_10_5_runbook.md` Step 1 — "What `AEDOS_DB_PATH` affects".

### Acceptance criteria

The F2 design committed to (with F1's wiring-correctness layer):

1. **Zero `NotImplementedError`s.** Phase E surfaced 20 of these on
   the derivation corpus under `RUN_LIVE_KB=1` before F2 (the headline
   blocker). After F2, the corpus must execute end-to-end against live
   Wikidata.

2. **Performance.** Corpus completes in ≤ 30 minutes including LLM
   calls + live Wikidata calls + HTTP cache amortization. Phase 10.5
   runbook budgets 30-90 minutes for this corpus — F2 should be at
   the lower end with caching active.

3. **Trace inspection (spot-check ≥ 3 cases).** For each spot-checked
   case, verify:
   - The trace records `purpose` values that match
     `DEFAULT_MODEL_BY_PURPOSE` keys exactly (F-009 verification).
   - At least one `kb_live_resolve` and one `kb_live_lookup` audit
     event fired per case (live API was actually reached).
   - For multi-hop derivation cases, at least one `kb_live_subsumption`
     fired.

4. **HTTP cache effectiveness.** After the corpus run, the in-process
   `LRUHTTPCache` should show non-zero hits (Phase 10.5 has repeat
   queries; the cache should amortize). Inspectable via
   `pipeline.kb._http._cache._cache` (the LRU dict).

5. **D33 expectations.** Some derivation cases will produce false
   abstains where the canonical entity is unreachable via default
   `wbsearchentities` ranking (e.g., the Williams College Q49112 case).
   This is the D33 finding; the F2 corpus run is *expected* to surface
   this empirically, and the results inform v0.16's type-filtering
   priority (D33 work item 1).

### What a failing run means

- **NotImplementedError surfacing:** F2 implementation bug; surface
  immediately.
- **>30 minute runtime:** caching may not be engaging; inspect the
  pipeline's HTTP cache state and the audit log for repeated identical
  `kb_live_*` events.
- **Purpose mismatches in trace:** F-009 didn't fully land somewhere;
  audit-log a few traces and compare against
  `DEFAULT_MODEL_BY_PURPOSE` keys.
- **Elevated false-abstain rate:** likely D33; record it for v0.16
  planning, not as an F2 hotfix.

---

## Summary

F2's implementation, wiring, and discipline-pattern fixes (F-009)
landed in commits `419d36c..e36d0e9`. Every capability F2 implements
is exercised by at least one live test against the real service and
reachable from the deployed pipeline path (the F1 wiring-correctness
criterion).

The full derivation-corpus validation run is operator work — it
requires LLM API budget and credentials this environment doesn't have.
The procedure above is reproducible from a Phase-10.5-ready environment.

After the operator confirms the corpus run passes, F2 is complete and
F3 begins (Python sandbox hardening per E elevation; broader Config
threading; `.env` loader if scope permits).

---

*End of Phase F2 validation log.*
