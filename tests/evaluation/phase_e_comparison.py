"""Phase E — open-weight model comparison harness.

Runs ONE candidate model against ONE calibration corpus and records accuracy,
false-verifieds, LLM-call count, cost, elapsed time, and per-case outcomes.
Writes `docs/phase_E/results/{candidate}__{corpus}.json` and a companion
`.transcript.json` (full per-call request/response, for post-hoc analysis).

This is a measurement instrument, NOT Phase 10.5 calibration. The corpora are
the existing calibration corpora; the runner functions are imported verbatim
from `tests/calibration/test_corpus_runner.py` — they are not modified. The
candidate model is injected by setting `AEDOS_OVERRIDE_MODEL_BY_PURPOSE` so
every internal purpose (extractor, substrate oracles, walker, python_verifier
— not `chat`) routes to it.

Designed to run anywhere: it loads `.env`, routes through the per-purpose
config the LLM client supports, and writes results to disk. It needs no
network to import or unit-test; a real run needs `OPENROUTER_API_KEY` and
egress (and, for `derivation_corpus`, `RUN_LIVE_KB` + Wikidata access — the
walker's KB verifier hits live Wikidata).

Usage:
    py -m tests.evaluation.phase_e_comparison --list
    py -m tests.evaluation.phase_e_comparison <candidate> <corpus>
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from tests.calibration.test_corpus_runner import _Harness

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RESULTS_DIR = _REPO_ROOT / "docs" / "phase_E" / "results"

# Routing endpoint shared by the open-weight candidates.
_OPENROUTER = {"base_url": "https://openrouter.ai/api/v1", "api_key_env_var": "OPENROUTER_API_KEY"}
# Direct-provider endpoints — used by the closed-weight baseline candidates
# (gpt-4.1-mini, claude-*-4.x) added late in Phase E to anchor the open-weight
# numbers against the proprietary models Aedos shipped on at v0.15-rc.8.
_OPENAI_DIRECT = {"base_url": "https://api.openai.com/v1", "api_key_env_var": "OPENAI_API_KEY"}
_ANTHROPIC_DIRECT = {"base_url": None, "api_key_env_var": "ANTHROPIC_API_KEY"}

# Candidate table — `model` IDs and pricing filled from OpenRouter's live
# /models endpoint on 2026-05-19 (Phase E3 prep; see
# docs/phase_E/openrouter_match_report.md). OpenRouter pricing is authoritative
# — `price_in_per_m` / `price_out_per_m` are USD per million tokens, the rates
# the runs will actually pay. A candidate whose `model` is still None is
# refused by run_comparison with a clear error.
# `disable_thinking`: when True the harness routes the candidate with
# _DISABLE_THINKING_EXTRA_BODY so OpenRouter turns reasoning off. All six
# candidates are False — Kimi/GLM/Qwen/Devstral because they have no known
# structured-output bug that would warrant disabling thinking, and DeepSeek V4
# (Flash and Pro) because sending the `reasoning` payload to them triggers an
# upstream Morph grammar-compile failure at ~19% rate (see the Phase E
# diagnostic at docs/phase_E/deepseek_v4_flash_structural_errors.md). The
# DeepSeek runs therefore measure model behaviour with reasoning ON — not a
# measurement choice, a provider-bug workaround. The field is explicit per
# candidate so the policy is one bool flip away if circumstances change.
_CANDIDATES: dict[str, dict] = {
    "deepseek-v4-flash": {
        "model": "deepseek/deepseek-v4-flash",
        "price_in_per_m": 0.112, "price_out_per_m": 0.224, **_OPENROUTER,
        "disable_thinking": False,
        "disabled": ("Disqualified by 10.5% baseline structural-error rate "
                     "(4 Morph grammar-compile failures + 2 empty-message "
                     "failures across 57 cases). Provider-side issues "
                     "documented in "
                     "docs/phase_E/deepseek_v4_flash_structural_errors.md."),
        "notes": "Paid variant. A `deepseek/deepseek-v4-flash:free` exists at $0 "
                 "but its supported_parameters omit structured_outputs/"
                 "response_format. Dropped from the Phase E comparison after "
                 "the post-fix run still showed ~10% structural errors — see "
                 "the `disabled` reason above.",
    },
    "devstral-small-2": {
        "model": "mistralai/devstral-small",
        "price_in_per_m": 0.1, "price_out_per_m": 0.3, **_OPENROUTER,
        "disable_thinking": False,
        "notes": "OpenRouter has no 'Devstral Small 2'; mistralai/devstral-small "
                 "is Devstral Small 1.1 (24B). Filled per operator decision — the "
                 "24B size class is what makes this the code-specialist mid-size "
                 "candidate; Small 1.1 vs Small 2 is a version, not a category, "
                 "difference. If it wins for python_verifier the recommendation "
                 "generalizes to the Devstral Small line. No `reasoning` toggle "
                 "exposed.",
    },
    "qwen-3.5-35b-a3b": {
        "model": "qwen/qwen3.5-35b-a3b",
        "price_in_per_m": 0.139, "price_out_per_m": 1.0, **_OPENROUTER,
        "disable_thinking": False,
        "notes": "Previous-generation 35B-A3B MoE; substituted for the blocked "
                 "qwen3.6-35b-a3b after Phase E3's post-F re-runs hit "
                 "OpenRouter upstream rate limits across every healthy "
                 "provider for the 3.6 variant on 2026-05-21. Probe "
                 "succeeded via Parasail at 1.6s/call. Same MoE-A3B "
                 "architecture as the original target; the BullshitBench "
                 "abstention-behavior rationale carries over within the "
                 "family. Exposes `reasoning`; left enabled.",
    },
    "qwen-3-next-80b-a3b-instruct": {
        "model": "qwen/qwen3-next-80b-a3b-instruct",
        "price_in_per_m": 0.09, "price_out_per_m": 1.1, **_OPENROUTER,
        "disable_thinking": False,
        "notes": "Qwen3 Next 80B A3B Instruct — newer MoE-A3B variant (80B "
                 "total / 3B active), the recent-generation sibling Phase E "
                 "ran as a substitute for the blocked qwen3.6-35b-a3b. Probe "
                 "succeeded via DeepInfra at 1.1s/call. Bigger model class "
                 "than the original target (80B vs 35B total), same active "
                 "parameter footprint (3B). Pricing $0.09/$1.10 per M. "
                 "Exposes `reasoning`; left enabled.",
    },
    "qwen-3.6-35b-a3b": {
        "model": "qwen/qwen3.6-35b-a3b",
        "price_in_per_m": 0.149, "price_out_per_m": 1.0, **_OPENROUTER,
        "disable_thinking": False,
        "extra_body": {"provider": {"ignore": ["AkashML", "Parasail"]}},
        "notes": "35B total / 3B active MoE — distinct from qwen3.6-27b (dense) "
                 "and qwen3.6-plus (API-only). Exposes `reasoning`; left enabled "
                 "(no known structured-output bug). "
                 "`extra_body.provider.ignore=['AkashML','Parasail']` excludes "
                 "the two endpoints currently reporting OpenRouter status=-5 "
                 "(deprecated/unhealthy) on 2026-05-21. The Phase E3 post-F "
                 "extraction re-run before this exclusion hit 47/57=82% "
                 "InternalServerError 503/502 from AkashML ('No healthy "
                 "backends available'). Four healthy providers remain "
                 "(DekaLLM, Ambient, AtlasCloud, WandB).",
    },
    "deepseek-v4-pro": {
        "model": "deepseek/deepseek-v4-pro",
        "price_in_per_m": 0.435, "price_out_per_m": 0.87, **_OPENROUTER,
        "disable_thinking": False,
        "disabled": ("Disqualified by the class-wide DeepSeek V4 issues "
                     "established empirically on V4-Flash (10.5% baseline "
                     "structural-error rate post-fix; 4 Morph grammar-compile "
                     "+ 2 empty-message). Same provider stack on OpenRouter "
                     "(Morph) implies the same failure modes apply. Not "
                     "separately tested. See "
                     "docs/phase_E/deepseek_v4_flash_structural_errors.md."),
        "notes": "1.6T MoE / 49B active, 1M ctx. Dropped from the Phase E "
                 "comparison without separate testing — see the `disabled` "
                 "reason above.",
    },
    "glm-5.1": {
        "model": "z-ai/glm-5.1",
        "price_in_per_m": 0.0, "price_out_per_m": 0.0, **_OPENROUTER,
        "disable_thinking": False,
        "notes": "OpenRouter lists pricing {prompt:'0', completion:'0'} — an "
                 "explicit $0, not a :free model and with full parameter "
                 "support. Likely a launch promotion; pricing is re-verified at "
                 "run time. Exposes `reasoning`; left enabled.",
    },
    # Closed-weight baselines — added late in Phase E to anchor open-weight
    # numbers against the proprietary models Aedos shipped on at v0.15-rc.8.
    # These do NOT go through OpenRouter, so the `_reverify_pricing` check
    # (which queries OpenRouter's /models endpoint) does not apply; each
    # carries `skip_pricing_verify: True` to bypass it. Pricing fields encode
    # the published rates at the time of these runs — they are NOT verified
    # live and must be re-checked before any future production commit.
    "gpt-4.1-mini": {
        "model": "gpt-4.1-mini",
        "price_in_per_m": 0.40, "price_out_per_m": 1.60, **_OPENAI_DIRECT,
        "disable_thinking": False,
        "skip_pricing_verify": True,
        "notes": "OpenAI gpt-4.1-mini — the model shipped as Aedos v0.15's "
                 "default substrate/extractor on rc.8 (per DEFAULT_MODEL_BY_PURPOSE "
                 "in src/aedos/llm/client.py). Included as a baseline anchor; "
                 "Phase E1's stated goal is migration off OpenAI. Pricing as of "
                 "2026-05-23: $0.40/$1.60 per M (published OpenAI rates).",
    },
    "claude-haiku-4-5": {
        "model": "claude-haiku-4-5",
        "price_in_per_m": 1.00, "price_out_per_m": 5.00, **_ANTHROPIC_DIRECT,
        "disable_thinking": False,
        "skip_pricing_verify": True,
        "notes": "Anthropic Claude Haiku 4.5 — Aedos v0.15's `chat` purpose "
                 "model. Included as a baseline. Pricing $1.00/$5.00 per M "
                 "(published Anthropic rates at 2026-05-23).",
    },
    "claude-sonnet-4-6": {
        "model": "claude-sonnet-4-6",
        "price_in_per_m": 3.00, "price_out_per_m": 15.00, **_ANTHROPIC_DIRECT,
        "disable_thinking": False,
        "skip_pricing_verify": True,
        "notes": "Anthropic Claude Sonnet 4.6 — a tier above Haiku, mid-tier "
                 "Claude. Included as a higher-capability baseline anchor. "
                 "Pricing $3.00/$15.00 per M (published Anthropic rates at "
                 "2026-05-23).",
    },
    "kimi-k2.6": {
        "model": "moonshotai/kimi-k2.6",
        "price_in_per_m": 0.73, "price_out_per_m": 3.49, **_OPENROUTER,
        "disable_thinking": False,
        "extra_body": {"provider": {"ignore": ["WandB"]}},
        "notes": "Paid instruct; no separate :free or thinking K2.6 variant on "
                 "OpenRouter (kimi-k2.5 and kimi-k2-thinking are different "
                 "models). Exposes `reasoning`; left enabled. "
                 "`extra_body.provider.ignore=['WandB']` pins OpenRouter away "
                 "from the WandB provider — the canonical Kimi run hit a 42% "
                 "(24/57) `no tool call in OpenAI-compatible response` rate, "
                 "100% routed via WandB; a targeted rerun of those 24 case_ids "
                 "with WandB excluded produced 0/24 structural errors. The "
                 "model emits valid `{claims: [...]}` as `message.content` "
                 "instead of `tool_calls` only when WandB serves the request. "
                 "Documented in docs/phase_E/kimi_k2_6_wandb_provider_diagnostic.md.",
    },
}

# Mitigation for vllm bug #41132 — structured output breaks when a model's
# thinking mode is enabled. For a `disable_thinking` candidate the harness
# routes with this as the call's extra_body, so OpenRouter turns reasoning off.
# `{"enabled": False}` disables reasoning outright; this is deliberately NOT
# `{"exclude": True}`, which only hides reasoning tokens from the response
# while the model still reasons — and so would not fix #41132.
_DISABLE_THINKING_EXTRA_BODY = {"reasoning": {"enabled": False}}

_ALL_CORPORA = (
    "extraction_corpus", "predicate_metadata_corpus",
    "derivation_corpus", "python_verification_corpus",
)
# Corpora that produce a verified/contradicted/no_grounding verdict — only
# these support the false_verified / false_abstention breakdown. extraction and
# predicate_metadata have no verdict; their soundness counts are reported null.
_VERDICT_CORPORA = {"derivation_corpus", "python_verification_corpus"}
_ABSTAIN_VERDICTS = {"no_grounding_found", "no_terminal_result"}


# ---------------------------------------------------------------------------
# Environment / import helpers — the harness must run anywhere
# ---------------------------------------------------------------------------

def _ensure_aedos_importable() -> None:
    try:
        import aedos  # noqa: F401
    except ImportError:
        src = str(_REPO_ROOT / "src")
        if src not in sys.path:
            sys.path.insert(0, src)


def _load_env() -> None:
    """Load `.env` into the process environment (the project keeps API
    keys there). F3 §6 / F-013 introduced `aedos.utils.env.load_dotenv_if_present`
    as the shared utility; this wrapper preserves the local function name
    for back-compat with existing call sites."""
    from aedos.utils.env import load_dotenv_if_present
    load_dotenv_if_present(_REPO_ROOT / ".env")


# ---------------------------------------------------------------------------
# OpenRouter pricing re-verification — a candidate's price in _CANDIDATES is a
# point-in-time snapshot (GLM-5.1's $0 is suspected promotional). Before a
# billed run, re-fetch /models and confirm the price still matches, so a
# changed price surfaces as a finding rather than being spent silently.
# ---------------------------------------------------------------------------

def _fetch_openrouter_models() -> list[dict]:
    """Fetch OpenRouter's /models metadata (no inference cost)."""
    import urllib.request
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set — cannot re-verify pricing")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/models",
        headers={"Authorization": "Bearer " + key, "User-Agent": "aedos-phase-e/0.15"},
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.loads(resp.read()).get("data", [])


