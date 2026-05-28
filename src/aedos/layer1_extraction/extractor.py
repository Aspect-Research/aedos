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

12. EMPLOYMENT EVENTS — when the verb describes a person STARTING work at \
an organization, emit the relationship claim with predicate='employed_by' \
and put the year in valid_from. The starting verb itself is not the \
predicate.
    - 'Asa joined Google in 2020' → subject='Asa', predicate='employed_by', \
object='Google', valid_from='2020'.
    - 'Asa was hired by Microsoft in 2018' → predicate='employed_by', \
valid_from='2018'.
    - 'Asa started at Apple in 2015' / 'Asa started working at Apple in \
2015' → predicate='employed_by', valid_from='2015'.
    DO NOT apply Rule 12 when:
    - The object is a non-employment group (a club, party, gym, team, \
meeting): use predicate='member_of' instead. 'Asa joined the chess club' \
→ predicate='member_of', object='the chess club'.
    - The object is an event/activity ('joined the meeting', 'joined the \
call'): no employment claim; emit the literal participation claim.

13. EMPLOYMENT TERMINATION — when the verb describes a person ENDING work at \
an organization, emit the same employment relationship claim with the year \
in valid_until.
    - 'Asa left Google in 2024' → subject='Asa', predicate='employed_by', \
object='Google', valid_until='2024'.
    - 'Asa quit Microsoft in 2019' → predicate='employed_by', \
object='Microsoft', valid_until='2019'.
    - 'Asa resigned from Apple in 2022' / 'Asa departed Apple in 2022' → \
predicate='employed_by', object='Apple', valid_until='2022'.
    DO NOT apply Rule 13 when:
    - 'Left' refers to physical departure ('Asa left the room', 'Asa left \
Paris'): emit the literal claim, not an employment termination.
    - The object is a non-employment group ('left the club', 'left the \
party'): use predicate='member_of' with polarity=0 or with valid_until per \
the group's semantics.

14. STATE CHANGES on state-bearing subjects (projects, partnerships, \
marriages, programs, eras, relationships) — when a state-bearing subject \
"ended/concluded/completed" or "began/started/launched", emit the state \
claim with predicate='status', object='ended' or 'ongoing'.
    - 'The project ended in 2024' → subject='The project', \
predicate='status', object='ended', valid_until='2024'.
    - 'The partnership concluded in 2019' → predicate='status', \
object='ended', valid_until='2019'.
    - 'The program began in 2015' → predicate='status', object='ongoing', \
valid_from='2015'.
    A STATE-BEARING subject is one that exists over a time interval and has \
a current state. The defining clue is that the subject is referenced with \
"the" + a noun denoting an ongoing thing (project, program, partnership, \
era, period, initiative, effort).
    DO NOT apply Rule 14 when:
    - The subject is a one-time historical event (the war, the ceremony, \
the summit, the conference): Rule 8 applies — keep the verb as the \
predicate ('the war ended in 1945' → predicate='ended', valid_until='1945').
    - The subject is a person undertaking an activity ('Asa started \
swimming'): emit the literal claim.

15. EVENT-PERIOD TEMPORAL QUALIFIERS — when a claim is scoped by a \
prepositional phrase referencing a NAMED EVENT or PERIOD (not a date), \
extract the qualifier as valid_during_ref with a generated id and do NOT \
put the temporal phrase in the object slot. Triggers: "during X", \
"throughout X", "at the time of X", "in the X period", "in the era of X" \
where X is a noun phrase referring to a named event, war, era, regime, \
crisis, period, etc.
    - 'France was in a recession during the war' → subject='France', \
predicate='in_a_recession', object='', valid_during_ref='claim_war'. \
The phrase "during the war" is a temporal qualifier, not part of the object.
    - 'The policy was in effect at the time of the merger' → \
subject='The policy', predicate='in_effect', object='', \
valid_during_ref='claim_merger'.
    - 'Inflation was high throughout the recession' → subject='Inflation', \
predicate='was', object='high', valid_during_ref='claim_recession'.
    DO NOT apply Rule 15 when:
    - X is a date, year, or decade — Rules 7, 8, and 17 handle those. \
'during 1985' → valid_from='1985'; 'during the 1970s' → Rule 17 expansion.
    - X is the actual object of the verb. 'Asa lived during the 1990s' — \
"the 1990s" is the temporal scope (Rule 17), not the object of 'lived'.
    - The text is a subordinate clause "A when B" — Rule 9 applies \
(emit B as a separate claim and reference it).

16. EVENT-RELATIVE BOUNDS — when the text bounds a claim by a NAMED EVENT \
("before X", "after X", "until X", "since X" where X is a named event, \
not a date), set valid_during_ref to a generated id referencing the \
event AND leave valid_from / valid_until as None. The v0.15 Claim \
shape lacks dedicated valid_from_ref / valid_until_ref fields for \
event-relative bounds, so valid_during_ref carries the event reference \
as the in-vocabulary representation. (Semantically "before X" and \
"after X" differ from "during X"; the v0.16 plan adds valid_from_ref / \
valid_until_ref to disambiguate. v0.15 expresses all three via \
valid_during_ref to preserve the event reference.)
    - 'The team had five members before the acquisition' → \
subject='The team', predicate='had', object='five members', \
valid_during_ref='claim_acquisition', valid_from=None, valid_until=None.
    - 'After the election, she was President' → subject='she', \
predicate='was', object='President', valid_during_ref='claim_election', \
valid_from=None, valid_until=None.
    - 'Since the merger, performance improved' → subject='performance', \
predicate='improved', valid_during_ref='claim_merger', valid_from=None, \
valid_until=None.
    Emitting valid_during_ref is load-bearing: it suppresses the \
implicit-past-tense default (valid_until='before_present') that would \
otherwise fire for past-tense verbs without other temporal signals.
    DO NOT apply Rule 16 when:
    - X is a date or year. 'before 2020' → valid_until='2020' (Rule 8 \
shape); 'after 1990' → valid_from='1990' (Rule 7 shape); 'since 2010' \
→ valid_from='2010'.
    - The text is a plain past-tense statement with no event reference. \
'She was President' (no "after the election") → standard past-tense \
handling applies; do not invoke Rule 16.
    - The text has a subordinate clause Rule 9 can express ("she was \
President when Bush was President") — Rule 9 emits a referenced claim id.

17. DECADE EXPANSION — when the text references a decade ("the 1970s", \
"the 70s", "the 2010s", "the nineties"), expand to explicit \
valid_from / valid_until rather than treating the decade as a relative \
reference. A decade is a CALCULABLE date range, not a named period.
    - 'During the 1970s, inflation was high' → subject='inflation', \
predicate='was', object='high', valid_from='1970', valid_until='1979'.
    - 'In the 90s, the internet became mainstream' → valid_from='1990', \
valid_until='1999'.
    - 'Throughout the 2010s, smartphones dominated' → valid_from='2010', \
valid_until='2019'.
    DO NOT apply Rule 17 when:
    - The reference is a century or longer ('the 20th century', 'the \
medieval period') — those are not decade-scale and may warrant Rule 15's \
valid_during_ref treatment.
    - A specific year within the decade is named ('1973 in the 1970s'): \
the specific year takes precedence (valid_from='1973').
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
