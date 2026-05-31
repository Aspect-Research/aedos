# Phase C — Plan

The last polish session on Aedos v0.15 before Phase 10.5 calibration. Two
contained clusters — C1 (architecture wording for the B2/B3 changes) and C2
(audit-logging unification for the oracles) — plus C3 (planning-doc update +
report). Phase C is **hygiene, not capability extension**: the architecture
document is brought into agreement with the code Phase B landed, and the
audit-logging pipeline gets end-to-end visibility for events the architecture
already specifies. No new audit event types. No verdict-behaviour change.
Deferred deltas (D5, D9, D10, D13, D14, D15) stay deferred; the seed pack (D23)
is not touched.

Baseline: `v0.15.0-rc.3`. Three cluster commits; tag `v0.15.0-rc.4`.

---

## Cluster C1 — architecture §6.1 + §6.4 (D21)

Doc-only. Align `docs/architecture.md` with `tier_u.py` (post-B3) and
`walker.py` (post-B2). The plan doc is committed with C1, matching Phase B's
pattern (`phase_B_plan.md` committed with B1).

### §6.1 — Tier U write-path

Current wording: *"Contradictions (asserting party previously asserted X, now
asserts not-X) close the prior row by setting `valid_until = now()` and write a
new row."* — describes only a same-object polarity flip; silent on object
conflict.

What `tier_u.write()` actually does (read post-B3):

- **Idempotency** — an exact match on the 5-tuple `(asserting_party, subject,
  predicate, object, polarity)` of a non-retracted row → return that row; no new
  row, no closure.
- **Closure — only on a genuine contradiction**, one of two sub-cases:
  - (a) a prior row with the **same object** at the **opposite polarity** →
    close (`valid_until = now()`). Any cardinality. Direct negation.
  - (b) prior **positive** rows with a **different object**, when the new claim
    is **positive** and the predicate is **functional** (`single_valued = 1`,
    consulted via the predicate-translation oracle) → close. Functional belief
    revision.
- **Parallel write — everything else**: multi-valued different object (parallel
  assertion); different object at opposite polarity (the contrastive-correction
  "X, not Y" shape); both-negative different object. New row written, prior
  stays open.
- `single_valued` absent an oracle / on consult failure → predicate treated
  multi-valued (the §5.2 conservative default — never a false closure).

The rewrite describes the **code**, not the prompt's pre-implementation
four-case framing. Two refinements the code makes that the four-case framing
did not: case (b) requires **both polarities positive** (not "same polarity" —
a both-negative different-object pair is parallel), and case (a) requires the
**same object** (not merely the same party/subject/predicate — a different-object
opposite-polarity pair is parallel). This is the B3 report's "close iff genuine
contradiction" rule.

Noted minor correction folded in: the current §6.1 also claims
*"Different-context-same-content … updates source_context history without
duplicating."* `tier_u.write()` does not implement per-context `source_context`
merging — idempotency is a flat 5-tuple match. This pre-existing inaccuracy is
corrected (the clause dropped) while the write-path paragraph is rewritten for
D21. Flagged here, not silent.

### §6.4 — Walker belief revision

**Ambiguity surfaced.** §6.4 currently contains **no belief-revision text at
all**. Its subsections are Algorithm, Default depth, Cycle detection, Polarity
tracking, Predicate distribution gating, Inline row generation, Resource
budgets, Justification trace, Termination, Multiple successful chains. The only
belief-revision mention in the whole document is §8.1's one-line bullet
("Cross-context belief revision via Tier U … contradictions across context
detected via lookup"); the walker code comments cite "architecture 8.1". So
C1's §6.4 work **adds a new "Belief revision" subsection** rather than editing
existing wording. Proceeding with that interpretation — §6.4 is the walker
section and the walker (`_direct_lookup`) is where both belief-revision paths
live.

The new subsection states what `walker._direct_lookup` does (read post-B2):

1. **Polarity belief revision** — the claim's exact negation (same
   subject/predicate/object, opposite polarity) is a currently-valid,
   non-retracted Tier U row → `contradicted`. Trace marker
   `belief_revision: polarity_conflict`. Fires for either claim polarity.
