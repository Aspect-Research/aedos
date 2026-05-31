# v0.16 — Step 1: Medium-Bar run & findings

Live run of the full 122-case Medium Bar against the seeded `aedos_phase10_5.db`
(migrated to the v0.16 schema in place; a pre-run copy was kept at
`aedos_phase10_5.db.pre_v016_bak`). Preceded by a 3-case cold-start smoke against
a fresh empty substrate. Both used live LLM + live Wikidata.

Artifacts (untracked, local): `docs/phase_10_5/medium_bar/medium_bar_v016_step1.{md,json}`,
the per-case `..._aedos.jsonl` / `..._baseline.jsonl`, and the `step1_*.log` console logs.
Runner: `scripts/medium_bar_step1_run.py` (per-instance logging + watchdog + live FV counter);
smoke: `scripts/medium_bar_step1_smoke.py`.

## Cold-start smoke (fresh empty substrate, live)

| case | mode | gt | verdict | result |
|---|---|---|---|---|
| mhd_001 (Paris→Europe) | multi_hop_distribution | verified | verified | OK (125s — cold discovery) |
| pt_001 (Obama→President) | predicate_translation | verified | verified | OK (18s) |
| pa_001 (Asa "best engineer") | principled_abstention | abstain | no_grounding_found | OK (80s) |

3/3 correct, **0 false-verified**, no hangs. Confirms the live discover-from-Wikidata
path works from zero seed and that abstention holds on a subjective claim.

## Full run results — v0.16 vs v0.15 (run_08)

| Metric | v0.15 (run_08) | **v0.16** | Δ |
|---|---|---|---|
| Accuracy | 57.4% (70/122) | **60.7% (74/122)** | **+3.3pp** |
| **False-verified** | 0.0% | **0.0% (0)** | **0 — soundness held** |
| False-abstain | 48.8% | **44.0% (37)** | −4.8pp (better) |
| errors | 3 | **0** | −3 |
| belief_revision | 60.0% | 60.0% | flat |
| cross_source_unification | 66.7% | **57.1%** | **−9.6pp (REGRESSION)** |
| entity_disambiguation | 43.5% | **56.5%** | **+13.0pp** |
| multi_hop_distribution | 45.0% | 45.0% | flat |
| predicate_translation | 39.3% | **50.0%** | **+10.7pp** |
| principled_abstention | 100.0% | 100.0% | flat |

Run duration: **110 min** (v0.15: 51 min). Aedos per-case latency median 38s / p90 145s /
max 381s (v0.15: median 11s / max 183s) — **~3.5× slower**.

> The Phase-10.5 "≥ baseline + 15pp" / "no-regression vs baseline" criteria FAIL, but
> that comparison is not meaningful: the LLM-only baseline scores high on the grounding
> modes precisely by hallucinating confident verifications (**13.1% false-verified** this
> run). Aedos trades that for **0% false-verified**. The meaningful comparison is
> v0.16-vs-v0.15, above.

## Headline

The full v0.16 change set, run live end-to-end, **held false-verified at exactly 0** and
**eliminated the 3 v0.15 crashes**, while improving overall accuracy (+3.3pp), cutting
false-abstention (−4.8pp), and lifting entity_disambiguation (+13pp) and
predicate_translation (+10.7pp) — the multi-property substrate + discovery are working.
Two clear divergences need Step-2 attention, and one of them is a soundness defect.

## Gains (v0.15 wrong → v0.16 correct): 9

- `csu_004`, `csu_015` — cross_source: abstain → verified
- `ed_002`, `ed_004`, `bonus_014` — entity_disambiguation: abstain → verified
- `mhd_007` — multi_hop: **error → verified** (a v0.15 crash, now fixed)
- `pt_001`, `pt_009`, `bonus_009` — predicate_translation: abstain → verified/contradicted

## Regressions (v0.15 correct → v0.16 wrong): 5 — Step-2 work-list

### R1 (SOUNDNESS) `bonus_006` — false contradiction
"Isaac Newton was born before Albert Einstein." gt=verified → v0.16 **contradicted**.
Newton (1643) *was* born before Einstein (1879); v0.16 wrongly contradicts.
**This violates §3.2 (never false-contradict)** even though it does not register in the
false-*verified* metric. **Highest priority.** Hypothesis: the WS6 `date→time` seed
reconciliation made the value-type gate live for date predicates, and a `born_before` /
temporal-comparison path now emits a contradiction it shouldn't (or a single_valued date
binding mis-arbitrates). Diagnose with observability on this exact claim.

### R2 `mhd_018` — lost geo-disjointness contradiction
"The Vatican is in Africa." gt=contradicted → v0.16 **no_grounding_found** (v0.15 contradicted).
Safe direction (abstain, not false-verify) but lost a correct contradiction. Hypothesis:
PATCH-A's definite-KB-negative change in `_verify_chain`, or the `_location_disjoint`
geo guard no longer reached on this path.

### R3 `csu_006` — compound population claim abstains
"Paris is the capital of France, and France has more than 60 million people." gt=verified →
v0.16 abstain (v0.15 verified). The `population_greater_than` quantitative conjunct or the
compound-claim rollup lost coverage. Hypothesis: the WS5 `kb_quantitative` `(verdict, detail)`
tuple-return change, or compound-claim handling.

### R4 `csu_012` — math/Python conjunct abstains
"10 squared is 100, and 100 is greater than 50." gt=verified → v0.16 abstain (v0.15 verified).
Python-tier / quantitative comparison coverage loss.

### R5 `csu_018` — temporal award-year abstains
"Einstein won the Nobel Prize in Physics. The year was 1921." gt=verified → v0.16 abstain
(v0.15 verified). Temporal handling (award + year); possibly related to the same WS6
date/value-type change as R1.

## Other observations

- **Latency ~3.5× v0.15.** The discover/verify composition + multi-property binding
  discovery do more live oracle/SPARQL work per claim. Bounded (no true hangs; one
  watchdog flag, `pt_013` at 199s, self-resolved). Worth a Step-2 look for redundant work,
  but secondary to the correctness regressions.
- **multi_hop flat (45%)**: the `mhd_007` gain offset the `mhd_018` loss; the other failing
  multi_hop cases still abstain despite WS2's transitive/premise-forward work. Step 2 should
  inspect *which* multi_hop cases abstain and whether the composition should have caught them.

## Step-2 plan (preview)

1. **R1 first** (soundness): trace `bonus_006` live with full observability; fix the date /
   value-type / temporal-comparison path so a true "born before" is not contradicted; pin
   with a regression test. Re-check R5 (likely same root).
2. **R3/R4** (quantitative/Python compound): trace `csu_006`/`csu_012`; restore the
   quantitative + Python conjunct grounding.
3. **R2** (geo): trace `mhd_018`; restore the disjointness contradiction without reopening
   any leak.
4. Granular live re-tests on each fixed case + its neighbors (no full Medium Bar until Step 4).
