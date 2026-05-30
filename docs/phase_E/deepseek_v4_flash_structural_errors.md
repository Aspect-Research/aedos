# DeepSeek V4-Flash structural errors — Phase E3 diagnostic

`deepseek-v4-flash × extraction_corpus` produced 15 of 57 cases (26%) classified
`runner_error`. This document classifies each by hypothesis and surfaces a
data-capture gap that must be closed before V4-Flash (or V4-Pro) can be
re-tested or disqualified.

## Data we have, data we don't

What the run produced:

- Per-case `classification`, `error` string, and `calls` count (the LLMClient
  `CallRecord` count) — in `deepseek-v4-flash__extraction_corpus.json`.
- Per-call full request + parsed response for **successful** calls —
  in `…__extraction_corpus.transcript.json`.

What the run did NOT capture — **the load-bearing gap:**

- **No raw response data for any errored case.** The harness's
  `_install_transcript` wrapper records the transcript entry only after a
  successful return from `client.extract_with_tool`: `result = orig(*a, **k);
  transcript.append(...)`. If `orig` raises, the wrapper propagates the
  exception with no entry written. Every one of the 15 errored cases therefore
  has `trans_entries = 0` in the transcript file.
- So we cannot read OpenRouter's raw `message.tool_calls` / `message.content`
  / `message.reasoning` for the 12 model-output failures. The operator's
  requested per-case capture isn't available from this run's outputs.

This is fixable with a small harness change (a `try/finally` around the
`orig()` call that records the response or the exception either way), then a
~$0.005 rerun. **Not fixing it in this diagnostic**, per scope; recorded as
the recommendation below.

## The 15 errored cases

| Case | `calls` | Error class | What we can infer |
|---|---|---|---|
| norm_003 · norm_004 · norm_009 · norm_012 · hardclaim_001 · hardclaim_005 · hardclaim_007 · firstperson_003 · firstperson_006 | 1 | `TypeError: 'NoneType' object is not subscriptable` | The OpenAI SDK returned a response; `_record` ran successfully (hence `calls=1`); then `resp.choices[0]` raised. See localisation below. |
| norm_010 · decomp_003 · decomp_005 | 1 | `RuntimeError: extract_with_tool: no tool call in OpenAI-compatible response` | The response *did* parse to `resp.choices[0].message`; `message.tool_calls` was falsy; our explicit raise fired. The model returned plain `content` instead of calling the tool. |
| temporal_008 · temporal_013 · firstperson_007 | 0 | `InternalServerError: 503 … 'no_available_workers'` | OpenRouter's upstream routing layer returned 503 before any usable response. **Transient provider availability, not a model-quality signal.** |

### Localising the 9 TypeErrors

`calls=1` is informative. `LLMClient.extract_with_tool` (OpenAI path):

```python
resp = client.chat.completions.create(...)             # success → resp object exists
class _U: input_tokens = getattr(resp.usage, "prompt_tokens", 0)
          output_tokens = getattr(resp.usage, "completion_tokens", 0)
self._record(purpose, cfg["model"], type("_R", (), {"usage": _U()})(), …)   # ← calls=1 written here
tc = resp.choices[0].message.tool_calls                # ← only line below _record that can produce
if tc:                                                  #    "'NoneType' object is not subscriptable"
    return json.loads(tc[0].function.arguments)
raise RuntimeError("extract_with_tool: no tool call in OpenAI-compatible response")
```

The error message rules out everything except subscripting None.
`resp.choices[0]` is the only candidate on the only line between `_record` and
the explicit RuntimeError that fits both the message *and* the
`calls=1`-but-error-not-RuntimeError pattern. Conclusion: **OpenRouter returned
a response object with `usage` populated but `choices = None`** (or a
pydantic-parsed equivalent) for those 9 cases. The provider produced enough
metadata to bill but no usable completion structure.

## Hypothesis classification

The operator's three hypotheses:

- **(a) Thinking still enabled** — the `reasoning: {enabled: false}` payload
  isn't being honoured (provider ignored it, or wrong schema, or partial
  honouring), so the structured-output bug (vllm #41132) is still firing.
- **(b) Genuinely malformed tool call** — the model produced an unusable
  response by some other route (capability limit, sporadic provider issue,
  decoding instability) regardless of thinking.
- **(c) Harness parsing brittleness on a recoverable response** — the response
  was recoverable but our parser refused it.

| Cases | Most likely | Could also be | Cannot be |
|---|---|---|---|
| 9 TypeErrors (`choices = None`) | **(a) or (b)** — and we cannot distinguish them without raw response data. Both predict `choices = None` after the provider gives up partway. #41132's documented signature (JSON leaking into the reasoning field and crashing structured generation) would manifest as exactly this — but so would generic v4-flash unreliability. | | (c) — there is nothing for the parser to recover; `choices` is absent. |
| 3 RuntimeErrors (`tool_calls` empty) | **(b)** — the model emitted a plain text response instead of calling the tool. | (a) is consistent if "thinking on" is causing the model to emit reasoning-as-content and skip the tool call — but a model that simply ignored the tool instruction would look identical. | (c) — *partially*. The harness raises strictly; one could imagine fallback parsing of `message.content` as JSON, but for Aedos's verification pipeline a tool-call contract that the model can ignore at will is not a contract. Not a parser bug. |
| 3 InternalServerError 503s | **transient infrastructure** — `no_available_workers` is OpenRouter's upstream pool exhaustion. Disjoint from (a)/(b)/(c). Retry would almost certainly clear them. | | |

### Cross-check on successful cases

The 42 non-errored cases all have transcript entries; spot-checking norm_001,
norm_002, temporal_001 shows clean parsed `{"claims": [...]}` dicts — no
reasoning artefacts in the *parsed* output. This is **partial** evidence that
`reasoning: {enabled: false}` is being honoured *at least some of the time*
(successful cases produce clean structured output). But the transcript records
the post-parse dict, not the raw SDK response, so we cannot see whether
`message.content` was simultaneously non-empty in those successes (some models
return both reasoning text and a tool call). The raw-response capture would
resolve this too.

## What this means for E3

The error rate matters more than the accuracy number. 38.6% accuracy is what
it is; the operator already noted accuracy is secondary. **The primary
question** is whether 12/57 = 21% of model-output errors can be eliminated. If
yes, V4-Flash is in the comparison; if no, V4-Flash (and probably V4-Pro,
which shares the same suspected pathology) cannot serve in Aedos's
verification pipeline regardless of capability scores on the cases it does
parse.

### The data we need to decide

The four follow-up options the operator listed all turn on one question: are
the 9 TypeErrors caused by thinking-still-enabled (a) — fixable by a different
disable payload — or by v4-flash structured-output unreliability independent
of thinking (b) — not fixable in the harness?

Right now we cannot tell, because we don't have the raw responses. To get
them: a tiny harness change so failures are captured into the transcript, then
rerun ~5–10 of the previously-erroring cases. Cost: well under a cent. Then
the response shape (presence of `message.reasoning`, presence of `content`,
shape of `choices`) discriminates (a) from (b) cleanly. **Recommended before
any more billed DeepSeek runs**, and before any other model is run (the same
gap will bite every model that produces a malformed response).

### What about the other four candidates

Devstral, Qwen, GLM-5.1, Kimi K2.6 are not affected by #41132. Their runs can
proceed without waiting on this diagnostic — provided we accept that any of
*their* errors would also lack raw-response data with the current harness. The
operator may want the harness fix landed before any further runs, for the same
reason it matters here: structural errors are the primary signal, and we
should see them clearly.

## Recommendation (data, not action)

This document does not fix anything. The operator's four follow-up options
remain open; the diagnostic data above informs the choice:

1. **Capture-and-rerun a slice** is the smallest discriminating step:
   patch the transcript wrapper to record failures (~10 lines), rerun 5–10
   previously-erroring cases against V4-Flash, read raw responses, classify
   (a) vs (b) definitively. Cost ≪ $0.001. Then decide on the remaining three.

2. The "different disable-thinking parameter shape" option is only useful if
   the capture-and-rerun shows hypothesis (a). If it shows (b), shape-tweaks
   won't help.

3. The "drop V4-Flash and V4-Pro" option is correct under hypothesis (b).
   Premature to invoke before the discriminating data.

4. The "investigate harness brittleness" option is largely ruled out by the
   error localisation above — `choices = None` and an empty `tool_calls` are
   not recoverable parser inputs.

The 3 InternalServerError 503s are independent of all of this and would
disappear on retry; they should not factor into the decision.

---

## Phase 2 — diagnostic rerun (harness fix applied)

The harness's failure-capture fix landed (commit `d4f894d`: `try/finally` around
the wrapped call writes a transcript entry on failure, and the LLMClient
attaches the raw SDK response to any exception raised after the SDK call
returns). The 9 TypeError case ids were rerun against the same model:

```json
{"candidate": "deepseek-v4-flash", "corpus": "extraction_corpus",
 "total_cases": 9, "passed": 7, "failed": 2, "runner_errors": 0,
 "total_calls": 9, "total_input_tokens": 6631, "total_output_tokens": 504,
 "total_cost_usd": 0.000855, "elapsed_seconds": 35.6,
 "pricing_verification": {"ok": true, "message": "pricing unchanged"}}
```

Output: `docs/phase_E/results/rerun/deepseek-v4-flash__extraction_corpus.json`
(+ `.transcript.json`).

### The result is itself a finding: 0 of 9 reproduced

Every case that errored in run 1 succeeded in run 2 — same inputs, same model,
same `reasoning: {enabled: false}` payload. No `runner_error`, no transcript
entry with `error` populated. **The TypeError failures are non-deterministic on
the same input.**

The 2 "failed" rerun cases are *accuracy* failures, not structural:

- `norm_003` ("Asa works at Google", expects `employed_by`) — model returned
  `{"claims": []}`. Refused to extract.
- `hardclaim_001` ("Asa works at Google", with context mentioning Bob) —
  model returned a single tool call with `participants: ["Asa", "Google"]` and
  `event_type: "employment"`, causing the extractor's `decompose_event` to
  reify an event with Asa/Google as has_participant edges. The resulting
  claim subjects are the synthetic event id, so `"Asa" in subjects` fails the
  hard_claim check. Inappropriate event-decomposition by the model on a clearly
  binary claim, not a structured-output bug.

Both completed cleanly through the SDK and the harness; neither errored.

### What this changes about hypothesis (a) vs (b)

The rerun was designed to capture raw failure responses so we could read
`message.reasoning` and discriminate (a) from (b). It produced no failures,
so we still have no raw response from a failed case. **But the
non-reproducibility itself is evidence:**

- **(a) thinking-still-enabled is weakened.** If the disable payload were
  being ignored outright, thinking would be on for every call on this model,
  and the structured-output bug would fire deterministically on the same
  input. Same input succeeding on retry is inconsistent with that.
- **(b) structured-output unreliability is mildly strengthened.** The original
  run's 9/57 (16%) TypeError rate plus this run's 0/9 (within sampling
  variance for p ≈ 0.16: P(0 fails | 9 trials) ≈ 21%) is consistent with a
  stochastic ~10–20% failure rate independent of input content.
- A third reading — **partial honouring of the disable payload, or
  thinking-residue effects that are stochastic** — fits both data points and
  cannot be ruled out without a captured failure.

### Independent of (a) vs (b): the baseline rate is the operative number

Whatever the cause, the original run's evidence is:

- **~16% structural-error rate** on the easiest LLM-only corpus, at the
  cheapest per-call price tier the comparison includes.
- That rate is **not a per-input issue** (reruns succeed), so it isn't
  fixable by changing which cases the comparison sees.
- The OpenRouter `reasoning` toggle, in its current `{enabled: false}` shape,
  is at best partially effective on V4-Flash for this concern.

A 16% rate of unusable responses is disqualifying for Aedos's soundness-
critical roles regardless of whether the cause is hypothesis (a) or (b). The
verification pipeline cannot tolerate one-in-six calls returning an unparseable
response on a corpus this easy; the cascade on derivation (multi-call walks)
would be much worse.

### Refined options after the rerun

The same four follow-up options remain, but their expected value has shifted:

1. **Different disable-thinking parameter shape** — expected value reduced.
   Non-reproducibility argues against (a); a shape-tweak primarily helps if
   (a) is the cause. Could still be worth one cheap try if the operator wants
   conclusive (a)-rule-out, but the baseline-rate evidence makes the
   shape-tweak's downstream usefulness limited.

