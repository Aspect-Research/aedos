# Phase E — Plan (empirical model comparison for open-weight migration)

Bounded measurement work before Phase 10.5: compare six open-weight candidate
models against four calibration corpora to inform a per-purpose
`DEFAULT_MODEL_BY_PURPOSE` migration off OpenAI. No production code changes
beyond the LLM client and `DEFAULT_MODEL_BY_PURPOSE`.

Five phases: E1 (client routing) → E2 (comparison harness) → E3 (16 runs) →
E4 (synthesis) → E5 (apply config, tag `v0.15.0-rc.7`).

---

## Blocker — E3/E4/E5 cannot execute in this environment

E3 runs 16 **billed** model comparisons through OpenRouter. E4 synthesizes
their results; E5 applies the operator's choice. All three depend on E3 having
actually run.

`OPENROUTER_API_KEY` is **unset** in this environment (so are
`ANTHROPIC_API_KEY` and `OPENAI_API_KEY` — confirmed last session). This is a
sandboxed environment; outbound API egress to `openrouter.ai` is not
guaranteed even with a key. This is the same wall the live-calibration attempt
hit one session ago.

So this session **cannot run E3**, and therefore cannot produce E4's synthesis
or E5's operator-chosen configuration or the `v0.15.0-rc.7` tag. What it *can*
do, fully and with no billed calls:

- **E1** — the LLM client routing change. Pure code; testable offline via the
  client's existing `_transport` injection (no keys needed).
- **E2** — the comparison harness. Pure code; *writing* it bills nothing.
  *Running* it (E3) is what bills.

**E3 is operator work** — like Phase 10.5's own runbook, it needs real
credentials and network egress. The honest deliverable of this session is E1
and E2: the routing and the harness the operator then runs. E3's results will
**not** be fabricated; E5's `rc.7` tag waits on a real E4 from real E3 data.

Recommended path below; the operator confirms it before E1 starts.

---

## What I cannot verify — the six model IDs

The six candidates (Kimi K2.6, DeepSeek V4-Pro, GLM-5.1, Qwen 3.6 35B-A3B,
DeepSeek V4-Flash, Devstral Small 2) and their cited specs (Artificial Analysis
hallucination rates, BenchLM 87, SWE-bench Pro 58.4, `vllm` issue #41132)
postdate this assistant's January 2026 knowledge cutoff — several version
numbers do not match models known as of then. **This is not a challenge to the
operator's candidate list** (the prompt scopes that as decided, and I take it
as given). It is a practical harness requirement:

E2's harness needs the **exact OpenRouter model-ID string** for each candidate
(e.g. `moonshotai/kimi-k2`, `deepseek/deepseek-v...`), not a marketing name.
OpenRouter's catalog is the source of truth. **The operator must supply the six
exact OpenRouter model IDs** before E3 — and ideally confirm each is currently
listed. The plan files a `_CANDIDATES` table in the harness with a clearly
marked "operator must fill exact OpenRouter ID" column.

---

## Phase E1 — LLM client per-purpose, per-provider routing

### Current state (`src/aedos/llm/client.py`)

`DEFAULT_MODEL_BY_PURPOSE: dict[str, str]` maps a purpose to a *model string*.
Provider routing is **inferred from the model-name prefix**: `is_openai_model()`
checks `gpt-`/`o1-`/`o3-`/`o4-`; everything else goes to the Anthropic SDK.
Two clients: `anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)` and a lazily-built
`openai.OpenAI(api_key=OPENAI_API_KEY)` (no `base_url`).

Prefix-inference breaks for OpenRouter: an ID like `moonshotai/kimi-k2` starts
with neither `gpt-` nor a Claude prefix. Routing must become **explicit**.

### The change

`DEFAULT_MODEL_BY_PURPOSE` becomes a dict of dicts:

```python
DEFAULT_MODEL_BY_PURPOSE = {
    "chat": {
        "model": "claude-haiku-4-5",
        "base_url": None,                       # None → native Anthropic SDK
        "api_key_env_var": "ANTHROPIC_API_KEY",
    },
    "extractor:user": {
        "model": "<openrouter-id>",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env_var": "OPENROUTER_API_KEY",
    },
    # ... extractor:assistant, substrate:*, python_verifier, walker likewise
}
```

Routing rule: `base_url is None` → Anthropic SDK; `base_url` set → the OpenAI
SDK constructed with that `base_url` and the key from `api_key_env_var`
(OpenRouter is OpenAI-API-compatible). `is_openai_model()` prefix inference is
replaced by this explicit lookup. The OpenAI client becomes a small per-base-URL
cache (`{base_url: OpenAI(base_url=…, api_key=…)}`) so OpenRouter and a direct
OpenAI endpoint can coexist.

### Backward compatibility

- `_resolve_purpose_model(purpose, fallback)` keeps returning a *model string*
  (`config["model"]`) for any external caller; internal routing uses the full
  config via a new `_resolve_purpose_config(purpose)`.
- The `AEDOS_MODEL_{purpose}` env override keeps working — it overrides
  `config["model"]` only (base_url/key unchanged).
- The `_transport` injection path (already in `__init__`) is untouched — it is
  how E1's tests run with no keys, and how the mocked suite stays green.
- `chat` stays on Anthropic/Haiku 4.5 — the chat-draft model is out of scope
  (per the prompt; weaker drafts give Aedos more to catch).

### Tests — `tests/unit/test_llm_client_multi_provider.py`

1. A purpose with `base_url` set routes to the OpenAI-compatible path with the
   right base_url; a purpose with `base_url=None` routes to the Anthropic path.
   (Asserted via a fake transport / monkeypatched client constructors — no
   network.)
2. `chat` still routes to Anthropic.
3. A missing `api_key_env_var` raises a clear, named error — not a silent
   `None`-key failure deep in an SDK call.

Expected: +3–5 tests, suite still green.

**Commit:** `Phase E1: LLM client supports per-purpose provider routing`

---

## Phase E2 — Comparison harness

### Model override

`AEDOS_OVERRIDE_MODEL_BY_PURPOSE` — a JSON env var the client checks before
`DEFAULT_MODEL_BY_PURPOSE`. For a comparison run it maps **every internal
purpose** (extractor, substrate:\*, walker, python_verifier — not `chat`) to the
candidate under test, so one model drives the whole pipeline for that run.

### `run_comparison(model_config, corpus_name) -> dict`

Foundation: the existing `_RUNNERS[corpus]` + a live `_Harness`. The harness
sets the override, runs each case, classifies the outcome, sums cost from the
OpenRouter usage metadata (`LLMClient._record` already captures token counts;
OpenRouter returns per-call cost), and writes
`docs/phase_E/results/{model}__{corpus}.json` with the structured result the
prompt specifies (`total_cases`, `passed`, `false_verifieds`,
`abstentions_on_positive`, `total_calls`, `total_cost_usd`, `elapsed_seconds`,
`per_case_outcomes`).

### Per-case classification is corpus-shape-dependent — a real wrinkle

The prompt's classes (`correct`, `false_verified`, `false_contradicted`,
`false_abstention`, `runner_error`) assume every corpus produces a
verified/contradicted/no_grounding **verdict**. Only **`derivation`** does
(among the four; `python_verification` produces verified/contradicted/
no_terminal_result). **`extraction` and `predicate_metadata` have no verdict** —
extraction compares produced claims, predicate_metadata compares metadata
fields. For those two, "false-verified" is structurally undefined.

So the harness classifies per corpus *shape*:

- **derivation** — full classes. `false_verified` = produced `verified` where
  the corpus expected `contradicted`/`no_grounding_found`; this is the
  soundness-critical count. (Needs `_run_derivation` to expose the verdict, not
  just pass/fail — a small harness-side change reading `result.verdict`, not a
  runner change.)
