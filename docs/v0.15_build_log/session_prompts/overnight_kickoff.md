# Aedos v0.15 — Overnight Build Kickoff

You are executing the Aedos v0.15 build, unattended, in a single continuous Claude Code session. No operator is present. You finish when Phase 10 is tagged `v0.15-phase-10-complete`, and then you stop.

## Inputs

Two documents define the work. Read both fully before doing anything else:

1. `aedos_v0_15_architecture_draft_2.md` — the architecture. Authoritative on *what* v0.15 is. Where this kickoff prompt or the implementation plan conflict with the architecture, the architecture wins.
2. `aedos_v0_15_implementation_plan_overnight.md` — the implementation plan, calibration-deferred variant. Authoritative on *how* the build is sequenced. Read in full, including the "Calibration deferral policy" and "Unattended-run operating constraints" sections near the top.

Existing v0.14 code lives at `src/`. It is **read-only reference**. You do not modify it, delete it, or rename it. v0.15 is built greenfield at `src/aedos_v0_15/`.

## End condition

You are done when:

1. The commit `v0.15-phase-10-complete` is tagged on the `v0.15` branch.
2. `docs/v0_15/run_log.md` contains an entry for every phase (0 through 10).
3. `docs/v0_15/phase_10_5_runbook.md` exists and is complete.

When all three are true, stop. Do not proceed to Phase 10.5. Do not proceed to the v0.14 deletion commit. Do not tag `v0.15.0` — that tag is reserved for after Phase 10.5 passes under operator supervision.

If you hit an unrecoverable blocker (see "Failure handling" below), stop earlier and record why.

## Per-phase discipline

For every phase from 0 through 10, in order, do the following:

### 1. Plan the phase in writing

Before writing any code for the phase, create `docs/v0_15/phase_N_plan.md`. This document is your record of decisions. It contains:

- A summary of what the phase produces, in your own words, derived from the implementation plan.
- The concrete file list you'll create or modify.
- The test plan: which tests you'll write, what they cover, target count.
- For phases with calibration corpora: the **adversarial-coverage strategy** for the corpus. The implementation plan requires that corpora be authored even though they won't be executed during this run. State explicitly how each sub-category of the corpus covers edge cases and failure modes the architecture targets, not just the easy cases. Resist the temptation to write thin corpora that would pass trivially when Phase 10.5 runs them.
- A list of ambiguities you encountered while reading the spec for this phase. For each ambiguity: the question, the resolution you chose, the alternative you rejected, and the reasoning. Bias toward the more conservative interpretation — the one that makes false verifieds less likely. Some anticipated ambiguities are listed in the implementation plan's "Surfacing ambiguity" section; treat those as your starting point, not the full list.

If `docs/v0_15/phase_N_plan.md` reveals that you cannot proceed (architecture under-specifies something critical, prior phase produced something incompatible, etc.), record this as a blocker per the failure-handling rules and stop.

### 2. Implement against the plan

Write the code, write the tests, write the calibration corpus (as an authored artifact, not for execution). Commit frequently within the phase — at minimum, one commit per major component (e.g., the extractor core, then the normalization module, then the tests). The phase-end tag is for the final commit; intermediate commits are for recoverability.

### 3. Verify acceptance criteria