def _reverify_pricing(candidate: str, cand: dict, *, models: Optional[list] = None) -> dict:
    """Compare `cand`'s recorded pricing to OpenRouter's live pricing. Returns
    {ok, message, live_in, live_out, recorded_in, recorded_out}. `models` may be
    injected for offline testing; otherwise it is fetched live."""
    if models is None:
        models = _fetch_openrouter_models()
    rec_in, rec_out = cand.get("price_in_per_m"), cand.get("price_out_per_m")
    entry = next((m for m in models if m.get("id") == cand["model"]), None)
    if entry is None:
        return {"ok": False, "live_in": None, "live_out": None,
                "recorded_in": rec_in, "recorded_out": rec_out,
                "message": "%r is no longer listed on OpenRouter" % cand["model"]}
    pr = entry.get("pricing", {})
    live_in = round(float(pr.get("prompt", 0)) * 1e6, 6)
    live_out = round(float(pr.get("completion", 0)) * 1e6, 6)
    changed = (rec_in is None or rec_out is None
               or round(live_in, 4) != round(rec_in, 4)
               or round(live_out, 4) != round(rec_out, 4))
    return {
        "ok": not changed,
        "live_in": live_in, "live_out": live_out,
        "recorded_in": rec_in, "recorded_out": rec_out,
        "message": ("pricing unchanged" if not changed else
                    "pricing CHANGED — recorded $%s/$%s per M, live $%s/$%s per M; "
                    "update _CANDIDATES" % (rec_in, rec_out, live_in, live_out)),
    }


