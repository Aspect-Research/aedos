# Aedos v0.15 Second Re-Audit Report

*Re-audit of fix-up 3. Paper + code audit only; no live calls. Conducted against
`v0.15-phase-10-complete-fixup-3` (`d4a8024`), compared with
`v0.15-phase-10-complete-fixup-2` (`c4719ae`) and `v0.15-phase-10-complete-fixup-1`,
and the architecture (`aedos_v0_15_architecture_draft_2.md`).*

*Execution performed: the mocked suite (`pytest tests/v0_15/ -q`), the
calibration runner harness dry-run (`pytest --run-calibration`), the benchmark
harness validation (`benchmark.py --validate-harness`), a D19 stash-and-verify
reproduced from the fixup-2 tag, the M4+N1 coupling check reproduced from the
fixup-1 tag, and manual code-path walks of the inverse and standard KB paths. No
`RUN_CALIBRATION`, `RUN_LIVE_TESTS`, or `RUN_LIVE_KB`.*

## Summary

**The build is ready for Phase 10.5. There are no blockers.** D19 is genuinely
and verifiably resolved: the KB verifier now keys its lookup on the entity the
KB actually stores the statement on, the two inverse seed predicates
(`capital_of`, `mother_of`) produce real verdicts, and the stash-and-verify
reproduces exactly (8 fail / 1 pass against fixup-2 `kb_verifier.py`). D19 is
**sound** — I walked the failure modes and found no path to a false verified: a
misclassified lookup direction produces an abstention, never a wrong verdict.
D19 is also **load-bearing** for Phase 10.5 — the medium-bar set has three
`capital_of` cases (`ed_011`, `csu_006`, `csu_011`) and the derivation corpus
one (`der_disambiguation_002`) that abstain wholesale pre-D19. All fixup-2
carryover findings (M4+N1, M5 Step 6, N5, N6, N7) remain resolved. The re-audit
produced **three new Minor findings** (R1–R3) and **one Phase-10.5 watch-item**
(W1); none blocks calibration, and the most important — W1 — is an extraction
coupling Phase 10.5 should check, not a code defect.

---

## Section 2.1 — Fix-up-2 carryover verification

Each finding fix-up 2 claimed to resolve was re-checked at fixup-3, after
Cluster D19's rewrite of `kb_verifier.py`.

### M4 + N1 coupling — **still resolved**

Reproduced the intermediate-state check. `git checkout
v0.15-phase-10-complete-fixup-1 -- src/aedos_v0_15/layer4_sources/kb_verifier.py`
puts `kb_verifier.py` at the pre-N1 (and pre-D19) revision while the seed pack
stays backfilled — the exact intermediate state. Running
`test_seed_single_valued_kb.py` against it: **`test_seeded_born_in_unresolvable_place_abstains`
fails** (`assert CONTRADICTED == NO_MATCH`) — the false contradiction the
coupling exists to prevent — and the other two pass. The coupling discipline is
intact.

Then verified D19 did not weaken N1's standard path: with `kb_verifier.py`
restored to fixup-3 (confirmed by `git hash-object` matching
`HEAD:...kb_verifier.py`), the four `TestKBVerifierN1ResolutionFailure` tests
and all three `test_seed_single_valued_kb.py` tests pass (7/7). The N1
resolution-failure-abstain logic lives in `_compare_positive`, which `git diff
fixup-2..fixup-3` confirms is **byte-identical** — the D19 diff touches only
`verify()` and adds `_lookup_targets` (the sole `+def`; no helper definition was
removed or modified). N1 is unaffected.

### M5 Step 6 — benchmark runner — **still resolved**

