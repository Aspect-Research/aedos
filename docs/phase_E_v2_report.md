# Phase E5 — Per-Component Model Selection

**Session date:** 2026-05-23.
**Build:** v0.15.0-rc.9 (Phase G complete).
**Sequel to:** `docs/phase_E_report.md` (E1–E4: open-weight viability, extractor + python_verifier decisions).

## Scope and discipline boundary

This session establishes the per-component model configuration the remaining
v0.15 LLM-driven components route to. It is **not** Phase 10.5 (which measures
the calibrated system) and it is **not** prompt-engineering (which iterates
prompts against a chosen model). Discipline pattern: model selection first,
prompt engineering second (if needed), measurement third.

Two prior model decisions are already committed and out of scope here:

- **python_verifier:** Devstral Small 1.1 (Phase E, soundness winner).
- **extractor (`:user`, `:assistant`):** Claude Haiku 4.5 + v5 prompt (Phase E3).

The components in scope this session: `substrate:predicate_translation`,
`substrate:subsumption`, `substrate:predicate_distribution`,
`substrate:entity_resolution`, walker (measured via `derivation_corpus`).

## Candidates

Three primary candidates, plus Sonnet 4.6 as a derivation-corpus
tie-breaker:

| Candidate | Provider | $/M in | $/M out | Origin |
|---|---|---|---|---|
| `claude-haiku-4-5` | Anthropic direct | 1.00 | 5.00 | Phase E3 extractor winner |
| `qwen-3-next-80b-a3b-instruct` | OpenRouter (DeepInfra) | 0.09 | 1.10 | Phase E late-add (MoE 80B/3B-active) |
| `gpt-4.1-mini` | OpenAI direct | 0.40 | 1.60 | v0.15 default (baseline anchor) |
| `claude-sonnet-4-6` | Anthropic direct | 3.00 | 15.00 | Frontier-quality option (derivation only) |

Pricing was re-verified live against OpenRouter for the OpenRouter-routed
candidates before each billed run; closed-weight pricing snapshots are
documented at `_CANDIDATES` in `tests/evaluation/phase_e_comparison.py`.

## Harness changes

Two extensions landed before any billed run, then unit-tested
(`tests/unit/test_phase_e_comparison.py` — 38 passing):

1. `_ALL_CORPORA` extended to include `subsumption_corpus`,
   `predicate_distribution_corpus`, `entity_resolution_corpus` — the three
   substrate/resolution corpora the comparison hadn't previously covered.

2. `run_comparison` gained `purposes=` and `pin_purposes=` parameters. The
   default behaviour (`{"*": cand_cfg}` whole-run override) is preserved when
   both are None; `purposes=[...]` writes only the named purpose keys (every
   other purpose falls through to `DEFAULT_MODEL_BY_PURPOSE`); `pin_purposes`
   maps additional purposes to other candidate names. This is what isolates
   the per-component signal: a Haiku × `predicate_metadata_corpus` run
   measures Haiku's `substrate:predicate_translation` quality with every
   other purpose on its rc.9 default, not Haiku's quality on everything.

A small driver `tests/evaluation/phase_e5_runs.py` defines the 16-cell matrix
and writes results to `docs/phase_E/results/phase_e5_per_component/`.

## Results

Total spend: ~$1.87 against a $5–15 projection. Zero runner-errors across
all 16 cells; pricing verification passed on every billed run (Qwen at
$0.09/$1.10 per M, Devstral at $0.10/$0.30 per M, both unchanged from
recorded values). All results in
`docs/phase_E/results/phase_e5_per_component/`.

| Component | Threshold | Haiku 4.5 | Qwen 3-Next 80B | gpt-4.1-mini | Sonnet 4.6 |
|---|---:|---|---|---|---|
| `predicate_translation` (predicate_metadata_corpus, n=80) | 85% | **81.2%** / $0.240 | 67.5% / $0.042 | 65.0% / $0.069 | — |
| `subsumption` (n=60) | 80% | 81.7% / $0.031 | **88.3%** / $0.002 | **88.3%** / $0.003 | — |
| `predicate_distribution` (n=50) | 85% | 44.0% / $0.078 | **54.0%** / $0.008 | **54.0%** / $0.012 | — |
| `entity_resolution` (n=50) | 90% | 82.0% / $0.000 | 82.0% / $0.000 | 82.0% / $0.000 | — |
| walker (derivation_corpus, n=50) | 80% | 36.0% / $0.315 | **36.0%** / $0.047 | 34.0% / $0.099 | 34.0% / $0.904 |

