# Aedos v0.15 — Fix-Up 3 Scope Check (Phase 0)

*Two pre-implementation scope checks that determine Phase 1's scope. Conducted
against `v0.15-phase-10-complete-fixup-2` (`c4719ae`). No code changes in this
commit.*

---

## Scope check 1 — D19 inverted-seed count

**Question.** How many of the 61 entries in `seeds/v0_15/predicate_translation.json`
have an inverted `slot_to_qualifier` map — Aedos subject → `statement_value`,
Aedos object → `statement_subject`?

**Answer: 2.** Both use the exact standard inversion
`{"subject": "statement_value", "object": "statement_subject"}` (no qualifier
keys, no extra keys).

| Predicate | KB property | `single_valued` | `slot_to_qualifier` |
|---|---|---|---|
| `capital_of` | P36 (capital) | 1 (functional) | `{"subject": "statement_value", "object": "statement_subject"}` |
| `mother_of` | P25 (mother) | 0 (multi-valued) | `{"subject": "statement_value", "object": "statement_subject"}` |

Both are `routing_hint = kb_resolvable`, so both reach the KB verifier and both
are affected by D19. Seed file lines: `capital_of` at line 142, `mother_of` at
line 226.

**Other categories (the "anything else" the task asked for).**

- **Qualifier-based subject/object mappings: 0.** Verified by grep — no seed maps
  its `subject` or `object` key to a `qualifier:Pxxx` value. The `qualifier:Pxxx`
  values that do appear (P642, P512, P580, P582, P585, P585…) are always on
  *additional* keys (`org`, `degree`, `start`, `end`, `year`, `point_in_time`,
  `valid_from`, `valid_until`), never on `subject`/`object`. The
  qualifier-based-lookup edge case the task plan flags as a possible v0.16
  deferral **does not occur in the seed pack** — there is nothing to defer.
- **`null` `slot_to_qualifier`: 1.** `lives_in` has `slot_to_qualifier: null`,
  but its `routing_hint` is `user_authoritative` — `KBVerifier.verify` returns
  `NO_KB_PATH` at the `routing_hint != "kb_resolvable"` guard
  (`kb_verifier.py:62`) before any `slot_to_qualifier` access. Not a D19 concern
  for the seed pack, but the D19 helper must still tolerate a `None`
  `slot_to_qualifier` defensively, because an inline-generated `kb_resolvable`
  predicate could carry one.
- **Standard mappings: 58.** All remaining `kb_resolvable` seeds use
  `{"subject": "statement_subject", "object": "statement_value", ...}`.

**Test coverage Phase 1 needs.** The count is small (2, well under the task's
"3–5 → a few targeted tests suffice" band). The five targeted tests in the task
plan — all built around `capital_of` — are sufficient: a `VERIFIED` case, a
`CONTRADICTED` case (functional mismatch), the `has_capital`/`capital_of`
symmetry check (N5), an inverted resolution-failure abstain, and a
standard-mapping regression guard. One additional test should exercise
`mother_of` (the inverted **multi-valued** predicate) so both seed entries are
covered and the inverted × `single_valued=0` interaction is exercised, not only
the inverted × `single_valued=1` path. No qualifier-based-mapping tests are
needed (no such seeds exist).

---

## Scope check 2 — D18 benchmark routing

**Question.** Does `AedosRunner.run_case` build the verification pipeline
directly via `build_pipeline`, or route through the `/chat` FastAPI endpoint in
`app.py`?

**Answer: it builds the pipeline directly and calls the components directly. It
never touches `/chat` or `ChatWrapper`.**

Evidence (`tests/v0_15/evaluation/benchmark.py`):

- `AedosRunner.__init__` takes a `pipeline` tuple; `run_case` unpacks it as
  `extractor, walker, aggregator = self._pipeline` (`benchmark.py:189`).
- `run_case` calls the components directly: `extractor.extract(case.statement, ctx)`
  (`benchmark.py:194`), `walker.walk(c, vctx)` (`benchmark.py:203`),
  `aggregator.aggregate(claims, results)` (`benchmark.py:204`).
- The pipeline tuple is sourced from `build_pipeline(...)`:
  `_run_live` builds it at `benchmark.py:472` (`build_pipeline(open_db(db_path))`),
  passing `(pipeline.extractor, pipeline.walker, pipeline.aggregator)` to
  `AedosRunner` (`benchmark.py:473`); `_validate_harness` does the same at
  `benchmark.py:435,437`.
- There is no import of `app.py`, no FastAPI `TestClient`, and no `ChatWrapper`
  reference anywhere in `benchmark.py`.

**D18 is purely a chat-wrapper (`/chat` deployment) issue and stays a v0.16
delta. D18 is OUT of Phase 1 scope.** Phase 10.5's medium-bar measurement runs
through `benchmark.py`'s `AedosRunner`, which exercises the verification pipeline
(extractor → walker → aggregator) directly — the same path the calibration
corpus runner uses. The broken `/chat` `extract` signature does not touch that
path, so Phase 10.5's calibration measurement is honest. This matches the v0.16
deltas file's own D18 note ("the calibration corpora and the medium-bar
evaluation run through the runner/walker directly … the deployment layer is not
part of the architecture §4.6").

---

## Phase 1 plan

Phase 1 is a **single cluster, D19** — D18 is confirmed out of scope by check 2.
The KB verifier (`kb_verifier.py`) currently resolves `claim.subject`, looks up
`lookup_statements(subject_id, kb_property)`, and compares against `claim.object`
for *every* predicate, ignoring `meta.slot_to_qualifier`. For the 2 inverted
seeds (`capital_of`, `mother_of`) this queries the wrong KB entity and always
returns `NO_MATCH`. The fix adds a `_lookup_targets(claim, meta)` helper that
reads `slot_to_qualifier` and returns `(kb_lookup_entity, expected_value_entity,
lookup_inverted)`: standard maps return `(subject, object, False)`, inverted maps
return `(object, subject, True)`, and any other shape is surfaced honestly
(abstain with a clear trace note, since no such seed exists and a crash would be
wrong). `verify` resolves and looks up against `kb_lookup_entity`, resolves and
compares against `expected_value_entity`, and records `lookup_inverted` in the
trace; the N1 resolution-failure-abstain logic continues to apply but now to
whichever Aedos slot is the expected value, with the trace's abstention reasons
kept meaningful (rename `subject_unresolved`/`object_unresolved` to
direction-neutral names). Six tests cover both seed predicates plus a
standard-path regression guard; stash-and-verify confirms tests 1–4 fail against
the fixup-2 `kb_verifier.py` and the regression guard passes both ways. Then
`fixup3_report.md`, v0.16 deltas update, and tag `v0.15-phase-10-complete-fixup-3`.