# ---------------------------------------------------------------------------
# Verdict capture — derivation/python_verification produce a verdict the runner
# only folds into a pass/fail bool. To classify false-verified vs
# false-abstention without modifying the runner, observe Walker.walk and
# PythonVerifier.verify for the duration of the run.
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _verdict_recorder():
    from aedos.layer4_sources import python_verifier as _pv_mod
    from aedos.layer4_sources import walker as _walker_mod

    recorded: list[Optional[str]] = []
    orig_walk = _walker_mod.Walker.walk
    orig_verify = _pv_mod.PythonVerifier.verify

    def walk(self, *a, **k):
        result = orig_walk(self, *a, **k)
        recorded.append(getattr(result, "verdict", None))
        return result

    def verify(self, *a, **k):
        result = orig_verify(self, *a, **k)
        recorded.append(getattr(result, "verdict", None))
        return result

    _walker_mod.Walker.walk = walk
    _pv_mod.PythonVerifier.verify = verify
    try:
        yield recorded
    finally:
        _walker_mod.Walker.walk = orig_walk
        _pv_mod.PythonVerifier.verify = orig_verify


# ---------------------------------------------------------------------------
# Harness — the calibration _Harness with the LLM client instrumented for a
# full per-call transcript.
# ---------------------------------------------------------------------------

