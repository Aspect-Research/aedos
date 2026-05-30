# Aedos v0.15 Re-Audit Report

*Re-audit of the post-audit fix-up. Paper + code audit only; no live calls. Conducted
against `v0.15-phase-10-complete-fixup-1` (`e6c0492`), compared with the pre-fix-up
`v0.15-phase-10-complete` (`c16bacb`) and the architecture (`aedos_v0_15_architecture_draft_2.md`).*

*Execution performed: the mocked suite (`pytest tests/v0_15/ -q`), the calibration
runner in harness-validation mode (`pytest --run-calibration`), and three
stash-and-verify checks (new tests run against pre-fix `src/`). No `RUN_CALIBRATION`,
`RUN_LIVE_TESTS`, or `RUN_LIVE_KB`.*

## Summary

The fix-up did real, verifiable work: both criticals (C1, C2) and M1/M3/M6 are
genuinely resolved, the stash-and-verify discipline reproduces **exactly** (5/7,
16/17, 6 TypeError), the de-rigging is substantive, and the mocked suite is clean
(664 passed, 1 skipped, 11 deselected). The fix-up did **not** under-fix the core
verification logic — the new tests genuinely fail against pre-fix code.

However, the build is **not ready for Phase 10.5 as it stands**, for two reasons that
the fix-up report's "all eight findings Fixed" claim overstates. First, **M4 is only
half-fixed**: the `single_valued` mechanism is correct in code but the 61-entry seed
pack was never backfilled, so every seeded predicate — including genuinely functional
ones (`born_in`, `died_in`, `capital_of`, `born_on`, …) — is treated as multi-valued,
and the KB verifier cannot emit `CONTRADICTED` for any of them. This is the exact
"schema says we know cardinality, data says everything is multi-valued" state the
audit prompt named as *worse than not having the column*. Second, **M5 is only
partially resolved**: Steps 3/4 and the thresholds are genuinely fixed, but the
runbook's Step 6 (the medium-bar evaluation that gates `v0.15.0`) invokes
`benchmark.py`, whose live entrypoint is a `"not yet implemented"` stub — the runbook
remains "partly non-executable," M5's own headline. M2 is also partial (the cascade is
absent and `ContradictionTracer` is wired only in tests, not in `app.py`).

Recommendation: **a second, targeted fix-up before Phase 10.5**, scoped to M4 (seed
backfill + N1) and M5 (benchmark Step 6). The other findings are watch-items or v0.16
deltas.

---

## Section 1: Verification of original findings

### C1 — KB verifier polarity blindness — **Fully resolved**

