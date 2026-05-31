# Kimi K2.6 WandB provider diagnostic — Phase E3 (post-F)

`kimi-k2.6 × extraction_corpus` (canonical first run, post-F-042 codebase)
produced **24 of 57 (42%) runner_errors**. Every errored call returned
`RuntimeError: extract_with_tool: no tool call in OpenAI-compatible response`.
The failure rate was 4× DeepSeek V4-Flash's already-disqualifying ~10% baseline,
and surfaced on the easiest LLM-only corpus.

This document records the diagnostic that identified the WandB provider as the
load-bearing variable and the resulting `_CANDIDATES` change that pins Kimi
away from that provider.

## What the canonical run produced

```json
{"candidate": "kimi-k2.6", "corpus": "extraction_corpus",
 "total_cases": 57, "passed": 24, "failed": 9, "runner_errors": 24,
 "accuracy": 0.4211, "total_cost_usd": 0.105782, "elapsed_seconds": 1621.0}
```

Output preserved at
`docs/phase_E/results/with_wandb/kimi-k2.6__extraction_corpus.json`
(+ `.transcript.json` with raw responses for all 24 errored cases — the F3
harness fix captures raw_response on exceptions, so the diagnostic gap that
existed at DeepSeek-time is closed for this analysis).

## Three diagnostic checks

The operator requested three checks on the 24 errored cases.

### (a) Content shape on errored cases — 24/24 PURE_JSON

Every errored response had `tool_calls: None` but `message.content` set to a
valid JSON string matching the extraction tool schema. Example from `norm_007`:

```text
message.content = '{\n  "claims": [\n    {"object": "President",
                       "polarity": 1, "predicate": "served as",
                       "source_text": "Obama served as President",
                       "subject": "Obama", "verb_tense": "past"}\n  ]\n}'
message.tool_calls = None
message.reasoning  = <present, non-empty>
```

- 24/24 contents parse as JSON.
- 24/24 parsed dicts match the top-level `{claims: [...]}` schema.
- No wrapping text, no markdown fences, no malformed JSON.

The model **had the right answer**; it just emitted it as content rather than
calling the tool. Distinct from DeepSeek V4-Flash's `choices: null` (provider
grammar-compile failure upstream of the model) — Kimi K2.6 generates correct
output; the failure is in the tool-call protocol layer.

### (b) Provider clustering — 24/24 WandB

Every errored response's `provider` field reads `"WandB"`. The transcript
captures `raw_response.provider` for every errored call (the F3 harness fix
again). The clustering is **complete** — not statistical, not "concentrated."

Caveat: the harness only records `raw_response` on exceptions. The 33
succeeded cases' provider is not recorded in this run's transcript. Two
readings remain possible until succeeded-case providers are recorded:

- **(b₁) Provider-isolated.** Other providers serve Kimi K2.6 fine; only
  WandB drops `tool_calls`. The provider-exclusion experiment below
  distinguishes this from (b₂) at the 24-case level.
- **(b₂) Provider-correlated, not isolated.** WandB drops `tool_calls`
  *more* than other providers, but other providers also drop sometimes.

The 24-case rerun with `provider.ignore = ["WandB"]` produced 0 structural
errors (below) — consistent with (b₁) at n=24. A full 57-case rerun would
strengthen the distinction; per the operator's experiment design we held to
the 24-case scope.

### (c) Succeeded-case variations — uniform shape

All 33 succeeded responses parse as `{"claims": [...]}`. Claim-array sizes
were 1 (29 cases) or 2 (4 cases). One case (`temporal_012`) included a
`reified_event_id` field consistent with the extraction tool schema's
decomposition output. No anomalous shapes.

## Provider-exclusion experiment

Targeted rerun of the 24 errored case_ids with
`extra_body = {"provider": {"ignore": ["WandB"]}}`:

```json
{"candidate": "kimi-k2.6", "corpus": "extraction_corpus",
 "total_cases": 24, "passed": 15, "failed": 9, "runner_errors": 0,
 "accuracy": 0.625, "total_cost_usd": 0.075328, "elapsed_seconds": 538.4,
 "experiment": "no_wandb (provider.ignore=[WandB])"}
```

Output: `docs/phase_E/results/no_wandb/kimi-k2.6__extraction_corpus.json`
(+ `.transcript.json`).

