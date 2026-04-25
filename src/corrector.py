"""Response corrector.

When verification contradicts one or more claims in the assistant's draft,
the corrector rewrites the draft so the corrected facts land naturally.
It's a single LLM call with a flat, deterministic prompt — no tools, no
structured output.

The pipeline decides when to invoke this; if every claim in a response was
VERIFIED, UNVERIFIED, or flagged, there's nothing to correct and the
corrector isn't called at all.
"""

from __future__ import annotations

from typing import Iterable

from src.llm_client import LLMClient

CORRECTOR_SYSTEM = """You rewrite an assistant response so that specific factual claims reflect the correct answer.

Rules:
- Preserve the tone, structure, and any content unrelated to a correction.
- State the correct information directly. Do not apologize, narrate the correction, or hedge with "actually..." phrasing.
- If a claim was wrong, replace it with the correct version as if the assistant had known it all along.
- Output only the rewritten response. No preamble, no explanation."""


class Corrector:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def correct(self, original_text: str, corrections: Iterable[dict]) -> str:
        corrections = list(corrections)
        if not corrections:
            return original_text

        lines = [
            "Original response:",
            "```",
            original_text,
            "```",
            "",
            "Apply these corrections:",
        ]
        for i, c in enumerate(corrections, 1):
            src = c.get("source_text", "").strip() or "(no source span recorded)"
            lines.append(
                f"{i}. In the phrase {src!r}: the claim that the object is "
                f"{c.get('original_object')!r} is wrong; the correct value is "
                f"{c.get('corrected_object')!r}. "
                f"({c.get('explanation', '').strip()})"
            )

        user_message = "\n".join(lines)
        return self.llm.rewrite(CORRECTOR_SYSTEM, user_message)