2. **Object-conflict belief revision** (new in v0.15, B2/D16) — a **positive**
   claim, a currently-valid **positive** Tier U row with the same
   `(party, subject, predicate)` and a **different** object, the predicate
   **functional** (`single_valued = 1`) → `contradicted`. Trace marker
   `belief_revision: object_conflict`.

The wording will make explicit: object-conflict fires **only** for functional
predicates — a multi-valued predicate with a different Tier U value is a
parallel belief, not a contradiction; and a negated claim against a Tier U
positive assertion with a different value does **not** produce a verified
verdict by `single_valued` entailment in v0.15 — that direction was left a
conservative abstention (Phase B Decision 1), a v0.16 candidate.

### §5.4 / §7.3 / §8.1 — checked, not lagging

Per the prompt's "surface if lagging" instruction:

- **§5.4** (substrate-internal consistency checks) — B3a wired Tier U's
  write-path audit events; those are write-path observability, not
  consistency-check events. §5.4 describes the consistency checker's events
  only. No edit needed.
- **§7.3** (over-time soundness) — silent on mechanism; B4's audit-log replay
  satisfies it as written (Phase B report confirms). No edit needed.
- **§8.1** belief-revision bullet — generic, remains accurate at its level; out
  of C1's named scope (§6.1/§6.4). Not edited.

Commit: `Cluster C1: architecture §6.1 and §6.4 reflect post-Phase-B semantics (D21)`

---

## Cluster C2 — oracle audit-logging unification (D22)

### Step 1 — systematic grep (run during planning; re-confirmed and a scope check-in raised before any fix)

`audit_log` / `self._audit` / `self.audit_log` across `src/aedos/`. **10
modules** carry an `audit_log=None` constructor parameter `build_pipeline`
never sets. They split into two classes:

**Class 1 — the 3 named oracles** (`predicate_translation.py`, `subsumption.py`,
`predicate_distribution.py`): parameter + `self._audit` attribute + `log_event`
calls **gated on `if self._audit is not None`**. `self._audit` is always `None`
in the deployed pipeline, so the events (`row_created`, `row_retracted`,
`row_generation_failed`) are **inert**. This is D22 proper — a functional
defect: architecture-specified observability that does not fire.

**Class 2 — 7 further modules** (`router.py`, `aggregator.py`,
`kb_verifier.py`, `kb_wikidata.py`, `python_verifier.py`, `resolver.py`,
`walker.py`): parameter + `self._audit` attribute, but **`self._audit` is never
read** (content-mode grep shows only the assignment line). `aggregator.py` logs
`verdict_recorded` **unconditionally** via `self._db` (its logging already
works); the other six call `log_event` **nowhere**. In Class 2 the `audit_log`
parameter is **pure dead residue** — removing it has **zero behavioural
effect**.

Not found: `self.audit_log` (dotted) — no matches; the old
`self._audit.log(...)` method form — no matches (A4 converted those). A4-fixed
modules (`consistency.py`, `retraction.py`, `contradiction_tracer.py`) and
B3a-fixed `tier_u.py` are clean.

`log_event` accepts any `event_type` string (no enum/whitelist in
`audit/log.py`). The oracle events are already supported — **no `log_event`
extension is required** for C2.

### The scope question — check-in before fixing

The grep found 7 modules beyond the 3 named oracles — exactly the case the
prompt said to pause on. Two options:

- **Option 1 — named oracles only.** Fix the 3 oracles per A4; record the 7
  Class-2 dead parameters as a new v0.16 delta. The final-verification grep does
  **not** come back clean; the "pattern stops here" goal is not met — a fourth
  round is guaranteed.