def _summarize_request(method: str, args: tuple, kwargs: dict) -> dict:
    def _pos(i):
        return args[i] if len(args) > i else None
    if method == "extract_with_tool":
        tool = kwargs.get("tool", _pos(2)) or {}
        return {
            "system": kwargs.get("system", _pos(0)),
            "user_message": kwargs.get("user_message", _pos(1)),
            "tool": tool.get("name") if isinstance(tool, dict) else None,
        }
    messages = kwargs.get("messages", _pos(1)) or []
    return {
        "system": kwargs.get("system", _pos(0)),
        "messages": [getattr(m, "content", str(m)) for m in messages],
    }


def _install_transcript(client: Any, transcript: list) -> None:
    """Wrap the client instance's call methods to record full request/response
    for every call — success AND failure. On failure the entry carries the
    exception's class+message and (if `LLMClient` got that far before the
    failure) the raw SDK response via `exc._raw_response`. Different failure
    modes — provider 503, malformed response, parser raise — all produce a
    transcript entry, so the diagnostic data is comprehensive.

    Instance-level wrapping — no production class is touched."""
    for method in ("extract_with_tool", "chat"):
        orig = getattr(client, method)

        def make(orig, method):
            def wrapped(*args, **kwargs):
                request = _summarize_request(method, args, kwargs)
                try:
                    result = orig(*args, **kwargs)
                except Exception as exc:
                    transcript.append({
                        "method": method,
                        "purpose": kwargs.get("purpose"),
                        "request": request,
                        "response": None,
                        "error": "%s: %s" % (type(exc).__name__, str(exc)[:2000]),
                        "raw_response": getattr(exc, "_raw_response", None),
                    })
                    raise
                transcript.append({
                    "method": method,
                    "purpose": kwargs.get("purpose"),
                    "request": request,
                    "response": result if isinstance(result, (dict, str)) else repr(result),
                    "error": None,
                    "raw_response": None,
                })
                return result
            return wrapped

        setattr(client, method, make(orig, method))


