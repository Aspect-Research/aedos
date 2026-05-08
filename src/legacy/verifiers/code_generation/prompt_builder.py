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
from src.legacy.verifiers.code_generation.claim_value import extract_claimed_value


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

# Directional claims — lock the convention in the prompt

Whenever a claim asserts a SIGNED difference, an "X is N more/less/
ahead/behind/before/after Y" comparison, or any value whose sign
encodes a direction, the prompt must explicitly state the
subtraction order AND what positive vs negative mean. Without this,
the code may compute either order and the comparator's literal
compare will read the right answer with a flipped sign as a
contradiction.

The general rule: for "X is N <direction> Y" claims, the prompt
should ask for `(X's value) − (Y's value)` — subject first, object
second — and state in plain English what positive means. Match the
claim's framing exactly so the verifier's verdict is interpretable.

This applies to:

  - Time differences ("Cairo is 7 hours ahead of NY")
  - Date / age differences ("X is 3 days after Y", "A is 5 years older than B")
  - Magnitude comparisons ("Population of A is 2× B's", "X is 30% taller than Y")
  - Stock / metric movements ("up 5% from yesterday")

For unit-bearing claims (times, dates, currencies, percentages),
the prompt should also specify the exact unit being asked. Don't
strip resolution that the claim carries — "9:56 AM" should be
compared against an HH:MM string, not against a bare hour;
"5.7%" against a float, not an int.

Claim: pattern=quantitative, predicate=current_time, slots={subject:'Cairo', property:'time', value:'9:56 AM'}, polarity=1
Correct neutral prompt:
  "Compute the current local time in Cairo, formatted as H:MM AM/PM (12-hour clock, lowercase 'am'/'pm', no leading zero on the hour). Print only the resulting string."
  expected_output_type: string
LEAK CHECK: omits "9:56 am". Good. Sign convention not applicable.

Claim: pattern=quantitative, predicate=time_difference, slots={subject:'Cairo', object:'New York', property:'hours_ahead', value:7}, polarity=1
Correct neutral prompt:
  "Compute Cairo's current UTC offset MINUS New York's current UTC offset, in whole hours. Use the IANA timezone database (zoneinfo). Print only the signed integer result; positive means Cairo is ahead of New York, negative means Cairo is behind."
  expected_output_type: int
LEAK CHECK: omits "7". Convention locked: subject first (Cairo), then object (NY). Positive = "ahead" matches the claim's predicate.

Claim: pattern=quantitative, predicate=has_count, slots={subject:'New York', property:'time_zone_offset_from_Cairo_in_hours', value:-7}, polarity=1
Correct neutral prompt:
  "Compute New York's current UTC offset MINUS Cairo's current UTC offset, in whole hours. Use the IANA timezone database (zoneinfo). Print only the signed integer result; positive means New York is ahead of Cairo, negative means New York is behind."
  expected_output_type: int
LEAK CHECK: omits "-7". The property names "from_Cairo" — so the subtraction subtracts Cairo. Subject (NY) goes first; result sign matches the claimed -7's framing.

Claim: pattern=quantitative, predicate=age_difference, slots={subject:'Pierre Curie', object:'Marie Curie', property:'years_older', value:8, subject_birth_year:1859, object_birth_year:1867}, polarity=1
Correct neutral prompt:
  "Given Pierre Curie's birth year 1859 and Marie Curie's birth year 1867, compute (Marie's birth year) MINUS (Pierre's birth year). Print only the signed integer result; positive means Pierre is older than Marie (born earlier), negative means Pierre is younger."
  expected_output_type: int
LEAK CHECK: omits "8". Same lock-the-direction pattern as timezone differences — names which value goes first in the subtraction, and what the sign means in plain English. The claim says "Pierre is 8 years older than Marie" → object Marie's year minus subject Pierre's year (because earlier birth = older = larger object-minus-subject difference), with the sign meaning calibrated against the claim's "older" framing.

Claim: pattern=quantitative, predicate=current_hour, slots={subject:'New York', property:'current_hour', value:2}, polarity=1
Correct neutral prompt:
  "Compute the current hour of day (0-23) in New York's local time. Use the IANA timezone database (zoneinfo). Print only the integer result."
  expected_output_type: int
LEAK CHECK: omits "2". Hour-of-day is the right unit when the claim's value is a bare integer like 2.

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
        purpose="prompt_builder",
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
