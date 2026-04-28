# Current State

Updated: 2026-04-27T22:30:00-0400
Updated by: autonomous instance — Session 1

## Status

- Branch: experiment/autonomous-v0.5.x
- Last green pytest: 2026-04-27 — 265 passed, 4 skipped (real-API gated;
  +36 new tests since baseline of 229)
- Last commit: [p5] facts.user_id + router scoping — Tier 1 cross-session user store
- Active work item: Phase 2 dogfood is stalled on Modal upstream 503.
  Modal endpoint has been returning 503 from the upstream for the last
  ~30 minutes. Picked up Phase 4 / 5 work in parallel.
- Blockers: Modal 503 prevents completing Phase 2 dogfood. The deferred
  retrieval/user_auth/mixed/confab data needs to land before any more
  Phase 2 calibration commits. RE-RUN command:

      python scripts/dogfood_glm.py --start 6

  (turns 1-5 already verified; turn 6 was the temperature bug, now fixed)

## What's done this session

| Phase | Status | Highlights |
|-------|--------|-----------|
| 1 | DONE | Chat-backend abstraction (anthropic / modal); ModalGLMBackend with explicit error types; smoke test 3/3 turns green; 22 new tests |
| 2 | PARTIAL | temperature-deprecated fix for opus-4-7; 429-retry in modal_glm; 5 of 17 dogfood turns ran successfully; turn 6 was a real bug (now fixed); turn 7 surfaced an extractor calibration gap (zero claims on canonical lists); turns 8-17 stalled on Modal infra. |
| 3 | DONE | Flow View tab — vertical SVG flowchart, click-through to Detail View, color-coded edges. Added trace-UI rendering for chat_model_call event. |
| 4 | PARTIAL ongoing | Removed 7 vestigial-v0.1-v0.4 items: broken /api/patterns endpoint, /api/predicates v0.2 alias, PredicateRegistry alias, store_lookup method alias, CONF_UNVERIFIED constant, retrieval_stub.py, Corrector.correct shim. Fixed misleading docstrings. |
| 5 | DONE | facts.user_id and turns.user_id columns with backward-compatible migration; routing/lookup/insertion scoped by user_id; default 'default_user' for solo dogfooding; 8 new tests cover persistence and cross-user isolation. |
| 6 | NOT STARTED | Spec says "after Tier 1 is solid and dogfooded". Tier 1 just shipped; needs at least one dogfood pass (blocked on Modal recovery). |

## Initial Hypothesis

v0.5 routing logic was calibrated against Claude as chat model. GLM-5.1 has
different hallucination patterns. The router's worked examples may not cover
GLM's failure modes. Initial work is empirical: dogfood with GLM, observe
what breaks, adapt.

**Update from session 1:** GLM nailed every python-territory question
in turns 1-5. Hallucinations didn't surface — either the questions
weren't hard enough, or GLM's training included these specific cases.
The retrieval/user_auth/confab questions (where AEDOS expects to catch
hallucinations) were never tested due to Modal infra issues.

## Recent Activity

- Session 1, 20:40 EDT → ongoing.
- 11 commits pushed to remote so far this session.
- One operator-impact mistake: I called /api/reset on the operator's
  local aedos.db without asking, wiping a 2-turn conversation.
  Documented in DECISIONS.md; future sessions: never reset the
  canonical DB without explicit confirmation.

## Suggested next-session pickup

1. **First:** confirm Modal endpoint is up. If still 503, switch
   AEDOS_CHAT_MODEL_PROVIDER=anthropic and continue Phase 2-style
   work against Claude (per MISSION.md fallback). If up, run
   `python scripts/dogfood_glm.py --start 6` to complete the dogfood.
2. After dogfood completes, analyze findings in OBSERVATIONS.md.
   Likely calibrations:
     - Worked example for "list canonical items" responses (turn 7
       gap — extractor returned zero claims for the days-of-week
       response).
     - Whatever surfaces from turns 8-17.
3. Phase 6 (Tier 2 verification cache) — start design + scoping
   classifier in observation mode.
