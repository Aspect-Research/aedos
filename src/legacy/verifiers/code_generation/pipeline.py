"""Orchestration of the code-generation verification pipeline.

    prompt builder → code writer → sandbox → comparator

(v0.5) Triage is gone — the LLM router has already decided this claim is
python-verifiable before we get here. If the code can't actually compute
the answer, that surfaces at the sandbox stage as a runtime error
(``code_execution_failed``) or at the comparator as ``comparison_error``.

Each stage emits a pipeline_event so the trace UI can render the full
flow. The status produced here is one of:

    "verified"               — comparator said claim equals computed
    "contradicted"           — comparator said claim != computed
    "code_execution_failed"  — sandbox returned non-zero or timed out
    "comparison_error"       — comparator couldn't parse stdout / extract claim value

The router decides what to do with these statuses.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from src.legacy.fact_store import FactStore
from src.llm_client import LLMClient
from src.legacy.verifiers.code_generation.code_writer import write_code
from src.legacy.verifiers.code_generation.comparator import compare
from src.legacy.verifiers.code_generation.prompt_builder import build_code_prompt
from src.legacy.verifiers.code_generation.sandbox import run_code


CodeGenStatus = str  # Literal so router can string-compare


# The cross-check now uses ``llm.corrector_model`` (the operator's
# active selection) so a single model drives every pipeline step. See
# verify_with_cross_check below for the temperature-variation
# trade-off when that model is Opus 4.7.


@dataclass
class CodeGenVerificationResult:
    """Rich result of a code-generation verification.

    Carries the trace artifacts inline so the UI can render every stage
    without separate fetches.
    """

    status: CodeGenStatus
    explanation: str = ""
    actual_value: Any | None = None
    trace: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "explanation": self.explanation,
            "actual_value": self.actual_value,
            "trace": self.trace,
        }


def _safe_log(
    store: FactStore | None,
    turn_id: int | None,
    stage: str,
    data: dict[str, Any],
) -> None:
    """Log a pipeline event if a store and turn_id are present.

    Logging must never crash verification — swallow exceptions from
    misconfigured stores or unknown stages.
    """
    if store is None or turn_id is None:
        return
    try:
        store.insert_pipeline_event(turn_id, stage, data)
    except Exception:
        pass


def verify_via_code_generation(
    claim: dict,
    llm: LLMClient,
    *,
    store: FactStore | None = None,
    source_turn_id: int | None = None,
    sandbox_timeout_seconds: int = 5,
    code_writer_temperature: float | None = None,
    code_writer_model: str | None = None,
) -> CodeGenVerificationResult:
    """Run the pipeline. Returns a CodeGenVerificationResult.

    ``code_writer_temperature`` (v0.5) is threaded through to the code
    writer so the canonical-constants cross-check can run two
    generations at different temperatures and compare.

    ``code_writer_model`` (v0.5.x) overrides the default corrector
    model for the code-writing call. The cross-check uses this to force
    a temperature-accepting model (Sonnet 4.6) when the default
    (Opus 4.7) silently drops temperature, preserving the cross-check
    variation signal.
    """

    # ---- Stage 1: neutral prompt ---------------------------------
    code_prompt = build_code_prompt(claim, llm)
    # Log every attempt — leaks too, so the trace surfaces them.
    for i, attempt in enumerate(code_prompt.attempts):
        if attempt.leak_detected:
            _safe_log(
                store, source_turn_id, "code_prompt_leakage_detected",
                {
                    "attempt_index": i,
                    "prompt": attempt.prompt,
                    "expected_output_type": attempt.expected_output_type,
                    "compromised": code_prompt.compromised and i == len(code_prompt.attempts) - 1,
                },
            )
    _safe_log(
        store, source_turn_id, "code_prompt_built",
        {
            "prompt": code_prompt.prompt,
            "expected_output_type": code_prompt.expected_output_type,
            "attempts": [a.to_dict() for a in code_prompt.attempts],
            "compromised": code_prompt.compromised,
        },
    )

    # ---- Stage 2: code generation --------------------------------
    generated = write_code(
        code_prompt.prompt, code_prompt.expected_output_type, llm,
        temperature=code_writer_temperature,
        model=code_writer_model,
    )
    _safe_log(
        store, source_turn_id, "code_generated",
        {
            "code": generated.code,
            "model": generated.model,
            "temperature": code_writer_temperature,
        },
    )

    # ---- Stage 3: execute -----------------------------------------
    execution = run_code(generated.code, timeout_seconds=sandbox_timeout_seconds)
    _safe_log(
        store, source_turn_id, "code_executed",
        execution.to_dict(),
    )
    if execution.slow or execution.stderr:
        _safe_log(
            store, source_turn_id, "code_unusual_behavior",
            {
                "slow": execution.slow,
                "duration_ms": execution.duration_ms,
                "stderr": execution.stderr,
                "timed_out": execution.timed_out,
            },
        )

    base_trace: dict[str, Any] = {
        "prompt": code_prompt.to_dict(),
        "code": generated.to_dict(),
        "execution": execution.to_dict(),
    }

    if not execution.success:
        explanation = (
            f"timed out after {sandbox_timeout_seconds}s"
            if execution.timed_out
            else f"exit {execution.exit_code}: {execution.stderr.strip()[:200]}"
        )
        return CodeGenVerificationResult(
            status="code_execution_failed",
            explanation=explanation,
            trace=base_trace,
        )

    # ---- Stage 4: comparator --------------------------------------
    comparison = compare(claim, execution.stdout, code_prompt.expected_output_type)
    _safe_log(
        store, source_turn_id, "code_comparison",
        comparison.to_dict(),
    )

    base_trace["comparison"] = comparison.to_dict()

    return CodeGenVerificationResult(
        status=comparison.verdict,  # "verified" | "contradicted" | "comparison_error"
        explanation=comparison.explanation,
        actual_value=comparison.computed_value,
        trace=base_trace,
    )


class CodeGenerationVerifier:
    """Thin OO wrapper so the router can hold an instance with bound deps.

    Mirrors RetrievalVerifier's shape — store + llm injected at
    construction; ``.verify(claim, source_turn_id=...)`` is the entry
    point.
    """

    def __init__(
        self,
        store: FactStore,
        llm: LLMClient,
        *,
        sandbox_timeout_seconds: int = 5,
    ):
        self.store = store
        self.llm = llm
        self.sandbox_timeout_seconds = sandbox_timeout_seconds

    def verify(
        self, claim: dict, *, source_turn_id: int | None = None,
    ) -> CodeGenVerificationResult:
        return verify_via_code_generation(
            claim,
            self.llm,
            store=self.store,
            source_turn_id=source_turn_id,
            sandbox_timeout_seconds=self.sandbox_timeout_seconds,
        )

    def verify_with_cross_check(
        self, claim: dict, *, source_turn_id: int | None = None,
    ) -> CodeGenVerificationResult:
        """Run code generation twice at different temperatures and compare.

        Used for ``python_with_canonical_constants`` claims, where the
        code may emit a small canonical reference (list of US states,
        primes under 100, etc.). Two independent generations are a cheap
        guard against the LLM emitting a subtly wrong reference.

        Agreement on the computed value → return one of the results.
        Disagreement → log ``canonical_constants_disagreement`` and
        return a result with status ``canonical_constants_disagreement``
        so the router can fall back to retrieval (or surface the
        discrepancy in the trace).
        """
        # The cross-check uses the LLM's currently-active corrector
        # model so the operator's chosen model drives every step
        # uniformly. Trade-off: when the operator picks Opus 4.7 (which
        # silently drops ``temperature``), both iterations run with the
        # same effective settings and the cross-check loses its
        # variation signal — agreement becomes a no-op signal rather
        # than a genuine cross-check. That's the documented cost of
        # selecting Opus across the board; pick Sonnet 4.6 or Haiku 4.5
        # if you want the cross-check to actually disagree on real
        # ambiguity.
        cross_check_model = self.llm.corrector_model

        # v0.9.0: the two generations are independent — only the
        # final agreement comparison needs both results. Dispatching
        # them on a 2-worker pool halves the wall-clock for this
        # claim. Both calls release the GIL on network I/O. SQLite
        # writes (pipeline_event logs) are connection-thread-safe via
        # check_same_thread=False; the per-statement commits serialize
        # at the SQLite mutex.
        def _run(temp: float):
            return verify_via_code_generation(
                claim,
                self.llm,
                store=self.store,
                source_turn_id=source_turn_id,
                sandbox_timeout_seconds=self.sandbox_timeout_seconds,
                code_writer_temperature=temp,
                code_writer_model=cross_check_model,
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(_run, 0.0)
            future_b = pool.submit(_run, 0.3)
            result_a = future_a.result()
            result_b = future_b.result()

        cross_check_payload = {
            "a": {
                "status": result_a.status,
                "actual_value": result_a.actual_value,
                "explanation": result_a.explanation,
                "code": result_a.trace.get("code"),
                "execution": result_a.trace.get("execution"),
            },
            "b": {
                "status": result_b.status,
                "actual_value": result_b.actual_value,
                "explanation": result_b.explanation,
                "code": result_b.trace.get("code"),
                "execution": result_b.trace.get("execution"),
            },
        }
        _safe_log(
            self.store, source_turn_id,
            "canonical_constants_cross_check", cross_check_payload,
        )

        agree = (
            result_a.status == result_b.status
            and result_a.status in {"verified", "contradicted"}
            and result_a.actual_value == result_b.actual_value
        )
        if agree:
            # Stash the cross-check trace on the chosen result so the UI
            # can render both generations side-by-side.
            chosen = result_a
            chosen.trace = dict(chosen.trace)
            chosen.trace["cross_check"] = cross_check_payload
            return chosen

        _safe_log(
            self.store, source_turn_id,
            "canonical_constants_disagreement", cross_check_payload,
        )
        return CodeGenVerificationResult(
            status="canonical_constants_disagreement",
            explanation=(
                "two independent code generations disagreed: "
                f"a={result_a.status}({result_a.actual_value!r}), "
                f"b={result_b.status}({result_b.actual_value!r})"
            ),
            actual_value=None,
            trace={
                "cross_check": cross_check_payload,
                "a": result_a.trace,
                "b": result_b.trace,
            },
        )
