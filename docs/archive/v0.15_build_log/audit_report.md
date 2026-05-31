# Aedos v0.15 Build Audit

*Post-build hostile audit. Paper + code audit only; no live calls, no calibration
execution. Conducted against the `v0.15` branch at `c16bacb` (tag
`v0.15-phase-10-complete`).*

## Summary

The build is **structurally complete but semantically incomplete in load-bearing
places**. All 11 phase tags exist, the mocked suite passes clean (623 passed, 1
gated skip), v0.14 is intact, and `v0.15.0` was correctly not tagged. But two of
the system's core mechanisms do not work as the run log claims: the derivation
walker never performs derivation (subsumption traversal is a `pass` stub), and
the KB verifier ignores claim polarity, which produces a concrete false-verified
hole. Several correctness mechanisms (consistency check, retraction propagation)
exist as tested classes but are never wired into the pipeline. The build is **not
ready for Phase 10.5** without fix-up: the derivation and medium-bar corpora will
fail wholesale against the current walker regardless of LLM calibration.

## Findings

### Critical

---

**C1 — The KB verifier ignores claim polarity, producing false verifieds for negated claims.**

- **Issue.** `KBVerifier.verify` never reads `claim.polarity`. A negated claim
  (`polarity=0`, e.g. "Obama does *not* hold the office of President") whose
  positive form is supported by a KB statement is returned as `VERIFIED`.
- **Evidence.** `src/aedos_v0_15/layer4_sources/kb_verifier.py`: `verify()`
  (lines 45–114) and `_value_matches()` (lines 117–123) make no reference to
  `claim.polarity`. For claim `(Obama, holds_role, Q11696, polarity=0)` the path
  is: routing `kb_resolvable` → resolve `Obama→Q76` → `lookup_statements(Q76,
  P39)` returns `[Q11696]` → `_value_matches("Q11696","Q11696")` is `True` →
  line 92 returns `KBVerdictType.VERIFIED`. The walker
  (`walker.py:232–240`) then returns `"verified"` with no polarity adjustment.
  No test exercises a `polarity=0` claim against the KB verifier — the only
  `polarity=0` test (`tests/v0_15/unit/test_walker.py:282`) asserts on
  `polarity_trace`, not on the verdict.
- **Severity rationale.** Critical. The soundness criterion — zero false
  verifieds — is the project's load-bearing gate (architecture §3.2). This is a
  reproducible false verified on the primary KB path: the system returns
  `verified` for a claim the KB *contradicts*. It is invisible to the current
  suite and would only surface at Phase 10.5 or in production.
- **Recommended fix.** In `KBVerifier.verify`, after determining whether the KB
  statement matches the claim's *positive* content, invert the verdict when
  `claim.polarity == 0`: a value-match on a negated claim is `CONTRADICTED`, a
  value-mismatch with a scope-compatible statement is `VERIFIED`. Add unit and
  integration tests with `polarity=0` claims that assert the verdict.

---

**C2 — The derivation walker does not perform derivation; subsumption traversal is a stub.**

- **Issue.** The walker's `_expand_via_substrate` only ever emits
  predicate-equivalence edges. The distribution-gated subsumption-traversal
  block — the mechanism behind multi-hop reasoning and cross-source unification —
  is stubbed with `pass` and produces zero edges. The walker is, in practice, a
  depth-0 direct-lookup engine.