class _ComparisonHarness(_Harness):
    """The calibration `_Harness` with a transcript-instrumented LLM client.
    `transport` (a fake) makes the whole harness offline-testable."""

    def __init__(self, transport: Optional[Any] = None):
        super().__init__()
        self._cmp_transport = transport
        self.transcript: list[dict] = []

    @property
    def client(self):
        if self._client is None:
            from aedos.llm.client import LLMClient
            client = LLMClient(_transport=self._cmp_transport)
            _install_transcript(client, self.transcript)
            self._client = client
        return self._client


# ---------------------------------------------------------------------------
# Classification + cost
# ---------------------------------------------------------------------------

def _classify(corpus: str, passed: bool, produced_verdict: Optional[str], error: Optional[str]) -> str:
    """One of: correct, false_verified, false_contradicted, false_abstention,
    failed, runner_error. The false_* breakdown applies only to the verdict
    corpora; extraction/predicate_metadata collapse to correct/failed."""
    if error is not None:
        return "runner_error"
    if corpus not in _VERDICT_CORPORA:
        return "correct" if passed else "failed"
    if passed:
        return "correct"
    produced = produced_verdict or "no_grounding_found"  # no walk → abstention
    if produced == "verified":
        return "false_verified"
    if produced == "contradicted":
        return "false_contradicted"
    return "false_abstention"


