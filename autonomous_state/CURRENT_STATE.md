# Current State

Updated: 2026-04-27T21:15:00-0400
Updated by: autonomous instance — Session 1

## Status

- Branch: experiment/autonomous-v0.5.x
- Last green pytest: 2026-04-27 — 252 passed, 4 skipped (real-API gated;
  +23 new tests for chat backends and modal_glm)
- Last commit: [p1] llm_clients: pluggable chat backend (anthropic | modal/GLM)
- Active work item: Phase 1 plumbing complete; smoke test passing.
  Next: Phase 2 dogfooding against GLM with harder prompts.
- Blockers: none. Modal endpoint reachable; cold-start ~5min, warm ~30s.

## Initial Hypothesis

v0.5 routing logic was calibrated against Claude as chat model. GLM-5.1 has
different hallucination patterns. The router's worked examples may not cover
GLM's failure modes. Initial work is empirical: dogfood with GLM, observe
what breaks, adapt.

## Recent Activity

- Session 1, 20:40 → 21:15 EDT.
- Read all autonomous_state files; confirmed pytest green at baseline (229
  passed, 4 skipped).
- Built pluggable chat-backend layer (`src/llm_clients/`) with
  `AnthropicChatBackend` and `ModalGLMBackend`. New `chat_model_call`
  pipeline event captured uniformly across providers.
- 22 new tests cover Modal client (payload translation, error handling,
  event logging), the factory, and pipeline integration.
- Smoke test (`scripts/smoke_test_glm.py`) runs three prompts through the
  full pipeline against GLM-5.1-FP8 served via Modal. All three: chat OK,
  one valid claim extracted per turn, router → python, code-gen → verified,
  no correction needed.
- Discovered GLM-specific quirks: reasoning model with separate
  `reasoning_content` (content can be null if max_tokens too low),
  ~5min cold-start, concurrency limit of ~1 per model on the endpoint.
  Documented in OBSERVATIONS.md and DECISIONS.md.
- Updated NEXT_STEPS.md with Phase 2 plan: build harder dogfooding prompts
  that target every router branch (the smoke set was all python-territory
  and all correct).

## What's NOT done in Phase 1

- The smoke set (3 turns) is too easy. Hallucinations didn't surface
  because GLM got everything right. Phase 2 needs prompts that exercise
  retrieval, user-authoritative, and mixed-claim paths.
- The 4096 default max_tokens for chat is wasteful for GLM, where most
  responses are well under 200 tokens but reasoning_content adds latency
  proportional to max_tokens. Not a correctness issue, but worth a note
  for future tuning.