- **Evidence.** `src/aedos_v0_15/layer4_sources/walker.py:307–318`: inside the
  `slot`/`relation_type` loop, after consulting `predicate_distribution` and
  `subsumption.query_neighbors`, the per-neighbor body is a comment ("For now,
  skip actual entity lookup in walker") followed by `pass`. Nothing is appended
  to `expanded`. The Phase 6 plan (`docs/v0_15/phase_6_plan.md:24`) explicitly
  committed to "distribution-gated subsumption traversal" — the implementation
  drifted from its own plan. No integration test covers a multi-hop walk:
  `tests/v0_15/integration/test_walker_with_substrate.py` contains only
  Tier-U-direct, KB-direct, budget and trace tests, and its `MockKB.subsumption`
  always returns `"unrelated"` while `MockTransport` always returns distribution
  `"neither"` — the mocks hold the (stubbed) gate closed.
- **Severity rationale.** Critical. The walker is "Aedos's inference engine"
  (architecture §6.4) and the thing that distinguishes Aedos from naive
  tool-use (§3.5). Multi-hop-with-distribution and cross-source unification are
  failure modes 1 and 2 of the six the system is built to address (§8.1). The
  Phase 6 acceptance criterion "the walker correctly handles each of the six
  failure modes (one integration test case minimum per failure mode)" is not
  met. The run log claims Phase 6 "Implemented the derivation Walker (… 
  predicate-distribution gating on subsumption expansion)" — substantively
  false. The flagship example "Asa lives in the United States" cannot be
  derived.
- **Recommended fix.** Implement the subsumption-traversal edge: for each
  scope-permitted `(slot, relation_type, direction)`, look up the neighbor
  entity identifier(s) and emit `subsumption_traversal` edges with new nodes.
  Add integration tests, one per failure mode, that walk an actual chain
  against fixtures + seeded substrate rows (the derivation corpus cases are
  ready-made specs).

### Major

---

**M1 — The substrate-internal consistency check is never wired into the pipeline.**

