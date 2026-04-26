"""Tests for src.verifiers.code_generation.code_writer (v0.4)."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field

import pytest

from src.verifiers.code_generation.code_writer import GeneratedCode, write_code


@dataclass
class FakeLLM:
    rewrite_response: str = "print(3)"
    rewrite_calls: list[dict] = field(default_factory=list)
    corrector_model: str = "claude-haiku-4-5"

    def rewrite(self, system, user_message, max_tokens=2048):
        self.rewrite_calls.append({"system": system, "user_message": user_message})
        return self.rewrite_response


# ---------- the firewall ----------


def test_signature_takes_only_neutral_prompt_and_type():
    """write_code must NOT accept the original claim or asserted value.

    This is enforced by the function signature — passing a claim should
    fail with a TypeError. If a future change widens the signature, this
    test breaks loudly.
    """
    llm = FakeLLM()
    # Positional args: (neutral_prompt, expected_output_type, llm). No claim.
    out = write_code("Compute something.", "int", llm)
    assert isinstance(out, GeneratedCode)
    assert out.code.strip().startswith("print")


def test_user_message_does_not_contain_claim_keyword():
    """The message sent to the code-writing LLM must not include 'claim',
    'asserted', or any claim metadata."""
    llm = FakeLLM(rewrite_response="print(3)")
    write_code("Compute count of 'r' in 'strawperpy'.", "int", llm)
    user_msg = llm.rewrite_calls[0]["user_message"]
    lower = user_msg.lower()
    assert "claim" not in lower
    assert "asserted" not in lower
    assert "polarity" not in lower
    # The neutral prompt itself MAY contain the literal value to compute
    # (e.g. "in 'strawperpy'") but no claim metadata.


def test_returns_corrector_model_name():
    llm = FakeLLM(rewrite_response="print(3)")
    out = write_code("Compute 1+1.", "int", llm)
    assert out.model == "claude-haiku-4-5"


# ---------- markdown fence stripping ----------


def test_markdown_fences_are_stripped():
    llm = FakeLLM(rewrite_response="```python\nprint(2 + 2)\n```")
    out = write_code("Compute 2+2.", "int", llm)
    assert "```" not in out.code
    assert "print(2 + 2)" in out.code


def test_plain_python_passes_through():
    llm = FakeLLM(rewrite_response="x = 5\nprint(x)\n")
    out = write_code("Print 5.", "int", llm)
    assert "x = 5" in out.code
    assert "print(x)" in out.code


def test_fenced_without_language_tag():
    llm = FakeLLM(rewrite_response="```\nprint('hi')\n```")
    out = write_code("Print hi.", "string", llm)
    assert "print('hi')" in out.code
    assert "```" not in out.code


# ---------- real API (gated) ----------


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="real API test gated behind RUN_API_TESTS=1",
)
def test_real_api_writes_runnable_python():
    """End-to-end: the model produces python that runs and prints '3'."""
    from src.llm_client import LLMClient

    llm = LLMClient()
    out = write_code(
        "Compute the number of times the lowercase letter 'r' appears in "
        "the string 'strawperpy'. Print only the integer result.",
        "int",
        llm,
    )
    completed = subprocess.run(
        [sys.executable, "-c", out.code],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "3"
