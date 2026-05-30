# Phase A Cleanup — Plan

Six contained items from the v0.16 deltas, between `v0.15.0-rc.1` and Phase 10.5.
Three architecture-document edits (D4, D12, D20-part-2) and three code changes
(D7, D8, D18). Hygiene only — no capability extension. Clusters run A1 → A2 →
A3 → A4.

Baseline at `v0.15.0-rc.1`: `pytest tests/ -q` → **699 passed, 1 skipped, 11
deselected** (711 collected; the skip is the live-KB cold-start test, the
deselected 11 are the calibration corpus runner).

---

## Cluster A1 — architecture wording (D4, D12, D20-part-2)

Zero code risk. Three edits to `docs/architecture.md`.

**D4 — `single_valued` in §5.2.** The `predicate_translation` schema block omits
the `single_valued` column that fixup-2 added (`database.py:41`, plus the
idempotent `ALTER TABLE` migration guard at `database.py:133`). Add the column
to the schema block (placed before `reason`, matching the code's column order)
and a prose paragraph documenting semantics: `1` = functional / single-valued,
`0` = multi-valued; conservative default `0`; a wrong `1` produces false
contradictions, a wrong `0` only false abstains (the accepted §3.2 cost). Note
that the seed-file format carries the field. Reference
`docs/v0.15_build_log/fixup2_report.md` for the per-predicate classification
rationale; do not transcribe the 11-predicate functional list (seed-pack
content, not architecture).

**D12 — inverse-predicate exemption in §5.4.** The `transitive_equivalence_violation`
bullet describes the rule as direction-blind. fixup-2's `consistency.py`
(`_is_inverse_mapping`) made it direction-aware: two predicates mapping to the
same KB property with `slot_to_qualifier` maps that are exact subject/object
inversions (the `capital_of`/`has_capital` pattern) are compatible inverses, not
a conflict. Revise the bullet to state the exemption; any other form of
`slot_to_qualifier` divergence on the same KB property remains a conflict.

**D20 part 2 — `slot_to_qualifier` governs lookup direction in §5.2 and §6.2.**
fixup-3 made `KBVerifier` honor `slot_to_qualifier`'s subject/object mapping —
for an inverse predicate the KB lookup is keyed on the claim's *object*. Update
§5.2 to state that `slot_to_qualifier`'s `subject`/`object` keys indicate which
Aedos slot maps to the KB statement's subject and which to its value. Update
§6.2 (`lookup_statements`) to state the lookup is keyed on whichever Aedos slot
maps to the KB statement subject — the claim's subject *or* object depending on
`slot_to_qualifier`.

Verification: re-read §5.2, §5.4, §6.2 for internal consistency. If a deeper
wording issue surfaces, record it as a question rather than expanding scope.

Commit: `Cluster A1: architecture wording aligned with code (D4, D12, D20-part-2)`

---

## Cluster A2 — ChatWrapper ExtractionContext (D18)

`ChatWrapper.respond` (`deployment/chat_wrapper.py:96`) calls
`self._extractor.extract(draft, asserting_party=asserting_party)`. The current
`Extractor.extract` signature is `extract(text, context: ExtractionContext)`
(`layer1_extraction/extractor.py:109`). The `asserting_party=` keyword raises
`TypeError`, swallowed by `respond`'s `except Exception: claims = []` — so `/chat`
extracts zero claims and every response is pass-through.

`ExtractionContext` fields: `asserting_party` (required), `context_type`
(required), `turn_id`, `prior_conversation`, `document_id` (all optional).

**Fix.**
1. In `respond`, build `ExtractionContext(asserting_party=asserting_party,
   context_type="chat_user", turn_id=ctx_dict.get("conversation_id"))` and pass
   it as the second positional argument to `extract`.
2. Tighten the exception handler. **Decision: remove it entirely.** The broad
   `except Exception: claims = []` is exactly the construct that silently
   absorbed this `TypeError` across two release candidates; removing it is the
   honest choice — the next unexpected extraction failure surfaces immediately
   rather than degrading `/chat` to silent pass-through. A research-prototype
   chat endpoint returning a 500 on a genuine extraction bug is preferable to
   one that quietly verifies nothing. (Rationale recorded in the commit message.)

**Tests** — new `tests/integration/test_chat_wrapper.py`:
1. *End-to-end `/chat` claim extraction* (load-bearing for D18). Build a
   `ChatWrapper` with a real `Extractor` over a `MockTransport` whose `chat`
   returns a draft "Obama was born in Honolulu." and whose `extract_with_tool`
   returns a `born_in(Obama, Honolulu)` claim. Assert `claims` non-empty after
   `respond`. (`born_in` is in triage's `_ALWAYS_VERIFY`, so it survives the
   VERIFY filter; the draft contains both entities, so the hard-claim check
   passes.)
2. *`/chat` response shape* — assert the `ChatResponse` carries a
   `VerificationResult` with verification machinery populated (per-claim
   verdict/trace present for the extracted claim).

**Stash-and-verify.** Stash `chat_wrapper.py`, run the new tests against
`v0.15.0-rc.1`: test 1 must fail (claims empty — stale signature `TypeError`
swallowed by the broad except). Unstash → test 1 passes.

Commit: `Cluster A2: ChatWrapper passes ExtractionContext (D18)`

---

## Cluster A3 — remove predicate_equivalence walker edge (D7)

`Walker._expand_via_substrate` (`walker.py:323-342`) emits `predicate_equivalence`
edges via `predicate_translation.query_neighbors`. `TierU.lookup` stage 3
(`tier_u.py:205-220`) already broadens by the same oracle, and an equivalent
predicate shares the same `kb_property` so its KB lookup is identical — the edge
is redundant.

**Fix.**
1. Delete the predicate-equivalence block from `_expand_via_substrate` (the
   `try/except` around `query_neighbors`, lines 323-342). The subsumption
   traversal stays.
2. `query_neighbors` on `PredicateTranslation` remains used by `TierU._stage3`
   (`tier_u.py:208`) — keep the method.
3. Run the suite. Expected impact: `test_walker_failure_modes.py::
   TestFailureModePredicateTranslation::test_predicate_equivalence_substitution`.
   Note: that test seeds two predicates on P108 and a Tier U row under one of
   them — TierU stage 3 covers exactly this, so the test likely *still passes*
   after the walker edge is removed. If so, it tests predicate-translation
   broadening (a real, retained capability), not the removed edge — leave it.
   If it fails, it was exercising the removed edge — delete it. `test_trace.py:40`
   constructs a `TraceEdge("predicate_equivalence", …)` directly; that exercises
   the trace primitive (a free-form string), not the walker — leave it.

**Stash-and-verify.** After removal, restore the deleted edge: no test should
*require* the edge to exist (every test must pass identically with and without
it). If a test passes only with the edge restored, the post-removal test state
is wrong.

Commit: `Cluster A3: remove redundant predicate_equivalence walker edge (D7)`

---

## Cluster A4 — unify audit-logging on log_event (D8)

This is the highest-uncertainty cluster.

**Inventory.** Three classes call `self._audit.log(...)` as a method:
- `consistency.py` — `resolve_conflict` logs `consistency_violation`
  (lines 119-128); `_increment_circuit_breaker` logs `circuit_breaker_triggered`
  (lines 322-327).
- `retraction.py` — `propagate_retraction` logs `verdict_retracted` (lines 57-65).
- `contradiction_tracer.py` — `trace_contradiction` logs `contradiction_traced`
  (lines 68-77).

`audit/log.py` exposes no object with a `.log` method — only the module-level
`log_event(conn, event_type, event_subject, event_data, verification_context=None)`
function. In all three constructors `audit_log` defaults to `None` and **no
caller anywhere passes it** (verified: `build_pipeline` passes none;
`tests/integration/test_end_to_end.py` constructs all three without `audit_log`;
no test passes `audit_log=` to these three classes). So every `self._audit.log`
branch is dead, guarded by `if self._audit:` / `if self._audit and …`.

**Assessment of the caveat.** The events the dead branches try to log —
`consistency_violation`, `circuit_breaker_triggered`, `verdict_retracted`,
`contradiction_traced` — are exactly the events architecture §5.4 says the audit
log records ("All retractions, regeneration cycles, and circuit-breaker
triggerings are recorded"). They *should* be logged. `log_event` already
supports them: `event_type` is a free-form string and `event_data` is an
arbitrary dict, so no new event type and no schema change is needed. The fix is
therefore to make the logging real, not merely to delete it — and that requires
**no scope expansion**. No deferred decision is forced here; this is noted in
the report rather than surfaced as a blocking question.

**Fix (standardize on the `log_event` function form).**
1. Each of the three classes already holds a DB connection: `consistency._db`
   (required), `retraction._db` (defaults `None`; `build_pipeline` passes it),
   `contradiction_tracer._db` (defaults `None`; passed in `end_to_end`).
2. Replace each `self._audit.log(event_type=…, event_subject=…,
   event_data=json.dumps({…}))` with `log_event(self._db, event_type=…,
   event_subject=…, event_data={…})` — passing the dict directly, since
   `log_event` does its own `json.dumps`.
3. Guard on `self._db is not None` where `_db` is optional (retraction,
   contradiction_tracer); `consistency._db` is a required positional, call
   directly.
4. Remove the now-unused `audit_log` parameter from all three constructors
   (safe — no caller passes it). `ContradictionTracer`'s default-propagator
   construction `RetractionPropagator(db=db, audit_log=audit_log)` becomes
   `RetractionPropagator(db=db)`.
5. Import `log_event` from `..audit.log` in each module.

**Per-call-site classification (additive vs no-op cleanup).**
- `consistency_violation`, `circuit_breaker_triggered`, `verdict_retracted` —
  **additive**: `build_pipeline` wires these classes with a real `db`, so the
  new `log_event` calls write entries that the dead `self._audit.log` calls
  never produced.
- `contradiction_traced` — **additive when a `db` is provided** (the
  `end_to_end` construction), but **no behavior change in the deployed
  pipeline**: `ContradictionTracer` is not wired into `build_pipeline` (D15,
  deferred). The call site is now correct; whether it runs in production is
  D15's concern.

**Tests.** Run the existing suite. The dead paths were untested, so the count
should be roughly unchanged. Key check: no previously-passing test fails for a
missing audit entry the old dead path notionally produced (it produced none).

Commit: `Cluster A4: unify audit-logging on log_event function form (D8)`

---

## Final verification

1. `pytest tests/ -q` — clean. Expected count change: D18 adds 2, D7 removes 0–1,
   net ≈ +1 to +2 from the 699 baseline; documented in the report.
2. `python -m tests.evaluation.benchmark --validate-harness` → `PASS`.
3. `pytest --run-calibration` — 11-corpus harness dry-run clean.
4. `git log --oneline v0.15.0-rc.1..HEAD` — four cluster commits.

Mark D4, D7, D8, D12, D18, D20 (both parts) **Resolved (Phase A cleanup)** in
`docs/v0.16_planning.md` with commit references, keeping the original entries.
Write `docs/phase_A_report.md`. Tag the final commit `v0.15.0-rc.2`.

## Ambiguities surfaced

- **A2 exception handler** — the prompt offered narrow-and-log vs remove-entirely
  as both defensible. Chosen: **remove entirely** (honest; prevents the
  silent-swallow failure mode from recurring). Recorded here and in the commit
  rather than asked, since the prompt explicitly delegated the choice.
- **A4 deferred-decision check** — the dead branches *do* target events that
  §5.4 says should be logged, but `log_event` already supports them with no
  scope expansion, so there is no forced extend-vs-simplify decision. Noted, not
  blocked.
- No deeper architecture wording issue is expected from A1; if §6.2's qualifier
  prose needs broader revision than the D20-part-2 sentence, that will be raised
  before editing.
