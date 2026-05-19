# Phase D — Report

The last polish session on Aedos v0.15 before Phase 10.5 calibration. Phase D
fixes the **measurement instrument** — the per-corpus runner code in
`tests/calibration/test_corpus_runner.py` — not the verification pipeline. The
extractor, walker, oracles, KB verifier, consistency check and retraction
propagation are unchanged; the audit chain (audit → … → Phase C) already
verified they produce correct verdicts. Phase D verifies that the runners that
score those components against the 11 calibration corpora can actually score
every case.

Three cluster commits, run D1 → D2 → D3:

```
07d764f  Cluster D1: hard-block runner fixes for extraction + temporal_scope
cbdd331  Cluster D2: soft mis-scoring fixes for kb_mapping + derivation
<D3>     Cluster D3: dry-run verified clean; Phase D report; rc.5 tag
```

The plan is `docs/phase_D_plan.md` (committed with D1). Start point
`v0.15.0-rc.4`; the inventory of the 11 runner/corpus pairs identified two hard
blockers and three soft mis-scoring runners.

---

## A finding that reshaped the verification methodology

The session inputs treated the dry-run (`pytest --run-calibration` without
`RUN_CALIBRATION=1`) as the discriminating mechanism — "confirm 42 extraction
KeyErrors pre-fix, all cases harness-validated post-fix."

**The dry-run does not invoke runners at all.** `test_corpus_calibration` loads
the corpus, asserts it is non-empty, and `pytest.skip()`s *before*
`runner = _RUNNERS[corpus]` — that is exactly why it has no LLM/KB cost. So:

- The dry-run output is **720 passed, 12 skipped, 0 errors** before and after
  every Phase D fix. It cannot distinguish pre-fix from post-fix.
- The `KeyError` hard blockers (42/57 extraction cases, 5/40 temporal_scope
  cases) only surface under live evaluation (`RUN_CALIBRATION=1`), where the
  per-case `except Exception` catches them and counts them as failures.
- **A corpus whose runner KeyErrors on 74% of its cases passes the dry-run
  green.** That is the audit-chain gap (see below) in its sharpest form.

Phase D therefore verified each fix by a **static key-access / comparison
cross-check** against the corpus schema (documented per sub-category below),
not by watching the dry-run change. The dry-run was still run at D3 to confirm
the corpora load and parse; `pytest tests/ -q` to confirm the suite is
unaffected. The harness was **not** modified to make the dry-run exercise
runners during the rc.5 clusters — out of scope then; folded into the D24
process-delta. *(It was modified immediately afterward — see the Phase D
follow-up section below, which landed that fix and tagged `v0.15.0-rc.6`.)*

---

## Cluster D1 — hard blockers

### D1a — `_run_extraction` per-category dispatch (`07d764f`)

`extraction_corpus.jsonl`: 57 cases across 5 categories — `normalization` 15,
`temporal` 15, `decomposition` 10, `first_person` 10, `hard_claim` 7.

**Before:** the runner read `case["input"]` and `case["expected_predicate"]`
unconditionally. Only `normalization` cases carry `expected_predicate`; the 7
`hard_claim` cases carry `text` not `input`. **42/57 cases raised `KeyError`**,
were caught by the test wrapper's broad `except`, and counted as failures —
max achievable accuracy 26.3%, hard-blocked below the 90% threshold.

**Now:** dispatch on `case["category"]`, one branch per sub-category. What each
branch tests:

| sub-category | n | tests |
|---|---|---|
| normalization | 15 | a produced claim's `predicate` equals `expected_predicate` |
| temporal | 15 | `is_future` cases (×2) → extractor drops the claim; else `claims[0]` matches `expected_scope` on `valid_from`, `valid_until` **and** `valid_during_ref` |
| decomposition | 10 | `len(claims) == expected_claim_count` (9 cases); decomp_008 → all claims share one non-`None` `reified_event_id` |
| first_person | 10 | extracted with the case's own `asserting_party`; a produced claim's `subject` equals `expected_subject` |
| hard_claim | 7 | extracted from `case["text"]`; `expected_subjects_in_output` all present, `expected_subjects_not_in_output` all absent; hardclaim_006 → a claim's `source_text` equals `source_text_check` |

