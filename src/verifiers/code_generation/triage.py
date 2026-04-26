"""Stage 1 — verifiability triage.

Decides whether a claim can be resolved by deterministic python code
given only the inputs in the claim itself (no external data, no human
judgment, no probabilistic interpretation).

Sees the full claim INCLUDING the asserted value — the firewall isn't
needed yet, this stage just routes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.llm_client import LLMClient


@dataclass
class TriageResult:
    verifiable: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"verifiable": self.verifiable, "reason": self.reason}


_TRIAGE_TOOL = {
    "name": "record_triage",
    "description": (
        "Record whether the given claim can be resolved by deterministic "
        "python code given only the inputs in the claim itself."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verifiable": {
                "type": "boolean",
                "description": (
                    "True iff the claim's question can be answered by "
                    "running python code with no external data, no human "
                    "judgment, and no probabilistic reasoning."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "One short sentence explaining the decision. Useful "
                    "for the trace UI."
                ),
            },
        },
        "required": ["verifiable", "reason"],
    },
}


_TRIAGE_SYSTEM = """You decide whether a structured factual claim can be resolved by deterministic python code, given ONLY the inputs in the claim itself.

A claim is python-verifiable when ALL of the following hold:
  1. The question it answers is computable from the slot values without any external data (no facts about the world, no databases, no APIs).
  2. The answer is determined by an unambiguous procedure — not by human judgment or interpretation.
  3. There is no probabilistic, statistical, or "usually" framing.

Mark VERIFIABLE: counting characters in a string, arithmetic, list operations, string manipulation, comparing numbers, checking primality, checking palindromes/anagrams, sorting, set operations, reversing strings, splitting/joining text, regex matching of literals.

Mark NOT VERIFIABLE: claims that require external data ("Trump was born in 1946", "the population of France"), claims that need human judgment ("the sentence is grammatical", "the painting is beautiful"), probabilistic claims ("dice usually roll 7"), claims about model state or training data ("the model knows X"), claims with ambiguous interpretation ("how many words are 'big'").

# Worked examples

Claim: pattern=quantitative, predicate=has_count, slots={subject:'strawperpy', property:'letter_r', value:3}
→ VERIFIABLE. Counting occurrences of a literal character in a literal string is a deterministic python operation.

Claim: pattern=quantitative, predicate=prime_count, slots={subject:'primes between 1 and 100', value:25}
→ VERIFIABLE. Counting primes in [2, 100) is a textbook deterministic algorithm.

Claim: pattern=quantitative, predicate=has_length, slots={subject:'hello', value:5}
→ VERIFIABLE. len() of a literal string.

Claim: pattern=quantitative, predicate=has_count, slots={subject:'the quick brown fox jumps over the lazy dog', property:'words_containing_letter_o', value:4}
→ VERIFIABLE. The subject is a literal string; splitting on whitespace and counting tokens that contain a letter is deterministic. Long subjects are fine — what matters is that the subject IS the data, not a description of it.

Claim: pattern=quantitative, predicate=has_count, slots={subject:'sentence words containing letter e', property:'count', value:7}
→ NOT VERIFIABLE. The subject is a *description* of what's being counted, not the literal data. There is no concrete sentence to operate on. (If the extractor had embedded the actual user sentence as the subject, this would be verifiable.)

Claim: pattern=quantitative, predicate=born_in_year, slots={subject:'Albert Einstein', property:'birth_year', value:1879}
→ NOT VERIFIABLE. Requires external biographical data not present in the slots.

Claim: pattern=relational, predicate=reverse_of, slots={subject:'nairatilage', object:'egalitarian'}
→ VERIFIABLE. String reversal is deterministic.

Claim: pattern=relational, predicate=is_anagram_of, slots={subject:'listen', object:'silent'}
→ VERIFIABLE. Compare multisets of characters.

Claim: pattern=categorical, predicate=is_a, slots={entity:'Marie Curie', category:'physicist'}
→ NOT VERIFIABLE. Requires external biographical knowledge.

Claim: pattern=propositional_attitude, predicate=feels, slots={agent:'user', proposition:'beautiful sunset'}
→ NOT VERIFIABLE. Subjective judgment, no procedure.

# Output

Always call the `record_triage` tool exactly once with {verifiable, reason}."""


def triage_claim(claim: dict, llm: LLMClient) -> TriageResult:
    """Ask the extractor model whether ``claim`` is python-verifiable."""
    user_message = (
        "Claim:\n"
        f"  pattern: {claim.get('pattern')!r}\n"
        f"  predicate: {claim.get('predicate')!r}\n"
        f"  slots: {json.dumps(claim.get('slots') or {}, default=str)}\n"
        f"  polarity: {claim.get('polarity')!r}\n"
        f"  source_text: {claim.get('source_text', '')!r}\n\n"
        "Decide whether this claim is python-verifiable per the rules. "
        "Call record_triage."
    )
    raw = llm.extract_with_tool(
        system=_TRIAGE_SYSTEM,
        user_message=user_message,
        tool=_TRIAGE_TOOL,
    )
    verifiable = bool(raw.get("verifiable", False))
    reason = str(raw.get("reason") or "")
    return TriageResult(verifiable=verifiable, reason=reason)
