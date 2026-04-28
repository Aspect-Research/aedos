# Active Work Queue

Items are picked top-to-bottom. New items are added at the priority level
they belong to. Completed items get a strikethrough and a one-line note,
then graduate to SESSION_LOG.md after a few sessions.

## Priority 1: Working v0.5.x with GLM as chat model under test

- [x] **Wire pluggable chat backend (Modal/GLM + Anthropic).** Done
  2026-04-27. AEDOS_CHAT_MODEL_PROVIDER selects backend; ModalGLMBackend
  matches OpenAI chat-completions; chat_model_call event logged from
  both backends. Smoke test scripts/smoke_test_glm.py now passes —
  three turns, all verified.

- [ ] **Phase 2: dogfooding session against GLM with hard prompts.**
  Smoke set was too easy (GLM nailed all three; verifier correctly
  said "verified"). Build a 10-20 prompt set that targets every router
  branch:

    - python territory: trickier counts ("how many m's in commitment",
      "how many vowels in serendipitous"), date arithmetic
    - python_with_canonical_constants: "list the New England states"
      (canonical reference), days of the week
    - retrieval territory: factoids about non-famous people / events,
      questions where confabulation is plausible (specific obscure
      historical dates, second-tier wikipedia stub topics)
    - user_authoritative: state a preference, then ask back next turn
    - mixed-claim responses: questions where the model will combine
      python + retrieval claims in one paragraph

  For each turn dump pipeline_events to diagnostic_output/. Tag issues
  in OBSERVATIONS.md by category (routing / extraction / verification /
  corrector). Promote actionable items here.

- [ ] **Phase 2 calibration outputs.** After dogfooding, tune the
  router's worked examples in `_ROUTER_SYSTEM` (src/llm_router.py) for
  any GLM-specific misroutes. Add scenario tests for each new pattern.

## Priority 2: Streamlining

(Not yet started. After Phase 2 has at least one calibration round.)

## Priority 3: Continuous improvement (ongoing — never empties)

(After Phase 1/2 have meaningful initial completions.)

## Discovered During Work

- **Latency for dogfooding is real.** Warm GLM turn ~30s, cold start
  ~5min. A 20-turn dogfooding session is 10+ minutes wall-clock just
  for chat. Consider scripting the dogfooding rather than going through
  the UI. (See OBSERVATIONS 2026-04-27.)
- **Modal concurrency=1.** Can't parallelize evaluation against a
  single Modal endpoint. (See OBSERVATIONS 2026-04-27.)
- **Reasoning models change the max_tokens calculus.** With GLM the
  effective max_tokens for assistant content is reduced by however many
  tokens go into reasoning_content. May want to make max_tokens
  per-backend configurable so the GLM path can request more headroom
  than Anthropic without changing both.
