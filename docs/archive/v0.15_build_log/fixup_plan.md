# Aedos v0.15 — Post-Audit Fix-Up Plan

*Concrete approach to the audit findings in `docs/v0_15/audit_report.md`. Written
before any code is touched. Surfaces ambiguities for resolution.*

Baseline confirmed: `pytest tests/v0_15/ -q` → **623 passed, 1 skipped** at
`v0.15-phase-10-complete` (`c16bacb`).

Scope is the audit's 2 critical + 6 major findings, with minors fixed
opportunistically (same-file-as-a-major) and otherwise deferred. v0.16
plan-bug recommendations are recorded in `v0_16_plan_deltas.md`, not acted on.

Clusters are addressed in the kickoff-specified order: Walker → KB verifier →
Pipeline wiring → Test gap → Runbook → minors. Each cluster is one commit,
preceded by stash-and-verify of its new tests.

---

## Cluster 1 — Walker (C2, M3) — `walker.py` + collaborators

**C2: subsumption traversal is a `pass` stub.** `_expand_via_substrate`
(walker.py:307–318) consults `predicate_distribution` and
`subsumption.query_neighbors` but the per-neighbor body is `pass`. The walker is
a depth-0 direct-lookup engine.

Fix:
1. `SubsumptionOracle` cannot currently surface a neighbor *entity* — its
   `query_neighbors` returns `SubsumptionVerdict` objects with no `entity_b`.
   Add `SubsumptionOracle.find_neighbors(entity, relation_type) ->
   list[(EntityRef, direction)]` where `direction ∈ {"parent","child"}`,
   reading non-retracted `subsumption` rows in *both* slot positions and
   interpreting the verdict (`a_subsumed_by_b` / `b_subsumed_by_a`) to classify
   each neighbor. (Children of E = entities X with `X relation E`; parents = Y
   with `E relation Y`.)
2. In the walker, replace the `pass` with real edge emission. For goal claim
   `P(E)`: `distributes_up` (`P(X) ∧ X R Y → P(Y)`) means to verify `P(E)` walk
   to children of E; `distributes_down` means walk to parents; `both` → both;
   `neither` → gate closed. Emit `subsumption_traversal` `TraceEdge`s and append
   new nodes (slot entity substituted) to the frontier.
3. Neighbor entities are read from substrate `subsumption` rows. This is
   faithful to the architecture: the subsumption oracle's role is to materialize
   KB-native and LLM-generated subsumption facts as rows (§5.2). Integration
   tests seed these rows + Tier U rows, exactly as the audit's recommended fix
   specifies ("against fixtures + seeded substrate rows").

Architectural sanity check this must satisfy: walker derives "Asa lives in the
United States" from Tier U "Asa lives_in Williamstown" + subsumption rows
"Williamstown part_of Massachusetts", "Massachusetts part_of United States" +
predicate_distribution "lives_in distributes_up part_of". Goal node
`(Asa, lives_in, United States)` → expand object down part_of → `… Massachusetts`
→ `… Williamstown` → Tier U hit → verified.

**M3a: negated claims grounded in a negated Tier U row return `contradicted`.**
`walker.py:223` returns `"verified" if node.polarity == 1 else "contradicted"`
on a Tier U hit. But `TierU._stage1` matches polarity *exactly*, so a `found`
hit means Tier U holds an assertion of the *same* polarity — the claim is
verified regardless of polarity. Fix: return `"verified"` on a Tier U `found`
hit unconditionally.

**M3b: dead multi-chain conflict-detection code.** The walker `break`s on the
first `verified`, so the `elif current_verdict != verdict` conflict branch is
unreachable. Architecture §6.4 wants multi-chain contradiction detected. Fix
(bounded): do not `break` on the first `verified` within a frontier level —
finish scanning the current frontier's nodes so a same-frontier `contradicted`
is caught and `contradicted` wins (architecture §6.4). Still `break` on
`contradicted`. This makes the branch reachable without unbounded continuation.

**M3c: static `polarity_trace`.** Initialised `[claim.polarity]`, never appended.
Fix: append the polarity of each node as it is visited/expanded. Our edge kinds
(predicate-equivalence via shared kb_property, subsumption traversal) preserve
polarity per §6.4, so the trace will correctly read `[p, p, …]`; the fix makes
it genuinely reflect the walk rather than being a one-element constant.

Collaborators touched: `subsumption.py` (new `find_neighbors`), and `tier_u.py`
(belief-revision ambiguity resolved "implement" — see below).

**M3 belief revision (resolved — Ambiguity 2 → implement).** Extend
`TierU.lookup` to detect a claim conflicting with an authoritative prior (same
`asserting_party`/`subject`/`predicate`, conflicting `object` or `polarity`,
non-retracted, currently-valid) and surface it via a new `LookupResult`
contradiction signal. The walker honors it and returns `contradicted`.

