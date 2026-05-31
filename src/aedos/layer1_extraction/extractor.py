from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from ..llm.client import LLMClient
from .decomposition import decompose_event
from .normalization import normalize_predicate
from .temporal import BEFORE_PRESENT, TemporalScope, extract_temporal_scope
from .triage import AbstentionReason, TriageDecision, triage

_FIRST_PERSON = re.compile(
    r"^(I|me|my|mine|myself|we|us|our|ours|ourselves)$", re.IGNORECASE
)

# Phase 10.5 Step 6 sub-cause F enforcement: Rule 18 (RESIDENCE VOCABULARY)
# says "lives in" / "lived in" / "resides in" should produce predicate
# 'lives_in', not 'located_in'. The extractor LLM has a strong "located_in"
# prior on these inputs and ignores Rule 18 about half the time. This regex
# matches the source-text verb form and the post-extraction normalizer
# rewrites `located_in` → `lives_in` when the source has a residence verb.
_RESIDENCE_VERB = re.compile(
    r"\b(lives?|lived|resides?|residing)\s+in\b", re.IGNORECASE
)

# (S1b) Nationality recognition moved to the extraction prompt (Rule 21):
# the LLM emits has_nationality(X, "<demonym>") directly. The demonym object
# ("German", "American") is resolved to a country Q-id at verification time by
# the adapter's P1549 reverse lookup (see WikidataAdapter._resolve_demonym_to_
# country), so no hardcoded demonym table or post-extraction regex remains.

# (S2/T7) The population-only comparison regexes are gone. Quantitative count
# comparisons now arrive from the extraction prompt (Rule 24) shaped as a
# '<measure>_greater_than' / '<measure>_less_than' predicate with the numeric
# threshold in the object slot, for any count measure (population, members,
# employees, …). The predicate-translation oracle routes these via the
# kb_quantitative routing_hint and supplies the count KB property (P1082,
# P2124, P1128, …); the walker reads comparator + property from metadata.

# (S1b) The hand-curated _DEMONYM_TO_COUNTRY map is gone. Demonym → country
# resolution is sourced from Wikidata P1549 (demonym) at verification time
# (WikidataAdapter._resolve_demonym_to_country), gated on a country-typed
# (Q6256) object slot. This generalizes to every demonym Wikidata records.

# (S2) The hand-curated _YEAR_AWARE_REWRITES verb→predicate table is gone.
# Date-valued event claims now arrive from the extraction prompt (Rule 23)
# already shaped as a date-sense predicate with the date in the object slot
# (born_on, died_on, founded_in_year, dissolved_in_year, published_in_year,
# released_in_year, occurred_in_year). Each date-sense predicate maps to its
# own date KB property via the predicate-translation oracle (object_type=time
# → P569 / P570 / P571 / P576 / P577 / P585), so there is no Python verb map
# and no runtime object-vs-scope disambiguation here.

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

18. RESIDENCE VOCABULARY — when the verb is a residence verb (live, \
lives, lived, reside, resides, residing) referring to where a person \
makes their home, use predicate='lives_in' with the location in the \
object slot. Do NOT use 'located_in' for personal residence.
    - 'Asa lives in Williamstown' → predicate='lives_in', \
object='Williamstown'.
    - 'Mary resides in Paris' → predicate='lives_in', object='Paris'.
    - 'I lived in Tokyo for five years' → predicate='lives_in', \
object='Tokyo' (past-tense scoping is handled by Rule 7/8 if a date is \
present; otherwise default past-tense applies).
    DO NOT apply Rule 18 when:
    - The verb is 'is in' / 'is located in' / 'is from' — those describe \
general location, not residence. 'Paris is in France' → predicate='located_in'.
    - The subject is not a person ('The company lives at this address' is \
metaphorical; emit the literal claim or skip).
    - The verb describes a temporary stay ('Asa is staying in Boston this \
week') — that's not residence; emit the literal claim or skip.

19. INSTANCE-OF FOR INDEFINITE-IDENTITY CLAIMS — when the pattern is \
'X is a Y' or 'X was a Y' (indefinite article + bare type noun like \
person, river, country, language, scientist, river, city), use \
predicate='instance_of' with object=Y. This routes to Wikidata P31 \
verification. Stripping the article from Y is correct.
    - 'The Amazon is a river' → predicate='instance_of', object='river'.
    - 'Einstein was a physicist' → predicate='instance_of', \
