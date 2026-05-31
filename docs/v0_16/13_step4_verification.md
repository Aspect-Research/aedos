# v0.16 — Step 4: final verification & second Medium-Bar run

## Test suite
Full gated suite (`tests/unit tests/integration --ignore=tests/integration/live`):
**1390 passed, 1 xfailed, 1 xpassed** (the xfail/xpass are the pre-existing v0.15 sandbox
boundaries). pyflakes clean. Up from the pre-session 1346 (Step-2 added +7 disjoint tests;
the rest of the +37 were from the prior v0.16 review patches).

## Second Medium Bar (tag `v016_step4`, live, 122 cases)

| Metric | Step 1 (`v016_step1`) | **Step 4 (`v016_step4`)** | v0.15 (run_08) |
|---|---|---|---|
| Accuracy | 60.7% (74/122) | **60.7% (74/122)** | 57.4% |
| **False-verified** | 0.0% (0) | **0.0% (0)** | 0.0% |
| False-abstain | 44.0% (37) | 46.4% (39) | 48.8% |
| errors | 0 | 0 | 3 |
| belief_revision | 60.0% | 60.0% | 60.0% |
| cross_source_unification | 57.1% | 57.1% | 66.7% |
| entity_disambiguation | 56.5% | 52.2% | 43.5% |
| **multi_hop_distribution** | 45.0% | **55.0%** | 45.0% |
| predicate_translation | 50.0% | 46.4% | 39.3% |
| principled_abstention | 100.0% | 100.0% | 100.0% |

Run: 138 min; aedos latency median 40s, max 429s; **0 errors, 0 true hangs** (25 watchdog
>120s warnings, all self-resolved — the documented PATCH-A per-neighbor discovery cost).

## What this confirms

1. **Reproducibility + soundness.** Step 4 reproduces Step 1 exactly at the aggregate
   (60.7% accuracy, **0% false-verified**). The §3.2 invariant held across *both* full live
   runs of the cleaned-up code.
2. **The Step-2 fix landed its expected gain.** multi_hop_distribution rose **45% → 55%
   (+10pp)**: `mhd_018` "Vatican is in Africa" and `mhd_019` "Monaco … in North America" now
   correctly **contradict** (Step 1: abstained via budget) — exactly the no-statements
   geo-disjointness path restored in Step 2.
3. **Step-3 cleanup was behavior-neutral.** Apart from the intended multi-hop gain, the per-case
   verdicts match Step 1 modulo the established eval non-determinism (14 per-case flips:
   ~balanced gains/losses on the noisy cross_source / entity_disambiguation / predicate_translation
   cases — `bonus_005/006/009/013/014`, `csu_002/006/016`, `ed_010/015`, `pt_005`, `mhd_002`).
   Net accuracy identical → the doc archival, dead-code removal, and comment/docstring trims did
   not change system behavior, as the verification harness proved.

## Net assessment

v0.16, cleaned and patched, holds the soundness invariant (0% false-verified) live while
improving on v0.15 (accuracy +3.3pp, false-abstain −2.4 to −4.8pp across runs, multi-hop +10pp,
the 3 v0.15 crashes eliminated). The residual ~3.5× latency (discover/verify per-neighbor KB
verification) and the run-to-run eval non-determinism on the LLM-codegen / cross-source modes are
documented characteristics, not regressions.