---

## Cluster 2 — KB verifier (C1, M4) — `kb_verifier.py`

**C1: `KBVerifier.verify` ignores `claim.polarity` → false verified for negated
claims.** A negated claim whose positive form the KB supports is returned
`VERIFIED`. Fix: compute the verdict for the *positive* content of the claim
(value-match / contradiction / no-match as today), then invert when
`claim.polarity == 0`: a value-match on a negated claim is `CONTRADICTED`; a
scope-compatible contradicting statement on a negated claim is `VERIFIED`;
`NO_MATCH` / `NO_KB_PATH` are polarity-invariant.

**M4a: object entity not resolved.** `verify` resolves `claim.subject` only;
`object_val = claim.object` is the raw natural-language string, then
string-compared against a KB Q-number. Fix: when `meta.object_type == "entity"`,
resolve `claim.object` through the entity resolver (a second `LocalContext` with
`slot_position="object"`) and compare the resolved KB id against the statement
value. Non-entity object types keep the literal/case-insensitive compare.

**M4b: single-valued-predicate assumption — resolved (see Ambiguity 1).** Add a
`single_valued INTEGER DEFAULT 0` column to `predicate_translation`; add a
`single_valued` field to the metadata-generation tool and `PredicateMetadata`;
emit `CONTRADICTED` from a value mismatch only when `meta.single_valued == 1`,
else `NO_MATCH`. This deviates from the architecture §5.2 printed schema — the
deviation is user-authorized and recorded in `v0_16_plan_deltas.md` (the
architecture doc should gain the column).

---

## Cluster 3 — Pipeline wiring (M1, M2)

**M1: consistency check never wired.** `ConsistencyChecker.check_on_write` has
zero callers. Fix: (a) add a `retraction_propagator` parameter to
`ConsistencyChecker.__init__` (the plan specified it; the implementation omitted
it); `resolve_conflict` calls `propagate_retraction` for each retracted row so
§5.4 step 2 happens. (b) Wire an optional `ConsistencyChecker` into the three
oracle constructors; after a row insert, call `check_on_write(table, row_id)` and
on a `conflict` result call `resolve_conflict`. The exercisable detection path is
`predicate_translation` (cross-predicate same-kb_property); subsumption /
distribution UNIQUE constraints preclude same-key conflicting inserts, as the
Phase 8 blocker note records.

**M2: retraction propagation inert; ContradictionTracer never UPDATEs.** Fix:
(a) `Aggregator.aggregate` calls `RetractionPropagator.record_verdict_trace` for
every verdict, with `source_rows` extracted from each trace's edges/metadata.
(b) `ContradictionTracer.trace_contradiction` issues the `retracted_at` SQL
`UPDATE` on contributing rows (it currently retracts nothing). The in-memory
trace index stays session-local; cross-process persistence via a `verdict_traces`
table is recorded as a v0.16 delta — the task's M2 scope names the two wirings,
not a new table, and a new table is a schema change beyond the audit's named fix.

**m6 (fixed in passing, same file):** `VerificationResult.audit_log_entries` is
hardcoded `[]`. The aggregator will collect ids of audit events it writes during
aggregation.

---

## Cluster 4 — Test gap (M6) — walker integration tests

Add integration tests covering the six failure modes (one minimum each) plus the
polarity cases, **sourced from `derivation_corpus.jsonl`**, not invented:

- multi_hop_distribution — `der_multihop_001` (lives_in + part_of) and the
  negative-gate case `der_multihop_009` (`prefers` does not distribute).
- cross_source — `der_cross_005` (Tier U + KB).
- entity_disambiguation — `der_disambiguation_002`/`008`.
- predicate_translation — `der_predicate_translation_003` (works_at→employed_by).
- belief_revision — `der_cross_009` (negated claim grounded in negated Tier U row,
  the M3a case); `der_revision_003` only if Ambiguity 2 resolves "implement".
- principled_abstention — `der_abstain_002`.

Where a corpus case is underspecified for an integration test (e.g.
`der_multihop_001` states the premise as `text` but the genuine multi-hop test
needs a distinct goal claim), the test docstring records the construction and the
case is treated as a close paraphrase. No failure mode lacks corpus coverage, so
no corpus expansion is expected here; if C1's negated KB cases need it (Cluster
2), the polarity-negated variant is added to `kb_mapping_corpus`/
`entity_resolution_corpus` with a docstring note.

Correct the run log's Phase 6 entry: it says "target was ~50 new"; the plan and
`phase_6_plan.md:32` say ~80.

---

## Cluster 5 — Runbook (M5) — `phase_10_5_runbook.md` + calibration runner

1. **SQL fix.** Step 3 uses `object_val` and `asserting_party_id`; the schema
   (architecture §6.1, `database.py`) has `object` and `asserting_party`. Correct
   them; the snippet also omits `asserted_at` ordering — align with the real
   `tier_u` columns.
