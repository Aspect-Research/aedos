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

- [ ] **Resume Phase 2 dogfooding once Modal recovers.** Modal endpoint
  is currently returning 503 from upstream (~22:00 EDT). Once back up:

      python scripts/dogfood_glm.py --start 6

  Turns 1-5 were already verified. Turn 6 (ne_states) was a real bug
  caused by `temperature` being deprecated for opus-4-7 — fix shipped
  in commit 6d466df. Turns 7-17 are the retrieval / user_auth / mixed /
  confab cases that haven't been exercised yet.

  The dogfood now (a) retries 429 internally with backoff, (b) sleeps
  90s after a pipeline error before the next turn, and (c) flushes
  print so the log file streams. Expected wall-clock for turns 6-17
  with retries: ~15-20 min if Modal is healthy.

- [ ] **Phase 2 calibration outputs.** After the rerun, tune the
  router's worked examples in `_ROUTER_SYSTEM` (src/llm_router.py) for
  any GLM-specific misroutes. Add scenario tests for each new pattern.
  **Already-known item:** the extractor returned ZERO claims for GLM's
  "days of the week" response (turn 7) — a real calibration gap. Either
  add an extractor worked example for "list canonical items in order",
  or accept that canonical reference data isn't an extractable claim.
  See OBSERVATIONS 2026-04-27.

- [ ] **Cross-check signal restoration on opus-4-7.** With temperature
  silently dropped, the canonical-constants cross-check runs both
  iterations with identical params and likely produces identical
  output, weakening (effectively eliminating) the cross-check signal
  on opus-4-7. Options:
    1. Force the cross-check to use Sonnet 4.6 (still accepts
       temperature). Per-stage model override.
    2. Use a different variation source — small prompt perturbation,
       reordered examples, etc.
  Either way, add a real-API-gated test that confirms the signal works
  end-to-end on the cross-check path.

## Priority 2: Streamlining

- [x] Drop /api/patterns staleness, /api/predicates alias, PredicateRegistry
  alias, store_lookup method alias, CONF_UNVERIFIED constant,
  retrieval_stub.py, Corrector.correct shim, ARCHITECTURE.md staleness.
  Done across 4 commits (e1c9730, cd6980d, c08e813, 313aab6).

- [ ] Continue audit: every file in `src/` for v0.1-v0.4 leftovers.
  Look for "back-compat", "v0.1 alias", "kept for", "legacy" comments.
  Many low-value items remain (e.g. PIPELINE_STAGES.code_triage is kept
  intentionally for old DBs — leave it).

- [ ] CLAUDE.md still has v0.2/v0.3/v0.4 sections describing how to add
  predicates. Some of that is operator-facing history. The "How to add
  a new predicate" v0.2 section is explicitly marked superseded but
  still present. Worth a docs cleanup PR — defer until operator
  comments on whether to keep or trim.

## Priority 3: Continuous improvement (ongoing — never empties)

- [x] Flow View tab — vertical SVG flowchart, click-through to
  Detail View. Done.

- [ ] Polish Flow View based on actual use against the dogfood data.
  Likely tweaks: clearer rendering when there are 0 claims, claim text
  truncation rules, hover tooltips. Hold until I've used it on real
  traces.

- [ ] Eval harness (Phase 7-style work). A real benchmark with a
  corpus of questions where GLM's hallucination rate is measurable,
  with and without verification. Build on top of dogfood_glm.py.

- [ ] Cost telemetry. Track LLM call counts and costs per turn,
  surface in the trace UI.

- [ ] Better entity resolution. Empty wishlist for now; will become
  load-bearing in Phase 6 (Tier 2 cache).

- [ ] **Failure-mode taxonomy.** After dogfood completes, build a
  structured taxonomy of GLM's failures in OBSERVATIONS.md. Useful for
  both router worked-example design and the eventual paper.

- [ ] More 5xx handling in modal_glm. Currently 5xx propagates as
  ModalServerError. May want bounded retry on transient 502/503 too,
  but only after seeing what they look like in practice.

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
- **Modal 503 outages happen.** Saw extended (~30 min) 503 from the
  Modal upstream during this session. Per MISSION.md fallback, switch
  AEDOS_CHAT_MODEL_PROVIDER=anthropic if it persists.
