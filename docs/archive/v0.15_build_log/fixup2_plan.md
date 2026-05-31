# Aedos v0.15 — Second Post-Re-Audit Fix-Up Plan (fix-up 2)

*Concrete approach to the re-audit findings in `docs/v0_15/reaudit_report.md`.
Written before any code is touched. Ambiguities surfaced in writing below.*

Baseline confirmed: `py -m pytest tests/v0_15/ -q` → **664 passed, 1 skipped,
11 deselected** at `v0.15-phase-10-complete-fixup-1` (`e6c0492`).

Scope: re-audit findings M4 (partial), M5 (Step 6), N1, N3, N5, N6, N7, and the
N2 test-honesty rewrite. Out of scope and recorded as v0.16 deltas: M2 cascade
(D14), N4 (D13), `ContradictionTracer` pipeline integration (D15), KB-sourced
neighbor enumeration (D5). Four fix commits (A, B, N2, C) plus a report commit;
tag `v0.15-phase-10-complete-fixup-2` on the final commit.

---

## Cluster A — M4 seed backfill + N1 (coupled, one commit)

The two fixes ship together: backfilling seeds without N1 converts an inert-but-
correct mechanism into an active-and-wrong one; N1 without the backfill is inert.

**Step 1 — N1 in `kb_verifier.py`.** `_compare_positive` gains an
`object_resolved` parameter and returns a 3-tuple `(verdict, statement,
abstention_reason)`. When `meta.object_type == "entity"` and `object_resolved is
False`, the functional-predicate `CONTRADICTED` branch is suppressed and the
verdict is `NO_MATCH` — a comparison against an unresolved natural-language
reference is not evidence of falsity (architecture §3.2: resolution failure is a
false-abstain source). The VERIFIED path (literal match) is untouched. The trace
records `abstention_reason`: `"no_statements"` (no KB statements),
`"object_unresolved"` (entity object did not resolve), `"no_matching_statement"`
(statements existed, none matched), `"subject_unresolved"` (subject did not
resolve — added in passing, one key, aids Phase 10.5 abstention debugging).

**Step 2 — backfill `seeds/v0_15/predicate_translation.json`.** Add a
`single_valued` field to all 61 entries. Functional classification below.

**Step 3 — `load_seeds.py`.** Add `single_valued` to `_REQUIRED_FIELDS` and the
INSERT column list so loaded rows carry the value, not the column default.

**Step 4 — metadata (closes N3).** `SEED_VERSION.txt` `entry_count: 65 → 61`;
runbook Step 2 `Loaded 65 → 61` and `65 seeds loaded → 61 seeds loaded`.

### Functional-predicate classification (semantic decisions)

The re-audit named 13 functional predicates. After reading each seed entry's
actual `kb_property` and `slot_to_qualifier`, the functional set (`single_valued
= 1`) is **11**:

`born_in`, `died_in`, `born_on`, `died_on`, `capital_of`, `has_capital`,
`continent_of`, `founded_in_year`, `head_of_government`, `head_of_state`,
`gender`.

Two deviations from the re-audit's list, both toward multi-valued (the
conservative direction — a wrong `single_valued=1` causes false *contradictions*;
a wrong `0` only causes false *abstains*, the accepted §3.2 cost):

- **`country_of` → 0 (multi-valued).** The task explicitly reclassifies it:
  citizenship can be plural (dual citizens). The seed maps it to P17 ("country",
  functional for *places*), but the Aedos predicate name is not subject-type-
  constrained — a citizenship claim could route here — so the §3.2-conservative
  choice for an ambiguous-cardinality predicate is 0.
