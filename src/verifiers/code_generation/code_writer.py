"""Stage 3 — code generation.

Receives ONLY the neutral prompt from Stage 2 plus the expected output
type. The original claim, the asserted value, and the conversation
context are not visible — the function signature enforces that.

This is the firewall in action. The code is written to compute a value,
not to validate a hypothesis.

Uses the corrector model (Haiku 4.5 if AEDOS_CORRECTOR_MODEL points
there) — code generation for these scoped problems is well within
Haiku's ability and the cost matters because this stage runs on every
python-verifiable claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.llm_client import LLMClient


_CODE_WRITER_SYSTEM = """You are a deterministic code-writing assistant.

Write a complete, self-contained Python script that resolves the question below. The script MUST:

  - Use only the Python standard library.
  - Print exactly ONE value to stdout, of the requested type.
  - Print nothing else: no prefix, no explanation, no trailing whitespace beyond a single newline.
  - Contain no comments and no reasoning.

Output ONLY the script — no markdown, no fences, no preamble. The first line of your reply must be the first line of the program.

# Format examples

Question: "Compute 2 + 2. Print only the integer result."
expected_output_type: int
Reply:
print(2 + 2)

Question: "Compute the lowercase reverse of 'cat'. Print only the resulting string."
expected_output_type: string
Reply:
print('cat'[::-1].lower())

Question: "Compute whether 7 is prime. Print True or False."
expected_output_type: bool
Reply:
n = 7
print(n > 1 and all(n % i for i in range(2, int(n**0.5) + 1)))"""


@dataclass
class GeneratedCode:
    code: str
    model: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "model": self.model}


def _strip_markdown_fences(text: str) -> str:
    """Some models still wrap code in ``` despite instructions. Tolerate it."""
    s = text.strip()
    if s.startswith("```"):
        # Drop opening fence (with optional language tag).
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1 :]
        # Drop trailing fence.
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3].rstrip()
    return s.rstrip() + "\n"


def write_code(
    neutral_prompt: str,
    expected_output_type: str,
    llm: LLMClient,
) -> GeneratedCode:
    """Generate python from a neutral prompt.

    The signature is the firewall: this function takes ONLY the neutral
    prompt and the expected output type — no claim, no asserted value,
    no conversation context. Do not weaken the signature.
    """
    user_message = (
        f"Question: {neutral_prompt}\n"
        f"expected_output_type: {expected_output_type}\n\n"
        "Reply with the complete Python script and nothing else."
    )
    raw = llm.rewrite(_CODE_WRITER_SYSTEM, user_message)
    code = _strip_markdown_fences(raw)
    return GeneratedCode(code=code, model=llm.corrector_model)