`py -m tests.v0_15.evaluation.benchmark --validate-harness` prints `Harness
validation: PASS`. Read `AedosRunner.run_case` post-D19: it builds the pipeline
via `build_pipeline`, unpacks `(extractor, walker, aggregator)`, and calls
`walker.walk(c, vctx)`. D19 is contained to `kb_verifier.py`; the walker calls
`self._kb_verifier.verify(node, ...)` and reads `KBVerdict.verdict` and
`.subject_kb_id` — both signatures unchanged — so the D19 change does **not**
cascade into the runner's wiring. The harness runs one case through the pipeline
with no `error` verdict. Clean.

### N5 — inverse-predicate consistency — **still resolved, and now confirmed end-to-end**

`consistency.py` was not touched by D19, so `_is_inverse_mapping` still treats
`capital_of`/`has_capital` (both P36, inverted maps) as a compatible inverse
pair. Re-ran `TestInversePredicates` (3 tests): the on-write and periodic-scan
checks do not flag the pair; a swapped-map-with-extra-divergence still
conflicts. All pass.

D19 made the deeper N5 check testable: with both predicates now producing real
verdicts, they must *agree*.
`test_inverse_predicate_kb.py::test_capital_of_and_has_capital_are_symmetric` —
rerun independently — confirms `capital_of(Berlin, Germany)` and
`has_capital(Germany, Berlin)` both reach `VERIFIED` against the same KB
statement `Germany P36 Berlin`, the inverse predicate keying its lookup on the
object and the standard one on the subject. N5 is resolved both as a
consistency-check exemption and as a verdict-level correctness property.

### N6, N7 — **still resolved**

`TestSingleValuedMigration` (2 tests): the idempotent `ALTER TABLE … ADD COLUMN
single_valued` guard still adds the column to a pre-fixup DB; `create_schema` is
idempotent on a fresh DB. `test_runbook_thresholds.py` (3 tests): the runbook
threshold table parses, covers every corpus, and matches the runner's
`THRESHOLDS` dict (the doc-test still catches divergence). D19 touched none of
`database.py`, the runbook, or the corpus runner. All pass.

---

## Section 2.2 — Cluster D19 verification

### Stash-and-verify — reproduced independently

`git checkout v0.15-phase-10-complete-fixup-2 -- kb_verifier.py` (the test files
stay at fixup-3) and run the nine D19 tests: **8 failed, 1 passed**
(`FFFFF.FFF`). The single pass is `test_born_in_standard_path_still_verified`,
the standard-path regression guard — by design it passes both ways. The eight
failures are the exact shape of the D19 defect: `capital_of`/`mother_of` claims
return `no_match` instead of `verified`/`contradicted` (the wrong-entity
lookup), and the uninterpretable-mapping case returns `verified` instead of
`no_kb_path` (pre-fix `slot_to_qualifier` is ignored). After restoring fixup-3,
all nine pass; the full suite is 696 passed, 1 skipped, 11 deselected. The
fixup-3 report's stash-and-verify section is accurate.

**Are the tests genuinely discriminating, or would they pass a wrong fix?** The
discriminating mechanism is the **entity-keyed MockKB**: `lookup_statements`
returns statements only for `(entity, predicate)` — a lookup against the wrong
entity returns `[]`. This is the key difference from the unit file's MockKB
(which returns all statements regardless of entity, and would *not* discriminate
a wrong-entity lookup). I checked the obvious wrong implementations: an
*always-inverted* `_lookup_targets` is caught by tests 6 and 7 (`born_in`
standard would key on Honolulu, get `[]`, fail `VERIFIED`); an implementation
that adds `lookup_inverted` but does not actually swap the lookup is caught by
tests 1–5/8. The test set is sound.

### Code-path walk — inverse predicate `capital_of(Berlin, Germany)`

