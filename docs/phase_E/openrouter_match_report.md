# Phase E3 prep — OpenRouter model ID + pricing match

Matched the six Phase E candidates against OpenRouter's live model catalogue
and filled `_CANDIDATES` in `tests/evaluation/phase_e_comparison.py`. No billed
inference calls — only the metadata endpoint.

## The API call (evidence it completed live)

`GET https://openrouter.ai/api/v1/models`, `Authorization: Bearer
$OPENROUTER_API_KEY` (key loaded from `.env`).

| | |
|---|---|
| HTTP status | `200` |
| Response size | 424,803 bytes |
| Elapsed | 0.31 s |
| `Date` header | Tue, 19 May 2026 20:44:35 GMT |
| Rate-limit headers | **none returned** — the response carried no `x-ratelimit-*` / `x-requests-*` headers (the models-list endpoint is unmetered metadata) |
| Total models in catalogue | 357 |
| Candidate-relevant entries (moonshotai/ deepseek/ z-ai/ qwen/ mistralai/) | 103 |

The previous handoff's reachability concern is resolved: the authenticated,
directed call completed cleanly.

## Matches — 5 of 6 filled, 1 unmatched

| Candidate | OpenRouter ID | $/M in | $/M out | Status |
|---|---|---|---|---|
| Kimi K2.6 | `moonshotai/kimi-k2.6` | 0.73 | 3.49 | exact |
| DeepSeek V4-Pro | `deepseek/deepseek-v4-pro` | 0.435 | 0.87 | exact |
| DeepSeek V4-Flash | `deepseek/deepseek-v4-flash` | 0.112 | 0.224 | exact (paid) |
| GLM-5.1 | `z-ai/glm-5.1` | 0.00 | 0.00 | exact ID — ⚠ $0 pricing |
| Qwen 3.6 35B-A3B | `qwen/qwen3.6-35b-a3b` | 0.15 | 1.00 | exact |
| Devstral Small 2 | — | — | — | **NO MATCH — unfilled** |

Pricing is OpenRouter's live `pricing.prompt` / `pricing.completion` × 1e6
(USD per million tokens) — authoritative, and superseding the planning-doc
figures (which were stale on every candidate that has a price: V4-Pro $0.43→
$0.435/$0.87, V4-Flash $0.14 flat → $0.112/$0.224, GLM-5.1 $1.05/$3.50 → $0,
Kimi $0.95 flat → $0.73/$3.49).

### Per-candidate notes

**Kimi K2.6 — `moonshotai/kimi-k2.6`.** Only one K2.6 entry — no `:free`
preview, no separate thinking variant. (`moonshotai/kimi-k2.5` and
`moonshotai/kimi-k2-thinking` are distinct models, not selected.) Clean.

**DeepSeek V4-Pro — `deepseek/deepseek-v4-pro`.** 1.6T-param MoE, 49B active,
1M context. One entry, no `:free`. Clean.

**DeepSeek V4-Flash — `deepseek/deepseek-v4-flash`.** Paid variant selected.
A `deepseek/deepseek-v4-flash:free` also exists at $0 — **not** selected: its
`supported_parameters` are stripped to `[include_reasoning, reasoning,
tool_choice, tools]`, dropping `structured_outputs` and `response_format`, and
(per the K2.6 selection rule) a free tier carries undocumented rate limits.
The paid variant has the full parameter set.

**GLM-5.1 — `z-ai/glm-5.1`.** ID is an exact, unambiguous match (under the
`z-ai/` prefix; `thudm/` and `zhipu/` returned nothing; `z-ai/glm-5` and
`z-ai/glm-5-turbo` are different models). **Pricing anomaly:** OpenRouter
returns `pricing: {"prompt": "0", "completion": "0"}` — an explicit zero. It is
**not** a `:free`-suffixed model and it has full parameter support, unlike the
stripped-down free DeepSeek variant. Sibling models are normally priced
(`z-ai/glm-5` $0.60/$1.92, `z-ai/glm-4.7` $0.40/$1.75), so this is almost
certainly a launch promotion or a temporary zero-rating. `_CANDIDATES` records
`0.0/0.0` as authoritative-now, but **the operator must re-confirm GLM-5.1
pricing at E3 run time** — if the promotion ends before the run, the cost
projection is wrong, and a $0 model often carries an undisclosed free-tier rate
limit.

