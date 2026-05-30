# Phase C — Report

The last polish session on Aedos v0.15 before Phase 10.5 calibration. Phase C
is **hygiene, not capability extension**: C1 brings the architecture document
into agreement with the code Phase B landed (D21), and C2 finishes the
audit-logging unification the A4 / B3a cleanups began (D22). No verdict
behaviour changed; no new audit event type was introduced; no deferred delta
was pulled forward.

Three cluster commits, run C1 → C2 → C3:

```
e0bbc4c  Cluster C1: architecture §6.1 and §6.4 reflect post-Phase-B semantics (D21)
65833f0  Cluster C2: oracle audit-logging unified on log_event (D22, A4 pattern finished)
<C3>     Cluster C3: planning update + Phase C report
```

The plan is `docs/phase_C_plan.md` (committed with C1).

---

## Cluster summaries

### C1 — architecture §6.1 and §6.4 (D21)

Doc-only. `docs/architecture.md` was brought into agreement with `tier_u.py`
(post-B3) and `walker.py` (post-B2).

**§6.1 — Tier U write-path.** The old wording described contradiction-closure
as a single same-object polarity flip ("asserted X, now asserts not-X"). The
rewrite describes the rule `tier_u.write()` actually implements (B3's "close
iff genuine contradiction"): idempotency on the exact 5-tuple; closure in two
cases only — (a) same object at opposite polarity, any cardinality; (b)
different object at *both-positive* polarity for a *functional* predicate; and
a parallel write for every other difference (multi-valued addition, the
contrastive-correction "X, not Y" shape, both-negative). The wording states
that closure depends on `single_valued` consulted via the predicate-translation
oracle, with the §5.2 conservative multi-valued default. The rewrite is
narrower than the prompt's pre-implementation four-case framing in two places —
case (b) requires *both polarities positive* (not "same polarity"), and case
(a) requires the *same object* (not merely the same party/subject/predicate) —
because that is what the code does.

The rewrite also dropped a pre-existing unsupported clause — the old §6.1
claimed "different-context-same-content … updates source_context history
without duplicating", which `tier_u.write()` does not implement (idempotency is
a flat 5-tuple match). A pre-existing inaccuracy, corrected while the paragraph
was being rewritten for D21; flagged here, not dropped silently.