- **Issue.** Architecture §5.4 requires consistency checks to run on-write
  ("a newly-created or modified row is checked against neighbors before
  commit"). No oracle calls the checker.
- **Evidence.** `ConsistencyChecker.check_on_write` /`check_periodic`
  /`resolve_conflict` (`src/aedos_v0_15/layer3_substrate/consistency.py:36,46,54`)
  have **zero callers** in `src/aedos_v0_15/` (grep across the tree returns only
  the definitions). The oracle `consult` paths in `predicate_translation.py`,
  `subsumption.py`, `predicate_distribution.py` insert rows without invoking the
  checker. Additionally `resolve_conflict` does not trigger retraction
  propagation — the plan specified `ConsistencyChecker.__init__(... ,
  retraction_propagator, ...)` but the implemented constructor
  (`consistency.py:26`) omits that parameter, so architecture §5.4 step 2
  ("verdicts whose traces include either retracted row are marked for
  re-derivation") never happens.
- **Severity rationale.** Major. The consistency check is the within-substrate
  half of the architecture's two-level error-catching story (§5.3). It is
  correctly implemented as a class and unit-tested in isolation, but inert in
  the assembled system — exactly the "code that exists but isn't wired up"
  failure mode. Not Critical because it is defense-in-depth and soundness is an
  over-time property; but the architecture mandate is unmet.
- **Recommended fix.** Call `check_on_write(table, row_id)` from each oracle's
  row-insert path; on a `conflict` result, call `resolve_conflict`. Wire a
  `RetractionPropagator` into `ConsistencyChecker` and invoke it from
  `resolve_conflict`.

---

**M2 — Retraction propagation is inert in the assembled pipeline.**

- **Issue.** `RetractionPropagator` works off an in-memory `_trace_index`
  populated by `record_verdict_trace`. Nothing in the pipeline calls
  `record_verdict_trace`, so `propagate_retraction` always returns `[]` in a
  real run. The `ContradictionTracer` likewise reads an empty index and never
  actually retracts the substrate rows it is named for.
- **Evidence.** `src/aedos_v0_15/layer5_result/aggregator.py` builds the
  `VerificationResult` but never calls `RetractionPropagator.record_verdict_trace`
  — the propagator's index is populated only by unit tests.
  `retraction.py:34` docstring concedes "session-local in Phase 8 … persistent
  storage via audit_log is Phase 10 work" — Phase 10 did not add it.
  `contradiction_tracer.py:32` reaches into the private
  `self._propagator._trace_index` and, for each `(table,row_id)`, calls
  `propagate_retraction` but never issues a DB `UPDATE` to set `retracted_at` on
  those rows — contrary to its docstring ("retract contributing rows") and
  architecture §7.3. `VerificationResult.audit_log_entries` is hardcoded `[]`
  (`aggregator.py:85`).
- **Severity rationale.** Major. Retraction propagation is the mechanism by
  which "soundness is preserved over time" (architecture §7.3, §3.3). End-to-end
  it does not function; the unit tests pass because they hand-populate the
  index. Cascade (verdict→dependent-verdict) is also absent — propagation is a
  single row→verdict hop.
- **Recommended fix.** Have the aggregator (or walker) call
  `record_verdict_trace(claim_id, verdict, source_rows)` for every verdict, with
  `source_rows` extracted from the justification trace. Make
  `ContradictionTracer.trace_contradiction` issue the `retracted_at` UPDATE on
  contributing rows. Persist the trace index (audit log or a `verdict_traces`
  table) so propagation survives across verifications.

---

**M3 — The walker mislabels grounded negated claims and contains dead conflict-detection code.**

- **Issue.** Three walker defects in verdict/polarity handling: (a) a negated
  claim correctly grounded in a Tier U negated assertion is returned
  `contradicted`; (b) the multi-chain conflict-detection branch is unreachable;
  (c) `polarity_trace` is static.
- **Evidence.** `src/aedos_v0_15/layer4_sources/walker.py:223`: on a Tier U hit
  the walker returns `"verified" if node.polarity == 1 else "contradicted"`.
  `TierU._stage1` (`tier_u.py:179–186`) matches polarity exactly, so a
  `found=True` for a `polarity=0` claim means Tier U holds the *same* negated
  assertion — the verdict should be `verified`, not `contradicted`. (b) Lines
  161–176: the walker `break`s immediately on the first `verified` or
  `contradicted`, so the `elif current_verdict != verdict` conflict branch
  (165–168) and `trace.walk_metadata["conflict"]` can never be reached. (c)
  `polarity_trace` is initialised `[claim.polarity]` at line 119 and never
  appended to; the Phase 6 plan committed to "polarity tracking: negation flips
  verdict interpretation" (`phase_6_plan.md:27`). `test_walker.py`'s
  `test_negated_polarity_tracked` asserts only `0 in polarity_trace`, giving
  false confidence.
- **Severity rationale.** Major. The negated-claim mislabel is a verdict
  correctness bug that would make the chat-wrapper "correct" a fact the user
  themselves asserted. It is not Critical because it produces a false
  *contradicted*, not a false verified (Tier U's polarity-exact match prevents
  the mirror false-verified). Dead conflict code means architecture §6.4's
  multi-chain contradiction handling is not implemented.
- **Recommended fix.** On a Tier U hit, return `verified` whenever the matched
  row's polarity equals the claim's (which `_stage1` already guarantees).
  Implement real polarity tracking through edges. Decide whether
  verified-then-contradicted conflict detection is in scope; if so, do not break
  on first `verified` — continue the frontier scan.

---

**M4 — The KB verifier never resolves object entities and assumes every predicate is single-valued.**

- **Issue.** `KBVerifier` resolves the *subject* entity but not the *object*; it
  then string-compares a KB Q-number against the claim's natural-language
  object. Separately, any scope-compatible statement with a non-matching value
  is treated as a contradiction, which is wrong for multi-valued predicates.
- **Evidence.** `kb_verifier.py:59–66` resolves `claim.subject` only; there is
  no resolve call for `claim.object` (`object_val = claim.object` at line 83 is
  the raw string). `_value_matches` (117–123) is a case-insensitive string
  compare, so for an entity-valued predicate it compares e.g. `"Q11696"` against
  `"President"` and never matches. The plan Phase 4 step 3 says "Resolves each
  entity slot" (plural) — the implementation resolves one. The walker/KB tests
  pass only because they feed Q-numbers directly as the claim object
  (`_claim(object_val="Q11696")` in `test_walker_with_substrate.py`) — a rigged
  input that masks the gap. Lines 86–101: for a multi-valued predicate (P39
  position-held, P106 occupation), a real but non-matching statement sets
  `contradicted_statement`, yielding `CONTRADICTED` where both values are true.
- **Severity rationale.** Major. With real extraction output, KB verification of
  entity-object claims either no-matches or false-contradicts; it is exercised
  in tests only through Q-number test inputs. Not Critical: it does not produce
  false verifieds. The single-valued assumption is partly a plan bug — the plan
  itself (Phase 4 step 7) says "matching predicate but contradicting value →
  contradicted" without a single-valued guard.
- **Recommended fix.** Resolve `claim.object` through the entity resolver before
  comparison when `meta.object_type == "entity"`. Add a single-valued/functional
  flag to predicate metadata; only emit `CONTRADICTED` for functional
  predicates. Stop feeding pre-resolved Q-numbers as test inputs.

---

**M5 — The Phase 10.5 runbook is partly non-executable and silently weakens the plan's thresholds.**

- **Issue.** `docs/v0_15/phase_10_5_runbook.md` — the operator handoff document
  and a named Phase 10 deliverable — has a step that will crash, a step that
  references machinery that does not exist, and acceptance thresholds below the
  implementation plan's.
- **Evidence.** (a) Step 3 (lines 84–106) inserts into `tier_u` using columns
  `object_val` and `asserting_party_id`; the actual schema (architecture §6.1,
  `tier_u.py:84–103`) has `object` and `asserting_party` — the snippet raises
  `no such column: object_val`. (b) Step 4 commands use `-k "extraction_corpus"
  --run-calibration`; `--run-calibration` is not a registered pytest option
  (`tests/v0_15/conftest.py` has no `addoption` for it) and the
  `tests/v0_15/calibration/` directory contains only `.jsonl` files and no
  `test_*.py` driver, so each command collects zero tests / errors on the
  unknown flag. (c) Thresholds are lowered vs the implementation plan's
  "Calibration deferral policy" table, which Phase 10's acceptance criterion
  required the runbook to *reproduce*: extraction 90%→85% (line 128),
  entity_resolution 90%→80% (line 174), kb_mapping 90%→85% (line 179),
  subsumption 90%→82% (line 190), predicate_distribution 85%→80% (line 191).
  Step 0 also states "~592+" passing tests; actual is 623.
- **Severity rationale.** Major. The runbook is the entire interface for Phase
  10.5. As written, an operator cannot run the calibration corpora at all, and
  if the harness were fixed they would be graded against a quietly relaxed bar —
  the "weaken the gate to make it pass" pattern, applied to the calibration
  acceptance criteria.
- **Recommended fix.** Fix Step 3's column names (`object`, `asserting_party`).
  Build an actual calibration runner (register `--run-calibration`, add a
  corpus-driving `test_*.py` or a `make calibrate` target) or rewrite Step 4 to
  invoke a real entrypoint. Restore the plan's thresholds verbatim.

---

**M6 — Test budget was front-loaded; the architecturally hardest phases are materially under-tested, and the run log obscures it.**

- **Issue.** New-test counts per phase diverge sharply from the plan's
  per-phase targets, and the run log misquotes its own Phase 6 target.
- **Evidence.** Per-phase new tests (verified via `git grep` of `def test_` at
  each tag) vs plan targets: P0 78/~30, P1 122/~80 (front-loaded); P2 39/~50,
  P3 49/~70, P4 64/~90, P5 43/~60, **P6 39/~80**, P7 34/~40, P8 54/~70, P9
  30/~50 (all under); P10 72/~40. The walker (P6) — the hardest component and
  the soundness gate's main locus — received 39 tests against a plan target of
  ~80 (51% under). The Phase 6 run-log entry states "target was ~50 new",
  contradicting `phase_6_plan.md:32` ("~80 new") and the implementation plan,
  making a 51%-under-target look like 22%-under. Cumulative 624 vs ~660 is
  within tolerance, masking the per-phase shortfalls.
- **Severity rationale.** Major. Test count is a target not a contract, but the
  *distribution* matters: trivial foundation/endpoint phases were over-tested
  and the semantically demanding verification phases under-tested, which is how
  C2/M3 reached `phase-10-complete` undetected. The run log's misquoted target
  is a self-serving inaccuracy.
- **Recommended fix.** Add walker derivation tests (per M3/C2). Correct the run
  log's Phase 6 entry. In a future v0.16 plan, treat per-phase test targets for
  substrate/walker phases as harder floors.

### Minor

- **m1 — `extraction_corpus.jsonl` is short of its required count.** It contains
  57 cases; the Phase 1 acceptance criterion requires "≥ 60", and the run log
  headline claims "60 cases" while its own sub-category breakdown
  (15+10+15+7+10) sums to 57. Hard-claim discipline got 7 cases vs the plan's
  10. Other corpora meet their counts.
- **m2 — Ambiguity-documentation discipline degraded after Phase 3.** The
  kickoff requires `docs/v0_15/phase_N_ambiguities.md` per phase. Phases 0–3
  have one; phases 4–10 do not — ambiguities were folded into `phase_N_plan.md`,
  and `phase_6_plan.md`'s "design decisions" do not state the rejected
  alternative for each, as the kickoff requires. This is the "drift" the
  kickoff explicitly warned about.
- **m3 — A v0.14 file was modified, violating the v0.14-untouched constraint.**
  Commit `feb0c18` ("v0.15-phase-0: branch + baseline") edits `src/app.py`
  (a v0.14 file), bumping version strings `0.14.8`→`0.15.0-alpha.0` in two
  places. Functionally trivial, but the kickoff names modifying anything under
  `src/` outside `src/aedos_v0_15/` a hard constraint violation.
- **m4 — The plan's live-KB test file was not created.** The Phase 4 plan
  specifies `tests/v0_15/live/test_wikidata_live.py` under `RUN_LIVE_KB=1`;
  there is no `tests/v0_15/live/` directory.
- **m5 — SPARQL fixtures carry a synthetic field.** `sparql_P39_Q76.json` /
  `sparql_P131_Q49112.json` include a `valueType` binding that the documented
  SPARQL query (plan §Phase 4, lines 927–935) does not bind and live WDQS would
  not return; per-qualifier columns (`qual_P580`) also diverge from the query's
  `?qual`/`?qualValue`. The `wbgetentities` fixtures omit minor real fields
  (`numeric-id`, statement `id`, `datatype`). Low functional risk — the adapter
  parses its own fixture shape — but Phase 10.5 must regenerate from live
  Wikidata before relying on them.
- **m6 — `VerificationResult.audit_log_entries` is stubbed.** Always `[]`
  (`aggregator.py:85`); architecture §7.1 specifies it should reference audit-log
  entries created during the verification.
- **m7 — Phase 3 tests were not updated to use the real resolver.** The Phase 4
  plan states "Phase 3's stubbed resolver is replaced. Phase 3's tests are
  updated to use the real resolver." The `phase-3→phase-4` test diff shows
  `test_tier_u.py` / `test_router.py` unchanged; Tier U stage-2 broadening
  remains a `return LookupResult(found=False)` stub (`tier_u.py:201–203`).
- **m8 — Run log / runbook count drift.** Run log Phase 10 says "623
  cumulative"; collection reports 624 `def test_` (623 run, 1 gated skip).
  Runbook Step 0 says "~592+". Cosmetic but indicates the numbers were not
  re-derived.

### Observations (not findings)

- **No test weakening across phases.** Every consecutive phase-tag diff of
  `tests/v0_15/unit` and `tests/v0_15/integration` is purely additive — new
  files only, zero deletions, zero modifications to prior test files. No
  assertion was weakened, loosened, or `skip`-papered between Phase 3 and Phase
  10. This was checked explicitly and is a clean negative result.
- **The mocked suite passes from a clean state:** 623 passed, 1 skipped. The
  single skip (`test_zero_seed_correctness.py`) is correctly gated on
  `RUN_LIVE_TESTS`/`RUN_LIVE_KB`. No unexpected skips, xfails, or
  `importorskip`.
- **Calibration corpora are genuinely adversarial, not thin.** Sampled
  `predicate_distribution_corpus` (the 25 `neither` cases are real traps —
  `prefers`/`hates`/`authored` non-distribution), `consistency_check_corpus`
  (structurally precise conflicting-mapping cases), and `derivation_corpus`
  (correct multi-hop specs). The corpora are well-authored; the risk is that the
  *implementation* (C2) cannot satisfy them, not that they are easy.
- **Structural integrity holds:** all 11 phase tags (`v0.15-phase-0` …
  `-phase-10`) exist on `v0.15`; `v0.15.0` correctly absent; the v0.14 tree is
  intact apart from m3; the seed pack has 61 mappings (≥60); the medium-bar set
  has 122 cases across the six modes + a bonus group.
- The `wbgetentities` fixtures (`entity_Q76`, `entity_Q49112`) have correct
  Wikidata shape — `entities`→`claims`→`mainsnak`/`datavalue`/`qualifiers`
  nesting is right, Q-numbers are real (Q76 Obama, Q11696 US-President-office,
  Q49112 Williams College, Q189004 liberal-arts-college). The shape concern is
  confined to the SPARQL fixtures (m5).

## Per-phase notes

**Phase 0 — Foundation.** Solid. 78 tests (well over the ~30 target); the seven
schema tables match architecture §5.2/§6.1. The only blemish is m3 (the
`src/app.py` version-string edit landed in this phase's baseline commit).

**Phase 1 — Extraction.** Heavily over-tested (122 new). Extractor, normalization,
decomposition, temporal, triage all present. The calibration corpus is 3 cases
short of the ≥60 requirement (m1). No semantic issues found in the extraction
code path.

**Phase 2 — Predicate translation oracle.** 39 tests vs ~50 target. Oracle
cold/warm-cache, retraction, `query_neighbors`, and audit logging (`log_event`)
are correctly wired. No findings.

**Phase 3 — Routing + Tier U.** Router and validator are sound. Tier U write path
(idempotency, contradiction-closure) is correct. Two issues seeded here surface
later: stage-2 entity-resolution broadening is a permanent stub never replaced
(m7), and the Tier U read path's polarity-exact matching is later misinterpreted
by the walker (M3).

**Phase 4 — KB protocol + Wikidata adapter.** The adapter, protocol dataclasses,
and fixture set exist and the `wbgetentities` fixtures are well-shaped. But the
KB verifier resolves only the subject, string-compares Q-numbers against
natural-language objects, and assumes single-valued predicates (M4); the SPARQL
fixtures carry a synthetic `valueType` (m5); the live-KB test file was not
created (m4-finding). This phase carries the most semantic risk.

**Phase 5 — Subsumption + predicate distribution oracles.** 43 tests vs ~60. The
two oracles and the `Substrate` facade are implemented to spec. No findings in
the oracles themselves — but their walker consumer (Phase 6) never uses the
subsumption oracle's output.

**Phase 6 — Derivation walker.** The weakest phase. Subsumption traversal is a
`pass` stub (C2); polarity tracking is static and the multi-chain conflict
branch is dead code (M3); 39 tests vs an ~80 target, with the run log misquoting
the target (M6). No integration test walks an actual derivation chain. The run
log's claim that the walker was "implemented" with "predicate-distribution
gating on subsumption expansion" is not accurate.

**Phase 7 — Python verification.** 34 tests vs ~40 — within tolerance. The
verifier, sandbox integration, and trace structure look correct; the corpus is
adversarial (leap-year, letter-count traps). No findings.

**Phase 8 — Layer 5 + consistency check.** The aggregator, consistency checker,
retraction propagator, and contradiction tracer all exist and are unit-tested,
but the consistency checker is never called from any oracle (M1) and the
retraction/contradiction machinery is inert end-to-end because nothing records
verdict traces (M2). The Phase 8 blocker note (UNIQUE constraints preventing
conflicting-row inserts) led to consistency tests using synthetic
`ConsistencyResult` objects — reasonable, but it means the *detection* path is
only exercised for `predicate_translation`.

**Phase 9 — Chat-wrapper deployment.** 30 tests vs ~50. The four-move
intervention selection logic is deterministic and tested. Because it consumes
walker verdicts, the C1/M3/M4 verdict bugs flow through into intervention
decisions (e.g. a false-verified negated claim → pass-through; a
false-contradicted negated claim → "correct"). No findings local to this phase.

**Phase 10 — Hardening + seeds + docs + evaluation.** 72 tests (over target).
Seed pack (61 entries), audit endpoints, and the medium-bar set (122 cases) are
present and correct. The runbook is the deliverable with problems: a crashing
SQL step, calibration commands with no backing harness, and weakened thresholds
(M5).

## Recommendations for Phase 10.5

1. **Do not run Phase 10.5 against the current build.** The walker stub (C2)
   means the `derivation_corpus` (all 12 `multi_hop_distribution` + 10
   `cross_source` cases) and the `medium_bar_test_set`'s `multi_hop_distribution`
   and `cross_source_unification` modes will fail wholesale — not because of LLM
   calibration but because the code cannot derive. Fix C2 first, then run.

2. **Fix C1 before any live run.** The polarity-blind KB verifier will register
   false verifieds against the medium-bar set's negated cases, breaching the
   ≤5% false-verified threshold for reasons unrelated to calibration. This is a
   code fix, not a calibration outcome.

3. **Repair the runbook before handing it to an operator (M5).** Step 3 will
   crash as written; Step 4 has no calibration harness to invoke. Restore the
   implementation plan's thresholds.

4. **Regenerate the Wikidata fixtures from live WDQS (m5).** The SPARQL
   fixtures' `valueType`/qualifier-column shape is hand-authored and diverges
   from what the documented query returns. Phase 10.5 is the first point live
   Wikidata is available — validate every fixture against live shape and against
   the adapter's actual parser before trusting fixture-backed verdicts.

5. **Wire and re-test the correctness mechanisms (M1, M2).** Before claiming the
   architecture's over-time soundness guarantee, the consistency check must run
   on-write and retraction must propagate from real verdict traces. These are
   currently green in unit tests but absent from the pipeline.

6. **Re-scrutinise Phase 6 and Phase 4 when calibration reveals failures.** Per
   M6, those phases are under-tested; calibration failures in `derivation`,
   `entity_resolution`, `kb_mapping`, or `subsumption` corpora most likely trace
   to the walker stub (C2) or the KB verifier object-resolution gap (M4) rather
   than to LLM prompt quality.

7. **Plan-bug feedback for v0.16.** (a) Phase 4 step 7 ("matching predicate but
   contradicting value → contradicted") needs a single-valued-predicate guard.
   (b) The deferred-calibration variant let the walker reach
   `phase-10-complete` with its core capability stubbed because mocked unit
   tests with gate-closing mocks are too weak a proxy — future plans should
   require at least one *non-mocked-substrate* derivation test per failure mode
   as a Phase 6 hard floor. (c) Require the run log to quote per-phase targets
   from the plan verbatim.
