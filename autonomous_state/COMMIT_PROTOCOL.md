# Commit Protocol

## When to Commit

Commit after every meaningful unit of work. A meaningful unit is:
- A change that passes tests and could be reviewed in isolation
- A decision worth recording (then commit DECISIONS.md update)
- An observation worth preserving (then commit OBSERVATIONS.md update)
- The end of any work session, even if mid-task (commit WIP)

The branch should always be in a state where pytest passes on every commit.

## Commit Message Format

Subject: short imperative description, prefixed with the priority area:
- `[p1] router: handle GLM's tendency to over-hedge confident claims`
- `[p2] cleanup: remove vestigial verification_method field from patterns.yaml`
- `[p3] perf: cache code-writer responses by claim hash`
- `[wip] router: in progress, prompt rewrite incomplete`
- `[state] update CURRENT_STATE after dogfooding session`
- `[obs] observation: GLM refuses certain math claims`

Body (when non-trivial): what changed, why, what to look for if it breaks.

## Push Discipline

Push after every commit. The remote is the operator's window into the run.
Never amend or rebase pushed commits. History is append-only.

## Recovery

If a test fails after a change, do not commit. Fix or revert. If you
accidentally break the branch (uncommitted work in a broken state and no
clean revert), prioritize getting back to green over making forward
progress. Note the recovery in DECISIONS.md.
