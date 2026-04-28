# AEDOS Autonomous Development — Standing Mission

## Operating Mode

You are running continuously. There is no stop condition. You iterate until you
hit a rate limit, the operator (Asa) intervenes, or your own infrastructure fails.
"Done" is not a state you can reach. When the active work queue empties, you
generate new work by reviewing the codebase, trace logs, observations, and
decision history.

## Standing Priorities

1. Working v0.5.x with GLM-5.1 as chat model under test (initial work)
2. Streamline the codebase (initial work, then ongoing)
3. Continuously improve correctness, performance, clarity, observability,
   test coverage, robustness, and UI quality (ongoing forever)

When (1) and (2) have meaningful initial completions, fold them into (3).

## Hard Constraints

- Do not merge to main. This branch is experimental.
- Do not delete uncommitted work — yours or the operator's.
- Commit frequently. The branch should always be in a state where pytest
  passes on every commit.
- Push every commit to the remote so the operator can observe progress.
- Never amend or rebase pushed commits.
- The chat model under test is GLM-5.1 via Modal. Your own reasoning is on
  Claude. Don't conflate these.
- GLM is free until April 30, 2026. After that date, switch the chat model
  back to Anthropic by setting AEDOS_CHAT_MODEL_PROVIDER=anthropic in
  .env.example and document the switch in DECISIONS.md.

## How to Run Continuously

1. Read autonomous_state/CURRENT_STATE.md to find where you left off.
2. Read autonomous_state/NEXT_STEPS.md to find the active work queue.
3. Pick the top item. If the queue is empty, generate new items (see below).
4. Do the work. Commit when complete.
5. Update CURRENT_STATE.md with what changed.
6. Update NEXT_STEPS.md (mark item done, add new items if discovered).
7. If you made a non-trivial choice, append to DECISIONS.md.
8. If you noticed something interesting, append to OBSERVATIONS.md.
9. Return to step 2.

## Generating New Work When the Queue Is Empty

The queue is rarely truly empty. Before declaring it so, do these:

- Review recent OBSERVATIONS.md entries — many become NEXT_STEPS.
- Re-run smoke tests and dogfooding scripts; note any quality issues.
- Read 2-3 recent pipeline_events traces; look for inefficiency, friction,
  or unclear behavior.
- Check test coverage; identify components without recent test additions.
- Re-read ARCHITECTURE.md's "Known Limitations" section; pick one to chip at.
- Look for code that has accumulated TODO comments or FIXME notes.
- Consider running a longer dogfooding session (10-30 turns) and analyzing
  the resulting traces for patterns.
- Consider whether any internal abstraction has become more confusing than
  the thing it abstracts.

If after all of these you still have no work, write an entry in
OBSERVATIONS.md describing what you tried and why nothing surfaced. Then
pick the most speculative or research-flavored idea from your reading and
spend a session exploring it. Document the exploration regardless of outcome.

The autonomous instance does not idle.

## Pointer to Other State Files

- CURRENT_STATE.md — heartbeat, updated frequently
- NEXT_STEPS.md — work queue
- DECISIONS.md — append-only choice log
- OBSERVATIONS.md — research notebook
- HANDOFF.md — instructions for the next instance to pick up
- COMMIT_PROTOCOL.md — how to commit
- SESSION_LOG.md — session-by-session summary, written at the end of each
  context window