- **Status.** Fully resolved.
- **Evidence.** `kb_verifier.py:verify` now computes a polarity-agnostic
  `pos_verdict` via `_compare_positive` (line 108) and then `final_verdict =
  _apply_polarity(pos_verdict, claim.polarity)` (line 114). `_apply_polarity`
  (lines 161–175) inverts `VERIFIED`↔`CONTRADICTED` for `polarity == 0` and leaves
  `NO_MATCH` untouched. Walked the audit's exact case `(Obama, holds_role, Q11696,
  polarity=0)`: subject resolves `Obama→Q76`, `lookup_statements(Q76,P39)` returns a
  matching statement, `_compare_positive` → `VERIFIED`, `_apply_polarity(VERIFIED,0)`
  → `CONTRADICTED`. The audit's false-verified case now returns `contradicted`.
- **`NO_MATCH` polarity-invariance.** Confirmed two ways: the no-statements branch
  (lines 99–105) returns `NO_MATCH` directly without calling `_apply_polarity`, and
  `_apply_polarity` returns `pos_verdict` unchanged when it is `NO_MATCH`.
- **Tests.** `TestKBVerifierPolarity` (5 tests) plus
  `test_negated_claim_kb_supports_positive_is_contradicted` walked through the walker.
  Stash-and-verify confirmed: 11 of the 22 rewritten `test_kb_verifier.py` tests fail
  against pre-fix `src/`, including the C1 headline.

### C2 — Walker subsumption-traversal stub — **Fully resolved**

- **Status.** Fully resolved (the stubbed defect is gone; see N2 for a residual
  cross-source caveat).
- **Evidence.** `walker.py:336–384`: the `pass` stub is replaced with a real
  distribution-gated subsumption traversal — it consults `predicate_distribution`,
  derives traversal directions via `_distribution_directions`, calls
  `subsumption.find_neighbors`, and appends `subsumption_traversal` edges with new
  nodes. `SubsumptionOracle.find_neighbors` (`subsumption.py:164–207`) genuinely
  enumerates non-retracted `subsumption` rows in *both* slot positions, derives
  `parent`/`child` direction from the verdict, and skips `equivalent`/`unrelated`
  rows — it is not a fixed-return stub.
- **Canonical example walked.** `test_multi_hop_distribution_derivation`
  (`test_walker_with_substrate.py:232`) seeds Tier U `Asa lives_in Williamstown` and a
  genuine **two-hop** chain (`Williamstown part_of Massachusetts`,
  `Massachusetts part_of United States`) plus `lives_in distributes_up part_of`. I
  traced the BFS by hand: depth 0 `(Asa,lives_in,United States)` → object substituted
  to `Massachusetts` → depth 1 → object substituted to `Williamstown` → depth 2 Tier U
  hit → `verified`. The walk reaches the conclusion. This is a real multi-hop test
  (two `_seed_subsumption` calls), not a one-hop case dressed up.
- **Tests.** Stash-and-verify confirmed 16 of 17 Cluster 1 walker/subsumption tests
  fail against pre-fix `src/` (the 17th, `test_distribution_gate_blocks_invalid_traversal`,
  is honestly documented as non-discriminating).
- **Caveat (not a C2 failure).** The walker's traversal reads *substrate* `subsumption`
  rows only; it never enumerates KB taxonomy. See N2 and M6 — this limits genuine
  cross-source unification, but the C2 stub itself is fixed.

### M1 — Consistency check unwired — **Fully resolved**

- **Status.** Fully resolved (named defect); two related observations below.
- **Evidence.** All three oracles call the checker on their generation path:
  `predicate_translation.py:291–294`, `subsumption.py:278–282`,
  `predicate_distribution.py:197–200` — each runs `check_on_write(...)` and, on a
  `conflict` result, `resolve_conflict(...)`. `ConsistencyChecker.__init__` now takes
  `retraction_propagator` (`consistency.py:30`), and `resolve_conflict` calls
  `propagate_retraction` for each retracted row id (`consistency.py:79–82`). `app.py`
  builds `consistency` and threads it into all three oracles (`app.py:129–133`).
- **Stash-and-verify.** The 6 Cluster 3 wiring tests fail pre-fix with
  `TypeError: ... unexpected keyword argument 'consistency_checker' /
  'retraction_propagator'` — the precise shape of the M1 defect.
- **Observations (not findings against M1's named defect).** (a) `load_seeds.py`
  inserts seed rows with raw `INSERT OR REPLACE` and never calls `check_on_write`, so
  seeds are not consistency-checked on write — and the seed pack contains a latent
  conflict (see N5). (b) `ConsistencyChecker.check_periodic` exists but has **no
  caller** anywhere in `src/` (only in `test_consistency_checker.py`); architecture
  §5.4 mandates a periodic scan ("default once daily"). The original M1 finding was
  scoped to on-write, so M1 itself is resolved, but the periodic half of §5.4 remains
  unwired.

### M2 — Retraction propagation inert — **Partially resolved**

- **Status.** Partially resolved. The wiring the fix-up describes is genuine, but two
  parts of the original finding are not addressed.
- **What is fixed.** The aggregator calls
  `record_verdict_trace(cid, verdict, source_rows)` for every verdict
  (`aggregator.py:104`), with `source_rows` extracted from trace-edge row ids by
  `_extract_source_rows` (`aggregator.py:31–44`). `ContradictionTracer.trace_contradiction`
  now issues a real `UPDATE {table} SET retracted_at=... WHERE id=?`
  (`contradiction_tracer.py:58–63`), not just logging. Both verified by stash-and-verify.
- **Still missing #1 — the cascade (M2's second sentence).** The original finding
  said: *"Cascade (verdict→dependent-verdict) is also absent — propagation is a single
  row→verdict hop."* It still is. `RetractionPropagator.propagate_retraction`
  (`retraction.py:39–67`) iterates `_trace_index` once, builds `VerdictRetraction`
  records, and returns them — no recursion, no re-derivation. A retracted verdict is
  never itself treated as a retracted premise; `consistency.resolve_conflict`
  *discards* the `propagate_retraction` return value entirely
  (`consistency.py:79–82`), and `contradiction_tracer` collects it but does not
  recurse. Architecture §7.3's "marked for re-derivation … may be re-derived from
  remaining premises" is not implemented at all. **The cascade is not implemented.**
- **Still missing #2 — `ContradictionTracer` is not in the deployed pipeline.**
  `git diff` of `app.py` confirms the fix-up added `RetractionPropagator` and
  `ConsistencyChecker` to the `/chat` pipeline but **not** `ContradictionTracer` — it
  is never imported or constructed in `app.py`. It is built only in
  `test_end_to_end.py:_make_pipeline` and the unit tests. So "downstream contradiction
  tracing" (architecture §7.3 retraction source #2) is inert end-to-end in the
  deployed system, and there is no trigger for it (no feedback endpoint). The fix-up
  report's "app.py's pipeline construction wires all of this together" overstates:
  the consistency check and the propagator/aggregator are wired; the contradiction
  tracer is not. See also N4 (KB-grounded verdicts are invisible to propagation) and
  the plumbing finding in Section 3.
- **Severity rationale.** Partial-resolution of a Major finding. The fix-up's wiring
  work (record_verdict_trace called; the `retracted_at` UPDATE issued) is genuine and
  is correctly the bulk of M2. But both the cascade (M2's explicit second sentence)
  and the pipeline-level integration of downstream contradiction tracing are
  unaddressed, so architecture §7.3's over-time soundness guarantee is not delivered
  end-to-end. Not Critical: nothing here produces a false verified — it is a
  missing-recovery-mechanism gap, and v0.15 verification results are per-call rather
  than a persistent belief store.
- **Recommended fix.** (1) Implement the cascade: when `propagate_retraction` marks a
  verdict retracted, treat that verdict's own consequences (and any Tier U row it
  produced) as a new retraction event and continue propagation; have
  `resolve_conflict`/`trace_contradiction` consume the return value rather than
  discard it. (2) Implement re-derivation, or explicitly scope it to v0.16 in the
  architecture. (3) Construct `ContradictionTracer` in `app.py`'s pipeline and define
  its trigger (a deployment feedback path, or automatic re-checking of verdicts
  against later premises) — otherwise §7.3 retraction source #2 cannot fire.
- **Notes.** Session-locality is acknowledged as v0.16 delta D6 and is not
  re-litigated here. See also N4 (KB-grounded verdicts are not even registered with
  the propagator) and D13/D14/D15 in the recommendations.

### M3 — Walker negated-claim mislabel; dead conflict code; static `polarity_trace` — **Fully resolved**

- **Status.** Fully resolved (three sub-parts); two scoping limitations noted.
- **(a) Negated-claim mislabel.** A Tier U `found` hit returns `"verified"`
  unconditionally (`walker.py:230–244`); `_stage1` already polarity-exact-matches, so
  a negated claim grounded in a negated Tier U row is `verified`. Confirmed by
  `test_negated_claim_grounded_in_negated_tier_u_is_verified`.
- **(b) Dead conflict branch.** The walker no longer breaks on the first `verified` —
  a grounded `verified` node `continue`s the frontier scan (`walker.py:188–190`); a
  same-frontier conflicting verdict reaches the conflict branch and sets
  `walk_metadata["conflict"]` (`walker.py:183–187`). `test_conflicting_chains_resolve_to_contradicted`
  exercises it (16/17 stash-and-verify failures include this). **Limitation:** the
  conflict branch only catches conflicts among siblings in the *same* BFS frontier;
  a `verified` at depth *n* and a `contradicted` at depth *n+1* are not both seen,
  because the walk breaks after the depth-*n* frontier completes. The fix-up report is
  honest about this ("a same-frontier conflicting verdict"). M3(b)'s named defect (the
  branch was unreachable) is fixed; full cross-depth multi-chain conflict per §6.4 is
  not.
- **(c) Static `polarity_trace`.** `polarity_trace.append(node.polarity)` runs per
  visited node (`walker.py:173`); `test_polarity_trace_records_every_visited_node`
  asserts `len > 1`.
- **Belief-revision (extended scope).** The walker performs a flipped-polarity Tier U
  lookup (`walker.py:252–267`): the exact negation of the claim asserted in Tier U
  yields `contradicted`. The Phase 3 write-path closes prior contradicting rows by
  setting `valid_until = now()` (`tier_u.py:63–82`, unchanged by the fix-up) — verified.
  **Limitation:** the re-audit prompt's framing was "conflict on object/polarity"; the
  implementation catches *polarity* conflicts only (same subject/predicate/object,
  opposite polarity), not *object* conflicts (`Asa lives_in Boston` vs a Tier U
  `Asa lives_in NYC`). This is sound (it never produces a false verdict) and
  acceptable per §3.2 soundness-over-completeness, but it is narrower than the prompt's
  description; recorded as a v0.16 candidate, not a finding.

### M4 — KB verifier object resolution + single-valued assumption — **Partially resolved**

- **Status.** Partially resolved. The code mechanism is correct; the **seed pack was
  not backfilled**, which makes the mechanism inert for all 61 seeded predicates.
- **(a) Object resolution — resolved.** `verify` resolves `claim.object` through the
  entity resolver when `meta.object_type == "entity"` (`kb_verifier.py:84–95`); on
  success the comparison is Q-number vs Q-number. `test_natural_language_object_is_resolved`
  asserts `trace["object_resolved"] is True` and `object_value == "Q11696"`, and it
  fails against pre-fix `src/`.
- **(b) `single_valued` guard — resolved in code.** The column is in the schema
  (`database.py:41`), the metadata tool (`predicate_translation.py:55–65`), and
  `PredicateMetadata` (line 118). Generation defaults safely:
  `single_valued = int(raw.get("single_valued", 0) or 0)` (line 254) — if the LLM
  omits the field the value is `0`, no crash. `_compare_positive` emits `CONTRADICTED`
  only when `scope_mismatch is not None and meta.single_valued` (`kb_verifier.py:156`);
  multi-valued predicates yield `NO_MATCH`.
- **The seed pack was not backfilled — Partial-resolution finding.** All 61 entries of
  `seeds/v0_15/predicate_translation.json` have **no `single_valued` field**
  (verified programmatically: 0 of 61), and `load_seeds.py` does not list
  `single_valued` in its INSERT column set (`load_seeds.py:73–93`). Every seeded
  predicate therefore loads with the column DEFAULT `0` (multi-valued). The seed pack
  contains many genuinely functional predicates — `born_in`, `died_in`, `born_on`,
  `died_on`, `capital_of`, `has_capital`, `country_of`, `continent_of`,
  `founded_in_year`, `mother_of`, `head_of_government`, `head_of_state`, `gender`.
  With `single_valued = 0`, `_compare_positive` can **never** return `CONTRADICTED`
  for any of them: an asserted false claim such as "Obama was born in Chicago" gets
  `NO_MATCH` → abstain instead of `contradicted`. This is precisely the state the
  audit prompt named ("a schema that says 'we know cardinality' but data that says
  'everything is multi-valued' … worse than not having the column"). v0.16 delta D4
  records the *column* addition but says nothing about backfilling the seed data.
- **Severity rationale.** Partial-resolution of a Major finding. Not Critical (it
  produces false *abstains*, not false verifieds, so soundness holds), but it directly
  degrades Phase 10.5: every contradiction-ground-truth medium-bar/derivation case
  whose predicate is seeded will be mis-scored as abstain.
- **Recommended fix.** Add a `single_valued` field to every entry in
  `predicate_translation.json` with the correct cardinality, add `single_valued` to
  `load_seeds.py`'s `_REQUIRED_FIELDS` and INSERT column list, and update the seed
  file format in architecture §9.2. Fix N1 first (see Section 3) — backfilling the
  seeds *activates* N1's false-contradiction for those predicates.

### M5 — Runbook non-executable; weakened thresholds — **Partially resolved**

- **Status.** Partially resolved. The three named M5 defects plus the Step 0 count are
  genuinely fixed, but the runbook's *headline* — "partly non-executable" — is still
  literally true at Step 6.
- **What is fixed.** (a) Step 3's SQL uses the correct columns `object` and
  `asserting_party` (`phase_10_5_runbook.md:104–108`, with an explanatory comment).
  (b) `--run-calibration` is registered (`conftest.py:25–31`) with a
  `pytest_collection_modifyitems` hook that deselects calibration tests by default
  (lines 41–51); `tests/v0_15/calibration/test_corpus_runner.py` exists with a
  parametrized per-corpus runner. I ran `pytest --run-calibration` — it collects 11
  corpus tests, each loads + validates its corpus and skips with a count (harness
  dry-run, 0.07s, no live calls, 0 failures/errors). (c) Every threshold in the
  runbook and in the runner's `THRESHOLDS` dict matches the implementation plan's
  "Calibration deferral policy" table verbatim (compared line-by-line; the M5-cited
  weakenings — extraction, entity_resolution, kb_mapping, subsumption,
  predicate_distribution — are all restored). (d) Step 0's test count is corrected to
  664.
- **Still non-executable — Step 6 (medium-bar evaluation).** The runbook Step 6
  presents `py -m tests.v0_15.evaluation.benchmark --test-set … --output
  docs/v0_15/evaluation_results.md` as a runnable command with expected output
  ("Results written to …"). But `benchmark.py`'s live `__main__` path
  (`benchmark.py:332–339`) checks the env vars and then prints **"Live evaluation not
  yet implemented — deferred to Phase 10.5"** and exits — it never instantiates
  `AedosRunner`/`BaselineRunner`, never calls `generate_report`, never writes the
  output file. Worse, `AedosRunner.run_case` (`benchmark.py:170–191`) calls
  `walker.walk(c)` with a single argument, but `Walker.walk` requires `(claim,
  context, …)` — the harness component is stale against the current API and would
  `TypeError` even if `__main__` were wired. Step 7 ("tag v0.15.0") depends on the
  `evaluation_results.md` that Step 6 never produces. The fix-up did not touch
  `benchmark.py` (`git diff` confirms). M5's claim "the Phase 10.5 runbook is partly
  non-executable" is therefore still accurate — at a step the original audit (which
  focused on Steps 3–4) and this fix-up both missed.
- **Residual #2 — Step 2 seed count.** Step 2 expects "Loaded 65 predicate
  translation seeds" and an acceptance threshold "65 seeds loaded"
  (`phase_10_5_runbook.md:73,78`). The seed pack has **61** entries and `load_seeds.py`
  prints "Loaded 61" (both verified by running it). `SEED_VERSION.txt:4` also declares
  `entry_count: 65`. These three (runbook, SEED_VERSION.txt) all disagree with the
  file. Pre-existing; not touched by the fix-up; the M5 runbook repair (which fixed
  Step 0's count) did not catch it. See N3.
- **Severity rationale.** Partial-resolution of a Major finding. Step 6 is a process
  blocker: Phase 10.5's medium-bar evaluation (the source of the headline
  false-verified-rate and accuracy-vs-baseline numbers, and the gate for `v0.15.0`)
  cannot be run as documented.
- **Recommended fix.** Implement `benchmark.py`'s live `__main__` (build a pipeline,
  call `AedosRunner`/`BaselineRunner`, `generate_report` to `--output`) and fix
  `AedosRunner.run_case`'s `walker.walk`/`extractor.extract` signatures; or, at
  minimum, rewrite runbook Step 6 to disclose that the live runner must be implemented
  first. Correct the Step 2 / `SEED_VERSION.txt` counts to 61.

### M6 — Walker under-tested; run-log target misquoted — **Resolved (with one degenerate test)**

- **Status.** Resolved for the test-count gap and the run-log correction; one of the
  seven new failure-mode tests is degenerate (raised as N2).
- **Run-log correction.** `run_log.md:77–79` now states the Phase 6 target as "~80 new
  per the implementation plan Phase 6 and phase_6_plan.md:32 — 39 is ~51% under
  target; the original entry misquoted the target as '~50'. Corrected during fix-up
  1." This is exactly right: it quotes the source with citation (the spirit of v0.16
  delta D3). The fix-up correctly did **not** retroactively pad the Phase 6 count — I
  endorse that; the run log should record what happened (39 tests), and the seven new
  walker integration tests are honestly attributed to the fix-up, not back-dated.
- **The seven failure-mode tests.** `test_walker_failure_modes.py` adds one test per
  the six §8.1 failure modes plus the C1 case. Stash-and-verify confirms 5 of 7 fail
  against pre-fix `src/` (multi_hop, cross_source, entity_disambiguation,
  belief_revision, C1) and 2 pass (predicate_translation, principled_abstention) — and
  the 2 that pass were never audit findings and are honestly documented as guards.
  Reviewed each test's substrate setup:
  - `belief_revision`, `principled_abstention`, `C1 polarity` — genuine, exercise the
    named mechanism.
  - `predicate_translation` — verifies via Tier U stage-3 broadening (which already
    handled it pre-fix); a legitimate guard, not padding.
  - `multi_hop_distribution` — uses a **single** subsumption row; it is a one-hop
    chain under a "multi-hop" label. Mitigated: a genuine two-hop test exists in
    `test_walker_with_substrate.py:232`, so the *capability* is covered; the
    failure-mode file's test is merely mislabeled/weak, not a coverage hole.
  - `entity_disambiguation` — two candidates do exist (Q308 @ 0.92, Q925 @ 0.40), so
    it is not the "only one candidate" degeneracy; but the disambiguation is decided
    purely by mock-assigned scores, not by contextual logic, so it is a shallow test.
  - `cross_source` — **degenerate**; raised as N2.
- **Severity rationale.** The bulk of M6 (the 39-vs-80 test gap, the run-log misquote)
  is genuinely addressed with discriminating integration tests. The single degenerate
  test is carved out as N2 rather than reopening M6.

---

## Section 2: Verification of fix-up claims

**Stash-and-verify discipline — confirmed, reproduces exactly.** I checked out
`v0.15-phase-10-complete -- src/aedos_v0_15/` and ran the new tests:
- Cluster 4: `test_walker_failure_modes.py` → **5 failed, 2 passed**. The 5 failures
  are multi_hop, cross_source, entity_disambiguation, belief_revision, and the C1
  case; the 2 passing are predicate_translation and principled_abstention — exactly as
  the report claims.
- Cluster 1: the 17 new walker/subsumption tests → **16 failed, 1 passed**. The 1
  pass is `test_distribution_gate_blocks_invalid_traversal` — exactly the test the
  report identifies as non-discriminating.
- Cluster 3: the 6 wiring tests → **6 failed**, every one a `TypeError` on the
  `consistency_checker` / `retraction_propagator` constructor parameters — exactly the
  shape claimed.
The fix-up report's stash-and-verify section is accurate and honest. This is strong
evidence the fix-up did **not** under-fix: the new tests genuinely discriminate the
pre-fix→post-fix transition.

**Non-discriminating tests — honestly documented; I agree with the classification.**
- `test_distribution_gate_blocks_invalid_traversal`: pre-fix the stub never traverses;
  post-fix the `neither` gate never traverses — same `no_grounding_found` both ways.
  Genuinely non-discriminating; serves as a gate-closed guard. Agree.
- `test_single_valued_mismatch_is_contradicted`: pre-fix every value mismatch →
  `CONTRADICTED` (the M4 bug); for a functional predicate the correct verdict is also
  `CONTRADICTED`, so they coincide. Confirmed it passes pre-fix in my run. Guard for
  the positive `single_valued` case. Agree.
- `test_negated_claim_no_statements_stays_no_match`: `NO_MATCH` is polarity-invariant
  pre and post. Confirmed it passes pre-fix. Guard. Agree.
None of the three is padding; each is a meaningful guard.

**De-rigged tests — substantive, not cosmetic.**
- `test_kb_verifier.py` (rewritten): run against pre-fix `src/`, **11 of 22 fail** —
  including object-resolution, polarity, and single-valued tests. The old file fed
  pre-resolved Q-numbers as `object_val`; the new file feeds natural language
  ("President of the United States") that must route through the resolver. Even
  `test_verified_when_value_matches` now fails pre-fix because it requires object
  resolution. Genuine de-rigging.
- `test_kb_path.py` (`git diff`): `MockKB.resolve_entity` changed from a hardcoded
  `Q76`-for-everything to a real `_RESOLUTIONS` lookup; `_claim` default `object_val`
  changed from `"Q11696"` to `"President of the United States"`. The old
  `test_kb_resolvable_wrong_value_contradicted` (which baked the M4 bug into an
  assertion) was **split** into `test_multivalued_wrong_value_is_no_match` (asserts
  `NO_MATCH`) and `test_single_valued_wrong_value_contradicted` (asserts
  `CONTRADICTED`). The rewrite corrects assertions that previously encoded the bug.
- `test_walker_with_substrate.py` / `test_end_to_end.py`: de-rigged via the same
  pattern (natural-language references, functional predicate for genuine KB
  contradiction). Confirmed substantive via the Cluster 1/3 stash-and-verify above.

**Test-count breakdown — confirmed.** Ran `pytest tests/v0_15/ -q` from a clean
checkout: **664 passed, 1 skipped, 11 deselected** — exactly the report's numbers.
`def test_` count: 624 (pre-fix) → 666 (post-fix) = +42 functions; collected items
624 → 676 (+52), of which 11 are the parametrized expansion of the single new
`test_corpus_calibration` def. Net new passing tests = 664 − 623 = 41 = Cluster 1 (17)
+ Cluster 2 (net +10; the report's "~+22" is the rewritten file's *total*, not the
net delta) + Cluster 3 (7) + Cluster 4 (7). Plus 11 deselected (Cluster 5). The math
is internally consistent. The only test removals are the ~12 old `test_kb_verifier.py`
tests replaced by the 22 de-rigged ones — an explained rewrite, not an unexplained
gap. No tests were silently deleted.

**No new skips — confirmed.** The 1 skip is `test_zero_seed_correctness.py` gated on
`RUN_LIVE_TESTS`/`RUN_LIVE_KB` (the only `skipif` marker in `tests/v0_15/`, pre-existing).
The 11 deselected are the calibration runner (deselected by the conftest hook in
default mode; they become 11 *skipped* harness dry-runs under `--run-calibration`). No
`xfail` and no `importorskip` anywhere in `tests/v0_15/`. The calibration runner's
internal `pytest.skip` is conditional and correctly gated.

**`src/app.py` v0.14 revert — confirmed.** `git diff v0.14.8..v0.15-phase-10-complete-fixup-1
-- src/app.py` is empty. m3 is fully reverted.

---

## Section 3: New defects introduced by the fix-up

### Critical

None.

### Major

**N1 — The KB verifier false-contradicts a true claim when a single-valued
predicate's object fails to resolve.**

- **Issue.** For an entity-valued, functional (`single_valued`) predicate, if the
  object reference does not resolve, the verifier compares a natural-language string
  against KB Q-numbers, never matches, and emits `CONTRADICTED` — turning an entity
  *resolution failure* into a false *contradicted* verdict.
- **Evidence.** `kb_verifier.py:84–95` resolves the object but falls back to the raw
  string when `select(...)` returns `None`; `object_resolved` is tracked (lines 83,
  95) and written into the trace (line 124) but is **not** passed to
  `_compare_positive`. `_compare_positive` (`kb_verifier.py:147–158`) treats the first
  scope-compatible non-matching statement as `scope_mismatch` and returns
  `CONTRADICTED` whenever `meta.single_valued` is set — it cannot distinguish "object
  resolved to a *different* entity" from "object did not resolve at all." Architecture
  §3.2 explicitly classes resolution failure as a *false-abstain* source ("none cause
  the system to lie"); here it produces a false verdict. No test covers single_valued
  + entity object + resolution failure (`test_single_valued_mismatch_is_contradicted`
  uses an object that *does* resolve).
- **Severity rationale.** Major, consistent with the original M4 severity calibration
  (false *contradicted*, not false verified). It produces a confident, citation-backed
  "correction" of a true user claim via the chat-wrapper. It is currently *masked* for
  all 61 seeded predicates because the seed pack leaves them `single_valued = 0`
  (M4 partial-resolution) — but it is live for any LLM-generated functional predicate,
  and, critically, **fixing M4 by backfilling the seed pack would activate N1 broadly**
  (every `born_in`/`died_in`/`capital_of` claim with a hard-to-resolve place). N1 and
  the M4 seed fix must be addressed together.
- **Recommended fix.** Pass `object_resolved` into `_compare_positive`; when
  `meta.object_type == "entity"` and the object did not resolve, do not emit
  `CONTRADICTED` (return `NO_MATCH`/abstain) — a comparison against an unresolved
  reference is not evidence of falsity.

**N2 — The `cross_source` failure-mode test is degenerate, and genuine cross-source
unification is not achieved by the walker.**

- **Issue.** `test_cross_source_tier_u_and_kb` — the designated integration coverage
  for architecture §8.1 failure mode 2 — does not exercise cross-source unification.
  It runs **two independent single-source walks** and asserts each used a different
  source; no single derivation chain composes a Tier U premise with a KB statement.
- **Evidence.** `test_walker_failure_modes.py:164–183`: two separate `walker.walk(...)`
  calls (`Asa lives_in Williamstown` grounded in Tier U; `Williams College located_in
  Massachusetts` grounded in the KB), with `assert tier_u_result.verdict == "verified"`
  and `assert kb_result.verdict == "verified"`. The test comment itself says "The two
  verdicts are grounded in different sources" — two verdicts, not one cross-source
  chain. The underlying reason: the walker's subsumption traversal calls
  `subsumption.find_neighbors`, which reads only substrate `subsumption` rows
  (`subsumption.py:177–197`); the KB is consulted only as a per-node direct lookup,
  and KB-mediated subsumption results are explicitly *not* cached as substrate rows
  (`test_subsumption_oracle.py:99–103`). So no walk can pull a KB-sourced taxonomy
  chain, and no verdict's trace composes a Tier U premise with a KB `lookup_statements`
  result — the architecture's flagship example ("Asa lives in the United States"
  composing Tier U + *KB* part_of chain, §8.1) is only reachable if the part_of chain
  is pre-seeded as substrate rows.
- **Severity rationale.** Major. This recreates the exact M6 "appearance of coverage
  without substance" pattern — for one of the two flagship failure modes — in a test
  the fix-up added specifically to demonstrate that mode. The capability gap is
  partially captured by v0.16 delta D5, but D5 frames it as a cold-start enumeration
  problem; the deeper point is that failure mode 2 is not realized in any warmth
  state, and Phase 10.5's `derivation_corpus` `cross_source` cases will fail unless
  they pre-seed substrate `subsumption` rows for every taxonomy step.
- **Recommended fix.** Either (a) give the walker a real cross-source test — one walk
  whose verdict trace contains both a Tier U `premise_lookup` and a KB `premise_lookup`
  edge — and, if that is not currently achievable, treat it as a capability gap; or
  (b) implement KB-sourced neighbor enumeration (v0.16 D5) so the walker can compose a
  Tier U premise with a KB-derived taxonomy chain. At minimum, the test must stop
  presenting two single-source walks as cross-source coverage.

### Minor

**N3 — Seed-count metadata says 65; the seed pack has 61.**

- **Issue.** `SEED_VERSION.txt` declares `entry_count: 65` and runbook Step 2 expects
  "Loaded 65" with an acceptance threshold "65 seeds loaded," but
  `predicate_translation.json` contains 61 entries and `load_seeds.py` prints "Loaded
  61."
- **Evidence.** Verified programmatically: `predicate_translation.json` → 61 entries;
  `load_seeds.py --db-path …` → "Loaded 61 predicate translation seeds";
  `SEED_VERSION.txt:4` → `entry_count: 65`; `phase_10_5_runbook.md:73,78` → "65". The
  original audit independently counted 61. The runbook's Step 2 lines were not touched
  by the fix-up (`git diff`), so this is pre-existing; M5's runbook repair fixed Step
  0's count but not this one.
- **Severity rationale.** Minor — cosmetic, but it sits in the operator-facing
  acceptance criteria and an operator following Step 2 literally would see a
  threshold mismatch. Same class as the original M5 "~592+ vs 623" defect.
- **Recommended fix.** Set `entry_count: 61` in `SEED_VERSION.txt` and "Loaded 61" /
  "61 seeds loaded" in runbook Step 2 — or, if 65 was intended, add the 4 missing
  seeds.

**N4 — KB-grounded verdicts carry no retractable row id, so they are invisible to
retraction propagation.**

- **Issue.** The fix-up's trace-row-id mechanism populates ids for 3 of 4 edge types;
  the KB `premise_lookup` edge carries none, and the `entity_resolution_cache` (which
  *is* retractable) is never referenced by any trace edge. A verdict grounded purely
  via the KB therefore records empty `source_rows` and can never be reached by
  `propagate_retraction`.
- **Evidence.** `walker.py:273–288` — the KB `premise_lookup` edges emit
  `metadata={"source": "kb", "verdict": …}` with no `*_row_id`.
  `aggregator.py:24–28` — `_TRACE_ROW_ID_KEYS` recognizes only `tier_u_row_id`,
  `predicate_translation_row_id`, `subsumption_row_id`. A purely-KB-grounded walk
  emits only the KB edge, so `_extract_source_rows` returns `[]` and
  `record_verdict_trace(cid, verdict, [])` registers the verdict with no dependencies.
  Meanwhile `entity_resolution_cache` is listed in
  `ContradictionTracer._RETRACTABLE_TABLES` (`contradiction_tracer.py:10–16`) but no
  trace edge ever emits an `entity_resolution_cache` row id. Architecture §7.3 states
  retraction of "a cached KB resolution" must propagate to dependent verdicts.
- **Severity rationale.** Minor, because it compounds an already-partial mechanism
  (M2 is session-local and cascade-less) and produces no false verdict — only an
  over-time-soundness coverage gap. But the gap covers the *most common* verdict type
  (KB-grounded factual claims), so it is worth fixing alongside M2.
- **Recommended fix.** Record the `entity_resolution_cache` row id(s) used by the KB
  verifier on the KB `premise_lookup` edge, add `entity_resolution_cache` to
  `_TRACE_ROW_ID_KEYS`, and decide how a cached KB `lookup_statements` result is
  identified for retraction (architecture §9.1 envisages an invalidatable cache).

**N5 — The now-wired consistency check classifies the hand-curated
`capital_of`/`has_capital` seeds as a conflict.**

- **Issue.** `capital_of` and `has_capital` are both seeded to Wikidata property
  `P36` with deliberately *inverted* `slot_to_qualifier` maps. The consistency
  checker's `transitive_equivalence_violation` rule treats "two predicates → same
  `kb_property`, different `slot_to_qualifier`" as a conflict — so these two correct,
  intentional inverse seeds are a latent conflicting pair, and the M1 wiring made that
  pair reachable.
- **Evidence.** `predicate_translation.json`: `capital_of` →
  `{"subject":"statement_value","object":"statement_subject"}`, `has_capital` →
  `{"subject":"statement_subject","object":"statement_value"}`, both `kb_property`
  `P36`. `consistency.py:118–133` (`_check_predicate_translation_row`) returns a
  `transitive_equivalence_violation` for exactly that pattern. It is currently dormant
  because `load_seeds.py` bypasses `check_on_write` and `check_periodic` has no caller
  — but architecture §5.4 mandates a periodic scan ("default once daily"); the first
  time it is wired (or the first time the oracle generates any third `P36`-mapped
  predicate, triggering an on-write check against the seeds), the retract-both policy
  will retract the correct `capital_of`/`has_capital` rows.
- **Severity rationale.** Minor *while dormant* — but it becomes Major the moment the
  architecturally-mandated periodic scan is wired. The impact is false-abstain on
  capital claims (sound but a capability loss), and it means the wired consistency
  check can destroy correct hand-curated seed data.
- **Recommended fix.** Make the consistency rule polarity/direction-aware so inverse
  mappings of the same property are not flagged (e.g. treat `slot_to_qualifier` maps
  that are exact subject/object inversions as compatible), or split such inverse
  predicates onto distinct representations. This is also an architecture §5.4 wording
  issue worth a v0.16 delta.

**N6 — The new `single_valued` column has no migration path.**

- **Issue.** `single_valued` was added only to the `CREATE TABLE IF NOT EXISTS`
  statement; there is no `ALTER TABLE`. A database file created before the fix-up
  would silently lack the column, and `_row_to_metadata`'s `row["single_valued"]`
  access (`predicate_translation.py:337`) would raise.
- **Evidence.** `database.py:10–126` — the entire schema is `CREATE TABLE IF NOT
  EXISTS`; opening a pre-existing DB never adds the new column. No `ALTER TABLE`
  anywhere in `src/`.
- **Severity rationale.** Minor and currently dormant — v0.15 is unreleased, and the
  runbook creates a fresh DB at Step 2, so no real database is affected. But the
  fix-up added a column to a schema with no migration strategy, leaving a latent
  break for any reused DB.
- **Recommended fix.** Add an idempotent guard in `create_schema` —
  `try: conn.execute("ALTER TABLE predicate_translation ADD COLUMN single_valued
  INTEGER DEFAULT 0") except sqlite3.OperationalError: pass` — or document that v0.15
  databases are not forward-compatible across the fix-up.

**N7 — Calibration thresholds are duplicated across three hand-maintained copies.**

- **Issue.** The acceptance thresholds live independently in the implementation plan's
  "Calibration deferral policy" table, in `test_corpus_runner.py`'s `THRESHOLDS` dict
  (the executable copy), and in the runbook's Step 4 prose. The fix-up added the third
  copy (the runner dict) without a single source of truth.
- **Evidence.** `test_corpus_runner.py:40–52` (`THRESHOLDS` dict, asserts in CI),
  `phase_10_5_runbook.md` Step 4 sub-sections (prose), `aedos_v0_15_implementation_plan_overnight.md:42–52`
  (table). All three currently agree (verified), but nothing keeps them in sync.
- **Severity rationale.** Minor — no current divergence, but M5's original finding was
  itself a *silent threshold divergence*, so re-introducing duplication invites
  exactly that failure to recur.
- **Recommended fix.** Make `test_corpus_runner.py`'s `THRESHOLDS` the single source
  of truth and have the runbook reference it, or generate the runbook's threshold
  lines from the dict.

---

## Findings summary table

| Finding | Original severity | Current status |
|---|---|---|
| C1 — KB verifier polarity blindness | Critical | **Fully resolved** |
| C2 — Walker subsumption-traversal stub | Critical | **Fully resolved** |
| M1 — Consistency check unwired | Major | **Fully resolved** (periodic scan still unwired — observation) |
| M2 — Retraction propagation inert | Major | **Partially resolved** — cascade absent; `ContradictionTracer` not in `app.py` pipeline |
| M3 — Walker negated-claim mislabel / dead conflict code / static polarity_trace | Major | **Fully resolved** (same-frontier-only conflict scope noted) |
| M4 — KB verifier object resolution + single-valued | Major | **Partially resolved** — seed pack not backfilled; all 61 seeds `single_valued=0` |
| M5 — Runbook non-executable; weakened thresholds | Major | **Partially resolved** — Step 6 benchmark is a stub; Step 2 seed count wrong |
| M6 — Walker under-tested; run-log misquote | Major | **Resolved** (one degenerate test carved out as N2) |
| N1 — False-contradiction on object-resolution failure | — | **New — Major** |
| N2 — `cross_source` failure-mode test degenerate; cross-source unification not achieved | — | **New — Major** |
| N3 — Seed count 61 vs 65 in SEED_VERSION.txt + runbook | — | **New — Minor** (pre-existing; folded into M5) |
| N4 — KB-grounded verdicts invisible to retraction propagation | — | **New — Minor** |
| N5 — Wired consistency check flags `capital_of`/`has_capital` seeds | — | **New — Minor** (Major if periodic scan is wired) |
| N6 — No migration for the `single_valued` column | — | **New — Minor** (dormant) |
| N7 — Calibration thresholds duplicated across three files | — | **New — Minor** |

Minor findings from the original audit (m1–m8): m3 (v0.14 edit) verified reverted;
m6 (`audit_log_entries`) verified populated. m1, m2, m4, m5, m7, m8 were deferred by
the fix-up with reasons and were not re-examined in depth here; none was reopened.

---

## Recommendations for Phase 10.5

**1. The build is not ready for Phase 10.5 as it stands. A second, narrowly-scoped
fix-up is recommended before calibration begins.** The core verification logic (C1,
C2, M3) is genuinely sound, but two issues block or distort Phase 10.5:

- **Fix M4 + N1 together.** Backfill `single_valued` into all 61 seed entries (and
  into `load_seeds.py`), and simultaneously fix N1 so that backfilling does not
  convert the current "never contradicts a seeded predicate" bug into a
  "false-contradicts on resolution failure" bug. Without this, every
  contradiction-ground-truth case over a seeded predicate is mis-scored.
- **Fix M5 Step 6.** Implement `benchmark.py`'s live runner (and fix
  `AedosRunner`'s stale `walker.walk`/`extractor.extract` signatures), or rewrite
  runbook Step 6 to disclose that the medium-bar harness must be built first.
  Otherwise Phase 10.5 cannot produce `evaluation_results.md` and Step 7 cannot tag
  `v0.15.0`.

**2. Watch for during Phase 10.5:**

- **`derivation_corpus` `cross_source` cases (N2).** The walker cannot pull KB-sourced
  taxonomy into a walk; cross-source cases will fail unless the corpus pre-seeds
  substrate `subsumption` rows for every step. If they fail, the cause is the N2/D5
  capability gap, not LLM calibration.
- **Multi-hop derivation depends on exact string identity** across extraction, Tier U,
  and substrate `subsumption` rows (the walker substitutes raw slot strings; there is
  no entity resolution inside subsumption traversal). Extraction variance ("US" vs
  "the United States") will break otherwise-correct multi-hop walks. Watch the
  derivation corpus closely.
- **`subsumption_corpus` compound bar.** The runner asserts only the ≥80%
  substrate-generation floor; the ≥90% KB-mediated bar must be checked by inspecting
  the KB-mediated subset manually (the runbook says so, but it is easy to miss).
- **The calibration runner uses an in-memory DB, not the seeded `aedos_phase10_5.db`.**
  Runbook Step 2 loads seeds into a file DB; `test_corpus_runner.py`'s `_Harness.db`
  is `open_memory_db()` and ignores `AEDOS_DB_PATH`. So Step 4 calibration runs
  zero-seed regardless of Step 2. This is defensible (calibration of LLM generation
  *should* be zero-seed) but is undocumented and will confuse the operator — clarify
  it in the runbook.

**3. Additional v0.16 deltas the re-audit identified (beyond the existing D1–D11):**

- **D12 — Architecture §5.4 consistency rule vs. inverse predicates.** The
  `transitive_equivalence_violation` rule flags two predicates mapped to the same KB
  property with different `slot_to_qualifier` as a conflict, but inverse predicates
  (`capital_of`/`has_capital`) legitimately do exactly that (N5). The rule, or the
  seed-pack representation of inverse predicates, must be revised.
- **D13 — Retraction propagation must cover KB-grounded verdicts (N4).** Trace edges
  for KB premise lookups carry no retractable identifier; the `entity_resolution_cache`
  is retractable but never referenced. §7.3 over-time soundness is unreachable for the
  most common verdict type.
- **D14 — The retraction cascade and re-derivation (M2).** Architecture §7.3's "marked
  for re-derivation … may be re-derived from remaining premises" and the
  verdict→dependent-verdict cascade are entirely unimplemented; `propagate_retraction`
  is a single row→verdict hop whose results are discarded by callers.
- **D15 — `ContradictionTracer` is not wired into `app.py`** and has no trigger;
  downstream contradiction tracing (§7.3 source #2) is inert in the deployed pipeline.
- **D16 — Object-conflict belief revision.** The walker's belief revision catches
  polarity contradictions only; with `single_valued` now available, a functional
  predicate's object conflict (`Asa lives_in NYC` in Tier U vs a claimed
  `Asa lives_in Boston`) could also be detected. The Tier U write-path's
  contradiction-closure (`tier_u.py:64–82`) similarly closes a prior row on *any*
  object difference, which is wrong for multi-valued predicates and should consult
  `single_valued`.
- **D17 — Single source of truth for calibration thresholds (N7); migration strategy
  for schema columns (N6).**

**4. What the re-audit verified clean.** The stash-and-verify discipline is real and
reproduces exactly. The de-rigging is substantive (the rewritten `test_kb_verifier.py`
fails 11/22 against pre-fix code; `test_kb_path.py`'s bug-encoding assertion was
corrected). No test was weakened, no new skip/xfail/`importorskip` was introduced. The
`src/app.py` v0.14 revert is byte-identical to `v0.14.8`. The run-log Phase 6
correction is honest and well-cited, and the fix-up correctly declined to retroactively
pad the Phase 6 count. C1, C2, M1, and M3 are genuinely and verifiably resolved — the
fix-up did not under-fix the core verification logic.