Sub-category decisions:

- **temporal** compares all three scope fields, not the two
  (`valid_from`/`valid_until`) that `_run_temporal_scope` checks — a new branch
  should test what its corpus pins, and temporal_005/_011/_015 pin
  `valid_during_ref`.
- **first_person** must build `ExtractionContext` with the *case's*
  `asserting_party` (not the default `"calibration"`) so `_canonicalize` maps
  `I/me/my/we` to it — firstperson_001 expects `user_test`.
- **hard_claim** uses `asserting_party="user_test"` because hardclaim_002's
  `text` is first-person ("I graduated …") and expects subject `user_test`.
  hardclaim_007's `expected_subjects_in_output` is `[]` with no
  `not_in_output`; the branch passes it vacuously — the runner compares only
  what the corpus pins.

### D1b — `_run_temporal_scope` key-name handling (`07d764f`)

`temporal_scope_corpus.jsonl`: 40 cases — `explicit_scope` 10, `implicit_past`
10, `relative_scope` 10, `no_markers` 5, `future_rejection` 5.

**Before:** the 5 `future_rejection` cases store their expected answer under
`expected` (the bare string `"rejected"`), not `expected_scope`.
`case["expected_scope"]` raised `KeyError` on those 5 → accuracy capped at
87.5%, one sub-category short of the 90% threshold. The empty-claims branch
also read `expected.get("rejected")` — a key no case carries — and would have
raised `AttributeError` on the future cases even past the `KeyError`, since
their `expected` is a string.

**Now:** sub-category dispatch (Option 2 — consistent with D1a). A
`future_rejection` branch: future-tense claims are dropped at extraction, so
the case passes iff `not claims`. It reads neither `expected_scope` nor
`expected`. The non-future path keeps the existing
`valid_from`/`valid_until` comparison and its empty-claims case returns
`False`; the wrong-key `expected.get("rejected")` line is gone.

**Recorded, not fixed (v0.16):** the non-future `_run_temporal_scope`
comparison checks only `valid_from`/`valid_until`, so the 10 `relative_scope`
cases pass without their `valid_during_ref` / `valid_from_ref` /
`valid_until_ref` expectation being checked — a soft over-leniency. Not in the
inventory's fix list (`Claim` also lacks `valid_from_ref`/`valid_until_ref`
fields — a `Claim`-schema change, genuinely v0.16). Per "fix exactly what the
inventory identified," recorded here, not changed.

---

## Cluster D2 — soft mis-scoring

A check-in (`docs/phase_D_plan.md`, Q1–Q3) was raised before implementing D2:
reading the corpora showed two of the inventory's three D2 observations did not
hold against the actual corpus files.

### D2a — `_run_entity_resolution`: **no change** (check-in Q1)

The inventory said all 15 `ambiguous` cases carry `top_kb_identifier`, making
the lenient `return True` branch dead code. **Against
`entity_resolution_corpus.jsonl` only 5 of 15 carry `top_kb_identifier`**
(er_ambiguous_001/_002/_006/_007/_013 — the cases the corpus author gave a
pinned answer, e.g. er_ambiguous_001 → `Q90`). The other 10 are
`disambiguation_key`-only and the lenient branch **already fires** for them.
The runner is therefore not mis-scoring: it strict-checks the 5 decidable
ambiguous cases and lenient-passes the 10 genuinely-undecidable ones. Applying
the inventory's reorder would have *loosened* scoring on the 5 — the "too
lenient" defect D2 is meant to remove. **Resolution: leave the runner
unchanged.** (`disambiguation_key` is also a free-text hint string, not a
machine-checkable candidate set.)

One unrelated soft-leniency was noticed and recorded for v0.16, not changed:
er_no_match_002's `result: "ambiguous_or_no_dominant"` falls through to
`return True` — a single case, outside the inventory.

### D2b — `_run_kb_mapping` slot_to_qualifier comparison (`cbdd331`)