- **`mother_of` → 0 (multi-valued).** The re-audit listed it functional, reading
  it as "a person's mother." But the seed's `slot_to_qualifier` is
  `{"subject":"statement_value","object":"statement_subject"}` — inverted, so the
  Aedos *subject* is the mother (parallel to the seed's `parent_of(parent,
  child)`). A mother has many children → multi-valued. This is a re-audit
  glance-level misread corrected by reading the seed; the conservative choice is
  also 0, so soundness is unaffected either way.

`head_of_state`/`head_of_government` are functional per the task's reasoning (one
at a time; the LLM-generated temporal qualifiers carry the over-time
multi-valuedness). `continent_of` keeps the re-audit's functional call (a place
is on one continent in intended use; transcontinental edge cases noted).
`member_of`, `is_a`/`instance_of`, `occupation` are multi-valued per the task and
the generation prompt's own examples.

### Tests (Cluster A)

- `test_kb_verifier.py`: N1-alone (functional entity predicate, unresolvable
  object → `NO_MATCH` + `abstention_reason == "object_unresolved"`); the
  resolved-mismatch contradiction is already covered by
  `test_single_valued_mismatch_is_contradicted` (object resolves) — the two form
  the resolved→`CONTRADICTED` vs unresolved→`NO_MATCH` contrast. Trace
  `no_statements` reason assertion added.
- `test_seed_loader.py`: every entry has `single_valued`; the 11 functional
  predicates load with `single_valued=1` and the rest with `0` against a clean DB.
- New `tests/v0_15/integration/test_seed_single_valued_kb.py`: **Test 3**
  (seeded `born_in`, "Obama born in Chicago" vs KB Honolulu → `CONTRADICTED` —
  would be `NO_MATCH` pre-backfill) and **Test 4, the load-bearing coupling test**
  (seeded `born_in`, "Obama born in Foobar" unresolvable → `NO_MATCH` +
  `object_unresolved`).

**Stash-and-verify (the coupling).** Test 4 is verified against the intermediate
state — seeds backfilled (Steps 2+3) but `kb_verifier.py` reverted (Step 1
absent): it returns `CONTRADICTED` there (the false-contradiction the coupling
prevents) and `NO_MATCH` post-Cluster-A. Done by stashing `kb_verifier.py` alone.

Commit: `fixup-2 A: M4 seed backfill + N1 resolution-failure abstain (coupled)`

---

## Cluster B — M5 Step 6 benchmark runner (one commit)

**Step 1 — extract shared pipeline wiring.** New `src/aedos_v0_15/pipeline.py`
with `build_pipeline(db, llm_client=None, kb=None) -> Pipeline` (a dataclass
bundling every component). `app.py`'s `/chat` is refactored to call it so app and
benchmark share one wiring definition. `build_pipeline` wires the resolver with
`llm_client` (the complete wiring the calibration runner uses; app's inline
version omitted it — a strict improvement, documented).

**Step 2 — fix stale runner signatures (`benchmark.py`).** `AedosRunner.run_case`
calls `extractor.extract(stmt, ExtractionContext(...))` and `walker.walk(claim,
VerificationContext(...))`. `BaselineRunner.run_case`'s `chat([...])` is corrected
to `chat(system=..., messages=[ChatMessage(...)])`.

**Step 3 — implement live `__main__`.** Parse `--test-set`/`--output`/
`--baseline-only`/`--aedos-only`/`--validate-harness`; require `RUN_LIVE_TESTS=1`
+ `RUN_LIVE_KB=1` (clear error, no silent mock fallback); open `AEDOS_DB_PATH`;
`build_pipeline`; run `BaselineRunner` and `AedosRunner`; `generate_report` to
`--output`.

**Step 4 — `--validate-harness` mode + `_validate_harness()`.** Builds the
production pipeline against mock LLM/KB, runs one case through each runner,
confirms no `error` verdict and a non-empty markdown report. Runs in the default
mocked suite via a new test in `test_benchmark_structural.py`.

**Step 5 — runbook Step 6.** Confirm command/expected-output/thresholds match the
implemented runner; `generate_report` emits all four acceptance criteria the
runbook lists.

**Tests.** `test_benchmark_structural.py`: `--validate-harness` test; a
synthetic-mix metrics test (false-verified + false-abstain counts).
**Stash-and-verify:** reverting `AedosRunner.run_case`'s signatures makes the
harness-validation test fail (runner reports `error`); restoring it passes.

Commit: `fixup-2 B: medium-bar benchmark runner (M5 Step 6)`

---

## N2 — honest cross-source test (own commit)

`test_walker_failure_modes.py`: rename `test_cross_source_tier_u_and_kb` →
`test_cross_source_independent_walks`; rewrite the docstring to state the test
does **not** exercise cross-source unification (it runs two independent
single-source walks), and that genuine unification needs KB-sourced neighbor
enumeration (v0.16 D5). Test body unchanged — it is a legitimate test of two
independent walks, now honestly labelled. No capability is implemented.

Commit: `fixup-2: rewrite degenerate cross_source test to honest scope (N2)`

---

## Cluster C — opportunistic cleanup (one commit)

- **N3** — closed in Cluster A Step 4 (marked Fixed-in-passing).
- **N5** — `consistency.py`: a new `_is_inverse_mapping` helper makes
  `_check_predicate_translation_row` direction-aware. Two predicates on the same
  KB property whose `slot_to_qualifier` maps are exact subject/object inversions
  (all other keys equal) are compatible inverses, not a conflict; any other
  divergence remains a conflict. Tests: seed `capital_of`+`has_capital`, run
  `check_periodic`, expect no conflict; a non-inverse divergence still conflicts.
- **N6** — `database.py` `create_schema`: idempotent `ALTER TABLE ... ADD COLUMN
  single_valued` guarded by `except sqlite3.OperationalError`. Test: a pre-fixup
  DB lacking the column gains it (rows default 0) after schema setup.
- **N7** — `THRESHOLDS` in `test_corpus_runner.py` becomes the documented single
  source of truth; a machine-readable threshold table is added to runbook Step 4;
  a new doc-test `tests/v0_15/unit/test_runbook_thresholds.py` (runs in `make
  test`, unmarked) parses the table and asserts it matches `THRESHOLDS`.

**Stash-and-verify:** the N6 migration test fails without the `ALTER TABLE`
guard; the N5 inverse-predicate test reports a conflict without the
direction-awareness fix; the N7 doc-test fails if the table is perturbed.

Commit: `fixup-2 C: opportunistic cleanup (N3, N5, N6, N7)`

---

## Out of scope (v0.16 deltas)

M2 cascade (D14), N4 KB-grounded retraction visibility (D13),
`ContradictionTracer` pipeline integration (D15), object-conflict belief revision
(D16), KB-sourced neighbor enumeration (D5). The re-audit's recommended D12–D17
will be transcribed into `v0_16_plan_deltas.md` (currently only D1–D11 are
physically present); D4 updated with the functional-predicate classification;
D12 updated to note N5's code fix landed and only the §5.4 wording revision
remains; D17 updated to note N6/N7 landed.

## Architecture

Not modified this session. Cluster A's `single_valued + object_resolved`
abstention refinement is a small extension of §3.2's abstention policy, landed as
code with the v0.16 delta capturing the architectural language change. No
architectural revision is performed.