2. **Thresholds.** Restore the implementation plan's "Calibration deferral
   policy" table verbatim (extraction ≥90, predicate_metadata ≥85,
   temporal_scope extraction ≥90/lookup 100, entity_resolution ≥90,
   kb_mapping ≥90, subsumption ≥90 KB-mediated/≥80 substrate, predicate_
   distribution ≥85, derivation ≥80, python_verification ≥85, consistency_check
   100/100, intervention ≥90). Sub-category lines that undercut the restored
   corpus threshold are removed or raised to be consistent.
3. **Calibration runner (largest piece).**
   - `tests/v0_15/conftest.py`: register `--run-calibration` via `pytest_addoption`;
     a `pytest_collection_modifyitems` hook skips calibration-marked tests unless
     the flag is set.
   - `tests/v0_15/calibration/test_corpus_runner.py`: for each corpus JSONL, load
     cases, run each through the appropriate component (extractor / KB verifier /
     walker / end-to-end), record pass/fail, compute per-corpus accuracy, assert
     against the plan thresholds.
   - Under default `make test` the runner collects but skips (flag unset). With
     `--run-calibration` set and `RUN_CALIBRATION` unset, it runs under mocked
     LLM/KB and reports (trivially, since mocks are canned). With
     `RUN_CALIBRATION=1` it uses live LLM/KB — the Phase 10.5 path.
   - Step 4 of the runbook is rewritten to invoke the real runner.
4. m8 (count drift) fixed in passing while in the runbook / run log.

---

## Cluster 6 — Minor findings

- **m3 (mandatory, explicitly in task scope):** revert `src/app.py` version
  strings `0.15.0-alpha.0` → `0.14.8` (lines 56, 178), restoring the v0.14 file.
- m1, m2, m4, m5, m7 — unrelated files / feature-sized; deferred with reasoning
  recorded in `fixup_report.md`. m7's Tier U stage-2 stub lives in `tier_u.py`
  which Cluster 1 may touch, but implementing entity-resolution broadening is a
  feature, not an opportunistic cleanup — deferred.

---

## Verification discipline (every cluster)

After each cluster: `pytest tests/v0_15/ -q`; confirm all prior tests still pass
and new tests pass. Then **stash-and-verify**: `git stash` the implementation
changes (keeping new tests), run the new tests, confirm they FAIL, `git stash
pop`, run again, confirm they PASS. A test green both before and after is not
exercising the fix. Blockers → `docs/v0_15/fixup_blockers.md`, stop, no tag.

---

## Ambiguities surfaced — RESOLVED

**Resolution (user, this session).** Ambiguity 1 → **Option A**: add the
`single_valued` column (audit's literal recommendation; architecture §5.2
schema deviation authorized, recorded as a v0.16 delta). Ambiguity 2 →
**implement**: add Tier U read-path contradiction detection; the belief_revision
integration test uses `der_revision_003`.

**Ambiguity 1 — M4b single-valued-predicate guard (audit-vs-architecture).**
The audit's recommended fix adds a "single-valued/functional flag to predicate
metadata" and emits `CONTRADICTED` only for functional predicates. But
architecture §5.2 prints the `predicate_translation` schema explicitly and it
has no such column — adding one deviates from the printed schema. The current
code false-contradicts every multi-valued mismatch (Obama P39 Senator vs
President → `contradicted`). Three options, taken to the user:
  - **A** — add a `single_valued` column + LLM-generated flag (audit's literal
    recommendation; schema deviation).
  - **C** — no schema change: emit `CONTRADICTED` only when the KB returns
    exactly one scope-compatible statement and it mismatches (a property that is
    multi-valued *for this entity* is observably so); otherwise `NO_MATCH`.
  - **B** — no schema change, maximally conservative: a value mismatch is always
    `NO_MATCH` (abstain); KB-path contradiction detection dropped in v0.15.
None of the three risk a false *verified*, so the soundness gate (§3.2) holds
under all. The difference is contradiction quality.

**Ambiguity 2 — M3 belief-revision contradiction detection.** The audit's M3
names three walker defects; none is "the walker fails to detect that a claim
contradicts an authoritative Tier U prior." But architecture §8.1 lists
cross-context belief revision as failure mode 5 ("contradictions across context
detected via lookup"), and the C2/M6 mandate to add "one integration test per
failure mode" needs a *genuine* belief_revision test. `der_revision_003` ("Asa
is not a student" vs Tier U "Asa is a student") expects `contradicted`; with only
the named M3a fix the walker abstains on it. Question to the user: implement Tier
U read-path contradiction detection (claim conflicts an authoritative prior →
`contradicted`), or keep the walker fix strictly to the three named M3 defects
and use the idempotent case `der_revision_004` for the belief_revision
failure-mode test?