Traced extractor → router → KB verifier. At `verify()`: `consult("capital_of")`
returns the seed metadata (`kb_property=P36`,
`slot_to_qualifier={"subject":"statement_value","object":"statement_subject"}`,
`single_valued=1`). `_lookup_targets` reads `subject_slot == "statement_value"`
and `object_slot == "statement_subject"` → returns `("Germany", "Berlin",
True)`. The resolver is called on **Germany** (the lookup entity, `slot_position
="object"`), not Berlin; `lookup_statements(Q_Germany, "P36")` is the call;
Berlin is resolved as the expected value; `_compare_positive` matches it against
the statement value → `VERIFIED`; the trace records `lookup_inverted=True` and
`subject_kb_id=Q_Germany`. Every step matches what should happen.

### Code-path walk — standard predicate `born_in(Obama, Honolulu)`

`_lookup_targets` reads `subject_slot=="statement_subject"`,
`object_slot=="statement_value"` → returns `("Obama", "Honolulu", False)`. The
lookup is `lookup_statements(Q_Obama, "P19")` — keyed on Obama, not Honolulu;
the trace records `lookup_inverted=False`. `git diff fixup-2..fixup-3` confirms
the standard path is behaviorally identical to pre-D19 (same resolution calls,
same `slot_position` values, same lookup) — the only additive change is the
`lookup_inverted` trace key. The 696-passing suite, which includes every
standard-predicate KB test, corroborates: no standard-path regression.

### Edge-case handling — uninterpretable `slot_to_qualifier`

Phase 0 established the seed pack has **zero** qualifier-keyed subject/object
mappings, so nothing was deferred. `_lookup_targets` still handles a
hypothetical malformed map: for an uninterpretable shape it returns `None`, and
`verify()` returns `NO_KB_PATH` with a `unsupported_slot_to_qualifier` trace
note. `test_unsupported_slot_to_qualifier_is_no_kb_path` confirms this — and
critically it confirms the verifier **abstains, not crashes**. There is no
`NotImplementedError` (the failure mode the task explicitly warned against): an
uninterpretable mapping is an abstention, never a runtime exception. A null
`slot_to_qualifier` is treated as the standard mapping (the pre-D19 default).
Confirmed clean.

### Trace handling — no rename, consistent

Fix-up 3 did **not** rename the trace fields. `object_resolved` / `object_value`
/ the abstention reasons `subject_unresolved` / `object_unresolved` are
retained, read as KB-*statement*-relative, with `lookup_inverted` added as the
disambiguator. I checked the consumers: the walker reads only
`KBVerdict.verdict` and `KBVerdict.subject_kb_id` — never the `trace` dict — and
`subject_kb_id` stays semantically correct (the KB statement subject id). The
aggregator never sees a `KBVerdict` at all (it works off `WalkResult`). The only
`KBVerdict.trace` consumers are the KB-verifier tests. So "is the rename clean"
is moot — there was no rename, no consumer breakage, and the 696-passing suite
confirms it. See R2 for the residual naming concern.

---

## Section 2.3 — Cluster D18 verification (out of Phase 1 scope)