`kb_mapping_corpus.jsonl`: 40 cases — `kb_resolvable` 30, `qualifier_mapping`
10.

**Before:** the runner compared only `meta.kb_property`. The 10
`qualifier_mapping` cases exist specifically to test `slot_to_qualifier`
handling — they passed on half their criterion; the qualifier dimension was
untested.

**Now:** for `category == "qualifier_mapping"` the runner also compares
`slot_to_qualifier`. **Subtlety:** `PredicateTranslation._generate_and_store`
stores a falsy (empty) `slot_to_qualifier` dict as SQL `NULL`, which loads back
as `None`. Two cases (kb_map_qualifier_006/_010) expect `slot_to_qualifier:
{}`. The comparison normalizes both sides — `(meta.slot_to_qualifier or {}) ==
(expected.get("slot_to_qualifier") or {})` — so `None` and `{}` compare equal.
The 30 `kb_resolvable` cases keep the `kb_property`-only check. This is a
capability addition: the runner now tests something it did not before.

### D2c — `_run_derivation` Issue 1: lenient auto-pass (`cbdd331`)

`derivation_corpus.jsonl`: 50 cases — `multi_hop_distribution` 12,
`cross_source` 10, `entity_disambiguation` 8, `predicate_translation` 8,
`belief_revision` 6, `abstention` 6.

**Before:** a blanket `return True` auto-passed any case whose
`expected_output.verdict` was not in `{verified, contradicted,
no_grounding_found}` — 4 of 50 cases scored correct regardless of walker
output.

**Now:** the 4 cases get specified expected behavior:

| case | corpus `expected_output` | runner now tests |
|---|---|---|
| der_disambiguation_001 | `verdict: "verified_with_correct_entity"` | `result.verdict == "verified"` **and** `Q76` present as a `kb_statement` entity in the walk trace |
| der_disambiguation_006 | `verdict: "verified_with_correct_entity"` | `verdict == "verified"` **and** `Q3783` in the trace |
| der_predicate_translation_007 | `verdict: "needs_tier_u_or_kb"` | `verdict == "no_grounding_found"` — the runner seeds no Tier U and the subject has no KB statement, so with neither premise the walker abstains |
| der_disambiguation_005 | *no `verdict` key* (note: "may abstain or need context") | **kept an explicit auto-pass** — check-in Q3 |

The intended Q-number for the two `verified_with_correct_entity` cases lives in
the case's free-text `disambiguation_note`, so it is pinned per case id in the
runner (`{der_disambiguation_001: Q76, der_disambiguation_006: Q3783}`) rather
than parsed from prose. The trace check reads `edge.target.content["entity"]`
on `kb_statement` trace nodes. der_disambiguation_005 is the one case the
corpus author deliberately left unpinned (no `verdict` key); per Q3 it stays a
documented auto-pass — silently failing an unpinned case would be as wrong as
silently passing it.

### D2c — Issue 2 (cross_source seeding): **skipped** (check-in Q2)

The inventory said `cross_source` cases carry `kb_claim`/`python_claim` "seed
data" the runner fails to seed into the substrate. **The premise does not hold
the way `tier_u` seeding does.** `tier_u` and `context_premises` seed *local*
tables (Tier U, `subsumption`). A `kb_claim` (`{entity: "Q49112", property:
"P131", value: …}`) is **not** seed input — the harness KB is the live
`WikidataAdapter`, and live Wikidata already holds the statement; the field
*documents* it. `python_claim` (`{code: "assert 4 < 7"}`) is likewise
descriptive — `PythonVerifier` generates its own code. There is no local
KB/Python substrate to seed. The only genuine seed input for `cross_source` is
`tier_u`, already seeded. **Resolution: skip Issue 2.** (der_cross_007 — a Tier
U value feeding a Python computation — will abstain in Phase 10.5, but that is
**D10**, a known deferred walker limitation, not a runner bug the runner can
fix.)

---

## Static verification (substitute for stash-and-verify)

Because the dry-run does not invoke runners (see the methodology finding), each
fix was verified by a static key-access cross-check: for every sub-category,
every `case[...]` key the runner branch reads is carried by every case in that
sub-category, and the comparison tests the field the corpus pins.