2. **Capture-and-rerun a larger slice** — now achievable thanks to the
   harness fix. A full-corpus rerun (~$0.005) would, in expectation, produce
   ~9 fresh failures, each with raw-response capture, giving the discriminator
   we couldn't get this round. The cost is trivial; the data would be
   definitive. **Recommended next step if the operator wants the (a) vs (b)
   answer on the record.**

3. **Drop V4-Flash and V4-Pro from soundness-critical roles** — this is
   what the evidence already supports. The ~16% baseline structural-error
   rate on extraction is independent of cause and is disqualifying. Whether
   confirmed as (b) or only inferred from the baseline rate, the operational
   conclusion is the same. (V4-Pro shares the suspected pathology per
   the original Phase E prompt; #41132 is named for both.)

4. **Investigate harness brittleness further** — ruled out by the original
   error-localisation (`choices = None` and missing tool calls aren't parser-
   recoverable). No new evidence to revisit.

### Recommended path

Option 3 (drop V4-Flash and V4-Pro for soundness-critical roles) is the
disciplined call on the evidence in hand. If the operator wants the
discriminating data on the record before doing that, option 2 (one cheap
full-corpus rerun under the fixed harness) gets it. Either path means the
remaining four candidates (Devstral, Qwen, GLM-5.1, Kimi K2.6) proceed — they
are not affected by #41132 and would benefit from the harness fix being in
place for their own runs.

This document does not invoke either path. The decision is the operator's.

---

## Phase 3 — full-corpus rerun (harness fix verified, cause identified)

Full `deepseek-v4-flash × extraction_corpus` rerun under the fixed harness:

```json
{"candidate": "deepseek-v4-flash", "corpus": "extraction_corpus",
 "total_cases": 57, "passed": 19, "failed": 27, "runner_errors": 11,
 "total_calls": 57, "total_input_tokens": 33902, "total_output_tokens": 3026,
 "total_cost_usd": 0.004476, "elapsed_seconds": 249.5,
 "pricing_verification": {"ok": true}}
```

Output: `docs/phase_E/results/rerun_full/deepseek-v4-flash__extraction_corpus.json`
(+ `.transcript.json`).

### Harness fix verified

**11 of 11 errored cases produced a transcript entry with `error` and
`raw_response` populated.** No gap. The failure-capture fix is doing what it
was meant to.

### Error rate held: 11/57 ≈ 19%

Comparable to run 1's 15/57 ≈ 26% (run 1 had 3 transient 503s; net model-output
rate ~21%). This rerun saw 0 provider 503s and 0 RuntimeError "no tool call"
— **all 11 errors were TypeErrors of identical shape**. The same baseline rate
shows on a fresh sample; it isn't a per-input issue.

### Hypothesis discrimination — neither (a) nor (b) as framed

Every one of the 11 `raw_response` objects has:

```json
{"id": null, "choices": null, "created": null, "model": null,
 "object": null, "service_tier": null, "system_fingerprint": null,
 "usage": null,
 "error": {"message": "Upstream error from Morph: Failed to compile
                       structural_tag grammar: … Invalid structural tag
                       error: Only the last element in a sequence can be
                       unlimited, but the 1th element of sequence format
                       is unlimited",
           "code": 502}}
```

And a recursive scan for any `reasoning` field with non-empty content across
all 11 raw responses returns `NONE`. The model never reasoned, never produced
content, never produced a tool call. **It never ran.**

- **(a) thinking-still-enabled — DEFINITIVELY RULED OUT.** No reasoning
  content anywhere in any of the 11 raw responses. The disable-thinking
  payload's effectiveness is not the question because the failure happens
  before the model would have reasoned.