object='physicist'.
    - 'Python is a programming language' → predicate='instance_of', \
object='programming language'.
    DO NOT apply Rule 19 when:
    - The object is an attribute or transient state ('X was happy', \
'X is tired', 'X was high') — keep predicate='was' or 'is'.
    - The object is a definite-article identity ('X is the king', \
'X was the Nth Y') — Rule 20 applies if it's a role/position; otherwise \
keep as 'is'/'was'.
    - The object is a comparative ('X is larger', 'X is the largest \
river') — comparison predicates are out of Rule 19's scope; keep as 'is'.
    - The subject is itself a class ('A river is a body of water' is a \
definitional statement, not an instance claim).

20. HOLDS-ROLE FOR DEFINITE-POSITION CLAIMS — when the pattern is \
'X is/was the [optional Nth] [Position] of [Org]' or 'X is/was the \
[Position]' where the position is a publicly-known role (President, \
Prime Minister, CEO, Chancellor, Pope, King, Queen, Senator, Mayor, \
Governor, etc.), use predicate='holds_role' with object containing the \
position-of-org compounded ('President of the United States', 'Prime \
Minister of the United Kingdom'). This routes to Wikidata P39 \
verification.
    - 'Lincoln was the 16th President of the United States' → \
subject='Lincoln', predicate='holds_role', \
object='President of the United States'. (The '16th' ordinal does NOT \
go into the object slot — it modifies the position but doesn't change \
its identity in Wikidata; verification matches Lincoln's P39 = \
President of the United States.)
    - 'Churchill was the Prime Minister of the United Kingdom' → \
predicate='holds_role', object='Prime Minister of the United Kingdom'.
    - 'Obama is the 44th President' → predicate='holds_role', \
object='President of the United States' (infer 'of the United States' \
from context only when the position name is unambiguous in the world; \
otherwise keep the object verbatim as 'President').
    DO NOT apply Rule 20 when:
    - The role is a private or non-public-record position ('X was the \
team captain' — too local for Wikidata).
    - The pattern is Rule 12 employment ('X joined Y in 2020').
    - The verb is 'became' ('X became President in 2009') — that's a \
state-change event; use 'holds_role' with valid_from for the start date.

21. NATIONALITY — when the claim states a person's nationality, whether as \
'X is [Demonym]' (a nationality adjective: American, German, Serbian) or \
'X has [Demonym] nationality / citizenship', use predicate='has_nationality' \
with object=[Demonym] — the bare demonym word ('German', 'American'). Do NOT \
convert the demonym to a country name; keep the demonym (verification \
resolves it to the country).
    - 'Einstein had German nationality.' → predicate='has_nationality', \
object='German'.
    - 'Obama is American.' → predicate='has_nationality', object='American'.
    DO NOT apply Rule 21 when:
    - The adjective modifies a noun rather than standing alone ('X is an \
American spacecraft' — 'American' attributes 'spacecraft', not nationality; \
emit the literal claim or skip).
    - The word names a language/ethnicity used non-nationally ('X speaks \
German' → predicate='speaks', object='German').

22. COMPOUND NATIONALITY — when a person is described with a compound \
nationality-and-profession phrase '[Demonym]-[Demonym] [Profession]' \
('Serbian-American inventor', 'British-Indian author'), emit ONE instance_of \
claim for the profession plus ONE has_nationality claim per demonym:
    - 'Tesla was a Serbian-American inventor.' → THREE claims: \
(Tesla, instance_of, 'inventor'), (Tesla, has_nationality, 'Serbian'), \
(Tesla, has_nationality, 'American').
    Keep each demonym as the bare demonym word. Apply only when both \
hyphenated tokens are nationality demonyms; a hyphenated proper name that is \
not a nationality stays intact.

23. DATE-VALUED EVENT PREDICATES — when a birth, death, founding, \
dissolution, publication, release, or occurrence is stated with a DATE/YEAR \
as the fact being asserted (not merely a temporal scope on some OTHER \
relation), put the date in the OBJECT slot and use the date-sense predicate, \
NOT the place/agent-sense predicate: born_on (date of birth), died_on (date \
of death), founded_in_year (inception), dissolved_in_year (dissolution / \
abolition / destruction), published_in_year, released_in_year, \
occurred_in_year.
    - 'Einstein was born in 1879.' → predicate='born_on', object='1879'. \
