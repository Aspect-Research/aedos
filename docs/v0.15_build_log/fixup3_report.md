# Aedos v0.15 — Third Post-Re-Audit Fix-Up Report (fix-up 3)

*Addresses the v0.16 deltas D18 and D19 named in `docs/v0_15/v0_16_plan_deltas.md`,
the two capability gaps the re-audit chain left open before Phase 10.5. Work
landed as a Phase-0 scope commit and one cluster commit on the `v0.15` branch
after `v0.15-phase-10-complete-fixup-2` (`c4719ae`), tagged
`v0.15-phase-10-complete-fixup-3`.*

**Test state.** Baseline at `v0.15-phase-10-complete-fixup-2`: 687 passed, 1
skipped, 11 deselected. After fix-up 3: **696 passed, 1 skipped, 11 deselected**
(`pytest tests/v0_15/ -q`) — +9 tests, no failures, no new skips. The one skip
is the pre-existing `RUN_LIVE_TESTS`/`RUN_LIVE_KB`-gated cold-start test; the 11
deselected are the calibration corpus runner (collected only under
`--run-calibration`).

This fix-up resolves **D19** (the KB verifier ignored `slot_to_qualifier`, so
the two inverse-mapped seed predicates abstained on every claim) and confirms
**D18** out of Phase 10.5 scope (it is a `/chat`-deployment defect that the
benchmark and calibration paths do not touch). With D19 resolved, the last named
capability gap that would distort Phase 10.5's measurement is closed.

---

## Phase 0 — scope check

Two scope checks ran before any code change; `docs/v0_15/fixup3_scope.md` records
them in full (committed at `b8dcb23`).

- **Scope check 1 (D19 inverted-seed count): 2.** `capital_of` (P36) and
  `mother_of` (P25) are the only entries in the 61-entry seed pack with an
  inverted `slot_to_qualifier` — both use the exact standard inversion
  `{"subject": "statement_value", "object": "statement_subject"}`. There are
  **zero** qualifier-based subject/object mappings (the `qualifier:Pxxx` values
  in the seed pack only appear on extra keys — `org`, `degree`, `start`, `end`,
  `year`, `point_in_time`, `valid_from`, `valid_until` — never on
  `subject`/`object`). The small count put D19 in the "a few targeted tests
  suffice" band.
- **Scope check 2 (D18 routing): `AedosRunner` builds the pipeline directly.**
  `AedosRunner.run_case` unpacks `extractor, walker, aggregator` from a pipeline
  built via `build_pipeline` and calls them directly (`benchmark.py:189,194,
  203-204`; pipeline built at `benchmark.py:472`/`435`). It never imports
  `app.py`, instantiates a FastAPI `TestClient`, or touches `ChatWrapper`.
  **D18 is therefore out of Phase 1 scope** — see Cluster D18 below.

---

## Cluster summaries

### Cluster D19 — KB verifier honors `slot_to_qualifier` — commit `6a6a466`

**Found.** `KBVerifier.verify` resolved `claim.subject`, called
`lookup_statements(subject_id, kb_property)`, and compared the result against
`claim.object` — for *every* predicate, with no reference to
`meta.slot_to_qualifier`. For an inverse predicate whose seed maps the Aedos
subject to the KB `statement_value` (`capital_of` on P36, `mother_of` on P25),
the KB keys the statement on the *other* entity. The verifier looked statements
up on the wrong entity, got nothing, and returned `NO_MATCH` — so every
inverse-predicate claim silently abstained, regardless of truth. Concretely, for
`capital_of(Berlin, Germany)` the verifier resolved Berlin and called
`lookup_statements(Q_Berlin, P36)`; Berlin holds no P36 statement, so the result
was `NO_MATCH` even though `Germany P36 Berlin` is in the KB.

**Fixed.** A new module-level helper `_lookup_targets(claim, meta)` reads
`meta.slot_to_qualifier` and returns `(kb_lookup_ref, expected_value_ref,
lookup_inverted)`:

- **Standard mapping** (`subject → statement_subject`): keys the lookup on the
  claim's subject; the object is the expected value. `(claim.subject,
  claim.object, False)`.
- **Inverse mapping** (`subject → statement_value`, `object →
  statement_subject`): keys the lookup on the claim's *object*; the subject is
  the expected value. `(claim.object, claim.subject, True)`.
- **Null/absent `slot_to_qualifier`**: treated as the standard mapping — this is
  the exact pre-D19 default, preserved so every non-inverse predicate (and any
  inline-generated row without an explicit map) behaves identically to before.
- **Uninterpretable mapping** (a qualifier-keyed or contradictory
  subject/object map): returns `None`. `verify` turns that into a `NO_KB_PATH`
  abstention with a `unsupported_slot_to_qualifier` trace note — it never
  guesses a direction and never raises. No seed has such a map; this branch
  guards only malformed inline-generated rows.

`verify` was rewritten to call `_lookup_targets` after the routing check, then:
resolve the KB lookup entity (the entity the statement is keyed on); resolve the
expected-value entity — the M4 object-resolution logic, now applied to whichever
Aedos slot is the KB statement value; look up statements on the lookup entity;
and compare. `_compare_positive` is **direction-agnostic** (it compares the
expected value against statement values regardless of which slot it came from)
and was left byte-identical — verified by reading it. The trace records the new
field `lookup_inverted: bool`; its `entity` / `object_value` / `object_resolved`
/ `subject_*` fields name KB *statement* positions (statement subject, statement
value), which coincide with the Aedos subject/object for a standard predicate
and are swapped for an inverse one, with `lookup_inverted` as the disambiguator.

The change is contained entirely to `kb_verifier.py`. The walker reads only
`KBVerdict.verdict` and `KBVerdict.subject_kb_id`; `subject_kb_id` stays
semantically "the KB statement subject id" (now correctly the lookup entity for
an inverse predicate, e.g. Germany for `capital_of(Berlin, Germany)`), so the
walker needs no change.

**Tests.** `tests/v0_15/integration/test_inverse_predicate_kb.py` (new, +8) —
loads the *actual* 61-entry seed pack and runs the KB verifier against
`capital_of`, `has_capital`, `mother_of`, and `born_in` with an **entity-keyed**
MockKB (a lookup against the wrong entity returns `[]` — this is what makes the
tests discriminate the fix). `test_kb_verifier.py` +1
(`TestKBVerifierInverseMapping` — the uninterpretable-mapping abstain path; the
unit-file MockTransport gained an additive `slot_to_qualifier` parameter).

### Cluster D18 — confirmed out of Phase 10.5 scope (no code change)

Phase 0 scope check 2 established that `AedosRunner.run_case` builds the
verification pipeline directly via `build_pipeline` and calls
`extractor.extract` / `walker.walk` / `aggregator.aggregate` itself — it does
not route through the `/chat` FastAPI endpoint. D18 (the chat-wrapper's stale
`extract` signature, which makes `/chat` extract zero claims) therefore does not
touch the benchmark's `AedosRunner` or the calibration corpus runner. **Phase
10.5's medium-bar measurement and calibration are honest** — they exercise the
verification pipeline, not the broken chat path.

Per the task's conditional scope, **Cluster D18 was skipped.** D18 remains a
v0.16 delta: the `/chat` deployment is still verification-inert and must be
fixed before the chat-wrapper is used, but that is a deployment-layer concern
(architecture §4.6) outside Phase 10.5. `v0_16_plan_deltas.md` D18 is updated to
record this confirmation.

---

## Finding-by-finding status

Every finding from the audit (C1, C2, M1–M6, m1–m8), the re-audit (N1–N7), and
the fixup-2 deltas (D18, D19), with status as of `v0.15-phase-10-complete-fixup-3`.

| Finding | Severity | Status | Resolved in |
|---|---|---|---|
| C1 — KB verifier ignores polarity (false verified) | Critical | Fixed | fix-up 1 |
| C2 — walker subsumption traversal stubbed | Critical | Fixed | fix-up 1 |
| M1 — consistency check never wired | Major | Fixed | fix-up 1 |
| M2 — retraction propagation inert | Major | Partially resolved — cascade + `ContradictionTracer` wiring are v0.16 D14/D15; does not block 10.5 | fix-up 1 (partial) |
| M3 — walker mislabels negated claims; dead conflict code | Major | Fixed | fix-up 1 |
| M4 — KB verifier object resolution + single-valued | Major | Fixed | fix-up 1 (code) + fix-up 2 (seed backfill) |
| M5 — runbook non-executable; weakened thresholds | Major | Fixed | fix-up 1 (Steps 3/4) + fix-up 2 (Step 6) |
| M6 — walker under-tested; run-log misquote | Major | Fixed | fix-up 1 |
| m1 — extraction_corpus 57 vs ≥60 | Minor | Deferred — v0.16 D11 | — |
| m2 — ambiguity-doc discipline degraded | Minor | Deferred — process/docs | — |
| m3 — a v0.14 file modified | Minor | Fixed | fix-up 1 |
| m4 — live-KB test file not created | Minor | Deferred — calibration runner exercises live KB | — |
| m5 — SPARQL fixtures carry synthetic field | Minor | Deferred — Phase 10.5 regenerates fixtures | — |
| m6 — `audit_log_entries` stubbed | Minor | Fixed | fix-up 1 |
| m7 — Phase 3 tests / Tier U stage-2 stub | Minor | Deferred — feature, not cleanup | — |
| m8 — run-log / runbook count drift | Minor | Fixed | fix-up 1 |
| N1 — false-contradiction on object-resolution failure | Major | Fixed | fix-up 2 |
| N2 — degenerate `cross_source` test | Major | Addressed — test made honest; capability gap is v0.16 D5 | fix-up 2 |
| N3 — seed count 61 vs 65 | Minor | Fixed | fix-up 2 |
| N4 — KB-grounded verdicts invisible to propagation | Minor | Deferred — v0.16 D13 | — |
| N5 — consistency check flags inverse seeds | Minor | Fixed | fix-up 2 |
| N6 — no migration for `single_valued` column | Minor | Fixed | fix-up 2 |
| N7 — thresholds duplicated across files | Minor | Fixed | fix-up 2 |
| **D18 — chat-wrapper stale `extract` signature** | — | **Confirmed out of Phase 10.5 scope; remains v0.16-scope** (`/chat` deployment fix) | — (v0.16) |
| **D19 — KB verifier ignores `slot_to_qualifier`** | — | **Fixed** | **fix-up 3 (`6a6a466`)** |

No finding is `Blocked`. `docs/v0_15/fixup3_blockers.md` was not created — the
single cluster's tests did not fail in a way that could not be resolved.

The two items still open going into Phase 10.5 are **M2** (the retraction
cascade and `ContradictionTracer` pipeline integration — v0.16 D14/D15) and the
**N2/D5 cross-source capability gap** (the walker cannot enumerate KB-sourced
taxonomy neighbors). Both are over-time-soundness / capability gaps that the
re-audit and fixup-2 already established do not block honest calibration; they
are not re-litigated here.

---

## Verification — stash-and-verify

The stash-and-verify discipline was followed for Cluster D19: the new tests were
confirmed to **fail against the pre-fix `kb_verifier.py`** and **pass after**. A
test green both before and after does not exercise the fix.

`git stash push -- src/aedos_v0_15/layer4_sources/kb_verifier.py` reverts
`kb_verifier.py` to its fixup-2 (`c4719ae`) revision while leaving the new and
modified test files in place — exactly the pre-fix state. Running the nine D19
tests against it:

- **8 failed.** The seven discriminating integration tests
  (`test_capital_of_correct_claim_is_verified`,
  `test_capital_of_wrong_functional_value_is_contradicted`,
  `test_capital_of_and_has_capital_are_symmetric`,
  `test_capital_of_unresolvable_capital_abstains`,
  `test_capital_of_unresolvable_country_abstains`,
  `test_born_in_records_lookup_inverted_false`,
  `test_mother_of_inverted_multivalued_is_verified`) plus the unit test
  (`test_unsupported_slot_to_qualifier_is_no_kb_path`). The failures are the
  exact shape of the D19 defect: `capital_of`/`mother_of` claims return
  `no_match` instead of `verified`/`contradicted` (the wrong-entity lookup);
  the uninterpretable-mapping case returns `verified` instead of `no_kb_path`
  (pre-fix `slot_to_qualifier` is ignored entirely).
- **1 passed.** `test_born_in_standard_path_still_verified` — the standard-path
  regression guard. It passes both pre-fix and post-fix by design: `born_in` is
  a standard predicate and D19 must not alter its verdict behavior. It guards
  against a regression; it does not discriminate the fix, and its docstring
  says so.

After `git stash pop`, all nine D19 tests pass, and the full suite is **696
passed, 1 skipped, 11 deselected** — +9 over the fixup-2 baseline of 687, with
no failures and no new skips. The +9 is exactly the eight new integration tests
plus the one new unit test; no test was removed or weakened (`test_kb_verifier.py`'s
only change is the additive `slot_to_qualifier` parameter on the `MockTransport`
helper, which defaults to `None` — every pre-existing test is unaffected).

---

## Cluster D19 — semantic decisions

**The two inverse seeds and how each was handled.** Both `capital_of` (P36,
`single_valued=1`) and `mother_of` (P25, `single_valued=0`) carry the seed map
`{"subject": "statement_value", "object": "statement_subject"}` — the exact
subject/object inversion. `_lookup_targets` recognizes this shape and returns
`(claim.object, claim.subject, True)`: the lookup is keyed on the Aedos object
and the Aedos subject is the expected value. No per-predicate special-casing —
the helper is driven entirely by the seed's `slot_to_qualifier`, so any future
inverse-mapped predicate (seeded or inline-generated) is handled by the same
path. `capital_of` exercises the inverse × functional combination
(`single_valued=1`, so a resolved value mismatch is a genuine `CONTRADICTED`);
`mother_of` exercises inverse × multi-valued (`single_valued=0`, so a mismatch
is `NO_MATCH`). Both are covered end-to-end against the real seed pack.

**Qualifier-based mappings — none exist; nothing deferred.** The task flagged
qualifier-keyed subject/object mappings as a possible v0.16 deferral. Scope check
1 established the seed pack has **zero** of them: every `subject`/`object` key
maps to `statement_subject` or `statement_value`, and the `qualifier:Pxxx`
values appear only on auxiliary keys. There was nothing to implement or defer.
`_lookup_targets` still handles a hypothetical qualifier-keyed
subject/object — it returns `None`, and `verify` abstains with a
`NO_KB_PATH` / `unsupported_slot_to_qualifier` trace note. This is the honest
"skip with a clear trace note" the task prescribes — and crucially **not** a
`NotImplementedError`: an uninterpretable mapping must abstain, never crash a
verification at runtime.

**`null` `slot_to_qualifier`.** One seed (`lives_in`) has
`slot_to_qualifier: null`, but its `routing_hint` is `user_authoritative`, so
`verify` returns `NO_KB_PATH` at the routing guard before `_lookup_targets` is
reached. Defensively, `_lookup_targets` treats a null/absent map as the standard
mapping — this is the pre-D19 behavior (the old code ignored `slot_to_qualifier`
and always assumed standard), so non-inverse predicates and inline-generated
`kb_resolvable` rows without an explicit map are unchanged.

**N5 interaction — verified consistent.** Fixup-2 Cluster C made the consistency
checker's `transitive_equivalence_violation` rule direction-aware
(`_is_inverse_mapping`), so `capital_of`/`has_capital` (both on P36, inverted
maps) are treated as a compatible inverse pair, not a conflict. D19 does not
touch `consistency.py`, so that exemption is unchanged. D19 makes both
predicates produce *real verdicts* rather than silently abstaining, so the two
must now also agree: `test_capital_of_and_has_capital_are_symmetric` confirms
`capital_of(Berlin, Germany)` and `has_capital(Germany, Berlin)` both reach
`VERIFIED` against the same KB statement `Germany P36 Berlin` — the inverse
predicate (lookup keyed on the object) and the standard predicate (lookup keyed
on the subject) converge on the same verdict and the same KB statement.

**N1 interaction — verified preserved on both paths.** N1 (fixup-2) makes a
functional-predicate value mismatch abstain rather than contradict when the
expected-value reference failed to resolve. D19 changes *which Aedos slot* is
the expected value for an inverse predicate (the subject, not the object), so
N1's resolution-failure-abstain now applies to the inverted slot.
`test_capital_of_unresolvable_capital_abstains` confirms `capital_of("FooCity",
Germany)` with an unresolvable "FooCity" returns `NO_MATCH` (abstention reason
`object_unresolved`), **not** a false `CONTRADICTED` — the inverse-direction
analogue of fixup-2's coupling check. The standard-path N1 behavior is unchanged
(all four fixup-2 `TestKBVerifierN1ResolutionFailure` tests still pass).

**Trace field naming — kept, not renamed.** The task noted the trace's
`object_unresolved` / `subject_unresolved` reasons could be renamed. They were
**kept**: read as KB-*statement*-relative (`subject` = the statement subject /
lookup entity; `object` = the statement value side / expected value), the
existing names are correct under both directions — they only coincided with the
Aedos slot names for standard predicates by happenstance. The new
`lookup_inverted: bool` field is the explicit disambiguator a Phase 10.5
debugger needs. Keeping the names means zero churn to the four fixup-2 N1 tests
(and `test_kb_path.py` / `test_seed_single_valued_kb.py`), zero change to the
walker, and a byte-identical standard-path trace plus the additive
`lookup_inverted` key. The `verify` docstring documents the KB-statement-relative
reading. A v0.16 delta (D20, below) records the option to rename for clarity if
a future audit prefers it.

**The architectural language change** D19 implies — that the KB verifier's
lookup direction is governed by `slot_to_qualifier` rather than always assuming
the Aedos subject is the KB statement subject — is recorded as a v0.16 delta and
was **not** written into the architecture document this session.

---

## v0.16 deltas updated

`docs/v0_15/v0_16_plan_deltas.md` was updated:

- **D19** — marked **Resolved (fix-up 3)**. The KB verifier now honors
  `slot_to_qualifier`; inverse-predicate KB claims (`capital_of`, `mother_of`)
  produce real verdicts. The note that Phase 10.5 `kb_mapping`/`derivation`
  cases over these predicates would abstain is removed — they no longer will.
- **D18** — annotated: scope check 2 confirmed D18 does not affect the benchmark
  or calibration paths, so it does **not** block Phase 10.5. It remains
  v0.16-scope — the `/chat` chat-wrapper deployment is still verification-inert
  and needs the `ExtractionContext` signature fix plus an end-to-end `/chat`
  test.
- **D20** (new) — optional v0.16 cleanup: rename the KB verifier trace's
  direction-ambiguous fields (`object_resolved` / `subject_unresolved` /
  `object_unresolved`) to direction-neutral names, and update the architecture
  §5.2 / §6.2 wording to state that `slot_to_qualifier` governs the KB lookup
  direction. fix-up 3 kept the names (correct when read as KB-statement-relative,
  disambiguated by `lookup_inverted`) to avoid churn; a future audit may prefer
  the explicit rename.

---

## End state

D19 is resolved: the KB verifier honors `slot_to_qualifier`, the two inverse
seed predicates produce real verdicts, and the `lookup_inverted` trace field
makes the lookup direction auditable. D18 is confirmed out of Phase 10.5 scope —
the benchmark and calibration runners build the pipeline directly and never
touch the broken `/chat` path. The mocked suite is clean (696 passed, 1 skipped,
11 deselected) with no new skips; the stash-and-verify confirms the D19 tests
genuinely fail against the pre-fix state. With D19 closed, the documented
capability gaps that would have distorted Phase 10.5's measurement are resolved
— the remaining open items (M2 cascade / `ContradictionTracer`, N2/D5
cross-source unification) are v0.16 deltas the prior re-audits already cleared
as non-blocking for honest calibration.

Tagged `v0.15-phase-10-complete-fixup-3`.
