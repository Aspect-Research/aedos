from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..layer1_extraction.extractor import Claim
from ..utils.sandbox import run_code


PYTHON_VERIFY_TOOL: dict[str, Any] = {
    "name": "generate_python_verify",
    "description": (
        "Generate a Python function to verify a factual claim via computation. "
        "Use only allowed stdlib: datetime, math, decimal, fractions, statistics, "
        "re, unicodedata, string."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": (
                    "Python code defining: def verify(subject: str, predicate: str, obj: str) -> bool"
                ),
            },
            "reasoning": {
                "type": "string",
                "description": "Brief explanation of the verification approach.",
            },
        },
        "required": ["code", "reasoning"],
    },
}

_SYSTEM_PROMPT = (
    "You are a Python code generator for factual claim verification. "
    "Given a claim (subject, predicate, object), write a Python function "
    "that returns True if the claim holds, False if it does not. "
    "Allowed imports: datetime, math, decimal, fractions, statistics, re, unicodedata, string. "
    "No other imports. Function signature: def verify(subject: str, predicate: str, obj: str) -> bool"
)

_SANDBOX_TIMEOUT = 5


@dataclass
class PythonVerdict:
    verdict: str  # verified | contradicted | no_terminal_result
    generated_code: str = ""
    inputs: dict = field(default_factory=dict)
    output: Any = None
    runtime_metadata: dict = field(default_factory=dict)


def _extract_code_block(text: str) -> str:
    """Strip markdown fences if present; return raw code."""
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


class PythonVerifier:
    def __init__(self, sandbox=None, llm_client=None) -> None:
        self._sandbox = sandbox  # unused — module-level run_code() used instead
        self._llm = llm_client

    def verify(self, claim: Claim) -> PythonVerdict:
        inputs = {
            "subject": claim.subject,
            "predicate": claim.predicate,
            "object": claim.object,
        }

        if self._llm is None:
            return PythonVerdict(verdict="no_terminal_result", inputs=inputs)

        # LLM code generation
        user_msg = (
            f"Claim: subject={claim.subject!r}, predicate={claim.predicate!r}, object={claim.object!r}\n"
            "Generate the verify() function."
        )
        try:
            tool_result = self._llm.extract_with_tool(
                _SYSTEM_PROMPT,
                user_msg,
                PYTHON_VERIFY_TOOL,
                max_tokens=1024,
                purpose="python_code_generation",
            )
        except Exception as exc:
            return PythonVerdict(
                verdict="no_terminal_result",
                inputs=inputs,
                runtime_metadata={"exception_info": str(exc)},
            )

        raw_code = tool_result.get("code", "")
        if not raw_code:
            return PythonVerdict(verdict="no_terminal_result", inputs=inputs, generated_code="")

        code = _extract_code_block(raw_code)

        # Build harness
        harness = (
            f"{code}\n"
            f"_result = verify({claim.subject!r}, {claim.predicate!r}, {claim.object!r})\n"
            "print('TRUE' if _result else 'FALSE')\n"
        )

        sandbox_result = run_code(harness, timeout_seconds=_SANDBOX_TIMEOUT)

        runtime_metadata: dict[str, Any] = {
            "runtime_ms": sandbox_result.duration_ms,
        }
        if sandbox_result.import_violation:
            runtime_metadata["import_violation"] = sandbox_result.import_violation
            return PythonVerdict(
                verdict="no_terminal_result",
                generated_code=code,
                inputs=inputs,
                output=None,
                runtime_metadata=runtime_metadata,
            )
        if sandbox_result.timed_out:
            runtime_metadata["timed_out"] = True
            return PythonVerdict(
                verdict="no_terminal_result",
                generated_code=code,
                inputs=inputs,
                output=None,
                runtime_metadata=runtime_metadata,
            )
        if not sandbox_result.success:
            runtime_metadata["exception_info"] = sandbox_result.stderr.strip()
            return PythonVerdict(
                verdict="no_terminal_result",
                generated_code=code,
                inputs=inputs,
                output=sandbox_result.stdout,
                runtime_metadata=runtime_metadata,
            )

        raw_out = sandbox_result.stdout.strip()
        if raw_out == "TRUE":
            verdict = "verified"
        elif raw_out == "FALSE":
            verdict = "contradicted"
        else:
            verdict = "no_terminal_result"
            runtime_metadata["unexpected_output"] = raw_out

        return PythonVerdict(
            verdict=verdict,
            generated_code=code,
            inputs=inputs,
            output=raw_out,
            runtime_metadata=runtime_metadata,
        )
