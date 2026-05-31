# Aedos v0.15 — Post-Audit Fix-Up Report (fix-up 1)

*Addresses the findings in `docs/v0_15/audit_report.md`. Work landed as cluster
commits on the `v0.15` branch after `v0.15-phase-10-complete` (`c16bacb`),
tagged `v0.15-phase-10-complete-fixup-1`.*

**Test state.** Baseline at `v0.15-phase-10-complete`: 623 passed, 1 skipped.
After fix-up: **664 passed, 1 skipped** (the cold-start test, gated as before),
**11 deselected** (the new calibration corpus runner, collected only with
`--run-calibration`). `pytest tests/v0_15/ -q` is clean — no failures, no
unexpected skips.

The two critical and six major findings are all **Fixed**. Minor findings were
fixed in passing where they shared a file with a cluster, and deferred with
reasoning otherwise. v0.16 plan-bug recommendations are recorded in
`v0_16_plan_deltas.md` and were not acted on this session.

---

## Cluster summaries

### Cluster 1 — Walker derivation + polarity (C2, M3) — commit `9b08c9b`

**Found.** The derivation walker's `_expand_via_substrate` had its
distribution-gated subsumption-traversal block stubbed with `pass` (C2): the
walker was a depth-0 direct-lookup engine, unable to do multi-hop or
cross-source derivation. Separately (M3): a negated claim grounded in a negated
Tier U row was mislabeled `contradicted`; the multi-chain conflict-detection
branch was unreachable (`break` on first `verified`); `polarity_trace` was a
static one-element list.

**Fixed.** Added `SubsumptionOracle.find_neighbors`, which enumerates an
entity's taxonomy neighbors (parent/child) from non-retracted `subsumption`
rows. Implemented the walker's subsumption traversal: gated by
`predicate_distribution`, it substitutes a slot entity with a neighbor
(`distributes_up` → descend to children, `distributes_down` → ascend to
parents) and emits `subsumption_traversal` edges. M3a: a Tier U `found` hit now
returns `verified` unconditionally (`_stage1` already polarity-exact-matches).
M3 belief-revision: the walker now does a flipped-polarity Tier U lookup — an
asserted negation of the claim yields `contradicted` (architecture §8.1, the
user-confirmed scope decision). M3b: the walker no longer breaks on the first
`verified`, so a same-frontier conflicting verdict reaches the conflict branch
and resolves to `contradicted`. M3c: `polarity_trace` records every visited
node.

**Tests.** `test_subsumption_oracle.py` +7 (`find_neighbors`).
`test_walker_with_substrate.py` +10: single/multi-hop derivation, the
`distributes_down` direction, gate-closed blocking, negated-claim verdicts,
belief-revision contradiction, multi-chain conflict, `polarity_trace`. The
multi-hop test is the architectural sanity check — the walker derives "Asa
lives in the United States" from a Tier U town fact + a two-hop `part_of` chain.

### Cluster 2 — KB verifier polarity + object resolution (C1, M4) — commit `e7f0e1d`

**Found.** `KBVerifier.verify` never read `claim.polarity` (C1) — a negated
claim whose positive form a KB statement supports was returned `VERIFIED`, a
reproducible false-verified on the primary KB path. It resolved only the
subject, string-comparing a KB Q-number against the claim's natural-language
object (M4a), and treated any scope-compatible non-matching statement as a
contradiction, false-contradicting multi-valued predicates (M4b).

**Fixed.** `verify` now computes the verdict for the claim's *positive* content,
then inverts it for a negated claim (`_apply_polarity`): a KB-supported triple
makes a negated claim `CONTRADICTED`, a KB-contradicted triple makes it
`VERIFIED`; `NO_MATCH` is polarity-invariant (C1). The object entity is now
resolved through the entity resolver when `object_type == "entity"`, with a
literal-comparison fallback (M4a). A value mismatch yields `CONTRADICTED` only
for a functional predicate (M4b): a `single_valued` column was added to
`predicate_translation` and to the metadata-generation tool — this is a
user-authorized deviation from the architecture §5.2 schema, recorded as v0.16
delta D4.

**Tests.** `test_kb_verifier.py` rewritten to feed natural-language objects
(the audit flagged Q-numbers-as-test-input as rigged) — 22 tests including new
`TestKBVerifierPolarity`, `TestKBVerifierObjectResolution`,
`TestKBVerifierSingleValued` classes. `test_kb_path.py`, `test_end_to_end.py`,
and `test_walker_with_substrate.py` de-rigged to resolve references properly and
to use a functional predicate for genuine KB contradictions.

### Cluster 3 — Wire consistency check and retraction propagation (M1, M2) — commit `4af5ebb`

