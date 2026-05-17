# v0.16 Plan Deltas

*Plan-level changes for the next build, collected during the v0.15 post-audit
fix-up (fix-up 1). These are **not** applied to the v0.15 implementation plan
this session — they are recorded here for whoever writes the v0.16 plan.*

The first three are the audit's explicit "Recommendations for Phase 10.5" item 7
plan-bug feedback. The rest are observations made while doing the fix-up.

---

## From the audit (recommendation 7)

### D1 — Phase 4 needs a single-valued-predicate guard in the plan

Phase 4 step 7 of the v0.15 plan says "matching predicate but contradicting
value → contradicted" with no guard. That instruction is wrong for multi-valued
KB properties (P39 position-held, P106 occupation, P166 award): the KB simply
holds *other* values and the claim's value may also be true. The implementation
inherited the bug (audit M4).

**v0.16 plan change.** Phase 4's KB-verifier step must specify: a value mismatch
yields `contradicted` only for a *functional* (single-valued) predicate, else
`no_match`. The predicate-translation oracle must carry a `single_valued` field
(the fix-up added this column — see D4) and the plan must describe generating it.

### D2 — Phase 6 needs a non-mocked-substrate derivation-test floor

The deferred-calibration variant let the derivation walker reach
`phase-10-complete` with its core capability (distribution-gated subsumption
traversal) stubbed as `pass`. Mocked unit tests with gate-closing mocks
(`MockKB.subsumption` always `unrelated`, `MockTransport` distribution always
`neither`) were too weak a proxy — they could not have failed.

**v0.16 plan change.** Phase 6's acceptance criteria must require, as a hard
floor, **at least one integration test per failure mode that walks an actual
chain against seeded substrate rows** (not gate-closing mocks). The per-phase
test-count target for substrate/walker phases should be treated as a floor, not
a soft target. (The fix-up added `test_walker_failure_modes.py` as the model.)

### D3 — Run log must quote per-phase targets verbatim

The Phase 6 run-log entry stated "target was ~50 new" against an actual plan
target of ~80, making a 51%-under-target shortfall look like 22%-under.

**v0.16 plan change.** The run-log template must require the per-phase test
target to be quoted verbatim from the implementation plan, with the phase plan
file and line cited. (The fix-up corrected the Phase 6 entry.)

---

## Observations from the fix-up

### D4 — Architecture §5.2 schema should include `single_valued`

The fix-up added a `single_valued INTEGER DEFAULT 0` column to the
`predicate_translation` table (user-authorized) to support D1's guard. The
architecture document §5.2 prints the `predicate_translation` schema and does
**not** include this column. The architecture doc should be updated to add
`single_valued` so the schema and the implementation agree.

### D5 — The walker has no KB-sourced neighbor *enumeration*

The fix-up implemented the walker's subsumption traversal by enumerating
neighbors from substrate `subsumption` rows (`SubsumptionOracle.find_neighbors`).
The KB protocol's three operations (`resolve_entity`, `lookup_statements`,
`subsumption`) all check or fetch for a *known* entity/pair — none *enumerates*
an entity's taxonomy neighbors. So in a cold-start system with no seeded
subsumption rows, the walker cannot discover a KB-sourced part_of/is_a chain on
its own; the substrate must first be populated (by seeds or prior `consult`
calls). v0.16 should either add a neighbor-enumeration protocol operation or
specify how the substrate's subsumption rows get populated ahead of a walk.

### D6 — Retraction propagation is session-local

`RetractionPropagator`'s verdict-trace index is in-memory and rebuilt per
process. The fix-up wired `record_verdict_trace` (from the aggregator) and the
`retracted_at` UPDATE (from `ContradictionTracer`), so propagation works within
a process, but it does not survive across verification runs. v0.16 should add a
persistent `verdict_traces` table (or audit-log replay on startup) so
architecture §7.3's over-time soundness holds across process restarts.

### D7 — The `predicate_equivalence` walker edge is largely redundant

`Walker._expand_via_substrate` emits `predicate_equivalence` edges via
`predicate_translation.query_neighbors`, but `TierU.lookup` stage 3 already
broadens by the same oracle, and an equivalent predicate shares the same
`kb_property` so its KB lookup is identical to the original's. The edge rarely
changes an outcome. v0.16 should either remove it or give it a distinct job.

### D8 — Audit-logging interface is inconsistent

`tier_u.py` and the oracles log via `log_event(conn, ...)` (a function);
`consistency.py`, `retraction.py`, and `contradiction_tracer.py` call
`self._audit.log(...)` (a method) but `audit/log.py` exposes no object with a
`.log` method — those branches are dead (audit is always `None` there). v0.16
should unify on one audit-logging interface.

### D9 — `VerificationResult.audit_log_entries` needs verification_context plumbing

The fix-up un-stubbed `audit_log_entries` (m6) by having the aggregator log a
`verdict_recorded` event per claim and collecting those ids. A complete
implementation — referencing *every* audit event created during a verification
(oracle `row_created`, `budget_exceeded`, `consistency_violation`, …) — needs a
verification id threaded through `log_event`'s `verification_context` argument
at every call site. v0.16 should specify that plumbing.

### D10 — Tier U → Python composition is unsupported

The walker cannot feed a Tier U value into a Python computation (the
`der_cross_007` "Asa's birth year plus 30 is 2003" pattern): `PythonVerifier`
sees only the claim's own slots, not premises retrieved from Tier U. v0.16
should decide whether cross-source chains into Python are in scope and, if so,
specify how the walker supplies computed-claim inputs.

### D11 — Corpus completeness (audit m1)

`extraction_corpus.jsonl` has 57 cases against a Phase 1 "≥ 60" requirement.
v0.16's plan should require a corpus-count assertion in each phase's acceptance
criteria so a short corpus is caught at build time. (Deferred this session as an
unrelated-file minor finding.)