(Contrast 'born in Ulm' → predicate='born_in', object='Ulm' — a place.)
    - 'Google was founded in 1994.' → predicate='founded_in_year', \
object='1994'. (Contrast 'founded by Larry Page' → predicate='founded_by'.)
    - 'The Berlin Wall fell in 1989.' → predicate='dissolved_in_year', \
object='1989'.
    - 'War and Peace was published in 1869.' → \
predicate='published_in_year', object='1869'.
    Put the bare year/date in the object slot; do NOT also copy it into \
valid_from — that double-counts the date. Rule 7's year→valid_from applies \
only when the date scopes a DIFFERENT relation ('X lived in Paris in 1990').

24. QUANTITATIVE COUNT COMPARISON — when a claim asserts a subject's COUNT \
of something exceeds or falls below a number ('has more than N \
[people / residents / inhabitants / members / employees / students / seats]', \
'has fewer than N ...'), emit a comparison predicate \
'<measure>_greater_than' or '<measure>_less_than' \
(population_greater_than, members_less_than, employees_greater_than, …) with \
the bare numeric threshold in the object slot (keep magnitude words: \
'2 million' stays '2 million'):
    - 'Paris has more than 2 million people.' → \
predicate='population_greater_than', object='2 million'.
    - 'The club has fewer than 500 members.' → \
predicate='members_less_than', object='500'.
    Apply only to DIMENSIONLESS counts (people, members, seats, …). A \
