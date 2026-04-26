"""Stage 2 — neutral prompt construction (the firewall).

Given a python-verifiable claim, produce a precise question for a code-
writing assistant that DOES NOT reveal the claim's asserted value.

This stage sees the full claim (so it can articulate the question
precisely) but its output is constrained to a question that omits the
asserted answer. Stage 3 will see ONLY this question.

A leak detector scans the produced prompt for stringifications of the
asserted value and triggers a single retry on detection. This is
heuristic — sophisticated leakage (the value re-expressed semantically)
is not caught and is a known limitation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from src.llm_client import LLMClient
from src.verifiers.code_generation.claim_value import extract_claimed_value


_VALID_OUTPUT_TYPES = {"int", "float", "string", "bool", "list"}


@dataclass
class PromptAttempt:
    prompt: str
    expected_output_type: str
    leak_detected: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "expected_output_type": self.expected_output_type,
            "leak_detected": self.leak_detected,
        }


@dataclass
class CodePrompt:
    prompt: str
    expected_output_type: str
    attempts: list[PromptAttempt] = field(default_factory=list)
    compromised: bool = False  # True iff every attempt leaked

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "expected_output_type": self.expected_output_type,
            "attempts": [a.to_dict() for a in self.attempts],
            "compromised": self.compromised,
        }


_PROMPT_TOOL = {
    "name": "record_neutral_prompt",
    "description": (
        "Record a neutral instruction for a code-writing assistant that "
        "answers the question the claim addresses, WITHOUT revealing the "
        "asserted answer."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Self-contained instruction telling a code-writing "
                    "assistant exactly what value to compute and print. "
                    "Must NOT contain the claim's asserted value."
                ),
            },
            "expected_output_type": {
                "type": "string",
                "enum": sorted(_VALID_OUTPUT_TYPES),
                "description": (
                    "The python type the script should print. The "
                    "comparator uses this to parse stdout."
                ),
            },
        },
        "required": ["prompt", "expected_output_type"],
    },
}


_PROMPT_SYSTEM = """You translate a structured factual claim into a NEUTRAL instruction for a separate code-writing assistant.

# The firewall

The code-writing assistant must NOT see what the claim asserts the answer to be. If your instruction contains the asserted value, the firewall is broken — the code writer will be biased toward producing code that confirms the claim. Compute the question the claim is *asking* and articulate that question, not the claim itself.

# Rules

