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
from typing import Any, Optional

from tests.calibration.test_corpus_runner import _Harness

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RESULTS_DIR = _REPO_ROOT / "docs" / "phase_E" / "results"

# Routing endpoint shared by the open-weight candidates.
_OPENROUTER = {"base_url": "https://openrouter.ai/api/v1", "api_key_env_var": "OPENROUTER_API_KEY"}

# Candidate table — run order is cheapest-first (E3 operational note 1).
#
# OPERATOR ACTION REQUIRED before E3: fill `model` with the EXACT OpenRouter
# model ID for each candidate (a marketing name will not resolve), and confirm
# `price_in_per_m` / `price_out_per_m` against OpenRouter's live pricing. The
# prices below are the planning-doc figures and may be stale. A candidate whose
# `model` is still None is refused by run_comparison with a clear error; prices
# left None → cost is reported as null (the run is not blocked).
_CANDIDATES: dict[str, dict] = {
    "deepseek-v4-flash": {"model": None, "price_in_per_m": 0.14, "price_out_per_m": 0.14, **_OPENROUTER},
    "devstral-small-2":  {"model": None, "price_in_per_m": None, "price_out_per_m": None, **_OPENROUTER},
    "qwen-3.6-35b-a3b":  {"model": None, "price_in_per_m": None, "price_out_per_m": None, **_OPENROUTER},
    "deepseek-v4-pro":   {"model": None, "price_in_per_m": 0.43, "price_out_per_m": 0.87, **_OPENROUTER},
    "glm-5.1":           {"model": None, "price_in_per_m": 1.05, "price_out_per_m": 3.50, **_OPENROUTER},
    "kimi-k2.6":         {"model": None, "price_in_per_m": 0.95, "price_out_per_m": 0.95, **_OPENROUTER},
}

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
    """Load `.env` into the process environment (the project keeps API keys
    there; nothing else on the calibration path loads it)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(_REPO_ROOT / ".env")


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
    """Wrap the client instance's call methods to record full request/response.
    Instance-level wrapping — no production class is touched."""
    for method in ("extract_with_tool", "chat"):
        orig = getattr(client, method)

        def make(orig, method):
            def wrapped(*args, **kwargs):
                result = orig(*args, **kwargs)
                transcript.append({
                    "method": method,
                    "purpose": kwargs.get("purpose"),
                    "request": _summarize_request(method, args, kwargs),
                    "response": result if isinstance(result, (dict, str)) else repr(result),
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
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    transcript = [{"case_id": o["case_id"], "calls": o["_transcript"]} for o in outcomes]
    (_RESULTS_DIR / f"{stem}.transcript.json").write_text(
        json.dumps(transcript, indent=2), encoding="utf-8")
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
) -> dict:
    """Run `candidate` against `corpus_name`; return the structured result and
    (by default) write it to `docs/phase_E/results/`.

    `transport` injects a fake LLM transport for offline testing — no network,
    no key, no cost. With `transport=None` the run is live and billed.
    """
    _ensure_aedos_importable()
    if candidate not in _CANDIDATES:
        raise KeyError(f"unknown candidate {candidate!r}; known: {sorted(_CANDIDATES)}")
    if corpus_name not in _ALL_CORPORA:
        raise KeyError(f"unknown corpus {corpus_name!r}; known: {list(_ALL_CORPORA)}")
    cand = _CANDIDATES[candidate]
    if cand["model"] is None and transport is None:
        raise ValueError(
            f"candidate {candidate!r}: _CANDIDATES[{candidate!r}]['model'] is None — "
            f"the operator must fill the exact OpenRouter model ID before a live run."
        )
    if load_env:
        _load_env()

    from tests.calibration.test_corpus_runner import _RUNNERS, _load_corpus
    cases = _load_corpus(corpus_name)
    runner = _RUNNERS[corpus_name]

    # Whole-run override: every internal purpose → the candidate (chat excepted).
    purpose_cfg = {
        "model": cand["model"] or f"stub:{candidate}",
        "base_url": cand["base_url"],
        "api_key_env_var": cand["api_key_env_var"],
    }
    prev = os.environ.get("AEDOS_OVERRIDE_MODEL_BY_PURPOSE")
    os.environ["AEDOS_OVERRIDE_MODEL_BY_PURPOSE"] = json.dumps({"*": purpose_cfg})

    outcomes: list[dict] = []
    started = time.monotonic()
    try:
        with _verdict_recorder() as recorded:
            harness = _ComparisonHarness(transport=transport)
            client = harness.client
            for case in cases:
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
                outcomes.append(_build_outcome(
                    corpus_name, case, passed, produced, error, records, cand,
                    elapsed_ms, harness.transcript[transcript_start:],
                ))
    finally:
        if prev is None:
            os.environ.pop("AEDOS_OVERRIDE_MODEL_BY_PURPOSE", None)
        else:
            os.environ["AEDOS_OVERRIDE_MODEL_BY_PURPOSE"] = prev

    result = _aggregate(candidate, cand, corpus_name, outcomes, time.monotonic() - started)
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