- **(b) "V4-Flash structured-output unreliability independent of thinking"
  — close, but mischaracterised.** The failure isn't the model producing a
  bad completion. It's the **provider's structured-output enforcement
  layer ("Morph", apparently DeepSeek's hosting / vllm-with-grammar-tagging)
  failing to *compile* the grammar OpenRouter hands it from our `tools` /
  `tool_choice` request.** A C++ grammar-compiler assertion fails
  (`grammar_compiler.cc:1037`) because the schema has an unlimited-size
  element not in the last position of a sequence — almost certainly the
  `claims: { type: "array", items: {…} }` field in the `EXTRACTION_TOOL`
  schema. The error is **upstream of the model**.

This is the operator's "Other (novel signature)" branch.

### What this means for the proposed parameter-shape variants

The operator's instruction was that if (a) is confirmed we try
`{effort: "low"}` and `{max_tokens: 0}`. **(a) is ruled out.** Both variants
control reasoning-step behaviour *inside the model* — neither can affect a
grammar-compile error that fires before the model is invoked. The variant
runs would not be informative for this failure mode. **Do not run them on
the (a)-route reasoning.**

There is one related question worth surfacing: could the `extra_body:
{reasoning: {enabled: false}}` payload itself be *causing* the grammar
compile to fail (by altering the schema the provider hands to its grammar
compiler)? Plausible but speculative — the error message names "sequence
format … 1th element … unlimited," which sounds like the `claims` array
in the tool schema, not anything reasoning-related. A clean rerun without
the `extra_body` would test this; arguably it is the only follow-up rerun
left whose outcome would change the recommendation. **Not doing it without
the operator's call.**

### Distribution of errors across sub-categories

| sub-category | run 1 | rerun (this) |
|---|---|---|
| normalization (15) | 5 | 0 |
| decomposition (10) | 2 | 3 |
| temporal (15) | 2 | 5 |
| hardclaim (7) | 3 | 2 |
| first_person (10) | 3 | 1 |

No category is immune; which cases happen to error shifts between runs. The
~19% rate is essentially "fraction of calls that get routed to a backend
where the grammar compile fails," not a per-input property.

### Confirmation: cause is class-wide for the DeepSeek V4 family on OpenRouter

The structural error is in the upstream provider's grammar-tagging
infrastructure, not the model weights. V4-Pro almost certainly shares the
same provider stack and the same grammar-compile failure mode. The original
Phase E prompt's framing — "the vllm bug affected both" — turns out to
match: both DeepSeek V4 variants on OpenRouter ride the same broken
structured-output layer, and the bug is in that layer, not the model.

### Refined recommendation

**Operator decision warranted; novel signature ≠ either pre-framed path.**

- The recommended "if (b) confirmed: drop both" path is operationally still
  the right call — the failure rate is real, persistent, provider-side,
  and class-wide for the DeepSeek V4 family on OpenRouter. Aedos's
  verification pipeline cannot accept ~19% provider grammar-compile errors
  on the easiest corpus.
- Before dropping, one final cheap experiment would conclusively test
  whether our `extra_body: {reasoning: {enabled: false}}` payload is the
  trigger: rerun without it. Cost ~$0.005. If error rate drops to ~0%,
  V4-Flash/V4-Pro could be in the comparison with the disable-thinking
  payload removed (a different harness configuration, with the operator's
  earlier note that "two run with reasoning off, four with reasoning on"
  asymmetry then becoming "all six with reasoning on" — cleaner). If the
  rate stays at ~19%, V4-Flash/V4-Pro are out for soundness-critical roles
  regardless of payload.

Both the parameter-shape variant rule-out and the no-extra-body rerun
question are surfaced; this addendum does not invoke either.

---

## Phase 4 — no-payload experiment (locating responsibility)

Targeted rerun of just the 11 cases that errored in Phase 3, with the
`extra_body: {reasoning: {enabled: false}}` payload removed
(`_CANDIDATES["deepseek-v4-flash"]["disable_thinking"] = False` for the
duration). Same model, same prompts, same cases, only the reasoning-control
payload was dropped.

```json
{"candidate": "deepseek-v4-flash", "corpus": "extraction_corpus",
 "total_cases": 11, "passed": 5, "failed": 6, "runner_errors": 0,
 "total_calls": 11, "total_input_tokens": 8121, "total_output_tokens": 1048,
 "total_cost_usd": 0.001142, "elapsed_seconds": 63.3,
 "pricing_verification": {"ok": true}}
```

Output: `docs/phase_E/results/no_payload/deepseek-v4-flash__extraction_corpus.json`
(+ `.transcript.json`).

### 0 of 11 errored. Per the operator's framework: payload-was-trigger confirmed.

The 11 cases that triggered the Morph grammar-compile assertion in Phase 3
all completed structurally clean in this run (5 correct, 6 accuracy-fail,
0 runner_error). The same model, same harness, same case texts — only the
`extra_body` payload differs.

### Caveat — n=11 sample size

The qualitative shift from ~19% (Phase 3, with payload) to 0% (Phase 4, no
payload) is what the operator's criterion calls. A statistical note worth
recording so the decision isn't read as airtight:

- P(0 errors | n=11, p=0.19) ≈ 0.81¹¹ ≈ **9.7%** — getting zero by chance
  alone if the underlying rate were unchanged is not negligible.
- The 9-case rerun in Phase 2 also produced 0/9 *with the payload on* —
  which by itself shows the failure isn't fully deterministic on input
  even with the payload (some stochasticity is genuine).

The fully airtight discriminator would be a full-corpus rerun without the
payload (~$0.005, expected ~10–11 errors if the rate is upstream-stochastic;
expected ~0 errors if the payload is the trigger). The operator's
experiment design was the 11-case targeted shape and a $0.001 budget, so
this addendum reports the result against that design. The full-corpus
no-payload run is not invoked here; it's available if the operator wants
the airtight version.

### Mechanistic plausibility

That the payload would alter what the provider's grammar compiler sees is
plausible: OpenRouter's request shape with `extra_body: {reasoning: {...}}`
is rewritten through Morph's structured-output enforcement layer, and that
rewrite likely adds reasoning-related grammar terms to the compiled output
grammar. The Morph error names "1th element of sequence format is
unlimited" — if `reasoning: {enabled: false}` causes the grammar to be
constructed as `[unlimited reasoning section | claims array | …]` rather
than just `[claims array]`, the 1th-position-unlimited assertion fits.
Speculative on the exact mechanism, but consistent with the data.

### Operative reading per the operator's framework

> If the 11 previously-erroring cases succeed without
> `extra_body: {reasoning: {enabled: false}}`, the payload was the trigger.
> DeepSeek V4-Pro and V4-Flash stay in the comparison with the payload
> removed for those candidates specifically.

11/11 succeeded structurally → **payload was the trigger** per the
operator's binary criterion. The follow-up actions called for (but not yet
invoked, awaiting operator confirmation):