def _cost(in_tokens: int, out_tokens: int, cand: dict) -> Optional[float]:
    pin, pout = cand.get("price_in_per_m"), cand.get("price_out_per_m")
    if pin is None or pout is None:
        return None
    return round(in_tokens / 1e6 * pin + out_tokens / 1e6 * pout, 6)


def _build_outcome(corpus, case, passed, produced, error, records, cand,
                   elapsed_ms, transcript_slice) -> dict:
    in_tok = sum(r.input_tokens for r in records)
    out_tok = sum(r.output_tokens for r in records)
    is_verdict = corpus in _VERDICT_CORPORA
    expected_verdict = (case.get("expected_output") or {}).get("verdict") if is_verdict else None
    return {
        "case_id": case.get("id", "?"),
        "classification": _classify(corpus, passed, produced, error),
        "passed": passed,
        "produced_verdict": produced if is_verdict else None,
        "expected_verdict": expected_verdict,
        "calls": len(records),
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cost_usd": _cost(in_tok, out_tok, cand),
        "elapsed_ms": round(elapsed_ms, 1),
        "error": error,
        "_transcript": transcript_slice,
    }


def _aggregate(candidate, cand, corpus, outcomes, elapsed) -> dict:
    n = len(outcomes)
    passed = sum(1 for o in outcomes if o["passed"] and o["error"] is None)
    errors = sum(1 for o in outcomes if o["classification"] == "runner_error")
    is_verdict = corpus in _VERDICT_CORPORA
    false_verifieds = (
        sum(1 for o in outcomes if o["classification"] == "false_verified")
        if is_verdict else None
    )
    abstentions_on_positive = None
    if is_verdict:
        abstentions_on_positive = sum(
            1 for o in outcomes
            if o["expected_verdict"] == "verified"
            and (o["produced_verdict"] or "no_grounding_found") in _ABSTAIN_VERDICTS
        )
    costs = [o["cost_usd"] for o in outcomes if o["cost_usd"] is not None]
    return {
        "candidate": candidate,
        "model": cand["model"],
        "corpus": corpus,
        "total_cases": n,
        "passed": passed,
        "failed": n - passed - errors,
        "runner_errors": errors,
        "accuracy": round(passed / n, 4) if n else 0.0,
        "false_verifieds": false_verifieds,
        "abstentions_on_positive": abstentions_on_positive,
        "total_calls": sum(o["calls"] for o in outcomes),
        "total_input_tokens": sum(o["input_tokens"] for o in outcomes),
        "total_output_tokens": sum(o["output_tokens"] for o in outcomes),
        "total_cost_usd": round(sum(costs), 6) if costs else None,
        "elapsed_seconds": round(elapsed, 1),
        "per_case_outcomes": [
            {k: v for k, v in o.items() if k != "_transcript"} for o in outcomes
        ],
    }


