# Phase D — Plan (calibration runner fixes)

Pre-Phase-10.5 polish session. Phase D fixes the **measurement instrument** —
the per-corpus runner code in `tests/calibration/test_corpus_runner.py` — so
that the Phase 10.5 numbers measure what they claim. The verification pipeline
itself is unchanged: D1–D3 touch one file.

Five fixes, two clusters, then a validation pass:

- **D1** — hard blockers: `_run_extraction` per-category dispatch,
  `_run_temporal_scope` key-name handling.
- **D2** — soft mis-scoring: `_run_entity_resolution`, `_run_kb_mapping`,
  `_run_derivation`.
- **D3** — dry-run + suite validation, Phase D report, `rc.5` tag.

Start point `v0.15.0-rc.4` (HEAD `75c92d7`). Highest existing delta is **D23**;
the Phase D process-delta is **D24**.

---

## A finding that reshapes the verification methodology

The inputs describe the dry-run (`pytest --run-calibration` without
`RUN_CALIBRATION=1`) as the discriminating mechanism — "confirm KeyError-then-fail
for 42 extraction cases pre-fix; all cases harness-validated post-fix."

**That is not what the dry-run does.** `test_corpus_calibration` loads the
corpus, asserts it is non-empty, and then — when `RUN_CALIBRATION` is unset —
`pytest.skip()`s **before `runner = _RUNNERS[corpus]` is ever reached**. The
runner functions are not called in a dry-run. The dry-run exercises JSON
parsing only; it has no LLM/KB cost precisely because it never enters a runner.

Consequences:

1. The dry-run output is **720 passed, 12 skipped, 0 errors** before *and*
   after every Phase D fix — unchanged. D3's stated expected outcome already
   says exactly this ("Same as the pre-D1 dry-run baseline"). The D1 testing
   prose ("confirm 42 KeyErrors in the dry-run") is not achievable: those
   KeyErrors only arise under live evaluation (`RUN_CALIBRATION=1`), where the
   per-case `except Exception` catches them and counts them as failures.
2. The `KeyError` blockers are therefore **invisible to every pre-release
   check that does not pay for a live run.** A corpus whose runner would
   `KeyError` on 74% of its cases (`extraction`) passes the dry-run green.
   This is the audit-chain gap the session is meant to surface, in its
   sharpest form — see D24 below.

**Verification methodology for Phase D** (substitute for the described
stash-and-verify): each runner fix is verified by a **static key-access and
comparison cross-check** against the corpus schema, documented per
sub-category in this plan and re-confirmed in the report. For every
sub-category: which `case[...]` keys does the runner branch read, do all cases
in that sub-category carry those keys, and does the comparison test the right
field. The dry-run is still run at D3 to confirm the corpora still load and
parse (`720 passed, 12 skipped`), and `pytest tests/ -q` to confirm the suite
is unaffected — but neither distinguishes pre/post fix, and the plan does not
pretend otherwise.

The harness is **not** modified to make the dry-run exercise runners — that is
out of scope ("don't refactor the harness") and is folded into the D24
process-delta recommendation instead.

---

## Cluster D1 — Hard blockers

### D1a — `_run_extraction` per-category dispatch

`extraction_corpus.jsonl`: 57 cases, 5 categories (key: `category`).
Current `_run_extraction` reads `case["input"]` and `case["expected_predicate"]`
unconditionally — only the 15 `normalization` cases carry `expected_predicate`,
and the 7 `hard_claim` cases carry `text` not `input`. 42/57 cases `KeyError`.
Max achievable accuracy 26.3%, hard-blocked below the 90% threshold.

Fix: dispatch on `case["category"]`. Per-branch design (key-access verified
against every case in the sub-category):

**normalization (15)** — keys: `input`, `expected_predicate`. Extract from
`case["input"]`; pass if any produced claim has
`predicate == case["expected_predicate"]`. (Current logic, now a branch.)

**temporal (15)** — keys: `input`, `expected_scope`. Extract from
`case["input"]`. If `expected_scope.get("is_future")` is true (temporal_004,
_013), the extractor drops the claim (`scope.is_future → _build_claim` returns
`None`) → pass iff `not claims`. Otherwise pass iff `claims` non-empty and
`claims[0]` matches `expected_scope` on **all three** scope fields
(`valid_from`, `valid_until`, `valid_during_ref`), each compared via
`expected_scope.get(field)` (absent ⇒ expected `None`). Comparing
`valid_during_ref` (not just the two fields `_run_temporal_scope` checks) is
deliberate: temporal_005/_011/_015 carry `valid_during_ref` expectations and a
new branch should test what its corpus pins. See the `_run_temporal_scope`
note below for the asymmetry.