**Qwen 3.6 35B-A3B — `qwen/qwen3.6-35b-a3b`.** The 35B-total / 3B-active MoE,
exactly as specified. Distinct catalogue entries `qwen/qwen3.6-27b` (dense
27B), `qwen/qwen3.6-plus` (API-only), `qwen/qwen3.6-flash`, and
`qwen/qwen3.6-max-preview` were correctly **not** selected.

### Devstral Small 2 — no clean match, left unfilled

`mistralai/` lists three Devstral models; **none is "Devstral Small 2"**:

- `mistralai/devstral-small` — name on OpenRouter is **"Devstral Small 1.1"**
  (24B, finetuned from Mistral Small 3.1). This is the right *size class* but
  an **earlier version**, not "Small 2".
- `mistralai/devstral-2512` — name **"Devstral 2 2512"**. This *is* a
  "Devstral 2", but its description states **123B-parameter dense** — the
  large model, **not** the 24B "Small" class the candidate calls for.
- `mistralai/devstral-medium` — "Devstral Medium", a different tier.

Per the task's instruction ("if a candidate has no exact match … report which
one and stop … don't substitute a similar model unilaterally"),
`devstral-small-2` is left with `model: None`. `--list` continues to show
"OPERATOR MUST FILL" for it, and `run_comparison` refuses a live run on it.

**Operator decision needed.** The plausible substitutes are `devstral-small`
(Devstral Small 1.1 — same 24B size, prior version) or `devstral-2512`
(Devstral 2 — current generation, but 123B dense, a different size class). Both
are deliberate trade-offs, not a unilateral pick — so this prep step does not
make the choice.

## Thinking toggle — DeepSeek V4-Pro and V4-Flash

Both expose a thinking/reasoning toggle. Their `supported_parameters` include
`reasoning` and `include_reasoning` (and `structured_outputs`,
`response_format`). OpenRouter's `reasoning` request parameter therefore lets a
caller disable thinking — relevant to vllm bug #41132 (structured output broken
when thinking is enabled).

**The harness does not yet send this parameter.** Disabling thinking for the
DeepSeek runs requires the LLM client / harness to pass `reasoning` (e.g.
`{"enabled": false}`) on DeepSeek calls — a harness-logic change, explicitly
out of this prep step's scope ("only the `_CANDIDATES` table changes"). It is
recorded in the `_CANDIDATES` notes for those two entries and flagged here so
it is handled before E3's DeepSeek runs. (Kimi K2.6, GLM-5.1, and Qwen
3.6-35B-A3B also expose `reasoning`; only the DeepSeek pair has the known
structured-output bug.)

## A field-name note

The task description referred to `input_cost_per_million` /
`output_cost_per_million`; the harness's `_CANDIDATES` and `_cost()` use
`price_in_per_m` / `price_out_per_m`. The existing field names were kept —
renaming them would touch `_cost()`, i.e. harness logic, which this step does
not change. The values filled are the same per-million-USD figures.

## Result

`py -m tests.evaluation.phase_e_comparison --list` now shows concrete IDs and
pricing for 5 of 6 candidates; `devstral-small-2` shows "OPERATOR MUST FILL"
pending the operator's variant choice. `pytest tests/ -q`: 744 passed
(`test_unfilled_candidate_without_transport_is_refused` was repointed from
`kimi-k2.6`, now filled, to `devstral-small-2`, still unfilled).

E3 cannot start its full 16-run plan until `devstral-small-2` is resolved and
GLM-5.1 pricing is re-confirmed.