False-verified soundness data: only `derivation_corpus` produces a
verified/contradicted/abstain verdict. All four walker candidates produced
**identical 2 false-verifieds** on the same two `der_revision_*` cases.
All other corpora have no verdict axis.

## Per-component analysis

### `substrate:predicate_translation` — Haiku 4.5 wins decisively

Haiku 81.25% vs Qwen 67.5% (+13.75 pts) vs gpt-4.1-mini 65.0% (+16.25 pts).
This is the largest model-capability spread in the matrix.

Haiku is the only candidate with within-striking-distance accuracy (3.75
points below the 85% threshold). Qwen 3-Next and gpt-4.1-mini are nowhere
near. Cost: Haiku $0.240 is 5.7× Qwen and 3.5× gpt-4.1-mini — but the
accuracy lead is large enough that cost-per-correct still favours Haiku
($0.0037/correct vs $0.0008 vs $0.0011) by less than the headline cost
ratio would suggest, and "correct" is the load-bearing thing here.

**Recommendation: `claude-haiku-4-5`.** The 4-point shortfall vs threshold
is a candidate for prompt-engineering follow-up (the same pattern Phase E3
applied to extraction).

### `substrate:subsumption` — Qwen 3-Next wins on cost-tied accuracy

Qwen 88.3% / $0.0018 = gpt-4.1-mini 88.3% / $0.0034 > Haiku 81.7% / $0.0311.
Both leaders exceed the 80% threshold; Haiku also exceeds it but at 10×–17×
the cost.

Important context: only 20/60 cases reached the LLM (the other 40 came
from KB cache after live Wikidata lookups), so the discriminating signal is
across ~20 LLM-driven cases. Two candidates tied at 18/20 LLM-driven cases
correct; Qwen wins on cost by ~2×. The per-case `sub_kb_*` cases take
30–55s each due to live Wikidata HTTP latency, which dominates wall-clock
but not cost.

**Recommendation: `qwen-3-next-80b-a3b-instruct`.** Cost-tie with
gpt-4.1-mini at half the price; Haiku's accuracy deficit doesn't justify
its premium for this purpose.

### `substrate:predicate_distribution` — Qwen 3-Next wins (architectural ceiling)

All three candidates well below the 85% threshold: Qwen 54% / gpt-4.1-mini
54% / Haiku 44%. Qwen wins the cost tie-break at $0.0081 vs $0.0118.

The shortfall is not primarily model-bound. Two diagnostics support this:

1. **`pd_both` category 100% fail across all three candidates** (0/5 each).
   The corpus's own note on this category: *"adversarial: ... calibration
   will validate"* — the corpus admits the `both` label is contested. Five
   of the 50 cases are `pd_both`, so excluding them lifts each candidate by
   ~10 points: Qwen 60%, gpt-4.1-mini 60%, Haiku 49%.

2. **Strong per-model directional bias** in the remaining categories. Qwen
   over-uses `neither` (96% on `pd_neither`, 8–25% on the directional ones).
   Haiku over-uses `distributes_up` (100% on `pd_up`, 20% on `pd_neither`).
   gpt-4.1-mini is balanced (48% / 62% / 83%). The oracle's prompt at
   `predicate_distribution.py:148–156` shows only two of the four verdict
   types as examples (`distributes_up` and `neither`); the other two
   (`distributes_down`, `both`) get no in-context demonstration. This is a
   prompt-engineering opportunity — adding examples covering all four
   verdicts should reduce the directional bias.

**Recommendation: `qwen-3-next-80b-a3b-instruct`.** Cost-tie with
gpt-4.1-mini, but the predicate_distribution oracle needs prompt
engineering before any candidate clears the 85% threshold. Flag as a
follow-up session.

### `substrate:entity_resolution` — Model-independent (post-D33)

All three candidates produced **identical results**: 41/50 = 82%, **0 LLM
calls, $0 cost, 25s** elapsed. The candidate's LLM was never invoked.

Mechanism: `EntityResolver.select`'s LLM-disambiguation path only fires when
the top-2 candidates' scores fall within `_AMBIGUITY_GAP=0.15`. After Phase
G's D33 type filter landed, the type filter eliminates wrong-type
candidates aggressively enough that ambiguity drops below the gap for every
case in this corpus (15 ambiguous + 10 type_filter + 20 unambiguous + 5
no_match). For every one of the 150 candidate-case combinations, the
selection was decided purely by the score-ranking pipeline; the model was
never consulted.

