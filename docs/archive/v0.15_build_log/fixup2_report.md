# Aedos v0.15 — Second Post-Re-Audit Fix-Up Report (fix-up 2)

*Addresses the findings in `docs/v0_15/reaudit_report.md`. Work landed as four
cluster commits on the `v0.15` branch after `v0.15-phase-10-complete-fixup-1`
(`e6c0492`), tagged `v0.15-phase-10-complete-fixup-2`.*

**Test state.** Baseline at `v0.15-phase-10-complete-fixup-1`: 664 passed, 1
skipped, 11 deselected. After fix-up 2: **687 passed, 1 skipped, 11 deselected**
(`pytest tests/v0_15/ -q`) — +23 tests, no failures, no new skips. The one skip
is the pre-existing `RUN_LIVE_TESTS`/`RUN_LIVE_KB`-gated cold-start test; the 11
deselected are the calibration corpus runner (collected only under
`--run-calibration`, where it still does an 11-corpus harness dry-run cleanly).

This fix-up resolves the two Phase-10.5 blockers the re-audit named (M4 partial,
M5 Step 6) and the four dormant minors (N3, N5, N6, N7), and rewrites the one
degenerate test (N2) to be honest about its scope. M2's cascade, N4, and the
`ContradictionTracer` pipeline integration are over-time-soundness gaps that do
not block Phase 10.5; they remain v0.16 deltas (D13–D15) per the task scope.

---

## Cluster summaries

### Cluster A — M4 seed backfill + N1 coupled fix — commit `61d4bde`

**Found.** The first fix-up added the `single_valued` column and the
`_compare_positive` guard correctly, but never backfilled the 61-entry seed
pack — so every seeded predicate loaded `single_valued = 0` and the KB verifier
could never emit `CONTRADICTED` for a functional predicate (M4 partial).
Separately, when an entity-valued single-valued predicate's object reference
*failed to resolve*, `_compare_positive` compared a natural-language string
against KB Q-numbers, never matched, and emitted `CONTRADICTED` — turning a
resolution failure into a confident false contradiction (N1). The two interact
destructively: backfilling without N1 makes N1 fire across every
`born_in`/`died_in`/`capital_of` claim with a hard-to-resolve place.

**Fixed.** Both ship in one commit. N1: `_compare_positive` now takes
`object_resolved` and, when `meta.object_type == "entity"` and the object did
not resolve, returns `NO_MATCH` instead of `CONTRADICTED` — architecture §3.2's
stance that resolution failure is a false-abstain source, never a
false-contradiction source. The trace records the abstention reason
(`object_unresolved`, `no_statements`, `no_matching_statement`,
`subject_unresolved`) so Phase 10.5 abstention debugging is tractable. M4:
`single_valued` is backfilled into all 61 seed entries (11 functional, 50
multi-valued — see *Cluster A semantic decisions* below), `load_seeds.py` lists
it in `_REQUIRED_FIELDS` and the INSERT column set, and `SEED_VERSION.txt` /
runbook Step 2 are corrected from 65 to 61 (closing N3 in passing).

**Tests.** `test_kb_verifier.py` +4 (`TestKBVerifierN1ResolutionFailure`);
`test_seed_loader.py` +5 (`TestSeedSingleValued` + a clean-DB-load check);
`test_seed_single_valued_kb.py` (new) +3 — the integration coupling tests that
load the real seed pack and run the KB verifier against it.

### Cluster B — medium-bar benchmark runner (M5 Step 6) — commit `ad7fe88`

**Found.** Runbook Step 6 presents `benchmark.py` as a runnable command, but its
live `__main__` printed `"Live evaluation not yet implemented"` and exited;
`AedosRunner.run_case` called `walker.walk(c)` with one argument (the API needs
`(claim, context)`) and `extractor.extract(stmt, context={})` (the API needs an
`ExtractionContext`); `BaselineRunner.run_case` called `chat([...])` against the
`chat(system, messages, …)` signature. Step 7 (`tag v0.15.0`) depends on the
`evaluation_results.md` Step 6 never produced.

**Fixed.** A new `src/aedos_v0_15/pipeline.py` exposes `build_pipeline(db,
llm_client=None, kb=None)`; `app.py`'s `/chat` and the benchmark both build
their pipeline through it, so the wiring has one definition. `AedosRunner` and
`BaselineRunner` signatures are aligned with the current APIs. `__main__` parses
`--test-set`/`--output`/`--baseline-only`/`--aedos-only`/`--validate-harness`,
requires `RUN_LIVE_TESTS=1` + `RUN_LIVE_KB=1` for the live path (clear error, no
silent mock fallback), builds the production pipeline, runs both runners, and
writes the four-criterion report. `--validate-harness` (and `_validate_harness`,
run by the mocked suite) builds the production pipeline against mocks and runs
one case through each runner — confirming the wiring with no live API. Runbook
Step 6 is updated to match the implemented runner.

