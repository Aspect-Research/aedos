from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..llm.client import LLMClient
from .decomposition import decompose_event
from .normalization import normalize_predicate
from .temporal import BEFORE_PRESENT, TemporalScope, extract_temporal_scope
from .triage import TriageDecision, triage

_FIRST_PERSON = re.compile(
    r"^(I|me|my|mine|myself|we|us|our|ours|ourselves)$", re.IGNORECASE
)

EXTRACTION_TOOL: dict[str, Any] = {
    "name": "extract_claims",
    "description": (
        "Extract all verifiable factual claims from the text as binary relational claims. "
        "Only extract claims explicitly stated in the TEXT — not from surrounding context. "
        "For future-tense claims set verb_tense=future. "
        "For multi-participant events use the participants field. "
        "source_text must be a verbatim substring of the input text."
    ),
    "input_schema": {
        "type": "object",
        "required": ["claims"],
        "properties": {
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["subject", "predicate", "object", "polarity", "source_text", "verb_tense"],
                    "properties": {
                        "subject": {"type": "string"},
                        "predicate": {"type": "string"},
                        "object": {"type": "string"},
                        "polarity": {"type": "integer", "enum": [0, 1]},
                        "valid_from": {"type": ["string", "null"]},
                        "valid_until": {"type": ["string", "null"]},
                        "valid_during_ref": {"type": ["string", "null"]},
                        "source_text": {
                            "type": "string",
                            "description": "Verbatim assertion span from text",
                        },
                        "reified_event_id": {"type": ["string", "null"]},
                        "verb_tense": {
                            "type": "string",
                            "enum": ["past", "present", "future"],
                        },
                        "participants": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "For multi-participant events only",
                        },
                        "event_type": {"type": ["string", "null"]},
                    },
                },
            }
        },
    },
}

_SYSTEM_PROMPT = """\
You are a precise claim extractor. Given a piece of text, extract all verifiable \
factual claims as binary relational claims (subject, predicate, object).

Rules:
1. Only extract claims explicitly stated in the TEXT. Do not extract claims from context.
2. Preserve first-person pronouns as subjects (I, me, my, we, us) — the caller will canonicalize them.
3. Set verb_tense=future for predictive claims (they will be filtered out).
4. source_text must be a verbatim substring of the input text.
5. For "Actually X, not Y" corrections: extract X with polarity=1 and Y with polarity=0.
6. For multi-participant events use the participants field and set event_type.
"""


@dataclass
class ExtractionContext:
    asserting_party: str
    context_type: str  # "chat_user", "document", "deployment"
    turn_id: Optional[str] = None
    prior_conversation: Optional[list] = None
    document_id: Optional[str] = None


@dataclass
class Claim:
    claim_id: str
    subject: str
    predicate: str
    object: str
    polarity: int
    source_text: str
    asserting_party: str
    triage_decision: TriageDecision
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    valid_during_ref: Optional[str] = None
    reified_event_id: Optional[str] = None


class Extractor:
    def __init__(self, llm_client: LLMClient) -> None:
        self._llm = llm_client

    def extract(self, text: str, context: ExtractionContext) -> list[Claim]:
        """Extract relational claims from text. Returns all non-future claims with triage decision set."""
        raw_result = self._llm.extract_with_tool(
            system=_SYSTEM_PROMPT,
            user_message=text,
            tool=EXTRACTION_TOOL,
            purpose="extractor:user",
        )

        raw_claims: list[dict] = raw_result.get("claims", [])

        flat: list[dict] = []
        for raw in raw_claims:
            flat.extend(decompose_event(raw))

        claims: list[Claim] = []
        for raw in flat:
            claim = self._build_claim(raw, text, context)
            if claim is not None:
                claims.append(claim)
        return claims

    def _build_claim(
        self, raw: dict, text: str, context: ExtractionContext
    ) -> Optional[Claim]:
        raw_subject = raw.get("subject", "")
        raw_object = raw.get("object", "")
        reified_id = raw.get("reified_event_id")

        # Hard-claim discipline heuristic: reject claims whose entities don't appear in text
        if not self._passes_hard_claim_check(raw_subject, raw_object, text, reified_id):
            return None

        verb_tense = raw.get("verb_tense", "present")
        scope = extract_temporal_scope(
            verb_tense=verb_tense,
            valid_from_raw=raw.get("valid_from"),
            valid_until_raw=raw.get("valid_until"),
            valid_during_ref=raw.get("valid_during_ref"),
        )
        if scope.is_future:
            return None

        subject = self._canonicalize(raw_subject, context.asserting_party)
        predicate = normalize_predicate(raw.get("predicate", ""))
        object_value = raw_object
        polarity = int(raw.get("polarity", 1))
        source_text = raw.get("source_text", "")

        triage_decision = triage(
            predicate=predicate,
            subject=subject,
            object_value=object_value,
            valid_from=scope.valid_from,
            valid_until=scope.valid_until,
            valid_during_ref=scope.valid_during_ref,
        )

        return Claim(
            claim_id=str(uuid.uuid4()),
            subject=subject,
            predicate=predicate,
            object=object_value,
            polarity=polarity,
            source_text=source_text,
            asserting_party=context.asserting_party,
            triage_decision=triage_decision,
            valid_from=scope.valid_from,
            valid_until=scope.valid_until,
            valid_during_ref=scope.valid_during_ref,
            reified_event_id=reified_id,
        )

    def _canonicalize(self, subject: str, asserting_party: str) -> str:
        """Replace first-person pronouns with the asserting party identifier."""
        if _FIRST_PERSON.match(subject.strip()):
            return asserting_party
        return subject

    def _passes_hard_claim_check(
        self,
        raw_subject: str,
        raw_object: str,
        text: str,
        reified_event_id: Optional[str],
    ) -> bool:
        """Return False when the LLM fabricated a claim about a context-only entity."""
        # Decomposed event claims: subject is an event_id, not a text entity — skip check
        if reified_event_id and raw_subject.startswith("event_"):
            return True
        # First-person subjects refer to the text's author — always valid
        if _FIRST_PERSON.match(raw_subject.strip()):
            return True
        text_lower = text.lower()
        if raw_subject.lower() in text_lower:
            return True
        if raw_object.lower() in text_lower:
            return True
        return False
