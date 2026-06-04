# v0.16.2 — Phase D Plan (central-claim selection)

v0.16.2 is largely an EFFICIENCY/verification release. Live use: "what do
chipmunks eat?" produced a draft with ~20 claims, many tangential to the question
(habitat, anatomy, behavior) that don't need verifying. Add a step that decides
which claim(s) are CENTRAL to answering the prompt and narrows verification to
those. Branch `v0.16.2`. Same build-review-build discipline.

## Design

**Chat-only.** Centrality is defined relative to the user's PROMPT, so this
applies to `/chat` (question → draft → claims), NOT `/verify` (the box has no
prompt; the user wants the whole text verified).

**Selector.** After extracting the draft's claims, one LLM call (purpose
`deployment:claim_selection`, a fast model) takes (user question, draft, numbered
claims) and returns the subset CENTRAL to answering the question — the factual
assertions that directly answer it. Then verify ONLY those (in parallel, as Phase
C); the rest pass through the draft unchanged, transparently marked "not
assessed (not central to your question)".

**Why this is the efficiency win.** One selection call typically cuts ~20 claims
to a handful, saving many full claim-walks (each = several KB/LLM calls + up to
the budget). Net: far fewer walks per turn.

**Soundness.** Skipping verification of a non-central claim is NOT a §3.2
violation — Aedos emits no verdict on it (like an abstain); it is shown unflagged,
not falsely verified/contradicted. Selection errors are safe in both directions:
wrongly-peripheral → unverified (safe, coverage loss); wrongly-central → verified
anyway (no harm). The selector prompt is INCLUSIVE ("if a claim could be part of
the answer, include it") to bias away from dropping real answer claims.

**Safety rails.**
- Fallback: selector error, empty result, or a malformed/parse-failed response ⇒
  verify ALL claims (the central claims "should definitely be verified if any
  response is returned" — never skip everything on selector failure).
- Threshold: skip selection entirely when there are few claims (≤ a small N) —
  nothing to narrow.
- Config: `AEDOS_SELECT_CENTRAL_CLAIMS` (default on), `AEDOS_SELECT_MIN_CLAIMS`.

**Transparency** (the release's theme):
- Stream a `selecting` step then a `selected` step ("N of M claims are central…").
- Emit a per-claim `skipped` event for each non-central claim (so the UI shows
  them, muted, as "not assessed — not central to your question").
- Surface `not_assessed` claims in the response body + a `selection` summary.

## Steps
1. **LLM purpose:** add `deployment:claim_selection` to `DEFAULT_MODEL_BY_PURPOSE`
   (fast model, like chat/extraction).
2. **Selector** (`src/aedos/deployment/claim_selection.py`): `select_central_claims
   (llm, question, draft, claims, *, min_claims, enabled) -> Selection` — builds
   the numbered prompt, calls the LLM, robustly parses the central numbers
   (JSON array → integer fallback → range-validate), returns
   `Selection(central_ids: set[str], applied: bool, reason: str)`; fallback to ALL
   on any failure/empty. Pure parsing is unit-tested without the LLM.
3. **`ChatResponse`**: add `not_assessed_claims: list[dict]` + a `selection` summary.
4. **`ChatWrapper.respond`**: select → partition central/peripheral → emit
   selecting/selected + per-skipped events → walk ONLY central (parallel) →
   aggregate central → response carries `not_assessed_claims`. `select_central`
   + `select_min_claims` params (deployment passes them).
5. **Backend**: thread the config (settings + pass to respond); `_chat_body`
   includes `not_assessed` + `selection`.
6. **Frontend**: render the `selected` step + the non-central claims as muted
   "not assessed" cards (the streamed `skipped` events + the response list).
7. **Tests**: selector parsing/fallback/threshold; partition; the wrapper verifies
   only central + records peripheral; backend surfacing. + adversarial review +
   live smoke ("what do chipmunks eat?" → only diet claims verified, habitat/anatomy
   marked not-assessed, fewer walks).

Commits only; no tag/push.

---

## Results (Phase D — done)

A chat draft's claims are now narrowed to those CENTRAL to the user's question
before verification. One LLM call (purpose `deployment:claim_selection`) selects
the central subset; only those are walked (in parallel, as Phase C); the rest pass
through the draft unverified, shown as "not assessed (not central to your
question)".

**Live (the reported example, "what do chipmunks eat?"):** 20 claims extracted →
**16 selected as central** (the diet facts) → **4 not assessed** (cheek pouches,
food-caching behavior — correctly judged peripheral to the diet question). The
non-central claims are skipped, surfaced transparently in the UI.

**Soundness — verified clean.** Skipping a non-central claim emits NO verdict on
it (abstain-equivalent), so this never false-verifies or false-contradicts; a
wrong selection only drops a claim to silent pass-through (the §3.2-safe
direction). The selector **fails open to verifying ALL** on every failure mode
(disabled / below threshold / LLM error / unparseable / empty / all-out-of-range)
— a selector malfunction can never skip verification on everything.

**Build-review-build.** Adversarial review (find→verify) of the soundness of
skipping, partition/aggregation correctness, and fail-open completeness returned
**7 findings, all confirmed not-a-bug**: no false-verify/contradict path;
fail-open complete; partition exhaustive + non-overlapping (unique uuid4 ids);
user-message premise promotion unaffected (still covers all user claims);
`_parse_selected_numbers` defensive (range-guarded, dedup, JSON-only — no loose
integer scrape); `not_assessed` surfacing carries only public draft content (no
row-ids/secrets). No patch required.

**Verification.** Gated suite (unit + integration + deploy): **1677 passed**,
1 xfailed / 1 xpassed. +16 Phase D tests (parsing, fail-open rails, and a
`respond()` integration: verifies only central + records not-assessed). Frontend
`tsc --noEmit` + `vite build` clean. Config: `AEDOS_SELECT_CENTRAL_CLAIMS`
(default on), `AEDOS_SELECT_MIN_CLAIMS` (default 4).
