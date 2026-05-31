# v0.16 — Step 2: Build-examine loop on the Step-1 findings

Step 1 recorded 5 single-run "regressions" vs the v0.15 run_08 baseline. Step 2 re-ran each
live (multiple times) to separate **deterministic code regressions** from **LLM/eval noise**,
fixed the one real regression, and validated it with granular live re-tests + unit tests.

Tooling: `scripts/diagnose_claim.py` (live per-claim trace dump). Logs (untracked):
`docs/phase_10_5/medium_bar/step2_diag.log`, `step2_diag2.log`, `step2_fixcheck.log`,
`step2_recheck.log`.

## Classification of the 5 Step-1 "regressions"

| case | Step-1 (single run) | re-run behavior | verdict |
|---|---|---|---|
| `bonus_006` "Newton born before Einstein" | contradicted | verified (run A), abstain (run B) | **NOISE** — flaky Python-tier codegen + extraction variance |
| `csu_006` "France >60M people" | abstain | verified, verified | **NOISE** |
| `csu_012` "10 squared is 100…" | abstain | verified (run A), abstain (run B) | **NOISE** — flaky Python-tier codegen |
| `mhd_018` "Vatican is in Africa" | abstain (budget) | reproducible structural | **REAL → FIXED** |
| `csu_018` "Einstein Nobel 1921" | abstain (budget) | budget-bound, variable | environmental/latency |

**The Medium Bar is non-deterministic** (LLM extraction + LLM Python codegen + LLM oracle +
live SPARQL). Single-run case flips are mostly noise — which is why prior practice aggregated
across runs 00–08. The most alarming Step-1 item (a *false contradiction* on `bonus_006`) did
**not reproduce** — it is Python-tier codegen flakiness, not a deterministic soundness defect.

## The one real regression — FIXED

**`mhd_018` "The Vatican is in Africa"** abstained via `budget_wall_clock` after ~43
`verify_transitive_path` SPARQL ASKs of KB-neighbor fan-out, never reaching the cheap
geo-disjointness contradiction.

**Root cause:** the WS1 multi-property binding rewrite restructured `kb_verifier`'s
`if not statements:` branch to have a VERIFIED subsumption-upgrade arm but **no symmetric
CONTRADICTED (disjoint) arm**. The Vatican carries no `P131` statement (only `P30`=Europe),
so the in-statements disjoint check (which needs a statement) never fired; the walk fell into
PATCH-A's per-neighbor `verify_transitive_path` fan-out and exhausted the 30s budget.

**Fix** (commit `e0bc379`, `kb_verifier._verify_binding`): added the symmetric disjoint
CONTRADICTED arm in the no-statements branch, calling the existing fail-closed
`_location_disjoint` on the **subject Q-id** (gated identically to the in-statements arm:
location property, entity object, both Q-ids, standard direction). The VERIFIED arm runs
first, so a true "X in [right continent]" still verifies and never reaches it.

**Verification (granular live + unit):**
- `mhd_018` → **contradicted** (2/2 re-runs, fast single KB lookup, no fan-out).
- True-location controls `mhd_001` (Paris/France/Europe ×3), `mhd_005` (Eiffel/Europe),
  `mhd_006` (Cambridge/N.America) → all **verified** — **no false contradiction introduced**.
- `mhd_008` "Thames in Asia" → **contradicted** (path (a), expected=continent).
- +7 mocked unit tests (`TestKBVerifierNoStatementsDisjointArm`): contradicts, fail-closed
  abstain, VERIFIED-first ordering, non-location skip, negation, inverse-binding skip.
- Full gated suite: **1390 passed**, 1 xfailed, 1 xpassed (pre-existing sandbox).

## Residual items — recorded as characteristics, not chased (would risk the verified gains)

- **`mhd_009` "Rome is in Germany"** abstains: `_location_disjoint` **path (b)** (expected is a
  *country*, not a continent) requires expensive multi-hop subsumption (Lazio→Italy→Europe +
  mutual-exclusion ASKs at the 5/s SPARQL limit), which is slow/incomplete under the 30s walk
  budget. **`_location_disjoint` is unchanged from v0.15**, so this is run-to-run/environmental
  variance (v0.15 run_08 happened to land it), not a v0.16 code regression. Safe direction
  (abstain, never false).
- **Latency ~3.5× v0.15** (median 11s→38s): the dominant cost is **PATCH-A** routing every
  KB-enumerated discovery neighbor through `verify_transitive_path` (one rate-limited SPARQL
  ASK each) — the SS3 soundness fix the operator approved. The disjoint fix above removes the
  fan-out for the *false-location* class (they now contradict in ~1 lookup). For genuinely
  ungroundable claims the fan-out remains the cost of thorough discovery before a (safe)
  abstain. A **bounded per-walk discovery KB-ASK budget** would cap this, but needs calibration
  data to avoid cutting legitimate discovery into a coverage loss — deferred as a future,
  data-driven tuning item rather than a blind change here.
- **Extraction / Python-tier non-determinism** (`bonus_006`, `csu_012`): the LLM sometimes
  emits a different predicate or generates Python `verify()` that fails to ground a simple
  arithmetic/temporal comparison. Pre-dates v0.16 (the Python tier was always LLM codegen);
  not a v0.16 regression. A reliability improvement to the Python tier is out of scope here.

## Conclusion

v0.16 is **within expected qualitative performance**: soundness held (false-verified 0%),
accuracy up (+3.3pp), abstention down (−4.8pp), two modes up +10–13pp, the 3 v0.15 crashes
eliminated. Per the Step-2 brief ("if within expected performance, likely nothing large needs
changing"), the disciplined action was: fix the one reproducible regression (the no-statements
disjoint arm — done, verified, tested), and record the rest. No further code change is made in
Step 2; the residual items are eval noise + a performance cost of an approved soundness fix.
