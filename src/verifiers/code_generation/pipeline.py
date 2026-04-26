"""Orchestration of the four-stage code-generation verification.

    triage → prompt builder → code writer → sandbox → comparator

Each stage emits a pipeline_event so the trace UI can render the full
flow. The status produced here is one of:

    "verified"               — comparator said claim equals computed
    "contradicted"           — comparator said claim != computed
    "not_python_verifiable"  — triage said the claim isn't python-resolvable
    "code_execution_failed"  — sandbox returned non-zero or timed out
    "comparison_error"       — comparator couldn't parse stdout / extract claim value

The router decides what to do with these statuses (Section 8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.fact_store import FactStore
from src.llm_client import LLMClient
from src.verifiers.code_generation.code_writer import write_code
from src.verifiers.code_generation.comparator import compare
from src.verifiers.code_generation.prompt_builder import build_code_prompt
from src.verifiers.code_generation.sandbox import run_code
from src.verifiers.code_generation.triage import triage_claim


CodeGenStatus = str  # Literal so router can string-compare


@dataclass
class CodeGenVerificationResult:
    """Rich result of a code-generation verification.

    Carries the trace artifacts inline so the UI can render every stage
    without separate fetches.
    """

    status: CodeGenStatus
    confidence: float = 0.99
    explanation: str = ""
    actual_value: Any | None = None
    trace: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "confidence": self.confidence,
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
) -> CodeGenVerificationResult:
    """Run the four-stage pipeline. Returns a CodeGenVerificationResult."""

    # ---- Stage 1: triage ------------------------------------------
    triage = triage_claim(claim, llm)
    _safe_log(
        store, source_turn_id, "code_triage",
        {"verifiable": triage.verifiable, "reason": triage.reason},
    )
    if not triage.verifiable:
        return CodeGenVerificationResult(
            status="not_python_verifiable",
            explanation=triage.reason,
            trace={"triage": triage.to_dict()},
        )

    # ---- Stage 2: neutral prompt ---------------------------------
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

    # ---- Stage 3: code generation --------------------------------
    generated = write_code(
        code_prompt.prompt, code_prompt.expected_output_type, llm,
    )
    _safe_log(
        store, source_turn_id, "code_generated",
        {"code": generated.code, "model": generated.model},
    )

    # ---- Stage 4: execute -----------------------------------------
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
        "triage": triage.to_dict(),
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

    # ---- Stage 5: comparator --------------------------------------
    comparison = compare(claim, execution.stdout, code_prompt.expected_output_type)
    _safe_log(
        store, source_turn_id, "code_comparison",
        comparison.to_dict(),
    )

    base_trace["comparison"] = comparison.to_dict()

    return CodeGenVerificationResult(
        status=comparison.verdict,  # "verified" | "contradicted" | "comparison_error"
        confidence=0.99,
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