**decomposition (10)** — keys: `input`, `expected_claim_count` (9 cases) **or**
`expected_shared_event_id` (decomp_008 only — it has no count). Extract from
`case["input"]`. If `expected_claim_count` present, require
`len(claims) == case["expected_claim_count"]`. If `expected_shared_event_id`
is truthy, require `claims` non-empty and every claim's `reified_event_id`
non-`None` and identical (`decompose_event` assigns one shared `event_<hex>`
id). Guarded with `in` / `.get` so the absent key never raises.

**first_person (10)** — keys: `input`, `asserting_party`, `expected_subject`.
Extract with `ExtractionContext(asserting_party=case["asserting_party"], …)`
so `_canonicalize` maps `I/me/my/we` → the asserting party. Pass if any
produced claim has `subject == case["expected_subject"]`. (The case's own
`asserting_party` must be used, not the default `"calibration"` — firstperson_001
expects `user_test`.)

**hard_claim (7)** — keys: `text` (all 7), `context_mentions`,
`expected_subjects_in_output` (6 cases), `expected_subjects_not_in_output`
(5 cases), and — hardclaim_006 only — `source_text_check` / `is_paraphrase`
instead. Extract from `case["text"]` (per inventory; **not** `input`) with
`asserting_party="user_test"` (hardclaim_002's `text` is first-person "I …"
and expects `user_test`). Then:
  - every subject in `expected_subjects_in_output` (`.get(…, [])`) must appear
    among produced claim subjects;
  - no subject in `expected_subjects_not_in_output` (`.get(…, [])`) may appear;
  - if `source_text_check` present (hardclaim_006), require some produced
    claim with `source_text == case["source_text_check"]`.
hardclaim_007's `expected_subjects_in_output` is `[]` and it has no
`not_in_output` — the branch passes it vacuously. The corpus pins only the
in-list; the runner compares only what the corpus pins (noted, not
"fixed" — adding a "claims must be empty" check would test something the
corpus does not state).

A trailing `raise KeyError(f"unknown extraction category: {category}")` guards
an unrecognised category (never fires for this corpus — defensive only).

### D1b — `_run_temporal_scope` key-name handling

`temporal_scope_corpus.jsonl`: 40 cases, 5 categories. The 5 `future_rejection`
cases store their expected answer under `expected` (the literal string
`"rejected"`), not `expected_scope`. `case["expected_scope"]` `KeyError`s on
those 5; accuracy caps at 87.5% vs the 90% threshold.

Fix: **Option 2 (sub-category dispatch)**, for consistency with the dispatched
`_run_extraction` after D1a (the inputs note Option 2 is "more consistent" once
the file uses dispatch). A `future_rejection` branch keyed on
`case["category"]`: future-tense claims are dropped at extraction, so the case
passes iff `not claims`. This branch reads neither `expected_scope` nor
`expected` — `expected` for these cases is a bare string, not a dict, so the
old `expected.get("rejected")` empty-claims line would have raised
`AttributeError` even once past the `KeyError`. The dispatch removes that
line: the non-future branch keeps the existing
`claim.valid_from/​valid_until == expected_scope.get(…)` comparison, and its
empty-claims case simply returns `False` (a non-future case that produced no
claim has failed).

**Observation, not fixed (v0.16):** the non-future `_run_temporal_scope`
comparison checks only `valid_from`/`valid_until`, so the 10 `relative_scope`
cases (which pin `valid_during_ref`/`valid_from_ref`/`valid_until_ref`) pass
without their relative-scope expectation being checked — a soft over-leniency.
This was **not** in the inventory's fix list; per "fix exactly what the
inventory identified," it is recorded for v0.16, not changed here. (`Claim`
also has no `valid_from_ref`/`valid_until_ref` fields, so ts_relative_007/_008
would need a `Claim`-schema change — genuinely v0.16 scope.)

### D1 commit

`Cluster D1: hard-block runner fixes for extraction + temporal_scope`

---

## Cluster D2 — Soft mis-scoring

### D2a — `_run_entity_resolution` ⚠ inventory premise does not hold