**Found.** `ConsistencyChecker.check_on_write` had zero callers — the
substrate-internal consistency check was inert (M1). Its constructor omitted the
`retraction_propagator` the plan specified, so §5.4 step 2 never happened.
`RetractionPropagator.record_verdict_trace` was never called from the pipeline,
so propagation always returned `[]`; `ContradictionTracer` never issued the
`retracted_at` UPDATE it is named for (M2). `VerificationResult.audit_log_entries`
was hardcoded `[]` (m6).

**Fixed.** `ConsistencyChecker` gained a `retraction_propagator` parameter;
`resolve_conflict` now propagates the retraction of each retracted row. The
three oracles call `check_on_write` after every row insert and `resolve_conflict`
on a conflict. The walker's trace edges now carry row ids (`tier_u_row_id`,
`predicate_translation_row_id`, `subsumption_row_id`); the aggregator extracts
those and calls `record_verdict_trace` for every verdict.
`ContradictionTracer.trace_contradiction` now issues the `retracted_at` UPDATE
on contributing rows. `app.py`'s pipeline construction wires all of this
together. m6: the aggregator, given a db, logs a `verdict_recorded` event per
claim and populates `audit_log_entries` with the ids.

**Tests.** `test_end_to_end.py` +7: `_make_pipeline` now assembles the wired
pipeline; `TestConsistencyCheckWiring` (oracle write triggers detection +
retract-both; `resolve_conflict` drives the propagator) and `TestRetractionWiring`
(aggregator records traces; retracting a recorded row propagates;
`ContradictionTracer` issues the UPDATE; `audit_log_entries` is populated).

### Cluster 4 — Walker failure-mode integration tests (M6) — commit `b7cd361`

**Found.** The walker (Phase 6) was materially under-tested — 39 tests against a
~80 plan target — and no integration test walked an actual derivation chain,
which is how C2/M3 reached `phase-10-complete` undetected. The run log misquoted
the Phase 6 target as "~50".

**Fixed.** Added `tests/v0_15/integration/test_walker_failure_modes.py`: one
integration test per the six architecture §8.1 failure modes
(multi_hop_distribution, cross_source, entity_disambiguation,
predicate_translation, belief_revision, principled_abstention) plus the C1
polarity case walked through the verification path — 7 tests, each sourced from
a `derivation_corpus.jsonl` case cited in its docstring. Substrate rows are
seeded, not gate-closed by mocks. Corrected the run log's Phase 6 entry to quote
the ~80 target with citation.

### Cluster 5 — Calibration runner + runbook repair (M5) — commit `4309be8`

**Found.** `phase_10_5_runbook.md` Step 3 inserted into `tier_u` using columns
`object_val` / `asserting_party_id` that do not exist (the schema has `object` /
`asserting_party`) — the snippet would crash. Step 4 invoked `--run-calibration`,
an unregistered pytest option, against `tests/v0_15/calibration/` which had no
test driver — every command collected zero tests. The runbook's acceptance
thresholds were silently weakened below the implementation plan's.

**Fixed.** Registered `--run-calibration` in `conftest.py` with a
`pytest_collection_modifyitems` hook that deselects calibration tests unless the
flag is passed (so default `make test` is unaffected — no new skips). Added
`tests/v0_15/calibration/test_corpus_runner.py`: a parametrized test per corpus
that loads cases, and under `RUN_CALIBRATION=1` runs each through the
responsible component (extractor, KB verifier, walker, oracles, …), computes
per-corpus accuracy, and asserts it against the plan threshold; under
`--run-calibration` without `RUN_CALIBRATION` it does a harness dry-run
(loads + validates the corpus, reports the count). Fixed Step 3's column names,
rewrote Step 4 to invoke the real runner, and restored every threshold verbatim
from the implementation plan's "Calibration deferral policy" table. Step 0's
test count was corrected (m8).

### Minor findings + reports — final commit

m3: reverted the v0.14-untouched-constraint violation — `src/app.py`'s version
strings were restored from `0.15.0-alpha.0` to `0.14.8`; `src/app.py` now
matches `v0.14.8` (`5876fef`) exactly. This report and `v0_16_plan_deltas.md`
were written.

---

## Finding-by-finding status

