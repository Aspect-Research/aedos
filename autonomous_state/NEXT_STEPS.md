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

- [x] **Phase 2 dogfood completed** (12/17 turns landed signal). See
  OBSERVATIONS 2026-04-27 "Phase-2 dogfood complete". 1 real bug
  fixed inline (judge parser abbreviations); 2 calibration items
  identified for follow-up below.

- [ ] **Calibration: lifespan/duration claims should embed inputs.**
  Real finding from turn 12 (Marie Curie). The CLAUDE.md spec says
  "Marie Curie was born in 1867 and died in 1934, so she lived 67
  years" should route to python. But the EXTRACTOR strips the dates
  from the lifespan claim's slots, so the router correctly says "needs
  external data". Fix the extractor: when a lifespan/duration/diff
  claim appears alongside its inputs in the same response, embed the
  inputs as slots. Add a worked example in `extractor.py`'s system
  prompt and a real-API test case in `test_routing_calibration.py`.

- [ ] **Calibration: canonical-list responses extract zero claims.**
  Reproduced (turn 7 days_of_week). The extractor returns
  `valid_facts: []` for "the seven days of the week are: Mon, ...,
  Sun". Architectural choice for the operator: add an `ordered_list`
  pattern to `patterns.yaml` (substantive change), or accept that
  canonical enumerations aren't extractable claims (and lose the
  ability to catch "the New England states are: ME, NH, VT, MA, RI,
  CT, Pennsylvania" — the LLM-trap hallucination).

- [ ] **Reduce chat max_tokens.** Two of two Modal cold-starts
  (turns 6 and 16) exceeded the 300s timeout. With max_tokens=4096
  and GLM's reasoning chain, requests can blow past the timeout.
  Chat responses are inherently short — try max_tokens=1024 in
  `pipeline._invoke_chat_backend`. Trade-off: occasionally truncates
  a long answer, but probably never matters for AEDOS chat use.

- [x] **Cross-check signal restoration on opus-4-7.** Done in commit
  91fac42. CROSS_CHECK_MODEL hardcoded to claude-sonnet-4-6;
  verify_with_cross_check forces it on both iterations regardless of
  configured corrector_model. The single-shot verify path is unchanged.
  2 new unit tests confirm. **Real-API end-to-end validation still
  pending** — needs to wait for Modal recovery so the dogfood can
  re-run turn 6 (ne_states) and exercise the full cross-check path.

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