**§6.4 — walker belief revision.** Finding worth recording: §6.4 carried **no
belief-revision text at all** before C1. Its subsections covered the walk
mechanics (algorithm, depth, cycle detection, polarity tracking, distribution
gating, …); the only belief-revision mention in the entire document was §8.1's
one-line bullet, and `walker.py`'s code comments cited "architecture 8.1". C1
therefore **added** a new **Belief revision** subsection (rather than editing
existing wording) describing both paths: polarity belief revision (existing —
the claim's exact negation is a currently-valid Tier U row) and object-conflict
belief revision (new in v0.15 via B2 — a positive claim, a functional
predicate, a currently-valid positive Tier U row with a different object). The
subsection makes explicit that object-conflict fires *only* for functional
predicates, and that the negated-claim direction is a deliberate conservative
abstention (Phase B Decision 1).

**§5.4 / §7.3 / §8.1 checked, not lagging.** §5.4 describes the consistency
checker's audit events, not Tier U write-path events — no edit. §7.3 is silent
on retraction *mechanism*, so B4's audit-log replay satisfies it as written —
no edit. §8.1's belief-revision bullet is generic and remains accurate — and is
outside C1's named scope (§6.1/§6.4) — no edit.

### C2 — oracle audit-logging unified on `log_event` (D22)

`predicate_translation.py`, `subsumption.py` and `predicate_distribution.py`
each carried a vestigial `audit_log` constructor flag that `build_pipeline`
never set, so their `log_event` calls — gated on `if self._audit is not None`
— were inert in the deployed pipeline. C2 removed the flag and the `self._audit`
attribute from all three and made the `log_event(db, …)` calls unconditional,
so `row_created`, `row_retracted` and `row_generation_failed` now fire for the
oracles in the deployed pipeline — the same shape A4 applied to
`consistency.py` / `retraction.py` / `contradiction_tracer.py` and B3a applied
to `tier_u.py`.

`build_pipeline` already passed no `audit_log` to any constructor, so it needed
no change. Four tests in `test_predicate_translation.py` that constructed the
oracle with `audit_log=audit_log` were updated to drop the dead argument (and
the now-unused import); they remain regression guards for the now-unconditional
logging. `test_error_logged_when_audit_present` was renamed `test_error_logged`
— there is no longer an "audit present/absent" distinction.

No `log_event` event type needed adding. `log_event` accepts any `event_type`
string; the oracle events (`row_created`, `row_retracted`,
`row_generation_failed`) were already supported — they were inert, not absent.

---

## C2 Step 1 — the systematic grep

The load-bearing audit. Three checks across `src/aedos/`: `audit_log`,
`self._audit`, `self.audit_log`.

**Found: 10 modules with an `audit_log=None` constructor parameter** that
`build_pipeline` never sets — splitting into two classes:

- **Class 1 — the 3 named oracles** (`predicate_translation.py`,
  `subsumption.py`, `predicate_distribution.py`). Parameter + `self._audit`
  attribute + `log_event` calls **gated** on `if self._audit is not None`.
  Effect: the events were **inert in the deployed pipeline** — a functional
  defect. This is D22 proper.
- **Class 2 — 7 further modules** (`router.py`, `aggregator.py`,
  `kb_verifier.py`, `kb_wikidata.py`, `python_verifier.py`, `resolver.py`,
  `walker.py`). Parameter + `self._audit` attribute, but `self._audit` is
  **never read** — content-mode grep showed only the assignment line.
  `aggregator.py` logs `verdict_recorded` unconditionally via `self._db` (its
  logging already worked); the other six call `log_event` nowhere. In Class 2
  the parameter was **pure dead residue** — removing it has **zero behavioural
  effect**.

The grep found more than the three named oracles, so — per the plan and the
session's "pause after the grep" instruction — a scope check-in was raised. The
decision was to clean **all ten modules**: fix the 3 oracles (functional, with
tests) and strip the dead parameter from the 7 Class-2 modules. This is the
choice that lets the grep return genuinely clean and ends the multi-round
"we said we got X, missed some" pattern that ran across A4 (missed `tier_u.py`),
B3a (found the broader pattern), and now C2.

**Already clean:** the A4-fixed modules (`consistency.py`, `retraction.py`,
`contradiction_tracer.py`) and the B3a-fixed `tier_u.py` — unconditional
`log_event`, no `audit_log` parameter. No `self.audit_log` (dotted) form, and
no `self._audit.log(...)` method form anywhere — A4 had already converted those.

**Post-C2 state — vestigial-flag confirmation.** No `audit_log` constructor
parameter and no `self._audit` attribute remain anywhere in `src/aedos/`. The
`audit_log` token still appears, all legitimately: the `audit_log` SQL table
name (`database.py`, `audit/log.py`, `retraction.py`); the architecture-§7.1
`VerificationResult.audit_log_entries` result field (`aggregator.py`); the
`from aedos.audit import log as audit_log` module alias (`app.py`); and one
explanatory comment in `tier_u.py` recording the B3a cleanup. None is a flag.
The audit-logging unification is genuinely complete.

---

## Per-delta status

| Delta | Before | After | Commit |
|-------|--------|-------|--------|
| D21 | §6.1/§6.4 describe only polarity-based contradiction; §6.4 carries no belief-revision text | §6.1 describes the post-B3 closure rule; §6.4 has a Belief revision subsection covering both paths | `e0bbc4c` |
| D22 | 3 oracles gate `row_created` / `row_retracted` / `row_generation_failed` on an unwired flag — inert in the deployed pipeline | oracles log via `log_event(db, …)` unconditionally; all 10 vestigial `audit_log` flags removed from `src/aedos/` | `65833f0` |

D21 and D22 are now **Resolved (Phase C)**. D23 was reframed in
`docs/v0.16_planning.md` as *deferred to Phase 10.5 data* — it waits on
empirical pressure from calibration (whether `lives_in` is functional is a
measurement question, and the seed `single_valued` value is a calibration
input), not on v0.16 design time. The deferred deltas D5, D9, D10, D13, D14,
D15 remain deferred — none was pulled forward.

---

## Tests

| | passed |
|--|--------|
| `v0.15.0-rc.3` baseline | 717 |
| after Phase C | 720 |

**+3:** C2 adds `tests/integration/test_oracle_audit_logging.py` — one test per
oracle, each building the pipeline through `build_pipeline` (the production
assembly) and asserting a cold consultation writes a `row_created` audit event.
C1 adds no test (doc-only). The four `test_predicate_translation.py` tests
updated for the dropped `audit_log` argument, and the one rename, change no
count.

**Stash-and-verify (C2).** `git stash` reverted the C2 code to the rc.3 state
(the new, untracked test file stayed). Against rc.3 all three new tests **fail**
— a clean `assert False`, the oracles' gated `log_event` never firing because
`build_pipeline` does not set the flag. With C2 restored (`git stash pop`) all
three **pass**. The discriminator confirms the three tests are load-bearing.

---

## Final verification

```
pytest tests/ -q                                    720 passed, 1 skipped, 11 deselected
python -m tests.evaluation.benchmark --validate-harness   Harness validation: PASS
pytest --run-calibration -q                          720 passed, 12 skipped
git log --oneline v0.15.0-rc.3..HEAD                 3 cluster commits
```

The systematic grep, re-run against the post-C2 tree, shows no `audit_log`
constructor flag and no `self._audit` attribute in `src/aedos/` (see "C2 Step 1"
above).

## Tag and Phase 10.5 start point

The final commit is tagged **`v0.15.0-rc.4`**. Phase 10.5 begins from
`v0.15.0-rc.4`. Phase C is documentation and hygiene — C1 is doc-only, C2 has
no effect on any verdict — so the fallback start point for a Phase 10.5
calibration anomaly is unchanged from Phase B's report: **`v0.15.0-rc.2`** if an
anomaly traces past D16/D6.

After Phase C, v0.15 is in its final form: the architecture document describes
the system that exists, and the audit-logging pipeline has end-to-end
visibility for the events the architecture specifies.