- **python_verification** — `false_verified` = produced `verified` where
  expected `contradicted`.
- **extraction, predicate_metadata** — `correct` / `failed` / `runner_error`
  only; `false_verifieds` and `abstentions_on_positive` reported as `null` (not
  `0`) so the report does not imply a soundness measurement that wasn't made.

The E4 synthesis must not present a `false_verified: 0` for extraction/
predicate_metadata as if it were a soundness result — it is N/A there.

### derivation also needs live Wikidata

`_run_derivation` → walker → KB verifier → **live Wikidata**. A derivation
comparison run is not "model X alone" — it also needs `RUN_LIVE_KB=1` and
Wikidata egress. extraction / predicate_metadata / python_verification are
LLM-only. This is one more reason E3 is operator work, and a caveat for
interpreting derivation costs/timings (Wikidata latency is in the wall-clock).

**Commit:** `Phase E2: comparison harness with cost/outcome tracking`

---

## Phase E3 — Run ordering (operator-run)

16 runs, **cheapest model first** so a malformed-output model is caught before
budget is spent on expensive ones:

1. DeepSeek V4-Flash × {extraction, predicate_metadata, derivation}
2. Devstral Small 2 × {python_verification}
3. Qwen 3.6 35B-A3B × {extraction, predicate_metadata, derivation}
4. DeepSeek V4-Pro × {extraction, predicate_metadata, derivation}
5. GLM-5.1 × {extraction, predicate_metadata, derivation}
6. Kimi K2.6 × {extraction, predicate_metadata, derivation}

**Check-in after run group 1** (V4-Flash's three corpora): verify actual cost
matches projection before continuing. **DeepSeek structured-output bug**: test
V4-Pro/V4-Flash with `thinking` disabled; if OpenRouter exposes no thinking
toggle, surface as a finding. **OpenRouter rate limits**: retry with backoff;
sustained limits → surface, don't wait hours. Per-call logs saved for post-hoc
analysis. After each model's first corpus run, inspect 3–5 per-case outcomes to
confirm the harness classifies correctly (the harness-bias guard).

**Commit per model:** `Phase E3: comparison results for {model}`

---

## Phases E4 / E5 — dependent on E3

E4 (`docs/phase_E_report.md`: per-corpus tables, per-purpose recommendation
with data-cited reasoning, trade-offs, honest gaps, proposed
`DEFAULT_MODEL_BY_PURPOSE`) and E5 (apply the operator's choice, update tests,
`v0.15.0-rc.7`) proceed only once E3 has produced real result files. They are
not startable in this environment.

---

## Recommended path

1. **This session: E1 + E2.** Land the routing change and the harness — both
   pure code, no billed calls, fully covered by offline tests. Two commits.
2. **Operator: E3.** Run the 16 comparisons with `OPENROUTER_API_KEY` (and
   `RUN_LIVE_KB` + Wikidata access for the derivation runs) in an environment
   with egress, cheapest-first, with the post-first-model cost check-in. The
   operator supplies the six exact OpenRouter model IDs into the harness's
   `_CANDIDATES` table first.
3. **Then E4 + E5** — synthesis and the operator-chosen config — in a session
   that has E3's result files.

This delivers the real, useful artifacts now (routing + harness) without
fabricating measurements or tagging `rc.7` on data that does not exist.

## Ambiguities surfaced

- **Environment blocker** (above) — E3/E4/E5 not runnable here.
- **Exact OpenRouter model IDs** — operator must supply; harness leaves a
  marked column.
- **`false_verified` is N/A for extraction & predicate_metadata** — the harness
  reports `null`, not `0`, for those, and E4 must not over-read it.
- **derivation runs need live Wikidata** — not LLM-only; affects cost/timing
  interpretation.
- **DeepSeek thinking/structured-output toggle** — viability-affecting, not a
  footnote; flagged for E3.