D18 was confirmed out of Phase 1 scope by Phase 0's scope check 2; re-verified
here. `benchmark.py`'s `AedosRunner.run_case` unpacks `extractor, walker,
aggregator` from a `build_pipeline` tuple and calls them directly
(`benchmark.py:189,194,203-204`); `_run_live` builds the pipeline at
`benchmark.py:472`, `_validate_harness` at `:435`. There is no import of
`app.py`, no FastAPI `TestClient`, no `ChatWrapper` reference. The calibration
corpus runner (`test_corpus_runner.py`) likewise drives components directly
against an in-memory DB. **Phase 10.5's calibration measurement is honest**: the
benchmark and the calibration corpora exercise the verification pipeline, not
the broken `/chat` wrapper. D18 remains a v0.16 delta for the chat-wrapper
deployment; it does not touch Phase 10.5.

---

## Section 2.4 — New defects

I searched for defects introduced by Cluster D19's rewrite of `kb_verifier.py`.
**No Critical or Major defect was found.** Documented negative results and three
Minor findings follow.

### Soundness — no false-verified path (negative result)

D19 changes which entity is the KB lookup key and which is the expected value.
I traced every way this could go wrong:

- A **misclassified direction** (a standard predicate read as inverse, or an
  inline-generated `slot_to_qualifier` the LLM gets wrong) keys the lookup on
  the wrong entity → `lookup_statements` returns `[]` or non-matching
  statements → `NO_MATCH`. An abstention, never a false verified.
- An **uninterpretable mapping** → `NO_KB_PATH`. An abstention.
- `_compare_positive` (the only code that can return `VERIFIED`/`CONTRADICTED`)
  is byte-identical to fixup-2 and returns `VERIFIED` only on a genuine
  `_value_matches`. `_apply_polarity` is byte-identical.

D19 introduces no path to a false verified. The soundness commitment
(architecture §3.2) holds.

### Multiply-inverted / unusual predicates (negative result)

There are no multiply-inverted predicates; the seed pack has the two clean
inverses and 58 standard maps (Phase 0). `_lookup_targets` handles an unusual
map by abstaining, verified above. Clean — and specifically **not** a
`NotImplementedError` crash.

### Consistency-check interaction (negative result)

D19 reads `slot_to_qualifier` from existing rows; it creates **no** substrate
rows. The consistency checker operates row-vs-row over `predicate_translation` /
`subsumption` / `predicate_distribution`; it does not check verdicts against
rows. D19 therefore adds no new consistency-check surface, and the N5
`_is_inverse_mapping` exemption is unchanged. No interaction.

### Single-valued + inverted interaction (verified correct)

`test_capital_of_wrong_functional_value_is_contradicted` — `capital_of(Munich,
Germany)` against `Germany P36 Berlin` — produces `CONTRADICTED`: `capital_of`
is `single_valued`, Munich resolves to a real-but-different Q-number, the
mismatch is a genuine contradiction. The inverse × functional combination is
correct.

### R1 — `lookup_inverted` is not propagated to the result-level trace — **Minor**

- **Issue.** D19 Step 3 added `lookup_inverted` so "Phase 10.5 debugging can see
  which lookups went through the inverted path." The field is in
  `KBVerdict.trace`, but the walker discards `KBVerdict.trace` entirely: it
  builds the KB `TraceEdge` from scratch with `metadata={"source": "kb",
  "verdict": …}` (`walker.py:274-288`) and reads only `KBVerdict.verdict` and
  `.subject_kb_id`. So `lookup_inverted` — and the whole KB verdict trace
  (`object_resolved`, `abstention_reason`, `positive_verdict`, …) — is **not**
  in `VerificationResult.per_claim_traces`. A Phase 10.5 debugger inspecting the
  verification result cannot see whether a verdict took the inverted path; they
  would have to call `KBVerifier.verify` directly.
- **Severity.** Minor. This is a pre-existing pattern, not a D19 regression —
  the walker's KB edge has always been minimal, and `KBVerdict.trace` has never
  reached the result (the same gap family as re-audit N4 / v0.16 D13). It has
  **zero** impact on verdict correctness or on Phase 10.5's accuracy /
  false-verified measurement; it only reduces trace debuggability. D19 Step 3's
  intent is met at the `KBVerifier` unit level (where the D19 tests observe it)
  but not the result level.
- **Recommended fix.** A one-line walker change — copy `lookup_inverted` (and
  ideally the KB abstention reason) onto the KB `TraceEdge.metadata` — or fold
  it into v0.16 D13's KB-trace-propagation work. Not required before Phase 10.5.

### R2 — direction-ambiguous trace field names — **Minor**

- **Issue.** `KBVerdict.trace` keeps `object_resolved` / `object_value` and the
  abstention reasons `subject_unresolved` / `object_unresolved`. These name KB
  *statement* positions (statement subject, statement value). For an inverse
  predicate they refer to the opposite Aedos slot from what the bare name
  suggests: a `capital_of` claim whose city fails to resolve abstains with
  `object_unresolved`, even though the city is the Aedos *subject*. The reading
  is correct but requires knowing the KB-statement-relative convention; the
  disambiguator (`lookup_inverted`) is in the same dict — but per R1 that dict
  does not reach the result trace.
- **Severity.** Minor. Observability only; no soundness or measurement impact.
  Fix-up 3 deliberately kept the names (renaming would churn four fixup-2 N1
  tests and is itself recorded as v0.16 delta D20). The `verify` docstring
  documents the convention.
- **Recommended fix.** v0.16 D20 — rename to direction-neutral names
  (`value_resolved`, `lookup_subject_unresolved`, …), updating every consumer
  together. Not required before Phase 10.5.

### R3 — polarity × inverted predicate is untested — **Minor**

- **Issue.** No test exercises a negated claim on an inverse predicate
  (`capital_of(Munich, Germany, polarity=0)` → should be `VERIFIED`). The D19
  tests cover inverse × asserted and the polarity tests cover negation ×
  standard, but not the intersection.
- **Severity.** Minor — a test-coverage gap, not a defect. I traced it: `verify`
  computes the direction-aware positive verdict via `_compare_positive`, then
  applies `_apply_polarity(pos_verdict, polarity)`. `_apply_polarity` is a pure
  function of `(KBVerdictType, int)`, byte-identical to fixup-2, with no
  direction dependence. `capital_of(Munich, Germany, polarity=0)` therefore
  yields `_apply_polarity(CONTRADICTED, 0) = VERIFIED` — correct by composition
  of two independently-verified facts (test 2 confirms the `CONTRADICTED`
  positive verdict; the diff confirms `_apply_polarity` is unchanged). The
  behavior is correct; only the test is missing.
- **Recommended fix.** Add one test (`capital_of` or `mother_of` with
  `polarity=0`) in a future pass. Not required before Phase 10.5.

### Audit log entries (negative result)

`VerificationResult.audit_log_entries` is populated by the aggregator's
`verdict_recorded` events; D19 does not touch the aggregator, and an
inverse-predicate claim produces an ordinary verdict the aggregator records like
any other. There is no inverted-path marker in the audit log — but that is the
same trace-propagation gap as R1, not a separate defect.

### Calibration runner regression (negative result)

`pytest --run-calibration` runs the 11-corpus harness dry-run clean (11 skipped,
0 failed/errored). D19 does not touch `test_corpus_runner.py` or any corpus.
Under live `RUN_CALIBRATION=1` the `kb_mapping` / `derivation` corpora would
exercise the post-D19 KB verifier — that is the intended D19 effect, not a
regression.

---

## Findings summary table

| Finding | Severity | Status at fixup-3 |
|---|---|---|
| C1 — KB verifier polarity blindness | Critical | Resolved (fix-up 1) — unaffected by D19 |
| C2 — walker subsumption-traversal stub | Critical | Resolved (fix-up 1) |
| M1 — consistency check unwired | Major | Resolved (fix-up 1) |
| M2 — retraction propagation inert | Major | Partially resolved — cascade/`ContradictionTracer` are v0.16 D14/D15; not a 10.5 blocker |
| M3 — walker negated-claim mislabel / dead conflict code | Major | Resolved (fix-up 1) |
| M4 — KB verifier object resolution + single-valued | Major | Resolved (fix-up 1 + fix-up 2); coupling re-verified at fixup-3 |
| M5 — runbook non-executable; weakened thresholds | Major | Resolved (fix-up 1 + fix-up 2); `--validate-harness` PASS at fixup-3 |
| M6 — walker under-tested; run-log misquote | Major | Resolved (fix-up 1) |
| m1–m8 — audit minors | Minor | m3/m6/m8 resolved; m1/m2/m4/m5/m7 deferred (unchanged) |
| N1 — false-contradiction on object-resolution failure | Major | Resolved (fix-up 2); standard path re-verified intact post-D19 |
| N2 — degenerate `cross_source` test | Major | Addressed (fix-up 2); capability gap is v0.16 D5 |
| N3 — seed count 61 vs 65 | Minor | Resolved (fix-up 2) |
| N4 — KB-grounded verdicts invisible to propagation | Minor | Deferred — v0.16 D13 |
| N5 — consistency check flags inverse seeds | Minor | Resolved (fix-up 2); re-verified, and confirmed end-to-end at fixup-3 |
| N6 — no migration for `single_valued` column | Minor | Resolved (fix-up 2); re-verified |
| N7 — thresholds duplicated across files | Minor | Resolved (fix-up 2); re-verified |
| D18 — chat-wrapper stale `extract` signature | — | Confirmed out of Phase 10.5 scope; remains v0.16-scope |
| D19 — KB verifier ignores `slot_to_qualifier` | — | **Resolved (fix-up 3)** — verified sound and load-bearing |
| **R1 — `lookup_inverted` not in result-level trace** | **Minor (new)** | Open — fold into v0.16 D13, or one-line walker fix |
| **R2 — direction-ambiguous trace field names** | **Minor (new)** | Open — v0.16 D20 |
| **R3 — polarity × inverted predicate untested** | **Minor (new)** | Open — add one test in a future pass |

---

## Recommendations for Phase 10.5

**The build is ready for Phase 10.5. Begin calibration.** D19 — the last named
capability gap that would have distorted the measurement — is resolved, sound,
and verified load-bearing. The three new findings are all Minor observability /
coverage items; none affects verdict correctness or the accuracy /
false-verified measurement, and none blocks calibration. M2's cascade and the
N2/D5 cross-source gap remain v0.16 deltas the prior re-audits already cleared
as non-blocking.

**Watch for during Phase 10.5:**

- **W1 — the extractor's slot ordering for inverse predicates (the most
  important watch-item).** D19's inverse lookup is correct *only if* the
  extractor produces `capital_of` with `subject` = the capital city and
  `object` = the country (and `mother_of` with `subject` = the mother) — the
  direction the seed's `slot_to_qualifier` is calibrated to. The medium-bar set
  exercises this through three cases — `ed_011` ("Washington, D.C. is the
  capital of the United States", ground truth `verified`), `csu_006`, `csu_011`
  — and the derivation corpus through `der_disambiguation_002`. If extraction
  calibration produces the slots in the other order (e.g. from "France's
  capital is Paris"), D19's inverted lookup queries the wrong way and the case
  false-abstains. This is an extraction-calibration coupling, not a D19 defect —
  D19 correctly implements the seed's stated direction — but Phase 10.5 should
  confirm the extractor's `capital_of`/`mother_of` slot convention matches the
  seed pack. If a `capital_of` case false-abstains, check the extracted claim's
  slot order before suspecting the KB verifier.

- **`cross_source_unification` medium-bar cases.** `csu_006` and `csu_011` are
  conjunctions of two independent KB claims (one of which is a `capital_of`
  claim D19 now handles), not single-chain cross-source derivations. They do not
  need the walker's KB-sourced subsumption enumeration (the N2/D5 gap), so D19
  alone should make their `capital_of` half tractable — but the fixup-2 report's
  blanket "cross_source cases will fail unless subsumption rows are pre-seeded"
  is worth re-checking against the actual case shapes during calibration.

- **R1/R2 — trace debuggability.** When debugging a failed inverse-predicate
  case, the `lookup_inverted` flag is not in `VerificationResult.per_claim_traces`
  (R1); inspect `KBVerifier.verify` directly, and read the trace's
  `subject_*`/`object_*` fields as KB-statement-relative (R2).

**Phase 10.5 readiness: yes.** The verification pipeline is sound, the named
capability gaps are closed, the calibration harness runs clean, and the
remaining open items are Minor observability findings and known v0.16 deltas.
Calibration can begin honestly.