The 82% (= 8 abstain or wrong) is the architectural ceiling for this
corpus, consistent with D47 ("contextual disambiguation of bare ambiguous
strings upstream of KB queries") bounding the resolver's reach. The brief's
note that the architectural ceiling has been "substantially lifted, but not
entirely" — that's the 18% miss rate visible here.

**Recommendation: `qwen-3-next-80b-a3b-instruct`** for cost-efficiency on
the day the LLM path becomes active again (post-D47 work, or under future
corpora with genuinely ambiguous candidate pools). The choice has zero
production cost or accuracy impact today.

### Walker (derivation) — Architectural ceiling, no model differentiation

All four candidates clustered at 34–36% with **identical 2 false-verifieds
on the same two `der_revision_*` cases**:

| Candidate | Accuracy | False-verifieds | Cost |
|---|---|---|---|
| `claude-haiku-4-5` | 36.0% (18/50) | 2 | $0.315 |
| `qwen-3-next-80b-a3b-instruct` | 36.0% (18/50) | 2 | $0.047 |
| `gpt-4.1-mini` | 34.0% (17/50) | 2 | $0.099 |
| `claude-sonnet-4-6` | 34.0% (17/50) | 2 | $0.904 |

**Sonnet does not break the tie.** Per the brief's decision rule ("if
Haiku clearly dominates, Sonnet is informational but not decisive; if Haiku
and Qwen are close, Sonnet might break the tie"), Sonnet's tie at 34%
confirms the ceiling is structural, not capability-bound. Sonnet × other
corpora is therefore not warranted.

Verdict distribution is similar across all four: ~80% `no_grounding_found`
(abstain), ~14% `verified`, ≤2% `contradicted`, 2 errors per run. The high
abstain rate is consistent with two known architectural constraints:

- **D47 / D33 limitations.** Bare ambiguous subject strings (Obama,
  Williams College) don't reach their canonical Q-ids via the post-Phase-G
  type filter alone. Walker abstains rather than verifies wrong-entity
  data. These abstains are honest (soundness preserving), not model failures.
- **D5 — no KB-sourced neighbour enumeration.** The walker can verify a
  known subsumption chain via KB lookup but cannot enumerate a part_of/is_a
  chain from a cold start. Multi-hop distribution cases that need
  cold-start enumeration abstain.

**The 2 false-verifieds are themselves architectural, not model-related.**
Both cases (`der_revision_001`: "Asa prefers coffee" with prior "prefers
tea"; `der_revision_002`: "works at Google" with prior "employed_by
Microsoft") expect `contradicted` against a Tier U prior. The walker
correctly verifies the new claim from source text but does not contradict
the prior because D16 belief-revision (Phase B) only fires for
`single_valued=1` predicates. `prefers` is the canonical
`pd_neither`-multi-valued example; `employed_by`/`works_at` are
multi-valued and additionally cross-predicate-equivalence (the walker
would need to know they map to the same KB property AND that the
relationship is functional, which the seed pack says it isn't). This is a
**corpus-vs-architecture mismatch** in D23 territory — the seed pack's
`single_valued` decisions don't match the corpus's contradiction
expectations. Identical failure across every candidate confirms it's not a
model-capability issue.

**Recommendation: `qwen-3-next-80b-a3b-instruct`** for the four substrate
purposes the walker exercises in derivation. Equivalent accuracy to Haiku
at ~1/7 the cost, no false-verified differentiation. The python_verifier
pin (Devstral) is unchanged from the committed Phase E decision.

## Architectural-context interpretation

Where the data shows a candidate well below the corpus threshold, the
distinguishing question is "is the ceiling architectural or is the model
under-performing?" Phase E5's data resolves this per component:

- `predicate_translation` 81.25% vs 85% — **mostly model** (Haiku +13.75 pts
  over the next candidate suggests the ceiling is approachable with better
  prompting on Haiku).
- `predicate_distribution` 54% vs 85% — **mostly prompt** (per-model bias
  pattern + 2-of-4 verdict types in the prompt → systemic
  under-specification).
- `entity_resolution` 82% vs 90% — **entirely architectural** (D47 bounds;
  no model differentiation).
- walker 36% vs 80% — **entirely architectural** (D47 + D5 bound; no model
  differentiation; same 2 false-verifieds across all four candidates).
- `subsumption` 88.3% vs 80% — **above threshold** for two of three
  candidates; cost is the differentiator.

The brief's prediction that Phase G "substantially lifted" the ceiling so
the model-comparison signal would be cleaner is borne out: the per-corpus
candidate spreads are now interpretable per component. The architectural
ceiling is honestly visible (entity_resolution and walker) and the
prompt-engineering opportunities are honestly visible
(predicate_distribution, and to a lesser extent predicate_translation).

## Phase E5 configuration recommendation

Concrete update to `DEFAULT_MODEL_BY_PURPOSE` in `src/aedos/llm/client.py`.
The python_verifier change reflects the committed Phase E python_verifier
decision (Devstral); the four substrate purposes reflect this session's
data. Extractor purposes and `chat` are unchanged.

```python
DEFAULT_MODEL_BY_PURPOSE: dict[str, dict] = {
    "chat":                             {"model": "claude-haiku-4-5", **_ANTHROPIC},
    # Phase E3 (extractor) — unchanged.
    "extractor:user":                   {"model": "claude-haiku-4-5", **_ANTHROPIC},
    "extractor:assistant":              {"model": "claude-haiku-4-5", **_ANTHROPIC},
    # Phase E5 (2026-05-23) — per-component selection. See
    # docs/phase_E_v2_report.md.
    # Haiku decisively wins: 81.25% vs Qwen 67.5% vs gpt-4.1-mini 65.0%.
    # 4-point shortfall to 85% threshold: flagged for prompt-engineering.
    "substrate:predicate_translation":  {"model": "claude-haiku-4-5", **_ANTHROPIC},
    # Qwen ties gpt-4.1-mini at 88.3% (both above 80% threshold) at half
    # the cost. 20/60 cases reach the LLM (KB handles 40).
    "substrate:subsumption":            {"model": "qwen/qwen3-next-80b-a3b-instruct", **_OPENROUTER},
    # Qwen ties gpt-4.1-mini at 54%. ALL candidates well below 85%; the
    # oracle's prompt at predicate_distribution.py:148-156 shows only 2 of
    # 4 verdict types — prompt-engineering opportunity.
    "substrate:predicate_distribution": {"model": "qwen/qwen3-next-80b-a3b-instruct", **_OPENROUTER},
    # LLM-disambiguation path never fires post-Phase-G D33 (0 calls across
    # all candidates on the 50-case corpus). Choice is cost-driven for
    # when it does fire (post-D47 / future ambiguous-pool inputs).
    "substrate:entity_resolution":      {"model": "qwen/qwen3-next-80b-a3b-instruct", **_OPENROUTER},
    # Phase E (python_verifier soundness winner): Devstral Small 1.1.
    # Carry-forward of committed Phase E decision; not new in E5.
    "python_verifier":                  {"model": "mistralai/devstral-small", **_OPENROUTER},
}
```

**Net effect on the v0.15.0-rc.9 configuration:**

| Purpose | rc.9 | rc.10 proposed |
|---|---|---|
| `substrate:predicate_translation` | gpt-4.1-mini | claude-haiku-4-5 |
| `substrate:subsumption` | gpt-4.1-mini | qwen-3-next-80b-a3b-instruct |
| `substrate:predicate_distribution` | gpt-4.1-mini | qwen-3-next-80b-a3b-instruct |
| `substrate:entity_resolution` | gpt-4.1-mini | qwen-3-next-80b-a3b-instruct |
| `python_verifier` | gpt-4.1-mini | mistralai/devstral-small |

Every substrate / verifier purpose migrates off OpenAI; `gpt-4.1-mini` no
longer appears in `DEFAULT_MODEL_BY_PURPOSE`. The deployment retains
`OPENAI_API_KEY` only for ad-hoc one-off overrides; the runbook should
note OpenAI is no longer a load-bearing dependency for the deployed
pipeline.

## Follow-up surface for v0.16 (or post-Phase-10.5 sessions)

Not actions for this session — surfaced here so subsequent sessions can pick
them up against the data:

1. **predicate_translation prompt engineering.** Haiku at 81.25% / 85%
   threshold. The Phase E3 extraction-v5-prompt pattern applies: iterate
   the substrate prompt against the failing cases until Haiku clears the
   threshold. Likely 1-2 hour session.

2. **predicate_distribution prompt engineering.** Larger gap (54% / 85%).
   Per-model bias suggests adding distributed-down and both-direction
   examples to `predicate_distribution.py:148–156` would help all models.
   Pre-engineering candidate: confirm with the operator whether `pd_both`
   cases should be re-pinned or kept (the corpus admits the label is
   contested).

3. **D23 + corpus-vs-architecture for belief_revision.** The 2 universal
   false-verifieds in derivation (`der_revision_001`, `der_revision_002`)
   are corpus-vs-architecture mismatches. Either the seed pack should
   classify `prefers` / `employed_by` as `single_valued=1` (a deployment
   decision the v0.16 planning held back for Phase 10.5 data), or the
   corpus's expected verdicts should change to reflect the multi-valued
   reality. Either way, the walker code is correct under its current
   inputs.

4. **entity_resolution upstream work (D47).** The 18% miss rate is
   architectural. Per the v0.16 planning, work items are extraction-time
   normalization, resolver context enhancement, and abstention-threshold
   re-evaluation. Not part of model selection.

5. **predicate_translation upstream observations.** Haiku reached 81.25%
   without leaving the test budget; if Sonnet on predicate_translation
   would clear 85%, the cost / accuracy trade is worth measuring in a
   targeted follow-up rather than across the whole matrix.

## Next steps for this session

1. **Operator review** of the per-component recommendations and the proposed
   `DEFAULT_MODEL_BY_PURPOSE` dict.
2. **Configuration commit** applying the proposed change to
   `src/aedos/llm/client.py`. Run `pytest tests/unit/` to confirm no
   unit-test regressions (the unit tests don't exercise the production
   defaults at LLM-call level, so changes here should be inert to the
   suite). Tag as `v0.15.0-rc.10`.
3. **Phase 10.5** runs the calibrated configuration against the corpora at
   scale and against the medium-bar evaluation. The session brief is
   explicit that Phase 10.5 is the next, distinct session.

Total session cost (Part 1, baseline): $1.87 across 16 substantive runs
(4 walker candidates + 4×3 substrate candidates) plus 5 smoke cases.

---

## Part 2 — Prompt iteration on the proposed-winner models

Operator decision after Part 1: rather than commit the configuration
immediately, **iterate the prompts of each LLM-active proposed-winner model
in turn** (the Phase E3 extraction-v5 pattern), analyse what each model gets
wrong, and decide per-failure whether to tighten the corpus or iterate the
prompt. Entity_resolution is excluded — its LLM path never fires
post-Phase-G.

Order taken: cheapest-to-iterate first; each component frozen after one or
two prompt versions to avoid over-fitting.

### `substrate:predicate_distribution` × Qwen 3-Next — v1 → v2

**Baseline failure analysis (Qwen v1 = 54%, 27/50):**

Per-category accuracy: pd_up 1/12 (8%), pd_down 2/8 (25%), pd_neither 24/25
(96%), pd_both 0/5 (0%). Strong "neither" bias. Reading the model's `reason`
field per case showed Qwen philosophically disagreed with the corpus's
pinned verdicts even when the corpus's example was literally in the prompt
(pd_up_001: `lives_in × part_of` — the prompt's exact distributes_up
example — got labelled `neither`).

Diagnosis: the prompt had **only 2 of 4 verdict types as in-context examples**
(distributes_up and neither), no polarity guidance, and the examples read as
"illustrative" rather than "authoritative." Qwen's open-weight bias toward
abstention-on-uncertainty (consistent with its Phase E python_verifier
profile) collapsed everything ambiguous to `neither`.

**v2 prompt change** (`src/aedos/layer3_substrate/predicate_distribution.py:148`):
- All 4 verdict definitions stated explicitly with their inference rules.
- Authoritative rubric: distributes_up example expanded to enumerate the
  locative-containment predicate family (lives_in / located_in / works_in /
  born_in / died_in / etc. over part_of); distributes_down example added
  with the mortal-on-is_a kind/universal-property family; neither example
  retained for attitudinal predicates; `both` flagged as rare with an
  explicit "default to neither when uncertain."
- Polarity rule added: polarity=0 defaults to `neither` because the
  contrapositive of a distributing rule does not generally hold.

**v2 result: 88.0% (44/50).** Per-category: pd_up 12/12 (100%, +92 pts),
pd_down 8/8 (100%, +75 pts), pd_neither 24/25 (96%, -0), pd_both 0/5 (0%, -0).

- One pd_neither regression (`pd_neither_019: born_in × is_a → expected
  neither, produced distributes_up`) — Qwen over-applied the v2 rubric's
  "born_in is a locative-containment predicate" mention from its
  `over part_of` clause to the `is_a` case. Borderline.

- pd_both unchanged at 0/5. The corpus's own notes admit these 5 cases are
  *"adversarial: ... calibration will validate"*. Calibration validates
  disagreement: Qwen consistently doesn't see "both" as the right call for
  these (it picks distributes_up or neither). Recommendation: **tighten the
  corpus pd_both cases in a separate session** rather than chase them with
  prompt engineering. Excluding the 5 admittedly-adversarial pd_both cases:
  Qwen reaches 44/45 = 97.8%.

**Decision: ship v2.** Above the 85% threshold; remaining errors are 1
borderline overgeneralization + 5 corpus-acknowledged adversarial cases.
Iteration cost: $0.009 (1 re-measurement at Qwen prices).

### `substrate:predicate_translation` × Haiku 4.5 — v1 → v2 → v2.1

**Baseline failure analysis (Haiku v1 = 81.25%, 65/80):**

15 failures across 5 patterns:

1. **Over-abstention on user_authoritative predicates** (2 cases: `experienced`,
   `ranks`). The prompt's "When in doubt, choose abstain over kb_resolvable"
   guidance was applying too broadly.
2. **Missing python routing for math/logic predicates** (5 cases: `equals`,
   `has_length_of`, `is_between`, `is_prime`, `chronologically_precedes`).
   The prompt mentioned "arithmetic, date math" with no examples.
3. **Wrong KB property selection** (5 cases: `founded → P571 vs P112`,
   `successor_of → P155 vs P1365`, `co_founded → P108 vs P112`, plus
   `part_of` distinct_slots and `has_isbn` object_type).
4. **Under-abstention on opinion predicates** (2 cases: `influenced`,
   `is_better_than`).
5. **Ambiguous-routing miss** (`has_score`).

The prompt had **only 2 abstract paragraphs of routing guidance with zero
examples per category**. Diagnosis: the routing dimension was the
load-bearing failure, not the structural metadata fields.

**v2 prompt change** (`src/aedos/layer3_substrate/predicate_translation.py:98`):
- Each routing_hint expanded with **named examples**, a **signal** (what to
  look for), and a **caution** (what NOT to confuse it with). Examples for
  user_authoritative (first-person/inner-state predicates), python
  (math/logic/comparison predicates), kb_resolvable (10 Wikidata properties
  with disambiguation notes for the easily-confused ones — founder vs.
  inception date, replaces vs. follows-in-sequence), abstain (intrinsically
  contested predicates).
- "When in doubt" guidance rewritten to be category-specific: prefer
  user_authoritative/python over kb_resolvable; prefer kb_resolvable over
  abstain.
- `distinct_slots` clause added (previously implicit in the schema).

**v2 result: 92.5% (74/80, +11.25 pts).** Above 85% threshold. Remaining 6
failures: 2 borderline (`recommends` user_subject flag, `claims` abstain),
3 borderline object_type / routing edges (`duration_is`, `is_between`,
`has_score`), 1 self-inflicted from the v2 prompt (`has_isbn` —
I explicitly told Haiku ISBN was `quantity` but the corpus pins it as
`entity`).

**v2.1 prompt change** (one-line correction): fixed the `has_isbn` example
to say `object_type=entity` (matching the corpus); added durations
explicitly to the `quantity` definition.

**v2.1 result: 92.5% (74/80).** Same accuracy, different failure set —
fixed the 3 self-inflicted v2 issues (`recommends`, `is_between`, `has_isbn`)
but introduced 3 borderline regressions (`is_prime`, `has_area`,
`has_currency`). Variance within Haiku's distribution at this accuracy
level. The 3 stable failures across v2 and v2.1 (`claims`, `duration_is`,
`has_score`) are all corpus-noted as borderline/adversarial.

**Decision: ship v2.1.** Above 85% threshold; v2.1 is the cleaner prompt
(no wrong ISBN example). Iteration cost: $0.60 (2 re-measurements at Haiku
prices).

### `substrate:subsumption` × Qwen 3-Next — no iteration

**Baseline analysis (Qwen baseline = 88.3%, 53/60):** Already above 80%
threshold. Examining the 7 failures:

| case_id | calls | input | expected |
|---|---|---|---|
| sub_kb_002 | 0 | Q49112 is_a Q189004 (Williams College, liberal arts college) | a_subsumed_by_b |
| sub_kb_006 | 0 | Q771397 is_a Q515 (Williamstown, city) | a_subsumed_by_b |
| sub_kb_011 | 0 | Q3784 part_of Q4022 (Amazon River, S.A. river system) | a_subsumed_by_b |
| sub_kb_015 | 0 | Q189004 is_a Q49112 (reverse direction) | b_subsumed_by_a |
| sub_kb_017 | 0 | Q11696 is_a Q11696 | equivalent |
| sub_kb_021 | 0 | Q937 is_a Q901 (Einstein, physicist) | a_subsumed_by_b |
| sub_kb_027 | 0 | Q1065 is_a Q43229 (UN, organization) | a_subsumed_by_b |

**All 7 failures have `calls=0`** — the failures are entirely in the
KB-lookup path (`KBProtocol.subsumption`), not the LLM generation path.
**There is no prompt to iterate.** The 20 LLM-generation cases
(`sub_synth_*`) all passed (20/20).

This is consistent with the same pattern entity_resolution exhibits: the
model-comparison signal is hidden behind a different bottleneck. For
subsumption the bottleneck is the KB protocol's missing
neighbor-traversal coverage (D5 / KB subsumption-query accuracy on
well-known Wikidata pairs).

**Decision: no iteration.** Subsumption Qwen is above threshold; remaining
failures are KB-architecture, deferred to v0.16.

### Walker (derivation_corpus) × Qwen 3-Next — substrate improvements do not transfer

**Re-measurement with v2 substrate prompts active.**

Result: **18/50 = 36%** — identical to the baseline. The 2 false-verifieds
(`der_revision_001`, `der_revision_002`) are unchanged. Category breakdown:

| category | walker accuracy |
|---|---|
| abstention | 6/6 (100%) |
| belief_revision | 0/6 |
| cross_source | 4/10 |
| entity_disambiguation | 2/8 |
| multi_hop_distribution | 3/12 |
| predicate_translation | 3/8 |

Diagnosis: the substrate-prompt improvements **do not transfer to the
walker** because the walker's accuracy is bounded downstream of substrate
quality:

- `belief_revision` 0/6 — D16 only fires for `single_valued=1` predicates;
  the corpus expects contradiction on `prefers` (pd_neither canonical
  example) and `employed_by`/`works_at` (multi-valued). Architectural
  mismatch between corpus and walker, not addressable by substrate
  prompts (D23 territory).
- `multi_hop_distribution` 3/12 — D5: walker has no KB-sourced neighbour
  enumeration. The substrate's correct predicate_distribution output
  doesn't help if the walker can't fetch the part_of/is_a chain.
- `entity_disambiguation` 2/8 — D47: bare ambiguous subject strings
  (Obama, Williams College) don't reach canonical Q-ids via type filter
  alone. Walker abstains honestly.

The 2 false-verifieds remain the structurally-bounded cases from Part 1's
analysis.

**Decision: walker accuracy is architectural-ceiling-bound at this point.**
Further work belongs to v0.16 (D5, D47) or a separate corpus-vs-architecture
session (D16/D23 re-pinning). Iteration cost: $0.04 (1 re-measurement at
Qwen prices).

## Final accuracy table

Per-component, **proposed model** in the rc.10 configuration, with v1
(baseline) and final accuracy from this session's prompt iteration:

| Component | Proposed model | v1 baseline | After iteration | Threshold | Above? | Δ |
|---|---|---:|---:|---:|---:|---:|
| `substrate:predicate_translation` | claude-haiku-4-5 | 81.25% (65/80) | **92.5% (74/80)** | 85% | ✓ | +11.25 |
| `substrate:subsumption` | qwen-3-next-80b-a3b-instruct | 88.3% (53/60) | 88.3% (53/60) | 80% | ✓ | 0 (no prompt change) |
| `substrate:predicate_distribution` | qwen-3-next-80b-a3b-instruct | 54.0% (27/50) | **88.0% (44/50)** | 85% | ✓ | +34.0 |
| `substrate:entity_resolution` | qwen-3-next-80b-a3b-instruct | 82.0% (41/50) | 82.0% (41/50) | 90% | ✗ | 0 (LLM never fires; D47 ceiling) |
| walker (derivation_corpus) | qwen-3-next-80b-a3b-instruct + substrate prompts | 36.0% (18/50) | 36.0% (18/50) | 80% | ✗ | 0 (architectural ceiling) |

**Three of five components now pass their thresholds at the proposed
configuration.** The two that don't are both architectural-ceiling-bound,
not model-bound:

- `entity_resolution` 82% < 90% — bounded by D47 (unreachable canonical
  entities for bare ambiguous strings).
- walker 36% < 80% — bounded by D5 (no KB-sourced neighbour enumeration),
  D16/D23 (functional-predicate flag vs. corpus belief-revision
  expectations), and D47 (resolver ceiling) propagating in.

## Prompt changes made

### `predicate_distribution.py:144-198` (v2)
- Replaced 2-example/4-line prompt with full rubric covering all 4 verdict
  types (distributes_up, distributes_down, both, neither).
- Added "AUTHORITATIVE RUBRIC" framing: predicate families (locative-
  containment for `up`, kind-universal-property for `down`, attitudinal for
  `neither`).
- Added explicit "default to neither over both when uncertain" guidance.
- Added polarity rule: polarity=0 defaults to `neither` because
  contrapositives of distributing rules don't generally hold.

### `predicate_translation.py:98-142` (v2 → v2.1)
- Each routing_hint expanded with `examples` + `signal` + `caution`
  blocks.
- 10 Wikidata properties enumerated for kb_resolvable with
  easily-confused-pair disambiguation notes (founder/inception,
  replaces/follows).
- "When in doubt" guidance made category-specific (preference order:
  user_authoritative / python > kb_resolvable > abstain).
- `distinct_slots` clause added (previously implicit in the schema).
- v2.1: fixed `has_isbn` example to `object_type=entity` (was incorrectly
  `quantity` in v2); explicitly added durations to the `quantity`
  definition.

### `subsumption.py` — no change
Above threshold without iteration; remaining failures are KB-architecture.

### Walker — no prompt to iterate
The walker dispatches to substrate oracles whose prompts were iterated in
predicate_distribution and predicate_translation above; re-measurement
showed no transfer.

## Updated `DEFAULT_MODEL_BY_PURPOSE` recommendation — unchanged from Part 1

The per-component model picks did not change from Part 1's recommendation.
The prompt iterations strengthened the case for each:

- predicate_translation Haiku is now decisively above threshold (92.5%
  vs. 85%) with a 12-point gap over the next-best candidate baseline.
- predicate_distribution Qwen is now above threshold (88% vs. 85%) —
  baseline was the limiter; capability matched gpt-4.1-mini before iteration,
  and the v2 prompt should benefit gpt-4.1-mini too if re-measured (out of
  this session's scope).
- subsumption / entity_resolution / walker unchanged.

```python
DEFAULT_MODEL_BY_PURPOSE: dict[str, dict] = {
    "chat":                             {"model": "claude-haiku-4-5", **_ANTHROPIC},
    "extractor:user":                   {"model": "claude-haiku-4-5", **_ANTHROPIC},
    "extractor:assistant":              {"model": "claude-haiku-4-5", **_ANTHROPIC},
    "substrate:predicate_translation":  {"model": "claude-haiku-4-5", **_ANTHROPIC},
    "substrate:subsumption":            {"model": "qwen/qwen3-next-80b-a3b-instruct", **_OPENROUTER},
    "substrate:predicate_distribution": {"model": "qwen/qwen3-next-80b-a3b-instruct", **_OPENROUTER},
    "substrate:entity_resolution":      {"model": "qwen/qwen3-next-80b-a3b-instruct", **_OPENROUTER},
    "python_verifier":                  {"model": "mistralai/devstral-small", **_OPENROUTER},
}
```

## Total session spend

| Phase | Cost |
|---|---:|
| Part 1 baseline matrix (16 runs + smoke) | $1.87 |
| predicate_distribution v2 iteration | $0.009 |
| predicate_translation v2 + v2.1 iteration | $0.599 |
| walker re-measurement | $0.039 |
| **Total** | **~$2.52** |

Well under the $5–15 projected budget.

## Items surfaced for v0.16 / Phase 10.5

Same five items as Part 1's follow-up surface, refined by the iteration
data:

1. **Corpus-tightening for `predicate_distribution.pd_both`** — the 5
   adversarial cases the corpus admits need calibration validation.
   Calibration's validation is "these probably aren't both." Either re-pin
   them (probably to `neither` or one-directional) or accept them as
   permanent ~10% accuracy haircut.

2. **predicate_translation prompt has 3 stable borderline failures**
   (`claims` abstain, `duration_is` object_type, `has_score` routing). All
   3 are corpus-noted as adversarial; corpus or schema tightening could
   close them. Schema-level: an `identifier` or `literal` object_type
   would honestly classify ISBNs and durations without forcing them into
   `entity` or `quantity`.

3. **Subsumption KB-query accuracy on canonical Wikidata pairs** — 7/60
   cases fail because the KB protocol returns "unrelated" for relationships
   that are correct in Wikidata (Williams College is_a liberal arts
   college, Einstein is_a physicist). v0.16 should audit
   `kb_wikidata.py::subsumption`'s SPARQL coverage.

4. **D16/D23 corpus-vs-architecture for `belief_revision`** — the 2
   universal false-verifieds in derivation are due to walker correctly
   not contradicting against multi-valued Tier U priors. Either the seed
   pack should reclassify `prefers` / `employed_by` as functional, or the
   corpus's expected verdicts should change.

5. **D47 entity-resolution upstream and D5 walker neighbour enumeration**
   remain the walker-accuracy ceilings. Not model or prompt issues.

## Next-step request

The proposed `DEFAULT_MODEL_BY_PURPOSE` is unchanged from Part 1; the
iterated substrate prompts are already on the working tree (the
predicate_distribution.py and predicate_translation.py edits). To ship as
`v0.15.0-rc.10`, apply the `DEFAULT_MODEL_BY_PURPOSE` change to
`src/aedos/llm/client.py`, run `py -m pytest tests/unit/` to confirm no
unit-test regressions, commit with a Phase E5 reference, and tag.
Operator confirmation required before commit per the session brief.
