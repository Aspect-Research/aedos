# Phase B — Report

Session between Phase A cleanup (`v0.15.0-rc.2`) and Phase 10.5 calibration.
Unlike Phase A (hygiene only), Phase B is **capability extension**: D16
(object-conflict belief revision) and D6 (persistent retraction propagation),
plus a database schema-consistency fix. The audit chain has not covered this
work; per-cluster stash-and-verify and the surfaced design decisions are the
load-bearing substitutes for audit coverage.

Five cluster commits, run in order B1 → B5:

```
6392fa4  Cluster B1: database.py CREATE TABLE matches ALTER TABLE for single_valued
662332b  Cluster B2: walker detects object conflicts on functional predicates (D16, walker half)
a7d26e4  Cluster B3: Tier U write-path respects single_valued (D16, Tier U half)
70da183  Cluster B4: persistent verdict traces (D6, audit-log replay)
<B5>     Cluster B5: planning update + Phase B report
```

The design proposal is `docs/phase_B_plan.md` (committed with B1).

---

## Cluster summaries

### B1 — database schema consistency

`database.py`'s `CREATE TABLE predicate_translation` wrote `single_valued
INTEGER DEFAULT 0` while the N6 `ALTER TABLE` migration and architecture §5.2
both specify `INTEGER NOT NULL DEFAULT 0` — a fresh database got the nullable
form, a migrated one the non-nullable form. Fixed by adding `NOT NULL` to the
`CREATE TABLE` block. One line; no test relied on inserting NULL into
`single_valued` (`_generate_and_store` always inserts an int). Suite stayed at
701.

### B2 — walker object-conflict belief revision (D16, walker half)

`walker._direct_lookup` previously detected only polarity contradictions. B2
adds an object-conflict path: for a **positive** claim, after the exact-match
and polarity-flip lookups, the walker queries Tier U for a currently-valid
*positive* row asserting a *different* object for the same `(party, subject,
predicate)` (`TierU.lookup_object_conflict`, new). If such a row exists and the
predicate is functional (`single_valued = 1`, read via the predicate-translation
oracle), the verdict is `contradicted`. The trace edge carries
`belief_revision: object_conflict`; the existing polarity-flip edge now carries
`belief_revision: polarity_conflict` so Phase 10.5 can distinguish the two.

Negated claims do **not** enter the path (Decision 1, below). Multi-valued
predicates do not fire it. `test_walker.py`'s `MockTierU` gained a
`lookup_object_conflict` method (returning "no conflict") — a faithful mock
update to the walker's widened contract, not an assertion change.

### B3 — Tier U write-path respects single_valued (D16, Tier U half)

`TierU.write` previously closed a prior row on *any* object difference. B3
replaces that with the precise rule: **close a prior row iff the new claim
genuinely contradicts it** —

- same object, opposite polarity → close (direct negation, any cardinality);
- different object, both positive polarity, functional predicate → close;
- everything else (multi-valued different object; different object at a
  different polarity; both-negative different object) → **parallel write, the
  prior row stays open**.

`single_valued` is read via the predicate-translation oracle `TierU` holds;
absent an oracle the predicate is treated multi-valued (the §5.2 conservative
default — never a false closure). `WriteResult.closed_row_id` became
`closed_row_ids: list[int]`. The write path emits `tier_u_row_closed` /
`tier_u_parallel_assertion` audit events.

B3 also finished the D8 cleanup for `tier_u.py` (per the B3 check-in decision —
see "Design decisions"): the vestigial `audit_log` constructor flag — which
`build_pipeline` never set — was removed; `TierU` now logs via `log_event(db,
…)` unconditionally, so its audit events (including the pre-existing
`row_created`) fire in the deployed pipeline.

### B4 — persistent retraction propagation (D6, audit-log replay)

`RetractionPropagator._trace_index` was in-memory, rebuilt per process — verdicts
from process N were invisible to process N+1's retraction propagation, breaking
§7.3's over-time soundness across restarts. B4 adds `RetractionPropagator.
replay()`, which rebuilds the index from persisted `verdict_recorded` audit
events at process startup. `build_pipeline` calls `replay()` after constructing
the propagator; `ContradictionTracer` replays the fallback propagator it builds
itself. `propagate_retraction` is unchanged — `replay()` only hydrates the index
it already walks. No new table, no schema migration, no aggregator change.

### B5 — planning update + report

`docs/v0.16_planning.md`: D6, D16, and the DB-consistency fix marked **Resolved
(Phase B)** with commit references; original entries kept. Three follow-ups
recorded as new deltas D21–D23. Deferred deltas (D5, D9, D10, D13, D14, D15)
left deferred. This report written; final commit tagged `v0.15.0-rc.3`.

---

## Per-delta status

| Delta | Before | After | Commit |
|-------|--------|-------|--------|
| DB consistency | `CREATE TABLE` nullable, `ALTER TABLE` non-nullable | both `NOT NULL DEFAULT 0` | `6392fa4` |
| D16 (walker) | object conflicts on functional predicates → abstain | functional object conflict → `contradicted` | `662332b` |
| D16 (Tier U) | write path closes on *any* object difference | closes only on genuine contradiction; multi-valued writes parallel | `a7d26e4` |
| D6 | retraction index in-memory, lost on restart | `replay()` rehydrates it from `verdict_recorded` events; §7.3 holds across restarts | `70da183` |

D16 and D6 are now **Resolved (Phase B)**. The retraction *cascade* / re-derivation
(D14) and KB-sourced neighbor enumeration (D5) remain deferred — they were not
pulled forward.

---

## Design decisions

### D6 — Option α vs β (the highest-risk decision)

**Chosen: Option β — audit-log replay.** Decided on the evidence of what the
audit log already contains: `aggregator.py` logs a `verdict_recorded` event per
verdict with `event_data = {"verdict": …, "source_rows": [[table, id], …]}`.
The audit log therefore already holds every datum a replay needs.

- **Option α** (a new `verdict_traces` table) would duplicate what
  `verdict_recorded` already persists, introducing a table to keep consistent
  with the audit log, plus a schema addition immediately before calibration.
- **Option β** reuses the existing events. `replay()` reconstructs *exactly* the
  state the in-process `record_verdict_trace` calls produce (events applied in
  `id` order, last-wins per `claim_id`); `propagate_retraction` is unchanged, so
  the replay path and the live path cannot diverge. ~3 files touched
  (`retraction.py`, `pipeline.py`, `contradiction_tracer.py`), no new table, no
  aggregator change.

Both satisfy §7.3 (which is silent on mechanism). β was confirmed at the B4
check-in. β's only cost — startup scales with audit-log size — is bounded for
v0.15 and confirmed harmless by the performance smoke test.

### Decision 1 — negated-claim belief revision (B2)

A functional Tier U assertion `S P O′` logically *implies* the negation of
`S P O` for any `O ≠ O′` — so a claim `(Asa, lives_in, Boston, polarity=0)`
against Tier U `(Asa, lives_in, NYC, polarity=1)` could be **verified**. §8.1's
one-sentence belief-revision bullet does not settle this; surfaced at the
post-plan check-in. **Decided: conservative — abstain.** The walker implements
`object_conflict → contradicted` for positive claims only; the negated-claim
`negation_implied → verified` path is not built. A negated claim against a
different functional value falls through to abstain (the accepted §3.2
false-abstain cost). This keeps Phase B's belief-revision surface minimal.

### B3 — the Tier U write-path closure rule

The plan's B3 design question was whether the session prompt's four-bullet
write-path spec is correct. It is **imprecise for negated claims**. Taken
literally ("different polarity → close"), writing the second half of a
contrastive correction — §4.1 says "I live in NYC, not Boston" extracts both
`(NYC, 1)` and `(Boston, 0)` — would *close the first half*: the system would
believe "Asa lived in NYC until now, and doesn't live in Boston," silently
dropping the current "lives in NYC."

**Resolution:** B3 implements the precise rule (close iff genuine contradiction;
see the B3 cluster summary). The closure predicate is guarded to *both
assertions positive* for the functional object-conflict case, and to *same
object* for the polarity case — so contrastive corrections, both-negative pairs,
and multi-valued additions all write parallel. This is a correctness fix to the
spec, not a scope change; it was surfaced in the plan and at the B3 check-in.

The architecture §6.1 write-path prose describes only the polarity case and is
silent on object-conflict closure. Per the session's scope rule the architecture
was **not** edited — the wording lag is recorded as **D21** for v0.16.

### B3 — TierU audit-event wiring

B3 Step 3 specifies `tier_u_row_closed` / `tier_u_parallel_assertion` events.
`TierU` gated all audit logging on an `audit_log` constructor flag that
`build_pipeline` never set (a D8 leftover; A4 fixed the same vestigial-flag
pattern in three other modules but not `tier_u.py`), so the new events — like
the pre-existing `row_created` — would have been inert in the deployed pipeline.
Surfaced at the B3 check-in; **decided: finish the D8 cleanup for `tier_u.py`** —
drop the flag, gate on `db`. The events now fire in the pipeline. The three
substrate oracles still carry the same unwired flag; that is recorded as **D22**
for v0.16 (Phase B did not expand into the oracles).

### Architecture §7.3 / §6.4 — no edit needed beyond the D21 lag

§7.3 ("soundness is preserved over time") is silent on mechanism; Option β
satisfies it as written — no §7.3 wording change. §6.4 (walker) does not
describe object-conflict belief revision; that lag is folded into **D21** with
§6.1. No architecture edits were made (the session's scope rule).

---

## Stash-and-verify

### B2 — `walker.py` stashed to the pre-B2 state

| test | pre-B2 | post-B2 |
|------|--------|---------|
| `test_functional_object_conflict_is_contradicted` | **FAIL** (`no_grounding_found`) | pass |
| `test_polarity_conflict_records_trace_marker` | **FAIL** (`'polarity_conflict' in [None]`) | pass |
| `test_multi_valued_object_difference_is_not_contradicted` | pass | pass |
| `test_negated_claim_against_functional_prior_abstains` | pass | pass |
| `test_both_negative_object_difference_is_not_contradicted` | pass | pass |

The two discriminators fail pre-B2 — the object-conflict verdict and the new
polarity-conflict marker are load-bearing. The three regression/conformance
tests pass both ways (they assert behavior B2 leaves unchanged).

### B3 — `tier_u.py` stashed to the post-B2 state (old write path)

7 failed, 1 passed against the pre-B3 write path:

- **Behavioral discriminators fail cleanly:**
  `test_multi_valued_object_difference_keeps_both` (pre-B3 closes the
  multi-valued prior), `test_contrastive_correction_keeps_both_rows`,
  `test_both_negative_object_difference_keeps_both`,
  `test_write_different_object_no_oracle_keeps_both`,
  `test_row_closed_emits_audit_event`, `test_parallel_assertion_emits_audit_event`.
- `test_functional_object_conflict_closes_prior` fails via `AttributeError` —
  it asserts the B3-new `WriteResult.closed_row_ids` field, absent pre-B3. A
  trivial pre-B3 failure, not a clean behavioral one — recorded honestly.
- `test_functional_idempotent_write_no_new_row` passes both ways — idempotency
  is predicate-agnostic and unchanged.

### B4 — discriminator built into the test (no git-stash)

Git-stashing `retraction.py` removes the `replay()` method, so the cross-process
test would raise `AttributeError` rather than cleanly demonstrate the
regression. Instead the discriminator is **permanent, inside
`test_cross_process_retraction_via_replay`**: after "process 1" records verdicts
and "process 2" opens a fresh connection + fresh propagator, the test asserts
`propagate_retraction(...) == []` *before* `replay()` (the process-1 verdicts
are invisible) and non-empty *after*. Every CI run re-verifies that `replay()`
is load-bearing — more robust than git-stashing a method the test must call.

**Honest limitation.** A single test process cannot fork a real OS process. The
process boundary is simulated as a new SQLite connection to the same file plus
a fresh `RetractionPropagator` with an empty index — the architecturally
meaningful boundary for β (the persistence medium is the file; the volatile
state is the propagator's dict). Concurrent multi-process writers are out of
D6's scope and untested.

---

## Tests updated to new behavior (not weakened)

Three pre-existing tests asserted the *old* "always close" write-path behavior
and were updated to the new semantics — per the discipline rule, the tests
follow the corrected behavior, the behavior was not weakened to the tests:

- `test_tier_u.py::test_write_different_object_closes_prior` → renamed
  `test_write_different_object_no_oracle_keeps_both`, asserting the
  no-oracle multi-valued-default parallel write.
- `test_routing_to_tier_u.py::TestContradictionFlow` (3 tests) — its
  `prefers`-with-different-object scenario is, correctly, a *parallel*
  assertion post-B3, not a contradiction; reworked to a polarity-flip
  contradiction (a genuine contradiction for any predicate). The
  functional-vs-multi-valued object-difference rule is covered by
  `test_tier_u.py::TestTierUWriteSingleValued`.

`test_walker.py::MockTierU` gained a `lookup_object_conflict` method — a mock
kept faithful to the walker's widened `TierU` contract.

---

## Test count delta

| | passed |
|--|--------|
| `v0.15.0-rc.2` baseline | 701 |
| after Phase B | 717 |

**+16:** B2 +5 (`TestWalkerObjectConflictVerdicts`), B3 +7
(`TestTierUWriteSingleValued`), B4 +4 (`TestRetractionPropagatorReplay`). B1
adds no test. The reworked tests (B3's one renamed test, the three
`TestContradictionFlow` tests) and the `MockTierU` update change no count. The
total is slightly above the plan's 712–715 estimate — B3 carries 7 tests rather
than the estimated 4–5 because the contrastive-correction guard, the
both-negative guard, and the two audit-event assertions each lock a distinct
behavior.

---

## v0.16 follow-ups surfaced

Recorded in `docs/v0.16_planning.md` under "From Phase B":

- **D21** — architecture §6.1 / §6.4 wording lags object-conflict belief
  revision (the document describes only polarity-based contradiction).
- **D22** — the three substrate oracles still gate audit logging on the unwired
  `audit_log` flag; the D8 cleanup B3 applied to `TierU` should extend to them.
- **D23** — the `lives_in` seed's `single_valued = 0` rationale ("not consulted
  on the user-authoritative route") is invalidated by D16; the classification
  and the stale `reason` text should be revisited. The seed pack was not touched
  in Phase B (it is a calibration input and a deployment decision).

A scoped limitation, folded into D16's planning-doc note rather than its own
delta: `TierU.lookup_object_conflict` is literal-match only — an object conflict
expressed through an equivalent predicate or an unresolved entity alias is not
detected (it would need the stage-2 / stage-3 broadening `TierU.lookup` uses).

---

## Final verification

```
pytest tests/ -q                                    717 passed, 1 skipped, 11 deselected
python -m tests.evaluation.benchmark --validate-harness   Harness validation: PASS
pytest --run-calibration -q                          717 passed, 12 skipped
git log --oneline v0.15.0-rc.2..HEAD                 5 cluster commits
```

The calibration run collects the 11-corpus runner as harness dry-runs (each
corpus's cases load and parse OK); live evaluation remains gated on
`RUN_CALIBRATION` for Phase 10.5.

## Tag and Phase 10.5 start point

The final commit is tagged **`v0.15.0-rc.3`**. Phase 10.5 begins from
`v0.15.0-rc.3`: each Phase B cluster's stash-and-verify confirmed its new code
is load-bearing, no "stop and surface" trigger fired (B4 touched exactly the
three planned files; the B2/B3 surprises were a mock update and a test rework,
not deep interactions), and the full suite, harness validation, and calibration
dry-run are all green. Phase B did not receive the audit-fix-reaudit cycle the
rest of v0.15 got; if Phase 10.5 calibration surfaces an anomaly traceable to
D16 or D6, `v0.15.0-rc.2` is the fallback start point.