def _write_result(result: dict, outcomes: list[dict]) -> Path:
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{result['candidate']}__{result['corpus']}"
    out_path = _RESULTS_DIR / f"{stem}.json"
    # `default=str` is insurance against a non-JSON-serializable value sneaking
    # into a raw response model_dump (e.g. a datetime); the diagnostic is more
    # useful with a stringified field than with a write that crashes.
    out_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    transcript = [{"case_id": o["case_id"], "calls": o["_transcript"]} for o in outcomes]
    (_RESULTS_DIR / f"{stem}.transcript.json").write_text(
        json.dumps(transcript, indent=2, default=str), encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# The harness function
# ---------------------------------------------------------------------------

def run_comparison(
    candidate: str,
    corpus_name: str,
    *,
    load_env: bool = True,
    write: bool = True,
    transport: Optional[Any] = None,
    verify_pricing: bool = True,
    case_ids: Optional[Iterable[str]] = None,
    progress: Optional[bool] = None,
    abort_after_consecutive_errors: Optional[int] = 5,
) -> dict:
    """Run `candidate` against `corpus_name`; return the structured result and
    (by default) write it to `docs/phase_E/results/`.

    `transport` injects a fake LLM transport for offline testing — no network,
    no key, no cost. With `transport=None` the run is live and billed.

    `verify_pricing` (live runs only) re-fetches OpenRouter's pricing for the
    candidate before the run and raises if it no longer matches `_CANDIDATES`,
    so a changed price (e.g. GLM-5.1's suspected promo ending) is caught before
    any spend rather than after.

    `case_ids` filters the corpus to just those case ids — used for diagnostic
    reruns of a small subset (e.g. previously-erroring cases) without spending
    on the whole corpus.
    """
    _ensure_aedos_importable()
    if candidate not in _CANDIDATES:
        raise KeyError(f"unknown candidate {candidate!r}; known: {sorted(_CANDIDATES)}")
    if corpus_name not in _ALL_CORPORA:
        raise KeyError(f"unknown corpus {corpus_name!r}; known: {list(_ALL_CORPORA)}")
    cand = _CANDIDATES[candidate]
    if cand.get("disabled") and transport is None:
        raise RuntimeError(
            f"candidate {candidate!r} is disabled: {cand['disabled']}"
        )
    if cand["model"] is None and transport is None:
        raise ValueError(
            f"candidate {candidate!r}: _CANDIDATES[{candidate!r}]['model'] is None — "
            f"the operator must fill the exact OpenRouter model ID before a live run."
        )
    if load_env:
        _load_env()

    # Re-verify pricing before any spend (live runs only — transport runs are
    # free and offline). A changed price aborts the run as a surfaced finding.
    # Closed-weight baselines route to direct providers (not OpenRouter); their
    # pricing comes from candidate-config snapshots, not OpenRouter's /models
    # endpoint. `skip_pricing_verify: True` opts them out of the check.
    pricing_check = None
    if verify_pricing and transport is None and not cand.get("skip_pricing_verify"):
        pricing_check = _reverify_pricing(candidate, cand)
        if not pricing_check["ok"]:
            raise RuntimeError(
                "%s: OpenRouter pricing re-verification failed — %s. Update "
                "_CANDIDATES and re-run, or pass verify_pricing=False to override."
                % (candidate, pricing_check["message"])
            )
    elif cand.get("skip_pricing_verify"):
        pricing_check = {"ok": True, "message": "skipped (closed-weight baseline; "
                         "pricing not OpenRouter-routed)"}

    from tests.calibration.test_corpus_runner import _RUNNERS, _load_corpus
    cases = _load_corpus(corpus_name)
    if case_ids is not None:
        wanted = set(case_ids)
        cases = [c for c in cases if c.get("id") in wanted]
        if not cases:
            raise ValueError("no cases matched case_ids=%r" % sorted(wanted))
    runner = _RUNNERS[corpus_name]

    # Whole-run override: every internal purpose → the candidate (chat excepted).
    purpose_cfg = {
        "model": cand["model"] or f"stub:{candidate}",
        "base_url": cand["base_url"],
        "api_key_env_var": cand["api_key_env_var"],
    }
    # `extra_body` composes two sources: candidate-level (e.g. OpenRouter
    # `provider.ignore` to exclude a flaky provider — see the Kimi K2.6 WandB
    # diagnostic) and the boolean `disable_thinking` shortcut. Both can be set;
    # the merge keeps both keys when they don't collide (the reasoning toggle
    # and provider routing live under different keys in extra_body).
    extra_body = dict(cand.get("extra_body") or {})
    if cand.get("disable_thinking"):
        extra_body.update(_DISABLE_THINKING_EXTRA_BODY)
    if extra_body:
        purpose_cfg["extra_body"] = extra_body
    prev = os.environ.get("AEDOS_OVERRIDE_MODEL_BY_PURPOSE")
    os.environ["AEDOS_OVERRIDE_MODEL_BY_PURPOSE"] = json.dumps({"*": purpose_cfg})

    # Per-case progress printing — defaults: live runs (transport=None) print
    # to stderr so the final JSON to stdout stays clean; transport runs (unit
    # tests) stay quiet unless explicitly enabled. Caller can override via the
    # `progress` kwarg.
    if progress is None:
        progress = transport is None

    outcomes: list[dict] = []
    started = time.monotonic()
    cum_cost = 0.0
    n_cases = len(cases)
    consecutive_errors = 0
    aborted_reason: Optional[str] = None
    try:
        with _verdict_recorder() as recorded:
            harness = _ComparisonHarness(transport=transport)
            client = harness.client
            for idx, case in enumerate(cases, 1):
                recorded.clear()
                client.pop_call_records()
                transcript_start = len(harness.transcript)
                error: Optional[str] = None
                passed = False
                case_started = time.monotonic()
                try:
                    passed = bool(runner(harness, case))
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                elapsed_ms = (time.monotonic() - case_started) * 1000
                records = client.pop_call_records()
                produced = recorded[-1] if recorded else None
                outcome = _build_outcome(
                    corpus_name, case, passed, produced, error, records, cand,
                    elapsed_ms, harness.transcript[transcript_start:],
                )
                outcomes.append(outcome)
                # Consecutive-error abort — catches "every call rate-limits
                # upstream" / "model not deployed" / similar wholesale failure
                # modes early so the rest of the corpus isn't spent on a model
                # that can't serve. The threshold is N consecutive errors from
                # the start of the slice. Per-corpus accuracy failures and
                # transient mid-run errors don't trigger it; only a run that
                # opens with N straight errors does.
                if outcome["classification"] == "runner_error":
                    consecutive_errors += 1
                else:
                    consecutive_errors = 0
                if (abort_after_consecutive_errors is not None
                        and consecutive_errors >= abort_after_consecutive_errors):
                    aborted_reason = (
                        "aborted after %d consecutive runner_errors at the "
                        "start of the run" % consecutive_errors)
                if progress:
                    if outcome["cost_usd"] is not None:
                        cum_cost += outcome["cost_usd"]
                    cls = outcome["classification"]
                    cost = outcome["cost_usd"]
                    cost_str = ("$%.4f" % cost) if cost is not None else "$?    "
                    elapsed_total_s = time.monotonic() - started
                    err_tag = ""
                    if error:
                        # First "ClassName: message"-style chunk; truncated so a
                        # long stack trace doesn't drown the progress line.
                        err_tag = " err=%s" % (error.split(":", 1)[0][:40])
                    line = (
                        "[%2d/%2d] %-32s %-16s calls=%d in=%5d out=%5d %s "
                        "elapsed=%5.1fs cum=$%.4f total_elapsed=%6.1fs%s"
                        % (idx, n_cases, outcome["case_id"], cls,
                           outcome["calls"], outcome["input_tokens"],
                           outcome["output_tokens"], cost_str,
                           elapsed_ms / 1000.0, cum_cost, elapsed_total_s,
                           err_tag)
                    )
                    print(line, file=sys.stderr, flush=True)
                if aborted_reason:
                    if progress:
                        print("ABORTING: " + aborted_reason, file=sys.stderr, flush=True)
                    break
    finally:
        if prev is None:
            os.environ.pop("AEDOS_OVERRIDE_MODEL_BY_PURPOSE", None)
        else:
            os.environ["AEDOS_OVERRIDE_MODEL_BY_PURPOSE"] = prev

    result = _aggregate(candidate, cand, corpus_name, outcomes, time.monotonic() - started)
    result["pricing_verification"] = pricing_check
    if aborted_reason:
        result["aborted_reason"] = aborted_reason
    if write:
        _write_result(result, outcomes)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--list":
        print("Candidates (run order, cheapest first):")
        for name, c in _CANDIDATES.items():
            if c.get("disabled"):
                reason = c["disabled"]
                state = "DISABLED — " + (reason[:60] + "…" if len(reason) > 60 else reason)
            else:
                state = c["model"] or "OPERATOR MUST FILL OpenRouter model ID"
            print(f"  {name:20s} {state}")
        print("Corpora:", ", ".join(_ALL_CORPORA))
        return 0
    if len(argv) != 2:
        print("usage: phase_e_comparison <candidate> <corpus>  (or --list)", file=sys.stderr)
        return 2
    result = run_comparison(argv[0], argv[1])
    print(json.dumps({k: v for k, v in result.items() if k != "per_case_outcomes"}, indent=2))
    print(f"-> docs/phase_E/results/{result['candidate']}__{result['corpus']}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