- The instruction must be self-contained, precise, and reproducible.
- The instruction must specify exactly what value to compute and what to print.
- The instruction must NOT contain the asserted value (the slot whose value is being verified — typically `value` for quantitative, `subject` for `reverse_of` relational, or the claim's polarity for boolean relational predicates).
- The instruction must use literal inputs from the claim's slots — quoting strings, naming numbers — but the *answer* must be computed by the script, not stated.
- Choose `expected_output_type` from: int, float, string, bool, list. The script will print exactly one value of this type.
- The instruction should ask for a clean output: "Print only the integer result", "Print only the resulting string with no quotes or extra whitespace", etc.

# Worked examples

Claim: pattern=quantitative, predicate=has_count, slots={subject:'strawperpy', property:'letter_r', value:3}, polarity=1
Correct neutral prompt:
  "Compute the number of times the lowercase letter 'r' appears in the string 'strawperpy'. Print only the integer result."
  expected_output_type: int
LEAK CHECK: the output omits "3". Good.

Claim: pattern=quantitative, predicate=prime_count, slots={subject:'primes between 1 and 100', value:25}, polarity=1
Correct neutral prompt:
  "Compute the count of prime numbers strictly greater than 1 and strictly less than 100. Print only the integer result."
  expected_output_type: int
LEAK CHECK: the output omits "25". Good.

Claim: pattern=relational, predicate=reverse_of, slots={subject:'nairatilage', object:'egalitarian'}, polarity=1
Correct neutral prompt:
  "Compute the string that results from reversing the characters of the string 'egalitarian'. Print only the resulting string, with no quotes or extra whitespace."
  expected_output_type: string
LEAK CHECK: the output omits "nairatilage" — the subject IS the asserted answer, do not include it.

Claim: pattern=relational, predicate=is_anagram_of, slots={subject:'listen', object:'silent'}, polarity=1
Correct neutral prompt:
  "Determine whether the strings 'listen' and 'silent' contain exactly the same multiset of alphabetic characters (case-insensitive, non-letters ignored). Print True or False."
  expected_output_type: bool
LEAK CHECK: bool questions don't have a numeric/string answer to leak — but you must NOT prejudge ("decide whether YES is correct" would leak the polarity).

Claim: pattern=quantitative, predicate=has_length, slots={subject:'hello', value:5}, polarity=1
Correct neutral prompt:
  "Compute the length of the string 'hello' (number of characters). Print only the integer result."
  expected_output_type: int

Claim: pattern=relational, predicate=contains_substring, slots={subject:'strawperpy', object:'berry'}, polarity=1
Correct neutral prompt:
  "Determine whether the string 'berry' is a substring of the string 'strawperpy' (case-sensitive). Print True or False."
  expected_output_type: bool

# Output

Always call the `record_neutral_prompt` tool exactly once."""


def _build_user_message(claim: dict) -> str:
    return (
        "Claim:\n"
        f"  pattern: {claim.get('pattern')!r}\n"
        f"  predicate: {claim.get('predicate')!r}\n"
        f"  slots: {json.dumps(claim.get('slots') or {}, default=str)}\n"
        f"  polarity: {claim.get('polarity')!r}\n\n"
        "Construct a neutral prompt and call record_neutral_prompt. "
        "Do NOT include the asserted value in the prompt."
    )


def _stringifications(value: Any) -> list[str]:
    """Return common stringifications of an asserted value, for leak detection.

    Booleans are intentionally skipped — "true"/"false" appear naturally
    in code-writing instructions and would produce false positives.
    """
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [str(value)]
    if isinstance(value, float):
        out = [str(value)]
        if value.is_integer():
            out.append(str(int(value)))
        return out
    if isinstance(value, str):
        s = value.strip()
        # Avoid flagging short generic words; "a", "is", etc. would create
        # false positives. The asserted-value strings we care about
        # (e.g. "nairatilage") are typically distinctive.
        return [s] if len(s) >= 3 else []
    if isinstance(value, list):
        try:
            return [json.dumps(value)]
        except (TypeError, ValueError):
            return []
    return [str(value)]


def detect_leak(prompt: str, claim: dict) -> bool:
    """Heuristic: does ``prompt`` contain a stringification of the claim's asserted value?"""
    asserted = extract_claimed_value(claim)
    candidates = _stringifications(asserted)
    if not candidates:
        return False
    p_lower = prompt.lower()
    for c in candidates:
        c_lower = str(c).lower()
        if isinstance(asserted, (int, float)):
            # Word-boundary match so "25" doesn't match inside "1259".
            if re.search(rf"\b{re.escape(c_lower)}\b", p_lower):
                return True
        else:
            if c_lower in p_lower:
                return True
    return False


def _call_llm_for_prompt(claim: dict, llm: LLMClient) -> tuple[str, str]:
    raw = llm.extract_with_tool(
        system=_PROMPT_SYSTEM,
        user_message=_build_user_message(claim),
        tool=_PROMPT_TOOL,
    )
    prompt = str(raw.get("prompt") or "").strip()
    output_type = str(raw.get("expected_output_type") or "").strip().lower()
    if output_type not in _VALID_OUTPUT_TYPES:
        # Be conservative — coerce unknown types to "string". The
        # comparator can still parse, just with weaker guarantees.
        output_type = "string"
    return prompt, output_type


def build_code_prompt(claim: dict, llm: LLMClient, *, max_retries: int = 1) -> CodePrompt:
    """Produce a leak-checked neutral prompt.

    On leak detection, retry up to ``max_retries`` times. If every attempt
    leaks, return the last attempt with ``compromised=True`` so the
    orchestrator can flag the verification.
    """
    attempts: list[PromptAttempt] = []
    last_prompt = ""
    last_type = "string"
    for _ in range(max_retries + 1):
        prompt, out_type = _call_llm_for_prompt(claim, llm)
        leaked = detect_leak(prompt, claim)
        attempts.append(PromptAttempt(
            prompt=prompt,
            expected_output_type=out_type,
            leak_detected=leaked,
        ))
        last_prompt, last_type = prompt, out_type
        if not leaked:
            return CodePrompt(
                prompt=prompt,
                expected_output_type=out_type,
                attempts=attempts,
                compromised=False,
            )
    return CodePrompt(
        prompt=last_prompt,
        expected_output_type=last_type,
        attempts=attempts,
        compromised=True,
    )