| Finding | Status | Where |
|---|---|---|
| C1 — KB verifier ignores polarity (false verified) | Fixed | Cluster 2 (`e7f0e1d`) |
| C2 — walker subsumption traversal stubbed | Fixed | Cluster 1 (`9b08c9b`) |
| M1 — consistency check never wired | Fixed | Cluster 3 (`4af5ebb`) |
| M2 — retraction propagation inert | Fixed | Cluster 3 (`4af5ebb`) |
| M3 — walker mislabels negated claims; dead conflict code; static polarity_trace | Fixed | Cluster 1 (`9b08c9b`) |
| M4 — KB verifier object resolution + single-valued assumption | Fixed | Cluster 2 (`e7f0e1d`) |
| M5 — runbook non-executable; weakened thresholds | Fixed | Cluster 5 (`4309be8`) |
| M6 — walker under-tested; run-log target misquoted | Fixed | Cluster 4 (`b7cd361`) |
| m1 — extraction_corpus 57 vs ≥60 cases | Deferred-minor — unrelated corpus file; noted as v0.16 delta D11 |
| m2 — ambiguity-doc discipline degraded after Phase 3 | Deferred-minor — process/docs, unrelated to any cluster file |
| m3 — a v0.14 file (`src/app.py`) was modified | Fixed | Minor-findings commit |
| m4 — live-KB test file `tests/v0_15/live/` not created | Deferred-minor — unrelated; the calibration runner exercises live KB for the entity_resolution / kb_mapping corpora |
| m5 — SPARQL fixtures carry a synthetic `valueType` field | Deferred-minor — unrelated fixture files; the audit itself defers regeneration to Phase 10.5 |
| m6 — `VerificationResult.audit_log_entries` stubbed `[]` | Fixed-in-passing | Cluster 3 (`4af5ebb`) — same file as M2 |
| m7 — Phase 3 tests not updated to real resolver; Tier U stage-2 stub | Deferred-minor — `tier_u.py` was not modified by the fix-up (the M3 belief-revision fix lives in `walker.py`); implementing stage-2 entity-resolution broadening is a feature, not an opportunistic cleanup |
| m8 — run-log / runbook count drift | Fixed-in-passing | run log Phase 6 entry (Cluster 4) and runbook Step 0 (Cluster 5); the run-log Phase 10 "623 cumulative" is the accurate historical passing count and was left as-is |

No finding is `Blocked`. `docs/v0_15/fixup_blockers.md` was not created — no
cluster's tests failed in a way that could not be resolved.

---

## Verification

The stash-and-verify discipline was followed for every cluster: the new tests
were confirmed to **fail against the pre-fix code** and **pass against the
post-fix code**. A test green both before and after does not exercise the fix.

**Cluster 1 (Walker).** `git stash` of `walker.py` + `subsumption.py`: of the 17
new tests, **16 failed** pre-fix (subsumption derivation produced
`no_grounding_found`, negated-claim verdicts were mislabeled, `polarity_trace`
had one element, the conflict branch was unreachable). The 17th —
`test_distribution_gate_blocks_invalid_traversal` — passes both ways: it is a
gate-correctness guard, and pre-fix the stub also never traverses, so it cannot
discriminate the stub→fixed transition. All 17 pass post-fix.

**Cluster 2 (KB verifier).** `git stash` of `database.py` +
`predicate_translation.py` + `kb_verifier.py`: the new C1/M4 test classes failed
pre-fix. The C1 headline test (`test_negated_claim_kb_supports_positive_is_
contradicted`) was deliberately constructed with a non-resolving object and a
literal-matching statement so it isolates the polarity defect — it returns
`VERIFIED` pre-fix (the bug) and `CONTRADICTED` post-fix. Two tests pass both
ways and are documented guards: `test_single_valued_mismatch_is_contradicted`
(pre-fix every mismatch contradicts, coinciding with the correct functional
verdict) and `test_negated_claim_no_statements_stays_no_match` (`NO_MATCH` is
polarity-invariant by design).

**Cluster 3 (Pipeline wiring).** `git stash` of the 8 implementation files: all
6 new wiring tests failed pre-fix — they raise `TypeError` on the
`consistency_checker` / `retraction_propagator` / `db` constructor parameters
that did not exist, which is the precise shape of the M1/M2 finding ("code that
exists but isn't wired up"). All pass post-fix.

**Cluster 4 (Test gap).** The six-failure-mode file was run against the original
`v0.15-phase-10-complete` build (`git checkout v0.15-phase-10-complete --
src/aedos_v0_15/`): **5 of 7 failed** — multi_hop_distribution, cross_source,
entity_disambiguation, belief_revision, and the C1 polarity case. The 2 that
pass pre-fix — predicate_translation and principled_abstention — were never
broken (neither is an audit finding); their tests confirm those modes remain
handled. All 7 pass post-fix. This is the definitive demonstration of the
audit's M6/C2 diagnosis.

**Cluster 5 (Runbook / calibration runner).** The runner is new infrastructure
with no pre-fix semantic behavior to discriminate against; it was verified
directly. `pytest --run-calibration` collects and runs all 11 corpus tests,
each loading and validating its corpus and reporting the case count; default
`make test` deselects them (664 passed, 1 skipped, 11 deselected — no new
skips). Pre-fix, `pytest --run-calibration` errors with "unrecognized
arguments" — the option did not exist.