- Set `disable_thinking: False` for both `deepseek-v4-flash` and
  `deepseek-v4-pro` in `_CANDIDATES`, with notes citing this addendum.
- Proceed with V4-Flash on `predicate_metadata` and `derivation`, then
  V4-Pro on its three corpora, both with reasoning *on*.
- E4 synthesis acknowledges the resulting asymmetry: all six candidates
  now run with reasoning on (DeepSeek's `disable_thinking: True` was
  flipped not because we *want* reasoning on but because the alternative
  was an unworkable rate of provider grammar-compile failures).
- The cause of those failures (Morph's structured-output grammar compiler
  rejecting the schema that includes a reasoning-disabled directive) is
  itself a finding worth carrying forward — V4-Flash and V4-Pro are
  effectively unusable on OpenRouter when one needs structured output AND
  reasoning disabled, which is a real production constraint regardless of
  this comparison's outcome.

### Files

- Phase 3 full corpus: `docs/phase_E/results/rerun_full/deepseek-v4-flash__extraction_corpus.json`
  / `.transcript.json` — 11 structural errors, all with `choices: null` +
  Morph grammar-compile error.
- Phase 4 no-payload: `docs/phase_E/results/no_payload/deepseek-v4-flash__extraction_corpus.json`
  / `.transcript.json` — 0 structural errors on the same 11 case ids.

---

## Phase 5 — final classification (V4-Flash and V4-Pro dropped)

The Phase 4 result called "payload was the trigger" under the operator's
binary criterion (0/11 errored). The operator then authorised continuing E3
with `disable_thinking: False` for both DeepSeek V4 entries, starting with a
post-fix V4-Flash × extraction_corpus run as the canonical first measurement.

### Post-fix full-corpus run

```json
{"candidate": "deepseek-v4-flash", "corpus": "extraction_corpus",
 "total_cases": 57, "passed": 26, "failed": 25, "runner_errors": 6,
 "accuracy": 0.4561, "total_calls": 57,
 "total_input_tokens": 37585, "total_output_tokens": 3904,
 "total_cost_usd": 0.005083, "elapsed_seconds": 274.4,
 "pricing_verification": {"ok": true}}
```