Run `pytest tests/v0_15/ -q` (or the equivalent command per the project's Makefile). Confirm:

- The phase's test count target is met (within ±15%; the plan calls these "targets, not contracts").
- All tests pass.
- The phase's calibration corpus parses as valid JSONL, conforms to the corpus's documented schema, has at least the documented number of cases distributed across documented sub-categories, and loads through the corpus loader without error.
- Zero false verifieds in the integration test suite (from Phase 3 onward; this is vacuous earlier).
- Any phase-specific acceptance criteria from the implementation plan.

If any of these fail, see "Failure handling".

### 4. Tag the phase

Commit any final cleanup with the phase-end commit message specified in the implementation plan. Tag the commit with the phase-end tag (e.g., `v0.15-phase-3-complete`). The tag is what marks the phase boundary.

### 5. Append to the run log

Append a one-paragraph entry to `docs/v0_15/run_log.md`:

```
## Phase N — <name>
- Commit SHA: <sha>
- Tag: <tag>
- Test count: <n>
- Calibration corpus: <filename>, <case count> cases, schema-valid: yes
- Ambiguities resolved this phase: <count> (see phase_N_ambiguities.md)
- Blockers: none | <description, see phase_N_blockers.md>
- One-sentence summary: <what got built>
```

Then proceed to the next phase. Do not stop between phases for any reason except an unrecoverable blocker.

## Failure handling

If a phase's acceptance criteria cannot be met after **2-3 attempts at the failing piece**, treat this as a blocker:

1. Create `docs/v0_15/phase_N_blockers.md`. Document the blocker: what was attempted, what failed, what you tried to fix it, what evidence suggests the root cause.
2. Append a blocker entry to `docs/v0_15/run_log.md`.
3. Commit the in-progress state of the phase to a branch tag like `v0.15-phase-N-blocked` (not the success tag). The success tag is reserved for completion.
4. **Stop.** Do not proceed to subsequent phases. Do not commit incomplete work under the success tag. Do not "skip the failing tests to keep moving."

The operator will review the blocker in the morning. A partial build with one documented blocker is far more useful than a fake-complete build with hidden failures.

The exception: if a single test is failing for what is clearly an unrelated reason (flaky network call, fixture path bug) and the actual phase work is sound, fix the trivial issue and proceed. Use judgment — but bias toward stopping when the failure touches on substrate semantics, the soundness criterion, or the architecture's core invariants.

## Operating constraints

These constraints apply throughout the run. Violating any of them is a critical error and should cause you to stop:

- **No live LLM in tests.** Do not set `RUN_LIVE_TESTS=1` under any circumstance. The mocked LLM client is what tests use. The system's *internal* LLM calls during smoke runs (the extractor calling the LLM, the oracle generating metadata, etc.) are not gated by this and may run as needed — but they should be infrequent during the build since most testing is against mocks.
- **No live Wikidata.** Do not set `RUN_LIVE_KB=1`. The KB adapter uses fixture JSON files at `tests/v0_15/fixtures/wikidata/`. Phase 4 produces this fixture set; subsequent phases extend it as needed.
- **No calibration execution.** Do not set `RUN_CALIBRATION=1`. Calibration corpora are authored, schema-validated, and loaded — never run against the live system.
- **v0.14 is untouched.** Do not modify, delete, rename, or move anything under `src/` that is not `src/aedos_v0_15/`. The v0.14 deletion commit happens after Phase 10.5, not during this run.
- **Do not tag `v0.15.0`.** That tag is reserved for post-Phase-10.5. The overnight run terminates at `v0.15-phase-10-complete`.
- **No merging to main.** All work stays on the `v0.15` branch.

## Ambiguity resolution policy

When you encounter an ambiguity that the architecture and implementation plan do not resolve:

1. Record it in `docs/v0_15/phase_N_ambiguities.md` with: the question, your chosen resolution, the alternative you rejected, and the reasoning.
2. Bias toward the conservative interpretation — the one that produces fewer false verifieds, the one that makes the soundness criterion easier to satisfy, the one that errs toward abstention rather than verification.
3. If the ambiguity is large enough that you genuinely cannot tell which resolution is right, and the architecture is silent, treat it as a blocker rather than guessing. The operator can resolve it in the morning. This is the right move for ambiguities that affect: substrate semantics, verdict polarity, retraction propagation, circuit-breaker behavior, the four-route routing classification, the consistency check.
4. For smaller ambiguities (file layout, internal naming, whether a helper goes in module A or module B), just pick something sensible, record the choice, and move on.

## What to do if you finish early

If you reach `v0.15-phase-10-complete` and there is time left, do not start Phase 10.5. Do not start the v0.14 deletion. Instead:

1. Re-read the run log. Confirm every phase has an entry. Confirm the test count progression matches the plan's targets.
2. Re-read every `docs/v0_15/phase_N_ambiguities.md`. Confirm each resolution is consistent with the architecture.
3. Run `pytest tests/v0_15/ -q` one more time from a clean state to confirm the full suite still passes.
4. Polish `docs/v0_15/phase_10_5_runbook.md` — it's the handoff document for the operator. Make sure it's complete and actionable.
5. Then stop.

## What you should produce by morning

When the operator wakes up, they should find:

- A `v0.15` branch with commits from Phase 0 through Phase 10, each phase tagged.
- `src/aedos_v0_15/` containing the full v0.15 implementation per the architecture.
- `tests/v0_15/` containing ~660 tests, all passing under mocked LLM and fixture KB.
- `tests/v0_15/calibration/` containing all calibration corpora as authored JSONL files, schema-valid, not executed.
- `tests/v0_15/fixtures/wikidata/` containing the fixture set Phase 4 produced.
- `tests/v0_15/evaluation/medium_bar_test_set.jsonl` containing the curated 100-150 cases for the medium-bar evaluation.
- `docs/v0_15/` containing: a per-phase plan document, a per-phase ambiguities document, a per-phase blockers document (only where there were blockers), the run log, the cold-start documentation, the evaluation methodology document, and the Phase 10.5 runbook.
- `seeds/v0_15/predicate_translation.json` containing the seed pack.
- v0.14 unchanged at `src/`.

If there are blockers, the operator finds them documented and addressable, not hidden. If the run terminated early because of a blocker, the operator can pick up where you left off.

## A note on quality

This is a long unattended run on a substantial codebase. Two failure modes to actively resist:

**Drift.** As phases accumulate, the temptation to skip the per-phase plan, write thinner corpora, or paper over inconsistencies grows. The per-phase plan is what keeps each phase's work disciplined. Write it every time, even when the phase feels routine.

**False completion.** A phase where the tests pass but the implementation is wrong is worse than a phase that fails honestly. The soundness criterion — zero false verifieds — is the load-bearing acceptance gate. When in doubt about whether an implementation truly satisfies it, abstain rather than verify. This applies to the system's behavior and to your own judgment about whether a phase is complete.

Now begin. Start with Phase 0.