measurement carrying physical units (metres, kilograms, km²) is NOT in \
scope — emit the literal claim instead.
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
    # v0.16 WS4: instead of silently dropping a malformed/non-checkworthy
    # claim, the extractor stamps the reason here (an AbstentionReason value)
    # and the walker short-circuits pre-lookup. None means a normal claim.
    abstention_reason: Optional[str] = None


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
            # v0.16 WS4: _build_claim now returns None ONLY for future-tense
            # claims (Rule 4 filter). Every other shaped claim is returned,
            # carrying an abstention_reason when malformed / not-checkworthy.
            if claim is not None:
                claims.append(claim)

        # (S1b) Compound-demonym decomposition ("Serbian-American inventor" →
        # instance_of(inventor) + has_nationality(Serbian) +
        # has_nationality(American)) moved to the extraction prompt (Rule 22).
        # Each demonym object resolves via Wikidata P1549 at verification time.
        return claims

    def _build_claim(
        self, raw: dict, text: str, context: ExtractionContext
    ) -> Optional[Claim]:
        # v0.16 WS4: this function NEVER returns None for a SHAPED claim. The
        # four former early `return None` drops (hard-claim, self-referential,
        # predicate==object, content-less-event) are gone; the first three now
        # capture an abstention_reason and fall through to the single Claim(...)
        # construction at the end (the content-less-event filter is DELETED
        # outright). The walker short-circuits any claim carrying an
        # abstention_reason pre-lookup (no KB/Tier U/Python/LLM call), so the
        # §3.2 soundness intent of the old drops is preserved.
        #
        # Abstention-reason precedence (FIRST set wins; matches the old
        # top-to-bottom drop order so a claim that today is dropped by the
        # hard-claim check still carries subject_absent_from_source):
        #   subject_absent_from_source → self_referential
        #     → predicate_eq_object → not_checkworthy
        #
        # The ONE remaining `return None` is the future-tense drop (Rule 4):
        # future claims are not shaped claims to verify. TestFutureTenseRejection
        # depends on it.
        raw_subject = raw.get("subject", "")
        raw_object = raw.get("object", "")
        reified_id = raw.get("reified_event_id")

        abstention_reason: Optional[str] = None

        # Hard-claim discipline: subject/object absent from source text.
        if not self._passes_hard_claim_check(raw_subject, raw_object, text, reified_id):
            abstention_reason = AbstentionReason.SUBJECT_ABSENT_FROM_SOURCE.value

        # v0.16 WS4 Deletion #2: the content-less occurred/happened/took_place
        # event filter is REMOVED (contract item 4, obsolete). A standalone
        # `(World War II, occurred, '')` claim now flows; per-claim verdicts
        # are independent (no compound-verdict drag), and an empty object
        # grounds to no_grounding_found — abstention, the conservative outcome.

        # (S2) Date-valued event predicate selection happens in the extraction
        # prompt (Rule 23): "Einstein was born in 1879" arrives as
        # born_on(Einstein, 1879), "Google was founded in 1994" as
        # founded_in_year(Google, 1994). No Python verb→predicate rewrite here;
        # a date-sense predicate routes to its date KB property via the oracle.

        # Phase 10.5 Step 5 root-cause: reject self-referential triples
        # (subject == object after trim/case-fold). The extractor
        # occasionally emits these when it fails to parse a non-entity
        # object — e.g. "Einstein was born in 1879" → (Einstein, born_in,
        # Einstein), copying the subject when the year token couldn't bind
        # to a predicate. Self-referential triples are rarely meaningful
        # for relational predicates (born_in / located_in / works_at all
        # describe distinct entities); the few self-referential predicates
        # that do exist (`is`, `equals`) route to abstain anyway, so
        # dropping them costs nothing and prevents the walker from
        # contradicting a true claim via a malformed self-reference.
        #
        # Phase 10.5 Step 6 evaluated relaxing this for inception-style
        # claims with valid_from set (Rule 7 example: 'Google was founded
        # in 1994' → subject=='Google', object=='Google'). The relaxation
        # was reverted: the walker would then look up the
        # extracted predicate's KB property (P112 founder for `founded`,
        # P19 birthplace for `born_in`) and find subject != KB-value,
        # yielding a §3.2 false-contradiction. The year in valid_from is
        # not consulted as the primary verification value. Until the
        # extractor produces year-aware predicates (`founded_in_year` →
        # P571, `born_on` → P569) directly, the strict drop preserves
        # soundness at the cost of accuracy on these cases.
        if (
            abstention_reason is None
            and raw_subject.strip().casefold() == raw_object.strip().casefold()
            and raw_subject.strip()
        ):
            abstention_reason = AbstentionReason.SELF_REFERENTIAL.value

        # Phase 10.5 Step 6 Batch 8+: also drop claims where the
        # PREDICATE equals the object (after trim/case-fold). This is
        # the "verb repeated into the object slot" mis-extraction —
        # e.g. "The Berlin Wall fell in 1989" → (Berlin Wall, fell,
        # fell), where the LLM put the verb token into both the
        # predicate and the object instead of recognizing the
        # implicit subject-as-object Rule 7 convention OR leaving the
        # object empty. The walker would then look up the predicate's
        # KB property and find the verb token isn't a Wikidata entity,
        # potentially producing a §3.2 false-contradiction shape.
        raw_pred_check = (raw.get("predicate") or "").strip().casefold()
        raw_obj_check = raw_object.strip().casefold()
        if (
            abstention_reason is None
            and raw_pred_check and raw_obj_check and raw_pred_check == raw_obj_check
        ):
            abstention_reason = AbstentionReason.PREDICATE_EQ_OBJECT.value

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

        # Phase 10.5 Step 6 sub-cause F enforcement: when the source-text
        # verb is a residence verb (lives/lived/resides/residing + in)
        # but the LLM emitted predicate='located_in', rewrite to
        # predicate='lives_in'. The extractor prompt's Rule 18 makes this
        # the canonical extraction, but the LLM ignores it when its prior
        # on 'located_in' is strong; the substring check on source_text
        # makes the rewrite deterministic.
        if predicate == "located_in" and _RESIDENCE_VERB.search(source_text):
            predicate = "lives_in"

        # (S1b) Nationality recognition is handled by the extraction prompt
        # (Rule 21): "X is [Demonym]" / "X has [Demonym] nationality" already
        # arrive as has_nationality(X, "<demonym>"). The demonym object is
        # resolved to a country at verification time via Wikidata P1549 — no
        # post-extraction rewrite or hardcoded demonym table here.

        # (T7) Quantitative count comparison ("Paris has more than 2 million
        # people") arrives from the extraction prompt (Rule 24) already shaped
        # as population_greater_than(Paris, "2 million") etc. The oracle routes
        # it (kb_quantitative) and the walker compares — no population-only
        # regex or hardcoded P1082 here.


        triage_decision = triage(
            predicate=predicate,
            subject=subject,
            object_value=object_value,
            valid_from=scope.valid_from,
            valid_until=scope.valid_until,
            valid_during_ref=scope.valid_during_ref,
        )
        # v0.16 WS4: inert prose is the lowest-precedence reason — only stamp
        # it if no malformed reason was already captured above.
        if abstention_reason is None and triage_decision == TriageDecision.INERT_PROSE:
            abstention_reason = AbstentionReason.NOT_CHECKWORTHY.value

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
            abstention_reason=abstention_reason,
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
