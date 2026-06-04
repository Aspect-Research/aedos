# v0.16.1 — Release Notes

Tagged `v0.16.1` on branch `v0.16.1`. Soundness invariant held and hardened
(§3.2: never false-verify, never false-contradict). Built in the same
build-verify-build discipline as v0.16, with an autonomous cycle-2 follow-up.

## Shipped (planned items 1–8; item 9 retraction-cascade deferred)

| WS | Item | Summary |
|---|---|---|
| WS1 | 1 | Circa/approximate-date false-contradict fix; **added the false-contradicted counter + hard gate** (the metric the rest of the cycle leaned on) |
| WS2 | 2 | Occupation-copula grounding — synthesized value-type-gated P106 candidate binding; fail-closed positive gate |
| WS3 | 3 | Multi-source derivation — vague-class instance check, traced rollup, premise→Python channel |
| WS4 | 5 | Resolved dormant mechanisms (activate-or-remove; SLING distant supervision activated; nogood veto + exception-cache param removed) |
| WS5 | 4 + 6a | Relocated all Wikidata hardcodes behind the `kb_protocol` CORE/adapter seam (geo, neighbor tables, normalizer); Router/Validator |
| WS6 | 6b | Python-tier deterministic front-end (numeric/year/exact-arithmetic) with a strict totality gate → else codegen fallback → abstain |
| WS7 | 6c | Standing eval harness — folded watchdog + live FV/FC counters into the benchmark CLI; dual hard gates; offline mocked regression net |
| WS8 | 7 (Stage 1) | Write-only event-relative temporal fields (`valid_from_ref`/`valid_until_ref`); resolver deferred to Stage 2 |
| WS9 | 8 (lever A) | Process-scoped positive-result memo for `verify_transitive_path` (definite answers only; behavior-neutral) |

## Autonomous cycle-2 (soundness follow-up)

The new false-contradicted gate surfaced false-contradicts the FV-only metric was
blind to. Cycle-2 closed **4** of them and held false_verified at 0:

- **geo place gate** (mhd_002 Germany-in-EU, pt_004 Williams-in-Consortium): a
  non-place object can't be a disjoint sub-region.
- **multi-value single_valued** (pt_006 France-843): never contradict a value the
  KB holds.
- **vague-subject existential** (csu_003 "a university founded before 1800"):
  never refute an existential subject resolved to one arbitrary entity.
- **extractor null-slot guard** (ed_005 crash).

Plus an adversarial review round (C2S-1 over-abstention patch, C2-COMPLETENESS-1
durable-net pins; C2S-2 dismissed).

**Final Medium Bar (`v161_c2_final2`): both hard soundness gates PASS** —
false_verified == 0 AND false_contradicted == 0. Accuracy 62.3%,
principled_abstention 100%. Full results: [15_cycle2_results.md](15_cycle2_results.md).

Gated suite: 1615 passed, 1 xfailed, 1 xpassed (the pre-existing v0.15 sandbox
boundaries — see v0.16.2 deployment scoping).