**Tests.** `test_benchmark_structural.py` +3: harness-validation,
synthetic-mix metrics, and a four-criteria report-render check.

### N2 — honest cross-source test — commit `aafe3c2`

**Found.** `test_cross_source_tier_u_and_kb`, the designated coverage for §8.1
failure mode 2, ran two independent single-source walks and asserted each used a
different source — it never composed a Tier U premise with a KB statement in one
chain. The walker's subsumption traversal reads only substrate `subsumption`
rows; KB-sourced neighbor enumeration is a v0.16 capability gap (D5).

**Fixed (test honesty, not capability).** The test is renamed
`test_cross_source_independent_walks`; its docstring now states explicitly that
it does **not** exercise cross-source unification, that genuine unification
requires KB-sourced neighbor enumeration (D5), and that the medium-bar
`cross_source_unification` cases will fail in Phase 10.5 unless they pre-seed
substrate `subsumption` rows — a known capability gap, not a calibration issue.
The test body is unchanged: two independent single-source walks is a legitimate
thing to test, now labelled honestly. KB-sourced neighbor enumeration was **not**
implemented (out of scope; v0.16 D5).

### Cluster C — opportunistic cleanup (N3, N5, N6, N7) — commit `6a99207`

**Found / Fixed.**
- **N3** — closed in Cluster A Step 4 (`SEED_VERSION.txt` and runbook Step 2
  corrected to 61).
- **N5** — `consistency.py`'s `transitive_equivalence_violation` rule flagged
  the hand-curated inverse seeds `capital_of`/`has_capital` (both on P36, with
  deliberately inverted `slot_to_qualifier`) as a conflict; once the §5.4
  periodic scan is wired, retract-both would delete the correct seeds. The rule
  is now direction-aware: a new `_is_inverse_mapping` helper treats two maps
  that are exact subject/object inversions of each other (every other key
  identical) as compatible inverses; any other divergence on the same property
  remains a conflict.
- **N6** — `predicate_translation`'s `single_valued` column had no migration
  path: a pre-fixup DB silently lacked it. `create_schema` now runs an
  idempotent `ALTER TABLE … ADD COLUMN single_valued` guarded by
  `except sqlite3.OperationalError`.
- **N7** — calibration thresholds lived in three uncoordinated copies.
  `THRESHOLDS` in `test_corpus_runner.py` is now the documented single source of
  truth; a machine-readable threshold table was added to runbook Step 4; a new
  doc-test `test_runbook_thresholds.py` (unmarked — runs in `make test`) fails
  CI if the runbook table and `THRESHOLDS` diverge.

**Tests.** `test_consistency_checker.py` +3 (`TestInversePredicates`);
`test_database.py` +2 (`TestSingleValuedMigration`); `test_runbook_thresholds.py`
(new) +3.

---

## Finding-by-finding status

| Finding | Re-audit status | fixup-2 status | Where |
|---|---|---|---|
| M4 — seed pack not backfilled | Partially resolved | **Fixed** | Cluster A (`61d4bde`) |
| M5 — runbook Step 6 benchmark is a stub | Partially resolved | **Fixed** | Cluster B (`ad7fe88`) |
| N1 — false-contradiction on object-resolution failure | New — Major | **Fixed** | Cluster A (`61d4bde`) |
| N2 — degenerate `cross_source` test | New — Major | **Addressed** — test made honest; capability gap is D5 | N2 (`aafe3c2`) |
| N3 — seed count 61 vs 65 | New — Minor | **Fixed-in-passing** | Cluster A Step 4 (`61d4bde`) |
| N5 — consistency check flags inverse seeds | New — Minor | **Fixed** | Cluster C (`6a99207`) |
| N6 — no migration for `single_valued` | New — Minor | **Fixed** | Cluster C (`6a99207`) |
| N7 — thresholds duplicated across files | New — Minor | **Fixed** | Cluster C (`6a99207`) |
| M2 — retraction cascade absent; `ContradictionTracer` unwired | Partially resolved | **Deferred** — v0.16 D14, D15 (out of scope, does not block 10.5) | — |
| N4 — KB-grounded verdicts invisible to propagation | New — Minor | **Deferred** — v0.16 D13 (out of scope) | — |