**D1a — `_run_extraction`.** Category counts confirmed
(`normalization` 15, `temporal` 15, `decomposition` 10, `first_person` 10,
`hard_claim` 7 = 57). Per branch: `normalization` reads `input` +
`expected_predicate` (all 15 carry both); `temporal` reads `input` +
`expected_scope` (all 15); `decomposition` reads `input`, guards
`expected_claim_count` with `in` and `expected_shared_event_id` with `.get`
(9 + 1 split, decomp_008 the lone shared-id case); `first_person` reads
`input` + `asserting_party` + `expected_subject` (all 10); `hard_claim` reads
`text` (all 7), `.get`s the two `expected_subjects_*` lists, guards
`source_text_check` with `in` (hardclaim_006 only). No branch reads a key its
sub-category lacks. Pre-fix, the single path read `expected_predicate` (absent
on 42 cases) and `input` (absent on 7) → confirmed KeyError surface of 42
cases.

**D1b — `_run_temporal_scope`.** `future_rejection` branch (5 cases) reads
neither `expected_scope` nor `expected`; non-future branch reads
`expected_scope` (carried by all 35 non-future cases). Pre-fix the single path
read `expected_scope`, absent on the 5 `future_rejection` cases → confirmed
KeyError surface of 5 cases.

**D2b — `_run_kb_mapping`.** `kb_property` read for all 40; `slot_to_qualifier`
compared only for the 10 `qualifier_mapping` cases, all of which carry it. The
`None`/`{}` normalization traced against kb_map_qualifier_006/_010.

**D2c — `_run_derivation`.** All 50 derivation `expected_output.verdict` values
enumerated: 46 standard, 4 non-standard (der_disambiguation_001/_005/_006,
der_predicate_translation_007) — matches the inventory exactly. Each of the 4
now hits a specified branch (`id` is read for the two trace-checked cases; all
50 cases carry `id`).

**Dry-run invariance.** `pytest --run-calibration -q` is `720 passed, 12
skipped, 0 errors` at `v0.15.0-rc.4` (per `docs/phase_C_report.md`) and
identical after D1+D2 — the dry-run cannot distinguish the states, confirming
the methodology finding rather than the fixes. `pytest tests/ -q` is `720
passed, 1 skipped, 11 deselected` before and after — the runner fixes add and
remove no tests and touch no other file.

---

## What this reveals about the audit chain

The verification pipeline was audited across ten rounds (audit → fixup-1 →
reaudit → fixup-2 → reaudit2 → fixup-3 → reaudit3 → Phase A → B → C). The
calibration runner — the code that *scores* that pipeline — was treated as test
infrastructure and trusted by association. It was never audited, and it was
partial: two of eleven runners hard-`KeyError`'d on the majority of their
corpus (`extraction` 42/57, `temporal_scope` 5/40), and three more mis-scored
softly (the inventory's framing; on inspection one of those three —
`entity_resolution` — was in fact scoring correctly, and a fourth issue, the
`cross_source` "seed gap", dissolved on inspection too).

The sharpest part: the **dry-run, the one cheap pre-release check, could not
see the hard blockers** — it skips before invoking runners. A corpus whose
runner KeyErrors on 74% of its cases passed the dry-run green. The hard
blockers would have surfaced only in Phase 10.5's live Step 4, as inexplicably
low accuracy on two corpora — and might have been read as a *system* failure
rather than a *measurement* failure.

This is the v0.16 process-delta **D24**: a build's measurement instrument needs
the same "audit it before you trust it" discipline as its production code.

---

## v0.16 planning updates

- **D24** added to `docs/v0.16_planning.md` (new "From Phase D" section): make
  "the runner can score every case of its corpus" a standard pre-release gate
  — either the dry-run invokes each runner's case-reading path against a stub
  harness (no LLM/KB cost), or a static runner-vs-corpus key audit runs in CI.
  D24 also records the two soft observations Phase D surfaced but did not fix
  (the `_run_temporal_scope` `relative_scope` leniency; er_no_match_002's
  `ambiguous_or_no_dominant` leniency).