- **Option 2 — all 10 modules (recommended).** Fix the 3 oracles (functional —
  events fire; tests + stash-and-verify) **and** strip the dead `audit_log`
  parameter + `self._audit` from the 7 Class-2 modules (mechanical 2-line
  removals, zero behavioural change; no caller passes `audit_log` —
  `build_pipeline`, `app.py`, `chat_wrapper.py` and the test suite confirmed
  clean). The grep then returns genuinely clean; the "we said we got X, missed
  some" pattern ends here, which is C2's stated thesis, and the
  final-verification "zero matches" criterion becomes satisfiable.

Recommendation: **Option 2**. Raised as an `AskUserQuestion` check-in at the
start of C2 before any code change.

### Step 2 — apply the A4 pattern

For each of the 3 oracles: remove the `audit_log` parameter + `self._audit`;
replace `if self._audit is not None: log_event(self._db, …)` with a direct
`log_event(self._db, …)`. Under Option 2, also delete `audit_log=None` +
`self._audit = audit_log` from the 7 Class-2 modules.

### Step 3 — tests

`test_predicate_translation.py` constructs the oracle with `audit_log=audit_log`
at 4 sites (`TestAuditLog` ×3 + `test_error_logged_when_audit_present`) — the
dead argument is dropped; the tests remain valid as regression guards for the
now-unconditional logging (`test_error_logged_when_audit_present` →
`test_error_logged`). `subsumption` and `predicate_distribution` have no
audit-log tests today.

3 new tests, one per oracle, exercising the **deployed pipeline**
(`build_pipeline`): consult an unseen predicate / entity-pair /
distribution-tuple, query `audit_log`, assert the `row_created` event was
written with the correct `event_subject` prefix. Stash-and-verify: against rc.3
each new test fails (gated branch, `self._audit is None`); post-fix each passes.
Expected suite delta: +3 (rc.3 baseline → +3).

Commit: `Cluster C2: oracle audit-logging unified on log_event (D22, A4 pattern finished)`

---

## Cluster C3 — planning update + report

`docs/v0.16_planning.md`: D21 and D22 → **Resolved (Phase C)** with commit
references (D17's "Resolved" note is the template); original entries kept. D23
reworded to *deferred to Phase 10.5 data* — it waits on empirical pressure from
calibration, not on v0.16 design time.

`docs/phase_C_report.md`: cluster summaries (C1, C2); per-delta before/after
(D21, D22); the C2 Step-1 grep result (what was found, what was in scope, what
was already clean); confirmation that no `log_event` event type needed adding;
test-count delta; confirmation that no audit-logging vestigial-flag pattern
remains in `src/aedos/`.

Commit: `Cluster C3: planning update + Phase C report`

---

## Final verification

`pytest tests/ -q`; `python -m tests.evaluation.benchmark --validate-harness`
(PASS); `pytest --run-calibration` (11-corpus dry-run); `git log --oneline
v0.15.0-rc.3..HEAD` (3 cluster commits); re-run the C2 Step-1 greps against the
post-C2 tree (clean under Option 2). Tag `v0.15.0-rc.4`. Fallback start point
for a Phase 10.5 anomaly remains `v0.15.0-rc.2` (Phase C is doc + hygiene; it
does not move the fallback).

---

## Ambiguities surfaced

1. **§6.4 has no existing belief-revision text** — C1 *adds* a subsection
   rather than editing one. Proceeding.
2. **C2 scope** — 3 named oracles vs. 10 modules. `AskUserQuestion` check-in at
   C2 Step 1; recommending Option 2.
3. **§6.1 `source_context` clause** — a pre-existing inaccuracy, corrected while
   the write-path paragraph is rewritten for D21.
4. **Four-case framing vs. code** — §6.1 describes the code's narrower "close
   iff genuine contradiction" rule, not the prompt's pre-implementation
   four-case list (per the discipline rule "wording should match code").
