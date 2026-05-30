"""Python verifier for Aedos v0.15.

Layer 4 source for the `python` route (architecture §6.3). Generates
Python verification code via the LLM and executes it in the sandbox
defined in `aedos.utils.sandbox`. See that module's docstring for the
threat model and the explicit list of what the sandbox blocks and does
not block.

**Security boundary in writing.** v0.15's sandbox is designed against
LLM-generated wrong code (the common case), not against an active
attacker crafting input to escape the sandbox. Production deployments
handling adversarial input must upgrade per
`docs/phase_F/f3_design.md` §4 (Options B or C).

The walker gates invocation of this verifier on the predicate's
`routing_hint == "python"` (architecture §6.5 step 3, F-042 fix).
The D40 structural test
(`tests/unit/test_layer4_routing_invariants.py`) enforces the gate as
a CI invariant.
"""

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
                    "Python code defining: def verify(subject: str, predicate: str, obj: str) "
                    "-> Optional[bool]. Return True if the claim deterministically holds, "
                    "False if it deterministically does not, or None if verification is "
                    "inherently uncertain — speculative numerical estimates, time-varying "
                    "values without timestamps, contested claims, or anything you cannot "
                    "compute from the allowed stdlib alone. Phase 10.5 §3.2 soundness "
                    "invariant: prefer None over a guessed True/False."
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
    "that returns True if the claim holds, False if it does not, or "
    "None if the claim is inherently uncertain. Examples of None-eligible "
    "claims: speculative numerical estimates (\"grains of sand exceeds 7 "
    "quintillion\"), time-varying values without a timestamp (\"current "
    "stock price\"), or anything you cannot deterministically compute. "
    "Soundness invariant: prefer None over a guessed True/False — the "
    "downstream system will route uncertainty to abstention, which is "
    "always safer than a fabricated verdict. "
    "Allowed imports: datetime, math, decimal, fractions, statistics, re, unicodedata, string. "
    "No other imports. Function signature: "
    "def verify(subject: str, predicate: str, obj: str) -> Optional[bool]"
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
                purpose="python_verifier",
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

        # Build harness — distinguishes None (uncertain → no_terminal_result)
        # from truthy (verified) and falsy non-None (contradicted) so the
        # verify function can honestly abstain on speculative or unverifiable
        # claims. Pre-Phase-10.5 behavior treated None as falsy → contradicted,
        # which forced the generator into a fabricated False on uncertainty
        # (§3.2 soundness violation); the None branch corrects that.
        harness = (
            f"{code}\n"
            f"_result = verify({claim.subject!r}, {claim.predicate!r}, {claim.object!r})\n"
            "if _result is None:\n"
            "    print('NONE')\n"
            "elif _result:\n"
            "    print('TRUE')\n"
            "else:\n"
            "    print('FALSE')\n"
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
        elif raw_out == "NONE":
            verdict = "no_terminal_result"
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