- **D21 / D22** confirmed **Resolved (Phase C)** — no Phase D change; the
  planning doc already records them resolved.
- Deferred deltas **D5, D9, D10, D13, D14, D15, D23** remain deferred — none
  pulled forward. D10 in particular bounds der_cross_007's expected Phase 10.5
  behavior (see D2c Issue 2).

---

## Tests

| | passed | skipped (dry-run) |
|--|--------|--------|
| `v0.15.0-rc.4` baseline | 720 | 12 |
| after Phase D | 720 | 12 |

Phase D adds and removes no tests — the 11 calibration-corpus tests are
unchanged in number; D1/D2 change only what 3 of the runner functions they
call do internally. `pytest tests/ -q`: 720 passed, 1 skipped, 11 deselected.
`pytest --run-calibration -q`: 720 passed, 12 skipped, 0 errors.

---

## Tag and Phase 10.5 start point

The final commit is tagged **`v0.15.0-rc.5`**. Phase 10.5 begins from rc.5.
Phase D changes only calibration-runner (scoring) code — no verdict-producing
code — so the fallback start point for a Phase 10.5 calibration anomaly is
unchanged: **`v0.15.0-rc.2`** if an anomaly traces past D16/D6.

After Phase D, both halves of the Phase 10.5 precondition hold: the system
produces correct verdicts (the audit chain), and every runner can score every
case of its corpus (Phase D). The Phase 10.5 numbers will mean what they say.

---

# Phase D follow-up (`fixup-1`) — the dry-run now invokes runners

Phase D proper (rc.5) verified the runner fixes by static cross-check because
the dry-run could not exercise runners. This follow-up fixes that: it makes the
dry-run a real gate, so a broken runner is caught for free instead of
discovered only under a paid live run. One commit, `v0.15.0-rc.5` →
`v0.15.0-rc.6`.

## The dry-run now invokes every runner against every case

`test_corpus_calibration`'s `not RUN_CALIBRATION` branch no longer skips after
merely loading the corpus. It now invokes the corpus's runner against every
case through a **stubbed harness** and fails if any case raises a structural
exception:

- **`_StubLLM`** — returns structurally-valid responses so the real
  (lightweight) `Extractor` and `PythonVerifier` run to completion: for the
  extraction tool, one claim whose subject is the input text verbatim (so it
  survives the extractor's hard-claim check and the runner sees a non-empty
  claim list); for the python tool, empty code (so `PythonVerifier` returns
  `no_terminal_result` without touching the sandbox).
- **`_Stub`** — a universal structural stub (attribute access and calls return
  another `_Stub`, iteration yields nothing) standing in for the heavy
  components: predicate translation, resolver, substrate, walker, Tier U, KB.
  Every chained access a runner makes resolves without raising.
- **`_DryRunHarness`** — `_Harness` with the stub LLM and stub components, but
  the **real in-memory DB** (cheap; `_run_consistency_check` and
  `_run_derivation` need a real schema). No LLM or KB call is made.

The component outputs are deliberately uncalibrated and unused — the dry-run
checks only that each runner *completes* (no `KeyError` / `AttributeError` /
…). A clean corpus still `skip`s (no real evaluation ran), but the skip is now
earned by invoking the runner, not by parsing JSON. A corpus with a structural
error **fails** the dry-run, with the count and first offenders in the message.
This is option (a) of the D24 process-delta — landed now, not deferred.

## It immediately found a third broken runner

The Phase D inventory classed `consistency_check` a clean runner. The enhanced
dry-run failed it: **3/25 cases raised structural errors.**

- **cc_conflict_008, cc_conflict_009** — `KeyError: 'aedos_predicate'`.
  `_run_consistency_check` hard-coded a `predicate_translation` INSERT and read
  `row["aedos_predicate"]` for *every* `seeded_conflict_detection` case,
  ignoring `input.table`. cc_conflict_008's table is `predicate_distribution`,
  cc_conflict_009's is `subsumption`; those rows carry no `aedos_predicate`.