**0 of 24 reproduced.** Every case that hit a "no tool call" structural error
in the canonical WandB-served run produced clean `tool_calls` when WandB was
excluded. The 9 remaining failures are pure-accuracy failures (the model
extracted, the extracted claim didn't match the corpus expectation) — not
structural.

OpenRouter serves Kimi K2.6 via 18 providers (Io Net, Chutes, Parasail,
DeepInfra, Inceptron, Novita, Venice, SiliconFlow, Fireworks, Moonshot AI,
WandB, AtlasCloud, AkashML, Cloudflare, StreamLake, Nebius, Phala, Together).
Excluding WandB leaves 17 — fallback bandwidth is not a concern.

## Hypothesis discrimination

The pre-diagnostic operator framing offered three hypotheses on Kimi's failure:

| Hypothesis | Status |
|---|---|
| Model produces unparseable output sometimes (capability limit) | **Ruled out** — model produced valid schema-matching JSON in every errored case |
| Reasoning interferes with tool compliance | **Weakened** — reasoning was on across both runs, and excluding WandB eliminated the failure with reasoning unchanged |
| Provider-side tool-call protocol noncompliance | **Confirmed** at n=24, consistent with (b₁) above |

The signature is "WandB's hosted Kimi K2.6 deployment, with reasoning on,
generates the tool's expected JSON output but does not emit it via the
`tool_calls` channel — content carries it instead." Whether this is a WandB
config error, a vllm-with-reasoning interaction at WandB, or some other
provider-level artefact is outside our diagnostic reach without WandB-side
logs. The operational call doesn't require knowing — we exclude WandB.

## `_CANDIDATES` change

```python
"kimi-k2.6": {
    "model": "moonshotai/kimi-k2.6",
    "price_in_per_m": 0.73, "price_out_per_m": 3.49, **_OPENROUTER,
    "disable_thinking": False,
    "extra_body": {"provider": {"ignore": ["WandB"]}},   # ← added
    "notes": "... `extra_body.provider.ignore=['WandB']` pins OpenRouter
             away from the WandB provider — see this diagnostic ...",
}
```

The harness was extended (same commit) to honour candidate-level
`extra_body` independent of `disable_thinking` — previously the
`extra_body` slot was set only by the disable-thinking shortcut. Now the
two compose: candidate-level extra_body forms the base, disable-thinking
keys merge over it. No collision in practice (provider routing lives
under `provider.*`; reasoning toggle under `reasoning.*`).

## Implications

### For the Phase E comparison

The canonical Kimi × extraction run is re-done under the WandB-excluded
config so Kimi's data is structurally clean parallel to GLM's (which had
0 runner_errors without any provider intervention). The old data is
preserved in `with_wandb/` as the diagnostic record. The synthesis reads
Kimi's accuracy off the WandB-excluded run.

GLM × {extraction, predicate_metadata, derivation} all ran with 0
runner_errors against OpenRouter's default provider routing; no GLM re-run
is needed. The Qwen re-run (separately scheduled) is unaffected — Qwen
has no known provider issue.

### For Aedos's LLMClient (v0.16 candidate, not v0.15 work)

The current `extract_with_tool` raises `RuntimeError("no tool call in
OpenAI-compatible response")` strictly when `message.tool_calls` is empty.
The Kimi WandB pattern is one realisation of a broader class:
**open-weight models on multi-provider routing layers vary in tool-call
protocol compliance**. Three plausible mitigations live at the client
layer rather than the candidate-config layer:

1. **Content-fallback parse with schema validation.** When `tool_calls`
   is empty and `content` parses as JSON matching the tool's expected
   output shape, accept it; log via `_record` that the fallback fired.
   The schema validation guards against the "client accepts arbitrary
   text as a tool result" failure mode. The operator's analysis at the
   diagnostic-decision point sketched this with a `matches_tool_schema`
   check + audit event.

2. **Provider-routing audit infrastructure.** Per-call OpenRouter
   `provider` capture, aggregated over a window, would surface a
   recurring WandB-class issue before it reaches the present 42%-rate
   pain. This is what the F3 harness fix did for Phase E specifically;
   generalising it to production is a v0.16 observability item.

3. **Per-purpose default provider exclusions.** `DEFAULT_MODEL_BY_PURPOSE`
   could carry an `extra_body` field whose `provider.ignore` accumulates
   the list of providers Aedos has empirically observed dropping
   tool-call compliance. The Kimi/WandB pair would be the first entry.

None are blockers for Phase E5 (the per-candidate `extra_body` already
gives us a clean Kimi run for the v0.15 comparison). They are v0.16
candidates because the longevity story for open-weight model migration
needs a more durable answer than "patch _CANDIDATES per incident."

## Files

- Canonical (now WandB-excluded) Kimi extraction run:
  `docs/phase_E/results/kimi-k2.6__extraction_corpus.json` (+ transcript).
  Re-run after the `_CANDIDATES` change, overwriting the pre-fix run.
- Preserved old run (with WandB included, 24/57 errors):
  `docs/phase_E/results/with_wandb/kimi-k2.6__extraction_corpus.json`
  (+ transcript with raw responses for every errored case).
- Diagnostic experiment (24-case rerun, WandB excluded):
  `docs/phase_E/results/no_wandb/kimi-k2.6__extraction_corpus.json`
  (+ transcript).