**Re-audit findings already resolved by fix-up 1** (C1, C2, M1, M3, M6) were
verified by the re-audit and not re-touched. One re-audit *observation* —
`ConsistencyChecker.check_periodic` has no caller in `src/` — is not reopened
here; it was not a named finding. N5's direction-awareness fix does, however,
make the periodic scan safe to wire in v0.16 (it would otherwise retract the
`capital_of`/`has_capital` seeds on its first run).

No finding is `Blocked`. `docs/v0_15/fixup2_blockers.md` was not created — no
cluster's tests failed in a way that could not be resolved.

---

## Verification

The stash-and-verify discipline was followed for every cluster: each cluster's
new tests were confirmed to **fail against the pre-fix code** and **pass after**.

### Cluster A — the coupling (the load-bearing check)

The re-audit prompt named the intermediate state — seeds backfilled (Steps 2+3)
but `kb_verifier.py` at the fixup-1 revision (Step 1 absent) — as the
discriminating check. `git stash push -- src/aedos_v0_15/layer4_sources/kb_verifier.py`
produces exactly that state. Running the Cluster A tests against it:

- **5 failed** — the 4 `TestKBVerifierN1ResolutionFailure` unit tests and
  `test_seeded_born_in_unresolvable_place_abstains` (the integration coupling
  test, Test 4). Test 4 returns `contradicted` against the intermediate state —
  **the exact false contradiction the coupling exists to prevent** — and
  `no_match` after Cluster A. This is the demonstration that M4 and N1 had to
  ship together.
- **52 passed** — including `test_seeded_born_in_contradicts_wrong_resolved_place`
  (Test 3) and `test_seeded_born_in_is_single_valued`: both pass against the
  intermediate state because the seed backfill alone activates them. Test 3
  discriminates the backfill; Test 4 discriminates the N1 fix.

After `git stash pop`, all 57 Cluster A tests pass.

### Cluster B — the signature fix

The benchmark's `__main__` and `_validate_harness` are new code with no pre-fix
form to stash, so the discriminating check is the signature fix in
`AedosRunner.run_case`. Reverting `run_case`'s `extractor.extract` and
`walker.walk` calls to their stale forms (`extract(stmt, context={})`,
`walk(c)`) makes `run_case` catch the resulting exception and report
`verdict = "error"`; `_validate_harness` then fails its `assert a.verdict !=
"error"` and `test_validate_harness` fails (`AedosRunner errored — pipeline
wiring is broken`). Restoring the fixed signatures makes it pass. The
`--validate-harness` CLI prints `Harness validation: PASS`, and the live path
without `RUN_LIVE_TESTS`/`RUN_LIVE_KB` exits with the clear error message and no
mock fallback.

### N2

`test_cross_source_independent_walks` passes; the rename and docstring rewrite
do not change the test body, so there is no behavioral pre/post difference — the
change is test-honesty, verified by reading the new docstring against §8.1.

### Cluster C

`git stash push -- consistency.py database.py` reverts the N5 and N6 fixes;
running their test files against that state produces **3 failures**:
- `test_inverse_predicates_on_write_no_conflict` and
  `test_inverse_predicates_periodic_scan_no_conflict` — without
  `_is_inverse_mapping`, `capital_of`/`has_capital` are reported as a
  `transitive_equivalence_violation` conflict.
- `test_alter_table_adds_missing_column` — without the `ALTER TABLE` guard, a
  pre-fixup DB never gains the `single_valued` column.

`test_swapped_with_extra_divergence_still_conflicts` and
`test_create_schema_idempotent_on_fresh_db` pass both ways — boundary guards,
correctly documented as non-discriminating. For N7, perturbing one row of the
runbook threshold table (`extraction_corpus` 90% → 88%) makes
`test_runbook_thresholds_match_runner` fail with a clear divergence message;
restoring the table makes all three N7 tests pass.

---

## Cluster A semantic decisions — which predicates are functional

`single_valued = 1` licenses the KB verifier to emit `CONTRADICTED` from a value
mismatch. A wrong `1` produces false *contradictions* (the system "corrects" a
true claim); a wrong `0` produces only false *abstains* (the accepted §3.2
cost). The classification therefore biases toward `0` wherever cardinality is
genuinely ambiguous — consistent with the metadata-generation prompt's own rule
("When unsure, choose 0").

**Functional (`single_valued = 1`) — 11 predicates:**

