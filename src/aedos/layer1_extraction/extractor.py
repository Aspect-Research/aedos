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

# Phase E5 prompt — the v5 result of Phase E3's prompt-engineering iteration
# (baseline → v1 → v2 → v3 → v4 → v5). The baseline 6-rule prompt produced 45/57
# = 78.95% on the original extraction corpus with Haiku 4.5; v5 produces 53/53
# = 100% on the cleaned 53-case corpus. The iteration sequence and per-case
# triage are documented in docs/phase_E_report.md / docs/phase_E/results/
# augmented_prompt_v{1..5}/. Each rule's "do this / do NOT do this" structure
# is the v0.16 D45 process pattern: component prompts encoding Aedos-specific
# contracts must specify both positive triggers AND explicit non-triggering
# conditions to prevent over-application.
_SYSTEM_PROMPT = """\
You are a precise claim extractor. Given a piece of text, extract all verifiable \
factual claims as binary relational claims (subject, predicate, object).

Rules:
1. Only extract claims explicitly stated in the TEXT. Do not extract claims from context.
2. Preserve first-person pronouns as subjects (I, me, my, we, us) — the caller will canonicalize them.
3. For reported speech of the form 'X said/claimed/wrote/told Y':
   - Emit a claim for X's act of assertion (X → said/claimed/wrote/told → reference to inner claim id)
   - Emit the inner claim Y as a standalone claim with X as the asserting party
   - Preserve first-person pronouns in the inner claim per the first-person rule
   Example: 'Obama said I won the election' →
     Claim 1: Obama → said → 'claim_election_win'
     Claim 2: 'I' → won → 'the election' (asserting_party: Obama)
4. Set verb_tense=future for predictive claims (they will be filtered out).
5. source_text must be a verbatim substring of the input text.
6. For "Actually X, not Y" corrections: extract X with polarity=1 and Y with polarity=0.

7. For point-in-time events where a YEAR or DATE is given (founded in 1976, \
born on 1970-01-01, died in 1955, started in 2010, launched in 2024), \
extract that year/date into `valid_from` and leave the predicate in its \
PREPOSITIONAL form. The year does NOT replace the object slot.
   - 'Apple was founded in 1976' → subject='Apple', predicate='was founded', \
valid_from='1976'. The year goes in valid_from; the object slot is whatever \
the founding produced (the company itself).
   - 'Asa was born in 1990' → subject='Asa', predicate='was born', \
valid_from='1990' (no object — 1990 is the year, goes in valid_from).
   - When the verb takes a LOCATION (not a year), keep the prepositional \
form and put the location in the object slot:
     - 'Asa was born in Massachusetts' → subject='Asa', predicate='was born in', \
object='Massachusetts'. (No valid_from — 'Massachusetts' is a location, not a date.)
     - 'Williams College is located in Massachusetts' → predicate='is located in', \
object='Massachusetts'.

8. For end-of-event statements where a YEAR is given (ended in 1945, \
finished in 2020, concluded in 1989), extract the year into `valid_until` \
and keep the predicate in its base form.
   - 'The war ended in 1945' → predicate='ended', valid_until='1945'.

9. For temporal subordinate clauses ('A when B'), set valid_during_ref to a \
generated id referencing the B claim. 'Asa was there when Obama was President' \
→ claim about Asa with valid_during_ref='claim_obama_president'; emit the \
Obama claim separately.

10. CRITICAL — multi-participant EVENTS emit ONE extraction, not one per \
participant. An EVENT is an action verb where ≥2 named participants perform \
that action together (signed, founded, attended, met, released, joined). \
Emit a single tool call with `participants` set and `event_type` set; the \
system expands automatically.

Side-by-side example for 'France, Germany, and Italy signed the accord':

CORRECT — ONE extraction:
  {"subject": "France", "predicate": "signed", "object": "the accord",
   "participants": ["France", "Germany", "Italy"],
   "event_type": "accord_signing",
   "polarity": 1, "source_text": "France, Germany, and Italy signed the accord",
   "verb_tense": "past"}

WRONG — three extractions (this OVER-PRODUCES; do not do this):
  {"subject": "France", "predicate": "signed", "object": "the accord",
   "participants": ["France", "Germany", "Italy"], ...}
  {"subject": "Germany", ...}
  {"subject": "Italy", ...}

Examples that REIFY (each emits ONE extraction with participants):
- 'Asa and Mike co-founded Acme' → participants=['Asa','Mike'], event_type='company_founding'
- 'Apple and IBM both released products' → participants=['Apple','IBM'], event_type='product_release'
- 'Alice, Bob, and Carol attended the summit' → participants=['Alice','Bob','Carol'], event_type='summit_attendance'

DO NOT reify binary RELATIONS or STATES — these are NOT multi-participant \
events even when two parties are named:
- Copular constructions ('X and Y are Z'): 'Asa and Bob are friends' → ONE \
claim. Do not populate participants/event_type. Friendship is a binary \
relation, not an event.
- Symmetric binary relations: 'Asa married Pat' → ONE claim. Marriage is \
intrinsically two-party — it is not a multi-participant event. Similarly \
'Asa is taller than Bob' (comparison) → ONE claim.

The distinction: an EVENT has an action verb that could include MORE \
participants if the text named them (founding could have 3+ founders; a \
summit could have 5+ attendees). A binary RELATION is intrinsically \
two-party.

11. Do NOT reify when only one named participant is present, even with a \
'co-' or 'jointly' verb. The participants list is for cases where the TEXT \
names multiple agents performing the action; it is not for cases where the \
verb merely implies cooperative action.
    - 'Asa co-founded the company' → emit ONE claim Asa → co_founded → the \
company. Do not populate `participants` or `event_type`.
    - 'The team won the championship' → emit ONE claim. The team is a \
single named entity; team members are not named.
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
