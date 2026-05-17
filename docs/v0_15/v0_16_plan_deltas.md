# v0.16 Plan Deltas

*Plan-level changes for the next build. These are **not** applied to the v0.15
implementation plan — they are recorded here for whoever writes the v0.16 plan.*

- **D1–D11** were collected during the first post-audit fix-up (fix-up 1):
  D1–D3 are the audit's "Recommendations for Phase 10.5" item 7 plan-bug
  feedback; D4–D11 are observations made while doing fix-up 1.
- **D12–D17** were identified by the re-audit (`reaudit_report.md` §3). D12 and
  D17 carry fixup-2 updates where the underlying defect was resolved in code.
- **D18–D19** were noticed in passing during the second fix-up (fix-up 2),
  out of that fix-up's named scope.

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

**Update (fixup-2).** Fix-up 2 backfilled `single_valued` into all 61 seed
entries and added the field to `load_seeds.py` and the seed-file format. 11
predicates are functional (`single_valued = 1`): `born_in`, `died_in`,
`born_on`, `died_on`, `capital_of`, `has_capital`, `continent_of`,
`founded_in_year`, `head_of_government`, `head_of_state`, `gender`. All others
are multi-valued — the conservative default, since a wrong `1` produces false
*contradictions* while a wrong `0` produces only false *abstains* (the accepted
§3.2 cost). The re-audit's functional list also named `country_of` and
`mother_of`; fix-up 2 reclassified both to multi-valued — `country_of` because
the predicate name is not subject-type-constrained (citizenship is plural), and
`mother_of` because its seed `slot_to_qualifier` is inverted (the Aedos subject
is the mother, who has many children). The §5.2 schema update should also note
that the seed-file format carries `single_valued`. Per-predicate reasoning is
in `docs/v0_15/fixup2_report.md`.

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

---

## From the re-audit (`reaudit_report.md` recommendation 3)

These were identified by the re-audit and are recorded here for the v0.16 plan.
D12 and D17 carry fixup-2 updates noting code-level resolution.

### D12 — Architecture §5.4 consistency rule vs. inverse predicates

The `transitive_equivalence_violation` rule flags two predicates mapped to the
same KB property with different `slot_to_qualifier` as a conflict, but inverse
predicates (`capital_of`/`has_capital`, both on P36) legitimately do exactly
that (re-audit N5). The rule, or the seed-pack representation of inverse
predicates, must be revised.

**Update (fixup-2).** N5 is **fixed in code** — `consistency.py`'s rule is now
direction-aware (`_is_inverse_mapping`): two predicates whose `slot_to_qualifier`
maps are exact subject/object inversions of each other are compatible inverses,
not a conflict. What remains for v0.16 is the **architecture §5.4 wording**: the
document still describes `transitive_equivalence_violation` as direction-blind
and should be revised to state the inverse-predicate exemption.

### D13 — Retraction propagation must cover KB-grounded verdicts (re-audit N4)

Trace edges for KB premise lookups carry no retractable identifier, and the
`entity_resolution_cache` (which *is* retractable) is never referenced by any
trace edge. A purely KB-grounded verdict records empty `source_rows` and can
never be reached by `propagate_retraction`. Architecture §7.3's over-time
soundness is unreachable for the most common verdict type. v0.16 should record
the `entity_resolution_cache` row id(s) on the KB `premise_lookup` edge, add
`entity_resolution_cache` to the aggregator's `_TRACE_ROW_ID_KEYS`, and decide
how a cached `lookup_statements` result is identified for retraction.

### D14 — The retraction cascade and re-derivation (re-audit M2)

Architecture §7.3's "marked for re-derivation … may be re-derived from
remaining premises" and the verdict→dependent-verdict cascade are unimplemented;
`propagate_retraction` is a single row→verdict hop whose return value is
discarded by `resolve_conflict` and `trace_contradiction`. v0.16 should
implement the cascade (a retracted verdict's own consequences become a new
retraction event) and re-derivation, or explicitly scope re-derivation out in
the architecture.

### D15 — `ContradictionTracer` is not wired into `app.py`

The fix-up (fix-up 1) wired `ConsistencyChecker` and `RetractionPropagator` into
the `/chat` pipeline but not `ContradictionTracer`; it is constructed only in
tests. Downstream contradiction tracing (architecture §7.3 retraction source #2)
is inert in the deployed pipeline and has no trigger. v0.16 should construct
`ContradictionTracer` in the pipeline and define its trigger (a deployment
feedback path, or automatic re-checking of verdicts against later premises).

### D16 — Object-conflict belief revision

The walker's belief revision catches polarity contradictions only. With
`single_valued` now populated (D1/D4), a functional predicate's *object*
conflict (`Asa lives_in NYC` in Tier U vs a claimed `Asa lives_in Boston`)
could also be detected. Relatedly, the Tier U write-path's contradiction-closure
(`tier_u.py`) closes a prior row on *any* object difference, which is wrong for
multi-valued predicates and should consult `single_valued`.

### D17 — Threshold single-source-of-truth (re-audit N7) + schema migration (N6)

Single source of truth for calibration thresholds, and a migration strategy for
schema columns.

**Update (fixup-2).** Both are **resolved**. N6 — `database.py`'s `create_schema`
now runs an idempotent `ALTER TABLE … ADD COLUMN single_valued` migration guard.
N7 — `THRESHOLDS` in `test_corpus_runner.py` is the documented single source of
truth, and `tests/v0_15/unit/test_runbook_thresholds.py` fails CI if the
runbook's Step 4 threshold table diverges from it. No v0.16 work remains for D17.

---

## From fixup-2 (noticed in passing — not in fixup-2's scope)

### D18 — The chat-wrapper never extracts claims (stale `extract` signature)

`ChatWrapper.respond` (`chat_wrapper.py`) calls
`self._extractor.extract(draft, asserting_party=asserting_party)`, but
`Extractor.extract`'s signature is `extract(text, context: ExtractionContext)`
— the `asserting_party=` keyword raises `TypeError`, which `respond`'s broad
`except Exception: claims = []` swallows. So the deployed `/chat` path extracts
**zero claims** and verifies nothing; every response is pass-through. This was
noticed while extracting the shared pipeline for Cluster B (the benchmark's
`AedosRunner` had the same stale-signature bug, fixed there under M5). It does
**not** block Phase 10.5 — the calibration corpora and the medium-bar
evaluation run through the runner/walker directly, and the deployment layer is
not part of the architecture (§4.6) — but it makes the chat-wrapper deployment
verification-inert. v0.16 should fix the call to pass an `ExtractionContext`
and add an end-to-end `/chat` test that asserts claims are actually extracted.

### D19 — The KB verifier ignores `slot_to_qualifier`; inverse predicates look up the wrong entity

`KBVerifier.verify` resolves `claim.subject` and calls
`lookup_statements(subject_id, meta.kb_property)` — it never consults
`meta.slot_to_qualifier`. For an inverse predicate whose seed maps the Aedos
*subject* to the KB `statement_value` rather than `statement_subject`
(`capital_of` on P36, `mother_of` on P25), the KB stores the statement on the
*object* entity, so the lookup queries the wrong entity and returns nothing →
`NO_MATCH`. Noticed while classifying `single_valued` for the inverse seed
predicates (D4). The KB path for every inverse-direction seed predicate is
therefore inert. v0.16 should make the KB verifier honor `slot_to_qualifier`'s
subject/object direction (resolve and look up against the entity the KB
actually keys the statement on). Phase 10.5 `kb_mapping`/`derivation` cases over
`capital_of`, `mother_of` will abstain until then — a capability gap, not a
calibration failure.