The inventory states: "all 15 ambiguous cases also carry `top_kb_identifier`,
so the strict `if 'top_kb_identifier' in expected` check fires first … the
lenient branch is dead code."

**Against `entity_resolution_corpus.jsonl` this is not the case.** Of the 15
`ambiguous` cases, only **5** carry `top_kb_identifier` (er_ambiguous_001, _002,
_006, _007, _013); the other **10** carry `disambiguation_key` only. The
lenient `return True` branch already fires for those 10 — it is **not dead
code.** The 5 that hit the strict check are exactly the cases the corpus author
gave a pinned answer to (e.g. er_ambiguous_001 → `Q90`, note "location slot
should prefer city"): an ambiguous reference that the predicate+slot context
nonetheless *resolves*. The 10 without `top_kb_identifier` carry keys like
`needs_context`, `country_or_state`, `person_or_place` — genuinely
undecidable from predicate+slot alone.

So the current `_run_entity_resolution` is arguably **already correct**:
strict-check the 5 decidable ambiguous cases (they have a pinned answer),
lenient-pass the 10 undecidable ones. Applying the inventory's reorder
(`disambiguation_key` ⇒ lenient for all 15) would **loosen** scoring on the 5
pinned cases — the precise "too lenient" failure mode D2 is meant to remove.

`disambiguation_key` is also a free-text hint string (`"city_not_person"`),
not a machine-checkable candidate set — there is nothing to verify "the
resolver picked a candidate consistent with" beyond "non-`None`".

**This contradicts an explicit instruction in the inputs and is raised as a
question** (Q1). Recommended: **no change** to `_run_entity_resolution` —
the runner is not mis-scoring; the inventory observation was inaccurate.
(One unrelated soft-leniency exists — er_no_match_002's
`result: "ambiguous_or_no_dominant"` falls through to `return True` — but it is
a single case, not in the inventory, and is recorded for v0.16, not changed.)

### D2b — `_run_kb_mapping` slot_to_qualifier comparison

`kb_mapping_corpus.jsonl`: 40 cases — 30 `kb_resolvable`, 10
`qualifier_mapping`. The `qualifier_mapping` cases exist to test
`slot_to_qualifier`, but `_run_kb_mapping` compares only `kb_property`. The
qualifier dimension is untested — a capability the corpus authors expected.

Fix: after the `kb_property` check, for `category == "qualifier_mapping"` also
compare `slot_to_qualifier`. **Subtlety:** `PredicateTranslation._generate_and_store`
stores `json.dumps(slot_to_qualifier_raw) if slot_to_qualifier_raw else None`
— an **empty dict is falsy and is stored/loaded as `None`**. Two
`qualifier_mapping` cases (kb_map_qualifier_006, _010) expect
`slot_to_qualifier: {}`. The comparison must normalise both sides:
`(meta.slot_to_qualifier or {}) == (expected.get("slot_to_qualifier") or {})`.
The 30 `kb_resolvable` cases keep the `kb_property`-only check (their
`expected_output` carries no `slot_to_qualifier`).

This is a capability addition — the runner will test something it did not
before — and the commit message names it.

### D2c — `_run_derivation`: lenient auto-pass + cross_source seed gap

`derivation_corpus.jsonl`: 50 cases, 6 categories.

**Issue 1 — the lenient `return True` branch.** It auto-passes any case whose
`expected_output.verdict` is not in `{verified, contradicted,
no_grounding_found}`. Four cases hit it:

| case | category | `expected_output` | proposed expected |
|------|----------|-------------------|-------------------|
| der_disambiguation_001 | entity_disambiguation | `verdict: "verified_with_correct_entity"`, note "select Q76 not Q842926" | `verdict == "verified"` **and** `Q76` appears as a `kb_statement` entity in the walk trace |
| der_disambiguation_006 | entity_disambiguation | `verdict: "verified_with_correct_entity"`, note "select Q3783" | `verdict == "verified"` **and** `Q3783` in the trace |
| der_predicate_translation_007 | predicate_translation | `verdict: "needs_tier_u_or_kb"` ("needs either Tier U or KB to verify") | `verdict == "no_grounding_found"` — the runner seeds no Tier U for this case and "Asa"/"Williams College" `educated_at` has no KB statement, so with neither premise present the walker abstains |
| der_disambiguation_005 | entity_disambiguation | `{disambiguation_note: …}` — **no `verdict` key**, note "may abstain or need context" | **open — see Q3** |

der_disambiguation_005 is the one case the corpus author did **not** pin a
verdict for. The implementation for the other three is unambiguous; 005 is
raised as a question (Q3).

The trace-entity check (001/006): walk the `WalkResult.trace.edges`, read
`edge.target.content.get("entity")` — KB premise-lookup edges carry the
resolved entity there. `verdict == "verified"` alone is already a strong proxy
(a wrong entity would fail the KB lookup), but the explicit entity check is
what the inventory asks for and is more faithful.

**Issue 2 — cross_source seed gap.** The inventory says cross_source cases
carry `kb_claim`/`python_claim` "seed data" that the runner fails to seed into
the substrate. **This premise also does not hold the way the tier_u seeding
does.** `tier_u` and `context_premises` seed *local* tables (Tier U, the
`subsumption` table). A `kb_claim` like
`{entity: "Q49112", property: "P131", value: "Massachusetts"}` is **not** seed
input — the harness's KB is the live `WikidataAdapter`, and live Wikidata
already holds Q49112/P131; the `kb_claim` field *documents* the expected KB
statement. `python_claim` (`{code: "assert 4 < 7"}`) is likewise descriptive —
`PythonVerifier` generates its own code. There is no local KB/Python substrate
to seed. The only genuine seed input for cross_source is `tier_u`, which the
runner already seeds.

(der_cross_007 — Tier U value feeding a Python computation — will abstain in
Phase 10.5, but that is **D10**, a known deferred walker limitation, not a
runner bug; the runner cannot fix it.)

**This contradicts the inventory and is raised as a question** (Q2).
Recommended: **skip Issue 2** — there is nothing to seed; the cross_source
cases verify against the live KB / Python verifier, which already have what
they need.

### D2 commit

`Cluster D2: soft mis-scoring fixes for entity_resolution, kb_mapping, derivation`
(scope depends on Q1/Q2 answers)

---

## Cluster D3 — Validation and report

- `pytest --run-calibration -q` → expect **720 passed, 12 skipped, 0 errors**
  (unchanged — the dry-run does not invoke runners; see the methodology
  finding above).
- `pytest tests/ -q` → expect **720 passed** (runner fixes add/remove no tests).
- Static key-access cross-check re-confirmed for each fixed runner.
- `docs/phase_D_report.md`: the five fixes, the static-verification results,
  the audit-chain finding, and the **D24** process-delta.
- D24 added to `docs/v0.16_planning.md`; D21/D22 confirmed Resolved (Phase C).
- Final commit tagged **`v0.15.0-rc.5`**. Phase 10.5 starts from rc.5;
  fallback remains `v0.15.0-rc.2`.

### D24 (process-delta, draft)

The production pipeline was audited across ten rounds; the calibration runner
was treated as test infrastructure and trusted by association. It was partial:
two runners hard-`KeyError`'d on the majority of their corpus, and the dry-run
— the one cheap pre-release check — could not see it, because it skips before
invoking runners. v0.16 should make "the runner can score every case of its
corpus" a standard pre-release gate: either the harness dry-run invokes each
runner's case-reading path (against a stub harness, so no LLM/KB cost), or a
static runner-vs-corpus key audit runs in CI.

### D3 commit

`Cluster D3: dry-run verified clean; Phase D report; rc.5 tag`

---

## Open questions — resolved (check-in before implementing D2)

- **Q1 — `_run_entity_resolution`:** *Leave runner unchanged.* The inventory's
  premise is inaccurate; the runner already strict-checks the 5 pinned
  ambiguous cases and lenient-passes the 10 undecidable ones. D2a is a no-op —
  recorded as a finding, no edit.
- **Q2 — `_run_derivation` Issue 2:** *Skip.* `kb_claim`/`python_claim` are
  descriptive, not seedable substrate. No seeding code added; recorded as a
  finding.
- **Q3 — der_disambiguation_005:** *Keep this one case lenient.* The corpus
  pins no `verdict`; der_disambiguation_005 stays an explicit, documented
  auto-pass. The other three Issue-1 cases (der_disambiguation_001/_006,
  der_predicate_translation_007) are tightened per the table above.

Net D2 scope after the check-in: **D2b only** (`_run_kb_mapping`) plus
**D2c Issue 1** (3 cases tightened, 1 kept lenient by design).
`_run_entity_resolution` and `_run_derivation` cross_source seeding are
untouched. D1 is unaffected.
