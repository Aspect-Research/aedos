"""Claim extractor.

One LLM call per message, driven by a forced tool-use schema so the response
is always structured JSON (never free-form prose we'd have to parse).

The extractor is role-aware: the same text can produce different subject
bindings depending on whether it came from the user or the assistant.

Claims whose predicate is not in the registry are dropped and logged, never
stored. The registry is the authoritative vocabulary — the LLM doesn't get
to invent new predicates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.llm_client import LLMClient
from src.predicate_registry import OBJECT_TYPES, PredicateRegistry


@dataclass
class ExtractionResult:
    valid_claims: list[dict[str, Any]] = field(default_factory=list)
    rejected_claims: list[dict[str, Any]] = field(default_factory=list)  # each: {claim, reason}
    raw_tool_input: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid_claims": self.valid_claims,
            "rejected_claims": self.rejected_claims,
        }


RECORD_CLAIMS_TOOL = {
    "name": "record_claims",
    "description": (
        "Record the list of typed factual claims extracted from the text. "
        "Pass an empty array if there are no factual claims."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "description": "One entry per discrete claim.",
                "items": {
                    "type": "object",
                    "properties": {
                        "subject": {
                            "type": "string",
                            "description": (
                                "Canonical entity the claim is about. "
                                "'user' for first-person references."
                            ),
                        },
                        "predicate": {
                            "type": "string",
                            "description": "Must be a name from the predicate registry.",
                        },
                        "object": {
                            "type": "string",
                            "description": (
                                "The object/value of the claim. For object_type='count', "
                                'encode as JSON: {"item": ..., "count": ...}. For '
                                "sum_equals/product_equals, the SUBJECT is a JSON list "
                                "of numbers."
                            ),
                        },
                        "object_type": {
                            "type": "string",
                            "enum": sorted(OBJECT_TYPES),
                        },
                        "polarity": {
                            "type": "integer",
                            "enum": [0, 1],
                            "description": "1 = positive claim, 0 = explicit negation.",
                        },
                        "source_text": {
                            "type": "string",
                            "description": "The exact span the claim was extracted from.",
                        },
                    },
                    "required": [
                        "subject",
                        "predicate",
                        "object",
                        "object_type",
                        "polarity",
                        "source_text",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["claims"],
        "additionalProperties": False,
    },
}


def _build_system_prompt(registry: PredicateRegistry) -> str:
    return f"""You extract discrete factual claims from text so they can be verified.

# Bounded vocabulary
Only use predicates from the registry below. If a potential claim does not fit
any listed predicate, DROP IT — do not invent new predicates, do not stretch an
existing predicate to fit. An empty list is a valid and common answer.

{registry.describe_for_prompt()}

# What counts as a claim
A claim is an assertion about the world or about the user's state (likes,
plans, beliefs, identity, count of something, spelling of something, etc.).

DO extract:
- Preferences, opinions, identity stated as assertions ("I like X", "I'm 34").
- Negations stated explicitly ("I don't like X" → polarity=0, or use 'dislikes').
- Factual claims about the external world ("Paris is the capital of France").
- Mathematical/structural claims ("strawberry has 3 p's", "2+3 = 5").

Do NOT extract:
- Questions ("Do I like peanut butter?" — no claim).
- Pleasantries / meta-conversation ("Thanks!", "Got it", "Let me check").
- Speculation with hedging the model is merely entertaining.
- Vague feelings without a concrete predicate ("I'm feeling reflective" —
  unless 'feels' applies cleanly).

# Binding
- In USER text: 'I', 'me', 'my', 'myself' → subject 'user'.
- In ASSISTANT text addressing the user: 'you', 'your' → subject 'user'.

# Encoding rules
- has_count: object is JSON '{{"item": "<thing being counted>", "count": <int>}}';
  subject is the container (word / phrase).
- sum_equals / product_equals: subject is a JSON list of numbers (e.g. '[2,3,4]');
  object is the numeric result.
- spelled_as: object may be dashed or plain (e.g. 's-t-r-a-w-b-e-r-r-y' or 'strawberry').
- For user-authoritative predicates, keep object_type consistent with the registry.

# Output
Always call the `record_claims` tool exactly once. Never respond with prose."""


class ClaimExtractor:
    def __init__(self, llm: LLMClient, registry: PredicateRegistry):
        self.llm = llm
        self.registry = registry
        self._system_prompt = _build_system_prompt(registry)

    def extract(self, text: str, role: str) -> ExtractionResult:
        if role not in ("user", "assistant"):
            raise ValueError(f"role must be 'user' or 'assistant', got {role!r}")
        user_message = (
            f"Role of speaker: {role}\n"
            f"Text:\n{text}\n\n"
            "Extract all claims via the record_claims tool."
        )
        raw = self.llm.extract_with_tool(
            system=self._system_prompt,
            user_message=user_message,
            tool=RECORD_CLAIMS_TOOL,
        )
        return self._validate(raw)

    def _validate(self, raw: dict[str, Any]) -> ExtractionResult:
        out = ExtractionResult(raw_tool_input=raw)
        claims = raw.get("claims") if isinstance(raw, dict) else None
        if not isinstance(claims, list):
            return out

        for c in claims:
            if not isinstance(c, dict):
                out.rejected_claims.append({"claim": c, "reason": "not a dict"})
                continue
            missing = {
                "subject",
                "predicate",
                "object",
                "object_type",
                "polarity",
                "source_text",
            } - c.keys()
            if missing:
                out.rejected_claims.append(
                    {"claim": c, "reason": f"missing fields: {sorted(missing)}"}
                )
                continue
            pred = c["predicate"]
            if not self.registry.has(pred):
                out.rejected_claims.append(
                    {"claim": c, "reason": f"predicate {pred!r} not in registry"}
                )
                continue
            expected_type = self.registry.get(pred).object_type
            if c["object_type"] != expected_type:
                out.rejected_claims.append(
                    {
                        "claim": c,
                        "reason": (
                            f"object_type {c['object_type']!r} doesn't match "
                            f"registry ({pred} expects {expected_type!r})"
                        ),
                    }
                )
                continue
            try:
                pol = int(c["polarity"])
            except (TypeError, ValueError):
                out.rejected_claims.append({"claim": c, "reason": "polarity not an int"})
                continue
            if pol not in (0, 1):
                out.rejected_claims.append(
                    {"claim": c, "reason": f"polarity must be 0 or 1, got {pol}"}
                )
                continue
            out.valid_claims.append(
                {
                    "subject": str(c["subject"]),
                    "predicate": pred,
                    "object": str(c["object"]),
                    "object_type": c["object_type"],
                    "polarity": pol,
                    "source_text": str(c["source_text"]),
                }
            )
        return out