- **cc_conflict_006** — `IntegrityError: UNIQUE constraint failed`. The runner
  inserted with a plain `INSERT` into a DB shared across all cases of the
  corpus; `holds_role` was already inserted by cc_conflict_001. This collides
  in **live mode too** (live shares one harness/DB per corpus) — a real runner
  bug, latent only because live calibration had never been run.

This is the failure mode the original Phase D prompt named — "one runner fix
surfaces another runner's latent issue not in the inventory." Fixing it was
authorized as an explicit scope expansion of this follow-up.

## The `_run_consistency_check` fix

The runner was rewritten (per the corpus's 10 `seeded_conflict_detection`
cases — 8 `predicate_translation`, 1 `predicate_distribution`, 1 `subsumption`):

- **Fresh in-memory DB per case.** A consistency case is a self-contained
  two-row scenario; a shared DB both collides on the `predicate_translation`
  UNIQUE key *and* cross-contaminates checks keyed on `(kb_namespace,
  kb_property)` (cc_conflict_001 and _006 both use P39). `_run_consistency_check`
  no longer uses the harness DB at all.
- **Table-type dispatch.** `_insert_consistency_row` inserts into the table
  named by `input.table` with that table's column schema (`subsumption`'s
  `entity_a` "ns:id" string is split into namespace/identifier).
- **Synthetic `ConsistencyResult` for subsumption/predicate_distribution.** A
  conflicting second row for these tables shares the table's UNIQUE key, so the
  write is rejected by the constraint — that rejection *is* the consistency
  enforcement for these tables (the `_check_*_row` logic is a defensive second
  line that the constraint makes unreachable in practice). The runner inserts
  row_a, attempts row_b; on `IntegrityError` with differing verdicts it
  synthesizes `ConsistencyResult(status="conflict", inconsistency_class=…)`
  (`contradicting_subsumption` / `conflicting_distribution`). If the second row
  instead coexists (a distinct key), the checker scores it normally.

Post-fix, `_run_consistency_check` scores all 10 seeded cases without a
structural error, and **9 of 10 correctly**. cc_conflict_007 scores as a
failure — and that is correct runner behavior, not a bug: the corpus expects
*no* conflict for `works_at` / `employed_by` on P108 with `null` vs
`{start: P580}` slot_to_qualifier, but the checker's
`transitive_equivalence_violation` rule flags any differing map, and
cc_conflict_002 (`null` vs `{degree: P512}` on P69) is structurally identical
yet expects a conflict. This is a **checker-vs-corpus discrepancy** recorded
for v0.16 / Phase 10.5 triage (v0.16_planning.md D24, observation 3), not a
runner defect. The 15 non-`seeded_conflict_detection` cases remain
`return True` (unscored) — recorded as the same observation; scoring them is a
runner-capability addition, out of this follow-up's authorized scope.

## Verification — pre-fix and post-fix

The enhanced dry-run is the discriminator the rc.5 session lacked:

- **Post-fix** (`pytest --run-calibration -q` at rc.6): **720 passed, 12
  skipped, 0 errors** — all 11 runners invoke every case cleanly.
- **Pre-fix** — the rc.6 commit cherry-picked onto `v0.15.0-rc.4` (pre-D1/D2
  runners + the enhanced dry-run): `extraction_corpus` fails with **42/57**
  cases raising structural errors, `temporal_scope_corpus` with **5/40** — the
  exact hard-blocker shape Phase D's D1 fixed. `consistency_check` is clean
  there (the rc.6 commit also carries the `_run_consistency_check` fix).

`pytest tests/ -q` (the default, non-calibration run) is unchanged — **720
passed, 1 skipped, 11 deselected** — the calibration test is deselected without
`--run-calibration`, and the dry-run change adds no test.

## Tag and Phase 10.5 start point

The follow-up commit is tagged **`v0.15.0-rc.6`**. Phase 10.5 begins from rc.6,
where the dry-run reports clean *because every runner was invoked against every
case*, not because it skipped. The follow-up changes only calibration-runner
and harness (scoring) code — no verdict-producing code — so the fallback start
point for a Phase 10.5 calibration anomaly is unchanged: **`v0.15.0-rc.2`**.