Output: `docs/phase_E/results/deepseek-v4-flash__extraction_corpus.json`
(+ `.transcript.json`).

**The fix held only partially.** 6 of 57 (~10.5%) still errored with the
payload removed — down from ~19% in run 1, but not the 0% suggested by the
small-sample Phase 4 experiment. Statistically, P(0 errors | n=11, p=0.10) ≈
31% — the 11-case test was always consistent with a true post-fix rate around
10%; we landed on the favourable side of the variance, which the operator's
binary criterion read as a clean elimination.

### The 6 remaining errors split into two distinct signatures

| Cases | Signature | Diagnosis |
|---|---|---|
| `norm_008` · `decomp_002` · `firstperson_003` · `firstperson_006` (**4**) | TypeError, `choices: None`, **same Morph grammar-compile error** as before | The Morph bug is reachable *without* the `reasoning` payload. Removing the payload made it less frequent (~7% baseline) but not unreachable. Some other internal request element (perhaps the tool schema's array field independently of the payload) still trips the "1th-element-unlimited" assertion sometimes. |
| `temporal_004` · `temporal_015` (**2**) | RuntimeError "no tool call", `choices: [{message: {tool_calls: null, content: null, reasoning: null}}]` | Model returned an assistant message with **everything null** — no tool call, no content, no reasoning. The model just gave up. A distinct failure mode (~3.5%) that didn't appear in the with-payload runs at all. |

### Final classification

- **Hypothesis (a) thinking-still-enabled — ruled out.** Confirmed in Phase 3
  (no reasoning content anywhere in any failed response) and the post-fix
  rerun does not introduce reasoning content either.
- **Hypothesis (b) family-level structured-output reliability — confirmed
  at ~10% residual rate.** Two distinct failure modes both present:
  - The Morph grammar-compile bug is genuinely class-wide for DeepSeek V4
    on OpenRouter and is reachable from multiple request shapes, not just
    when the `reasoning` payload is present.
  - V4-Flash additionally sometimes returns empty assistant messages — a
    failure mode that didn't appear with the payload on, because most calls
    failed earlier at grammar compile before they reached generation.
- **The Phase 4 "payload-was-trigger" reading was directionally correct
  but quantitatively wrong.** The payload doubled the rate from ~10% to
  ~19%; removing it didn't eliminate the failure mode. The small-n
  experiment had insufficient power to detect the residual ~10%.

### Operator decision

**Drop both `deepseek-v4-flash` and `deepseek-v4-pro` from the comparison.**
~10% baseline structural-error rate on the easiest LLM-only corpus is
disqualifying for soundness-critical roles, irrespective of cause. V4-Pro was
not separately tested — the disqualification is inferred from the class-wide
nature of the Morph bug (same provider stack, same expected failure modes).

`_CANDIDATES` updated: both entries carry a `disabled` field whose value is
the disqualification reason; `run_comparison` refuses to invoke a disabled
candidate on a live run (transport-injected unit tests still exercise the
wiring). `--list` now shows both as "DISABLED — …".

The Phase E comparison proceeds with the four remaining candidates: Devstral
Small 1.1 (specialty), Qwen 3.6 35B-A3B, GLM-5.1, Kimi K2.6. The total
remaining run count is **10** (Devstral × python_verification + the other
three × {extraction, predicate_metadata, derivation}).

### Files for the V4-Flash diagnostic record

- Phase 1 (initial with-payload, summarised here): committed at `1356afb`,
  later overwritten at the top level by the post-fix run; the same
  payload-on data is preserved in `rerun_full/` with raw responses.
- Phase 2: 9-case rerun with payload, `docs/phase_E/results/rerun/`.
- Phase 3: full-corpus rerun with payload + raw-response capture,
  `docs/phase_E/results/rerun_full/`.
- Phase 4: 11-case no-payload, `docs/phase_E/results/no_payload/`.
- Phase 5: post-fix full corpus, `docs/phase_E/results/deepseek-v4-flash__extraction_corpus.json`
  (committed at `ec37e68`).
