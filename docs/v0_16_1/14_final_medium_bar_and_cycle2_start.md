# v0.16.1 — Final Medium Bar + Autonomous Cycle-2 Start

## Final Medium Bar (v0.16.1, warm `aedos_phase10_5.db` re-seeded with the v0.16.1 pack)

| Metric | v0.16 (Step-4) | **v0.16.1 final** |
|---|---|---|
| Accuracy | 60.7% (74/122) | **60.7% (74/122)** |
| **false-verified** | 0.0% | **0.0% (0)** — held |
| **false-contradicted** (NEW gate) | (unmeasured) | **2.5% (3)** — **GATE FAIL** |
| false-abstain | 46.4% | 46.4% (39) |
| belief_revision | 60.0% | 60.0% |
| cross_source_unification | 57.1% | **61.9%** (+4.8, WS3) |
| entity_disambiguation | 52.2% | **39.1%** (−13.1, regression?) |
| multi_hop_distribution | 55.0% | 55.0% |
| predicate_translation | 46.4% | **53.6%** (+7.2, WS3/WS6) |
| principled_abstention | 100.0% | 100.0% |

`false_verified` held at 0 (the paramount invariant). The **new false-contradicted gate — built this
cycle (WS1/WS7) — did its job and surfaced 3 real false-contradicts** the old FV-only metric was
blind to. Run health: 0 errors, 28 watchdog >120s flags (all self-resolved). vs Step-4: 7 gains
(bonus_006/csu_012/csu_016 now verify via WS3/WS6; mhd_008 contradicts; pt_003/005 verify) and 7
regressions (3 ed flips to abstain + csu_008/csu_015 + ed_015 + mhd_019 — eval noise + the ed dip).

## The 3 false-contradicts (§3.2 — never false-contradict; the cycle-2 work-list)

1. **`mhd_002`** "Berlin is in Germany and Germany is in the European Union." gt=verified → **contradicted**.
   Germany *is* in the EU (membership). Hypothesis: the geo-disjoint check fires on an `in`/located_in
   conjunct whose object (the EU) is **not a geographic container** (it's a political union/org), so a
   shared-continent "disjoint" mis-classification contradicts a true membership claim.
2. **`pt_004`** "Williams College is part of the Consortium of Liberal Arts Colleges." gt=verified →
   **contradicted**. A `part_of`/membership claim. Hypothesis: the consortium doesn't resolve / Williams's
   P361 doesn't match → a single-valued or disjoint path contradicts instead of abstaining.
3. **`pt_006`** "France was founded in 843." gt=verified → **contradicted**. France's P571 inception has
   **multiple** values (843 West Francia, 1958 Fifth Republic, …). Hypothesis: a single_valued date
   predicate contradicts when the claim matches one KB value but not the one compared — a multi-value
   false-contradict. (Gold is debatable, but contradicting a value the KB actually holds is unsound.)

## Also: entity_disambiguation regression (52.2 → 39.1%)
`ed_005`, `ed_006`, `ed_011` flipped verified→abstain and `ed_015` contradicted→abstain vs Step-4. Coverage
loss (safe direction, not false-contradict). Hypothesis: the WS5b `user_subject_required` fail-closed
walk-entry guard mis-firing on entity-disambiguation predicates, or eval non-determinism. Investigate.

---

## AUTONOMOUS CYCLE 2 — STARTS HERE (this commit)

Operator authorized a fully-autonomous build-verify-build cycle (same rigor as v0.16.1, no check-ins) to
address the observations above. Branch `v0.16.1` continues. Targets, soundness-first:

- **C2-1 (soundness):** fix `mhd_002` geo-disjoint-on-non-container-object false-contradict.
- **C2-2 (soundness):** fix `pt_004` part_of/membership false-contradict.
- **C2-3 (soundness):** fix `pt_006` multi-value single_valued date false-contradict (match-any of the
  KB's values; never contradict a value the KB holds).
- **C2-4 (coverage):** investigate + fix the entity_disambiguation regression (likely the WS5b
  `user_subject_required` guard mis-firing).

Process: live diagnosis of each case → plan → build-verify-build (code+test agents, small test subsets) →
adversarial review → patch → one final Medium Bar confirming **false_contradicted == 0 AND
false_verified == 0**. Commits only; no tag, no push.
