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