| Predicate | KB property | Reasoning |
|---|---|---|
| `born_in` | P19 | A person has exactly one birthplace. |
| `died_in` | P20 | A person has exactly one place of death. |
| `born_on` | P569 | A person has exactly one birth date. |
| `died_on` | P570 | A person has exactly one death date. |
| `capital_of` | P36 | A country has one capital in intended use (inverse of `has_capital`). |
| `has_capital` | P36 | A country has one capital in intended use. |
| `continent_of` | P30 | A place is on one continent in intended use (re-audit's call; transcontinental edge cases noted). |
| `founded_in_year` | P571 | An entity has one inception date. |
| `head_of_government` | P6 | One at a time; the LLM-generated `valid_from`/`valid_until` qualifiers carry the over-time change. |
| `head_of_state` | P35 | One at a time; temporal qualifiers carry the over-time change. |
| `gender` | P21 | P21 carries a single sex-or-gender value per subject. |

**Two deviations from the re-audit's 13-predicate functional list**, both toward
multi-valued — surfaced here because they override a re-audit recommendation:

- **`country_of` → multi-valued (0).** The task's Cluster A guidance explicitly
  reclassifies it: country of citizenship can be plural (dual citizens). The
  seed maps `country_of` to P17 ("country"), which is functional for a *place*
  — but the Aedos predicate name is not subject-type-constrained, so a
  citizenship-style claim could route to it. Under §3.2 the conservative choice
  for an ambiguous-cardinality predicate is 0.
- **`mother_of` → multi-valued (0).** The re-audit listed it functional, reading
  it as "a person's mother." But the seed's `slot_to_qualifier` is
  `{"subject": "statement_value", "object": "statement_subject"}` — inverted, so
  the Aedos *subject* is the mother (parallel to the seed's `parent_of(parent,
  child)`). A mother has many children → multi-valued. This is a re-audit
  glance-level misread, corrected by reading the seed; the conservative choice
  is also 0, so soundness is unaffected either way.

The remaining 50 predicates are multi-valued. Predicates that are *arguably*
functional but were left at 0 as the conservative call, with the seed entry's
`reason` field recording it: `area_of`/`elevation_of` (land/total/water and
high/low/mean values recorded separately), `population_of`/`number_of_employees`/
`revenue` (many figures across reporting years), `published_in_year`/
`released_in_year`/`occurred_in` (editions and regional releases carry distinct
dates), `currency_of` (some countries have several legal-tender currencies),
`has_successor`/`has_predecessor` (branching sequences). None of these is in the
re-audit's functional list; the "rest are multi-valued unless clearly otherwise"
default applies, and false-abstain is cheaper than false-contradiction.

`lives_in` is `single_valued = 0` but the field is moot for it — its
`routing_hint` is `user_authoritative`, so the KB verifier returns `NO_KB_PATH`
before `single_valued` is ever consulted.

The architectural language change implied by Cluster A — that
`single_valued + object_resolved` together refine §3.2's abstention policy (a
functional-predicate mismatch is a contradiction only when the object resolved)
— is recorded as a v0.16 delta (D4) and was **not** written into the
architecture document this session.

---

## v0.16 deltas updated

`docs/v0_15/v0_16_plan_deltas.md` was updated: D4 gained the functional-predicate
classification; D12–D17 (identified by the re-audit but physically missing from
the file) were transcribed; D12 notes N5's code fix landed and only the §5.4
*wording* revision remains; D17 notes N6 and N7 are fully resolved by fix-up 2.
Two deltas were added for items noticed in passing and out of fixup-2's scope:
**D18** — the chat-wrapper's stale `extract` signature leaves the `/chat`
deployment verification-inert (does not block Phase 10.5; the deployment layer
is not part of the architecture); **D19** — the KB verifier ignores
`slot_to_qualifier`, so inverse predicates (`capital_of`, `mother_of`) look up
statements on the wrong entity and abstain.

---

## End state

M4 + N1 are coupled-resolved; M5 Step 6's benchmark runner is implemented and
harness-validatable; N3/N5/N6/N7 are fixed; N2's test is honest. The mocked
suite is clean (687 passed, 1 skipped, 11 deselected) with no new skips. Every
cluster's stash-and-verify confirms its tests genuinely fail against the pre-fix
state. Calibration failures from this point forward should be calibration
failures, not capability failures — with the documented exceptions of the
`cross_source_unification` cases (D5) and inverse-predicate KB claims (D19),
which are known capability gaps recorded for v0.16.
