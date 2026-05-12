"""Claim extractor (v0.14 — 9 patterns).

Ports v1's ``src/extractor.py`` to the v2 stack. The validation /
substitution-detection / tool-schema construction shape is unchanged;
the only deltas are:

  * ``PatternRegistry`` is loaded from
    ``src.layer1_extraction.pattern_registry``.
  * The system prompt enumerates 9 patterns (legacy 8 + mereological)
    and includes a dedicated few-shot block contrasting constitutive
    parthood (mereological) with locational containment
    (spatial_temporal). The disambiguation pair "Williamstown is part
    of Massachusetts and Asa lives in Williamstown" appears as a
    multi-fact example so the contrast is visible to the LLM in both
    a single-fact and a paired form.

The extractor still imports ``LLMClient`` from the legacy
``src.llm_client`` — that module is pure infrastructure (an
Anthropic SDK wrapper) with no v0.14-specific behavior, so keeping
one copy avoids divergence. The legacy module isn't EDITED, just
imported. If a v0.14-specific LLMClient is ever needed it'll be a
later phase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.layer1_extraction.pattern_registry import PatternRegistry
from src.llm_client import LLMClient


_PUNCT_STRIP_RE = re.compile(r"[^\w\s]")


def _normalize_for_substring(s: str) -> str:
    if not s:
        return ""
    s = _PUNCT_STRIP_RE.sub(" ", s.lower())
    return " ".join(s.split())


@dataclass
class ExtractionResult:
    valid_facts: list[dict[str, Any]] = field(default_factory=list)
    rejected_facts: list[dict[str, Any]] = field(default_factory=list)
    raw_tool_input: dict[str, Any] | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid_facts": self.valid_facts,
            "rejected_facts": self.rejected_facts,
            "warnings": self.warnings,
        }


def _build_record_tool(
    registry: PatternRegistry, *, role: str = "assistant",
) -> dict[str, Any]:
    """Build the record_facts tool schema.

    ``role`` controls whether ``expected_verifier`` is REQUIRED on
    each fact. v0.14.3 design: assistant-side claims must
    self-attest a verifier expectation (high signal-value for
    downstream cross-checks); user-side claims may omit it (most user
    claims route to user_authoritative regardless of the extractor's
    expectation, so the field is lower-value there).
    """
    base_required = ["pattern", "predicate", "slots", "polarity", "source_text"]
    if role == "assistant":
        base_required = base_required + ["expected_verifier"]
    return {
        "name": "record_facts",
        "description": (
            "Record the list of structured facts extracted from the text. "
            "Pass an empty list if the text states no fact in any of the "
            "available patterns."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "facts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "pattern": {
                                "type": "string",
                                "enum": registry.names(),
                                "description": (
                                    "The structural pattern this fact belongs to."
                                ),
                            },
                            "predicate": {
                                "type": "string",
                                "description": (
                                    "A descriptive label for the relation. "
                                    "Prefer the pattern's example_predicates "
                                    "but free to invent a more precise label."
                                ),
                            },
                            "slots": {
                                "type": "object",
                                "description": (
                                    "NESTED OBJECT holding the pattern's slot "
                                    "key-value pairs. Required slots from the "
                                    "chosen pattern's slot list must be "
                                    "populated. Slot keys (agent, object, "
                                    "subject, entity, part, whole, etc.) MUST "
                                    "live INSIDE this object — never at the "
                                    "top level of the fact. A fact emitted as "
                                    "{pattern, predicate, polarity, "
                                    "source_text, agent, object, ...} with "
                                    "slot keys flattened to the top level is "
                                    "REJECTED by the validator and the user's "
                                    "claim is lost. Always wrap them: "
                                    "{..., \"slots\": {\"agent\": ..., "
                                    "\"object\": ...}, ...}."
                                ),
                                "additionalProperties": True,
                            },
                            "polarity": {
                                "type": "integer",
                                "enum": [0, 1],
                                "description": "1 = positive; 0 = explicit negation.",
                            },
                            "source_text": {
                                "type": "string",
                                "description": "The exact span the fact came from.",
                            },
                            "anchor_entity": {
                                "type": "string",
                                "description": (
                                    "The TOPICAL noun phrase the response is about, "
                                    "captured from the surrounding paragraph. Free-form. "
                                    "Set when the claim's slots would otherwise read as "
                                    "context-free out of the paragraph (e.g. 'social rank "
                                    "is passed across generations' has slots about social "
                                    "rank but its anchor is 'baboon' from the paragraph). "
                                    "Optional — omit when the claim's subject already "
                                    "names the topic specifically (e.g. claims about "
                                    "'Marie Curie' or 'Anthropic' don't need an anchor). "
                                    "NOT a slot: this is verification context, never part "
                                    "of the claim's identity."
                                ),
                            },
                            "expected_verifier": {
                                "type": "string",
                                "enum": [
                                    "python",
                                    "python_with_canonical_constants",
                                    "retrieval",
                                    "user_authoritative",
                                    "unverifiable",
                                ],
                                "description": (
                                    "Which verification method you expect this claim to "
                                    "land on. python = sandbox computation (counts, "
                                    "string ops, time/zoneinfo, arithmetic). "
                                    "python_with_canonical_constants = sandbox + "
                                    "stable lookup tables (population, distances). "
                                    "retrieval = Wikipedia search (world facts about "
                                    "specific entities). user_authoritative = the user "
                                    "is the source of truth (preferences, attitudes). "
                                    "unverifiable = no method applies (predictions, "
                                    "others' internal states, aesthetic judgments). "
                                    "REQUIRED for assistant claims; downstream layers "
                                    "cross-check this against their own decisions."
                                ),
                            },
                            "pattern_confidence": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                                "description": (
                                    "Your self-assessed confidence that the pattern you "
                                    "chose is the correct one. low = the boundary case "
                                    "could plausibly belong to a different pattern. "
                                    "Optional — omit when confident."
                                ),
                            },
                        },
                        "required": base_required,
                    },
                }
            },
            "required": ["facts"],
        },
    }


SYSTEM_PROMPT_TEMPLATE = """You extract structured facts from text by mapping each fact-stating clause to one of a fixed set of structural PATTERNS.

# Patterns
{patterns}

# Rules

- The pattern set is closed (NINE patterns: role_assignment, preference,
  quantitative, spatial_temporal, categorical, relational, event,
  propositional_attitude, mereological). PREDICATE labels within a pattern
  are FREE-FORM — prefer the example_predicates listed in each pattern,
  but invent a more specific label when none of the examples capture the
  relation precisely (e.g. `is_obsessed_with` for an extreme `preference`,
  `subregion_of` for a fine-grained `mereological` claim).
- If a clause does not fit any pattern, do NOT extract it. An empty list is
  a valid and common answer. The pattern set covers role assignment,
  preferences, quantitative facts, spatial/temporal facts, categorical
  membership, binary relations, events, propositional attitudes, and
  mereological (constitutive part-whole) facts — claims outside this scope
  (aesthetic judgments, counterfactuals, complex causal claims, scientific
  process descriptions) are out of scope.
- Pattern decision aids:
    * preference vs propositional_attitude: object vs proposition.
    * categorical vs role_assignment: intrinsic vs conferrable.
    * relational vs role_assignment: relation between entities vs binding to a role.
    * event vs role_assignment: discrete moment vs ongoing tenure.
    * mereological vs spatial_temporal: constitutive parthood ("part of",
      "member of", "composed of") vs locational containment ("in", "at",
      "lives in", "located in"). The speaker's surface form is the
      tiebreaker — extract per what they SAID, not by inference about
      which is "really" the case. Both can be true of the same pair.
- One sentence can yield MULTIPLE facts. Extract them all
  (e.g. "Tokyo is a city in Japan" → categorical AND spatial_temporal;
  "Williamstown is part of Massachusetts and Asa lives in Williamstown"
  → mereological AND spatial_temporal).

# HARD-CLAIM DISCIPLINE — extract less, but better

A "hard claim" is one that meets ALL THREE criteria. If a clause fails
ANY one, do not extract it. An empty list is a normal answer.

  1. **Specific.** Names entities, numbers, dates, or relations between
     named things. Vague qualitative descriptors ("intelligent", "complex",
     "diverse", "fascinating", "sophisticated", "advanced") FAIL specificity.
  2. **Falsifiable.** Could in principle be checked against an external
     source. Aesthetic judgments ("baboons are fascinating"), evaluative
     descriptions ("they have advanced cognition"), and empty subjective
     claims ("they exhibit interesting behavior") FAIL falsifiability.
  3. **Load-bearing (centrality test).** Removing this fact would weaken
     the response's main point. Descriptive padding sentences ("they
     thrive in many habitats" as a side note in a paragraph about
     cooperation) and topical asides ("species vary widely") FAIL
     centrality.

The bar is HIGH on purpose. A 4-sentence reply about baboons that contains
one core specific claim ("baboons hunt cooperatively for meat") and three
descriptive sentences should yield ONE fact, not four. Soft claims that
slip through pollute the verifier with unfalsifiable inputs and waste
budget on retrieval that always returns inconclusive.

## Reject categories with examples

REJECT — vague qualitative descriptors:
  Input: "Baboons are highly intelligent animals."
  Output: facts=[]
  Reasoning: "highly intelligent" is unfalsifiable — there's no external
  measure that maps cleanly to it. Skip.

REJECT — descriptive coloring:
  Input: "Baboons are highly social and live in complex troop structures."
  Output: facts=[]
  Reasoning: "highly social" + "complex troop structures" are both
  evaluative descriptors with no specific number, named structure, or
  falsifiable relation. The response could be saying any of a dozen
  more specific things. Skip both.

REJECT — generic platitudes:
  Input: "Different baboon species exhibit interesting variations in behavior."
  Output: facts=[]
  Reasoning: "interesting variations" is content-free. No specific
  variation is named; no specific species is named. Nothing to verify.

REJECT — speculative / hedged claims:
  Input: "Baboons may have rudimentary forms of communication."
  Output: facts=[]
  Reasoning: "may have rudimentary forms" is a hedged conjecture, not
  a falsifiable assertion. The model is signaling its own uncertainty.
  Don't extract; let the hedged language pass through.

REJECT — peripheral asides in a focused response:
  Context: assistant just said "Baboons hunt cooperatively for meat,
  with males driving prey while others ambush."
  Input (continuation): "They're highly adaptable and can thrive in
  many habitats from savanna to mountains."
  Output: facts=[]
  Reasoning: the response's load-bearing claim is cooperative hunting.
  The habitat sentence is descriptive padding around it. Even though
  "thrives in savanna" / "thrives in mountains" sounds extractable,
  it's not central to what the response is asserting. Skip.

ACCEPT — specific, falsifiable, central:
  Input: "Baboons live about 30 years in the wild."
  Output: facts=[{{"pattern":"quantitative","predicate":"wild_lifespan_years","slots":{{"subject":"baboon","property":"wild_lifespan_years","value":30}},"polarity":1,"source_text":"Baboons live about 30 years in the wild"}}]
  Reasoning: specific number (30), specific subject (baboon), falsifiable
  against species reference data, central to the response's content.

ACCEPT — relation between named entities:
  Input: "Baboons are one of the few primates that hunt cooperatively for meat."
  Output: facts=[{{"pattern":"relational","predicate":"hunts_cooperatively_for","slots":{{"subject":"baboons","object":"meat"}},"polarity":1,"source_text":"Baboons are one of the few primates that hunt cooperatively for meat"}}]
  Reasoning: specific behavior, specific subject, falsifiable
  ("baboons hunt cooperatively" is checkable against ethology
  references), central to the response.

## List-of-examples sentences — extract ONE relational claim, NOT N categorical claims

Sentences of the shapes:
  * "X includes A, B, C"
  * "examples of X are A, B, C"
  * "X such as A, B, C"
  * "common X include A, B, C"
  * "common examples of X include A, B, C"

extract as a SINGLE relational claim whose object is the list (joined
or as a list-typed slot value), NOT as N separate
``categorical.is_a`` claims for each item. The relational form is
strictly better: one retrieval call instead of N, no risk of
triggering the suffix-tautology validator on items whose names
contain X (e.g. "monitor lizard" containing "lizard"), and the same
underlying information.

ACCEPT — list-of-examples as one relational claim:
  Input: "Old World monkeys include baboons, macaques, and mandrills."
  Output: facts=[{{"pattern":"relational","predicate":"includes","slots":{{"subject":"Old World monkeys","object":"baboons, macaques, and mandrills"}},"polarity":1,"source_text":"Old World monkeys include baboons, macaques, and mandrills"}}]
  Reasoning: the sentence asserts a containment relation between a
  taxonomic group and its members. One claim captures the full
  assertion. Splitting into "baboon is an Old World monkey" + "macaque
  is an Old World monkey" + "mandrill is an Old World monkey" pays 3x
  the verifier cost AND produces "Old World monkey is a monkey" as a
  side-extraction (which the validator rejects as a suffix tautology).

REJECT (anti-pattern — do not do this):
  Input: "Common examples of lizards include iguanas, geckos, skinks, and monitor lizards."
  WRONG output: facts=[
    {{"pattern":"categorical","predicate":"is_a","slots":{{"entity":"iguana","category":"lizard"}},"polarity":1,"source_text":"iguanas"}},
    {{"pattern":"categorical","predicate":"is_a","slots":{{"entity":"gecko","category":"lizard"}},"polarity":1,"source_text":"geckos"}},
    {{"pattern":"categorical","predicate":"is_a","slots":{{"entity":"skink","category":"lizard"}},"polarity":1,"source_text":"skinks"}},
    {{"pattern":"categorical","predicate":"is_a","slots":{{"entity":"monitor lizard","category":"lizard"}},"polarity":1,"source_text":"monitor lizards"}}
  ]
  RIGHT output: facts=[
    {{"pattern":"relational","predicate":"includes","slots":{{"subject":"lizards","object":"iguanas, geckos, skinks, and monitor lizards"}},"polarity":1,"source_text":"Common examples of lizards include iguanas, geckos, skinks, and monitor lizards"}}
  ]
  Reasoning: the relational form is one claim. The categorical-split
  also produces "monitor lizard is a lizard" which the validator
  rejects as a suffix tautology — the entity name contains the
  category. This kind of side-rejection is a tell that the
  categorical-split shape was the wrong choice.

## Generic-noun objects on relational claims — abstain

A relational claim whose `object` slot is a GENERIC NOUN with no
specific complement isn't falsifiable by retrieval. The verifier
can't confirm or contradict "lizard role as predator ecosystem"
because "ecosystem" doesn't name a specific thing — every species
plays some role in some ecosystem, the claim is true by
construction. Same for `environment`, `system`, `world`,
`structure`, `nature`, `wildlife` when used as a bare object.

REJECT — generic-noun object:
  Input: "Lizards play important roles in their ecosystems as both predators and prey."
  Output: facts=[]
  Reasoning: "ecosystem" / "predator" / "prey" are categories, not
  specific contexts. There's no Wikipedia page that says
  "lizards play role X in ecosystem Y" — the claim isn't structured
  to map onto a verifier query. Skip; the model's wording stands.

ACCEPT — same shape but with a specific complement:
  Input: "Lizards prey on insects, small mammals, and other reptiles."
  Output: facts=[{{"pattern":"relational","predicate":"preys_on","slots":{{"subject":"lizards","object":"insects, small mammals, and other reptiles"}},"polarity":1,"source_text":"Lizards prey on insects, small mammals, and other reptiles"}}]
  Reasoning: the object names SPECIFIC prey types — verifier can
  check this against Wikipedia's lizard or feeding-behavior
  articles.

## "X can be A, B, or C" — range / variation sentences extract as ONE claim

Sentences asserting that a property has a RANGE OF VALUES — "X can be
A, B, or C", "X varies among A, B, and C", "X ranges from A to C" —
extract as a single claim whose object/value captures the variation,
NOT as N separate claims (one per value).

ACCEPT — variation as one claim:
  Input: "A lizard's diet can be carnivorous, omnivorous, or herbivorous."
  Output: facts=[{{"pattern":"relational","predicate":"diet_varies_among","slots":{{"subject":"lizard","object":"carnivorous, omnivorous, herbivorous"}},"polarity":1,"source_text":"A lizard's diet can be carnivorous, omnivorous, or herbivorous"}}]
  Reasoning: the sentence asserts that the diet is variable across
  these categories — one claim. Splitting into "lizard diet can be
  carnivorous" + "lizard diet can be omnivorous" + "lizard diet can
  be herbivorous" pays 3x the verifier cost for the same fact, and
  each individual claim is awkwardly worded ("can be" hedges the
  assertion in a way that doesn't map cleanly to retrieval).

## Contrastive corrections — extract BOTH polarities

Sentences of the shape "X, not Y" / "X rather than Y" / "X instead
of Y" are DOUBLE-POLARITY corrections — the speaker is asserting X
(positive) AND simultaneously denying Y (negative) on the same
predicate and same agent. Extract BOTH facts, with opposite polarity
values.

The discourse-marker prefixes "Actually," / "Wait," / "Sorry,"
signal a CORRECTION — the speaker is updating a prior state. Extract
the new claim (or claims) NORMALLY; the prefix is NOT a reason to
abstain. (The interrogative-meta abstain rule applies to "I think I
told you X" / "Did I say X?" — those are QUESTIONS about prior
assertions; "Actually, I love X" is a NEW assertion.)

ACCEPT — contrastive correction yields TWO facts:
  Input: "Actually, I love ramen, not sushi"
  Output: facts=[
    {{"pattern":"preference","predicate":"loves","slots":{{"agent":"user","object":"ramen","intensity":"strong"}},"polarity":1,"source_text":"I love ramen"}},
    {{"pattern":"preference","predicate":"loves","slots":{{"agent":"user","object":"sushi"}},"polarity":0,"source_text":"not sushi"}}
  ]
  Reasoning: "Actually" signals a correction of a prior state; "I
  love ramen, not sushi" asserts BOTH a positive (loves ramen) and
  an explicit negation (does not love sushi). Two preferences, same
  agent, opposite polarities. Note the slots structure: agent /
  object / intensity are NESTED inside `slots`, never at the top
  level of the fact.

ACCEPT — same shape without the discourse marker:
  Input: "I love ramen, not sushi"
  Output: facts=[
    {{"pattern":"preference","predicate":"loves","slots":{{"agent":"user","object":"ramen","intensity":"strong"}},"polarity":1,"source_text":"I love ramen"}},
    {{"pattern":"preference","predicate":"loves","slots":{{"agent":"user","object":"sushi"}},"polarity":0,"source_text":"not sushi"}}
  ]
  Reasoning: same double-polarity shape; the "Actually," prefix is
  optional. Still TWO facts.

ACCEPT — comparative preference is ONE fact (different shape):
  Input: "I prefer ramen to sushi"
  Output: facts=[{{"pattern":"preference","predicate":"prefers","slots":{{"agent":"user","object":"ramen","over":"sushi"}},"polarity":1,"source_text":"I prefer ramen to sushi"}}]
  Reasoning: "I prefer X to Y" is a SINGLE comparative preference,
  not a contrastive correction. Capture Y as an `over` (or
  `compared_to`) slot. Do NOT split into two preferences here —
  the comparative form asserts ranking, not affirmation+negation.

# Slot rules

- **STRUCTURAL CRITICAL: every slot key MUST live inside the `slots`
  object on each fact, never at the top level. Correct shape:
  `{{"pattern": ..., "predicate": ..., "slots": {{"agent": "user",
  "object": "ramen"}}, "polarity": 1, "source_text": ...}}`. WRONG
  shape (validator rejects, claim is silently lost):
  `{{"pattern": ..., "predicate": ..., "agent": "user", "object":
  "ramen", "polarity": 1, "source_text": ...}}`. Short inputs like
  "I love ramen" tempt the model to flatten the structure — don't.
  Always wrap slot keys in the `slots` object even when there is
  only one key.**
- **CRITICAL: extract VERBATIM what the source text says. Never
  substitute, correct, round, normalize, or "improve" a value. If
  the text says "the population is 21,455", the value MUST be
  21455 — even if you know the actual population is 20,340. The
  source_text MUST be the literal substring of the input you
  extracted from. Substituting your own world knowledge for what
  the source said breaks the verification pipeline: the verifier
  then catches YOUR substitution as a contradiction, masking the
  actual model claim. This rule is non-negotiable. Your job is
  STRUCTURAL extraction; correctness is the verifier's job.**
- Required slots must be populated from the source text or from
  unambiguous inference (e.g. "I" → agent="user").
- Optional slots — populate when the source text supplies them. Do NOT
  invent values. Especially: never leave a temporal scope implicit if
  the text contains it. "from 2017 to 2021" → valid_from, valid_until.
- For role_assignment claims that are CURRENTLY HELD, leave valid_until
  null (do not write "present" or "now"). The downstream verifier
  treats null valid_until as currently held.
- For preference / propositional_attitude with first-person "I", set
  agent="user".
- For mereological, the slots are part and whole. Both are required and
  MUST be different entities (no self-parthood). The lexical cue
  ("X is part of Y", "X is composed of Y", "X is a member of Y")
  almost always maps directly: part=X, whole=Y. Edge case: "Water is
  composed of hydrogen and oxygen" — the whole is water, the parts are
  hydrogen and oxygen; combine into a single composite part value
  ("hydrogen and oxygen") rather than emitting two facts.

# Anchor entity — preserve topic context across claim boundaries

When a claim's slots would read as context-free out of the surrounding
paragraph, set the optional `anchor_entity` field on that fact to the
topical noun phrase. The anchor is verification context (used by the
retrieval verifier to keep query construction on-topic) — NEVER part
of the claim's identity, NEVER a slot.

Set the anchor when:
  * The claim's subject is a possessed noun phrase or generic concept
    that loses its referent out of context. "social rank is passed
    across generations" — subject "social rank" needs anchor="baboon"
    to be verifiable.
  * The claim is one of several in a paragraph about a single topic
    and the slot values don't repeat the topic. "they hunt
    cooperatively" in a baboon paragraph → anchor="baboon".

Skip the anchor (omit the field) when:
  * The claim's subject already names the topic specifically. "Marie
    Curie was a physicist" — subject="Marie Curie" is the anchor;
    don't duplicate.
  * Named entities (people, organizations, places, products) appear
    in the slots. "Anthropic was founded by Dario Amodei" — both
    slots are specific named entities; no anchor needed.

Anchor examples:

Input (in a paragraph about baboons): "Their social rank is passed
across generations through matrilineal lines."
Output: facts=[{{"pattern":"relational","predicate":"passes_across","slots":{{"subject":"social rank","object":"generations","mechanism":"matrilineal lines"}},"polarity":1,"source_text":"social rank is passed across generations through matrilineal lines","anchor_entity":"baboon"}}]
Reasoning: "social rank" alone is context-free. The anchor "baboon"
preserves the topic so the retrieval verifier searches for "baboon
social rank generations" instead of "social rank generations" (which
returns articles about cycles of poverty and Generation Z).

Input: "Marie Curie was born in 1867."
Output: facts=[{{"pattern":"quantitative","predicate":"birth_year","slots":{{"subject":"Marie Curie","property":"birth_year","value":1867}},"polarity":1,"source_text":"Marie Curie was born in 1867"}}]
Reasoning: "Marie Curie" is already specific; no anchor needed.

Input (in a paragraph about Apple's iPhone 15): "It was released in
September 2023."
Output: facts=[{{"pattern":"event","predicate":"released","slots":{{"event_type":"release","participants":["iPhone 15"],"occurred_at":"2023-09"}},"polarity":1,"source_text":"It was released in September 2023","anchor_entity":"iPhone 15"}}]
Reasoning: pronoun subject ("It") loses its referent out of context.
Anchor preserves the topic.

# Self-attesting fields — expected_verifier and pattern_confidence

Two metadata fields you set on EACH assistant claim (optional on user
claims) so downstream layers can cross-check your decisions.

## expected_verifier (REQUIRED on assistant claims)

Which verification method you expect the system to use on this claim.
Pick one:

  python — sandbox computation. Counts ("strawberry has 3 r's"),
    string operations ("'tacos' is an anagram of 'costa'"), time
    operations using zoneinfo ("it's 2:56 pm in Cairo"), arithmetic
    ("1 + 1 = 2"), date math ("January 20 2025 was a Monday").

  python_with_canonical_constants — sandbox + stable lookup tables.
    Population numbers, distances, weights — anything where the answer
    is a stable measurement that the python verifier can produce.

  retrieval — Wikipedia search. World facts about specific named
    entities ("Marie Curie was born in 1867"; "Cairo is in Egypt
    Standard Time"; "baboons hunt cooperatively for meat"). The
    verifier searches Wikipedia and asks an LLM judge whether the
    snippets support the claim.

  user_authoritative — the user is the source of truth. Their
    preferences (preference pattern), their attitudes
    (propositional_attitude), their personal history. Routes to the
    user microtheory, not external verification.

  unverifiable — no method applies. Predictions about the future,
    claims about other people's internal states, aesthetic judgments
    that slip past the abstain rules, policy positions. The system
    will mark these as unverifiable_in_principle and the corrector
    will soften them.

When the choice is genuinely ambiguous, prefer retrieval (the
verifier has the best dynamic range there).

## pattern_confidence (optional on assistant claims)

high — you're sure this is the right pattern.
medium — boundary case; could plausibly fit another pattern.
low — boundary case AND your second-choice pattern is also plausible.

Set to "low" when the claim could read as preference vs relational
("baboons forage for X" — relational by the rule, but reads like
preference) or any other tight boundary. Validator-rejected claims
are flagged for re-extraction; pattern_confidence=low is the
upstream signal that re-extraction might be needed.

# Polarity

- 1 = positive ("I like X", "Trump is the president").
- 0 = explicit negation ("I don't like X", "Trump was NOT the 45th",
  "Hawaii is not part of the contiguous US").
- Implicit negations or absences do NOT extract.

# Few-shot examples

Input: "Photosynthesis converts sunlight into chemical energy."
Output: facts=[]
Reasoning: scientific process. None of the nine patterns capture
chemical conversion. Abstain.

Input: "Marie Curie was a physicist."
Output: facts=[{{"pattern":"categorical","predicate":"is_a","slots":{{"entity":"Marie Curie","category":"physicist"}},"polarity":1,"source_text":"Marie Curie was a physicist"}}]
Reasoning: profession noun → categorical, NOT role_assignment.

Input: "Donald Trump is the 47th President"
Output: facts=[{{"pattern":"role_assignment","predicate":"holds_role","slots":{{"agent":"Donald Trump","role":"47th President","org":"United States"}},"polarity":1,"source_text":"Donald Trump is the 47th President"}}]
Reasoning: named office → role_assignment. valid_until omitted (currently held).

Input: "Trump served as the 45th president from 2017 to 2021"
Output: facts=[{{"pattern":"role_assignment","predicate":"served_as","slots":{{"agent":"Donald Trump","role":"45th President","org":"United States","valid_from":"2017-01-20","valid_until":"2021-01-20"}},"polarity":1,"source_text":"Trump served as the 45th president from 2017 to 2021"}}]
Reasoning: explicit time-bounded tenure. Populate valid_from and valid_until.

Input: "Trump defeated Kamala Harris in the 2024 election"
Output: facts=[{{"pattern":"relational","predicate":"defeated_in_election","slots":{{"subject":"Donald Trump","relation":"defeated_in_election","object":"Kamala Harris","valid_from":"2024"}},"polarity":1,"source_text":"Trump defeated Kamala Harris in the 2024 election"}}]
Reasoning: election outcome is a relation, NOT succession. Do not use succeeded_by here.

Input: "I think the Fed will cut rates"
Output: facts=[{{"pattern":"propositional_attitude","predicate":"believes","slots":{{"agent":"user","attitude":"thinks","proposition":"Fed will cut rates"}},"polarity":1,"source_text":"I think the Fed will cut rates"}}]

Input: "I love peanut butter"
Output: facts=[{{"pattern":"preference","predicate":"loves","slots":{{"agent":"user","object":"peanut butter","intensity":"strong"}},"polarity":1,"source_text":"I love peanut butter"}}]

Input: "Strawberry has 2 p's"
Output: facts=[{{"pattern":"quantitative","predicate":"has_count","slots":{{"subject":"strawberry","property":"letter_p","value":2}},"polarity":1,"source_text":"Strawberry has 2 p's"}}]
Reasoning: structural extraction only. The verifier checks correctness.

Input: "Saturn has 274 confirmed moons."
Output: facts=[{{"pattern":"quantitative","predicate":"has_count","slots":{{"subject":"Saturn","property":"confirmed_moons","value":274}},"polarity":1,"source_text":"Saturn has 274 confirmed moons"}}]
Reasoning: VERBATIM extraction. The text says 274; extract 274 — even though you may "know" the actual count is different (it changes over time as the IAU recognizes new moons). DO NOT substitute your own value. The verifier's job is to compare 274 against external sources and produce a contradiction if it disagrees.

Input: "The 2021 census recorded Yellowknife's population at 22,085, though earlier figures suggested ~20,000."
Output: facts=[
  {{"pattern":"quantitative","predicate":"has_population","slots":{{"subject":"Yellowknife","property":"population_2021_census","value":22085}},"polarity":1,"source_text":"2021 census recorded Yellowknife's population at 22,085"}}
]
Reasoning: extract the VERBATIM census figure (22085). Even if the actual census number was 20,340, extract what the text says. The verifier will catch the discrepancy via retrieval. DO NOT correct the value during extraction.

Preceding context: "How many words in 'the quick brown fox' have the letter o?"
Input (assistant's text): "Two words contain the letter 'o'."
Output: facts=[{{"pattern":"quantitative","predicate":"has_count","slots":{{"subject":"the quick brown fox","property":"words_containing_letter_o","value":2}},"polarity":1,"source_text":"Two words contain the letter 'o'"}}]
Reasoning: 'words' here refers to words in the user's literal sentence. Embed the literal sentence ('the quick brown fox') as the subject so the verifier has the data needed to count. Property names the predicate-shaped operation precisely.

Preceding context: "List the words in this sentence that contain 'e' and count them."
Input (assistant's text): "Total count: 7"
Output: facts=[{{"pattern":"quantitative","predicate":"has_count","slots":{{"subject":"List the words in this sentence that contain 'e' and count them.","property":"words_containing_letter_e","value":7}},"polarity":1,"source_text":"Total count: 7"}}]
Reasoning: 'this sentence' resolves to the user's preceding message in full. Embed the entire user sentence as subject, even if long, so the verifier can split-and-count.

Preceding context: "How many words in 'three free trees' contain 'e'?"
Input (assistant's text): "Words containing 'e' in order:\\n1. three\\n2. free\\n3. trees\\n\\nIf counting all instances: 3 words. If you exclude duplicates: 2 words."
Output: facts=[{{"pattern":"quantitative","predicate":"has_count","slots":{{"subject":"three free trees","property":"words_containing_letter_e","value":3}},"polarity":1,"source_text":"3 words"}}]
Reasoning: The speaker hedges between 3 and 2 depending on interpretation. Extract the PRIMARY value — the one matching the enumerated list (3 items listed → value=3). The "or 2 if duplicates" alternative is the speaker's hedge, not a separate fact. Always extract a fact when a count is asserted with an enumerated list, even if the speaker wraps it in conditional language.

Input: "Tokyo is a city in Japan"
Output: facts=[
  {{"pattern":"categorical","predicate":"is_a","slots":{{"entity":"Tokyo","category":"city"}},"polarity":1,"source_text":"Tokyo is a city"}},
  {{"pattern":"spatial_temporal","predicate":"located_in","slots":{{"entity":"Tokyo","location":"Japan","relation_kind":"containment"}},"polarity":1,"source_text":"Tokyo is a city in Japan"}}
]
Reasoning: one sentence yields two facts in two different patterns. Note: "in Japan" is locational containment (spatial_temporal), NOT constitutive parthood (mereological). The speaker said "in", not "part of".

# Mereological vs spatial_temporal — the constitutive/locational boundary

This boundary is genuinely subtle. Use the speaker's surface form as
the tiebreaker. "part of" / "member of" / "composed of" → mereological.
"in" / "at" / "lives in" / "located in" → spatial_temporal.

Input: "Williamstown is part of Massachusetts"
Output: facts=[{{"pattern":"mereological","predicate":"part_of","slots":{{"part":"Williamstown","whole":"Massachusetts"}},"polarity":1,"source_text":"Williamstown is part of Massachusetts"}}]
Reasoning: "is part of" — constitutive parthood. The town constitutes
territory of the state. Mereological, not spatial_temporal.

Input: "Asa lives in Williamstown"
Output: facts=[{{"pattern":"spatial_temporal","predicate":"lives_in","slots":{{"entity":"Asa","location":"Williamstown","relation_kind":"residence"}},"polarity":1,"source_text":"Asa lives in Williamstown"}}]
Reasoning: "lives in" — locational containment. Asa is located in the
town as a resident. Spatial_temporal, not mereological. Removing Asa
doesn't change what Williamstown is.

Input: "Williamstown is part of Massachusetts and Asa lives in Williamstown"
Output: facts=[
  {{"pattern":"mereological","predicate":"part_of","slots":{{"part":"Williamstown","whole":"Massachusetts"}},"polarity":1,"source_text":"Williamstown is part of Massachusetts"}},
  {{"pattern":"spatial_temporal","predicate":"lives_in","slots":{{"entity":"Asa","location":"Williamstown","relation_kind":"residence"}},"polarity":1,"source_text":"Asa lives in Williamstown"}}
]
Reasoning: BOTH facts are extracted, in DIFFERENT patterns. The first
clause is constitutive (part_of); the second is locational (lives_in).
This is the canonical disambiguation pair — process each clause
independently per its own surface form.

Input: "Tokyo is part of Japan"
Output: facts=[{{"pattern":"mereological","predicate":"part_of","slots":{{"part":"Tokyo","whole":"Japan"}},"polarity":1,"source_text":"Tokyo is part of Japan"}}]
Reasoning: Same pair as the spatial_temporal example "Tokyo is in
Japan", but the speaker said "part of" — extract per surface form.
Mereological.

Input: "The engine is part of the car"
Output: facts=[{{"pattern":"mereological","predicate":"part_of","slots":{{"part":"engine","whole":"car"}},"polarity":1,"source_text":"The engine is part of the car"}}]

Input: "The engine is in the car"
Output: facts=[{{"pattern":"spatial_temporal","predicate":"located_in","slots":{{"entity":"engine","location":"car","relation_kind":"placement"}},"polarity":1,"source_text":"The engine is in the car"}}]
Reasoning: same pair, different surface forms, different patterns. The
speaker's word choice is the signal.

Input: "Massachusetts is one of the New England states"
Output: facts=[{{"pattern":"mereological","predicate":"member_of","slots":{{"part":"Massachusetts","whole":"New England"}},"polarity":1,"source_text":"Massachusetts is one of the New England states"}}]
Reasoning: "one of" + a named group → membership in a SPECIFIC larger
thing (mereological). Not categorical, because "the New England states"
names six specific states, not a kind. Not spatial_temporal, because the
relation is constitutive (Massachusetts is part of what makes up the
New England group).

Input: "Hawaii is not part of the contiguous United States"
Output: facts=[{{"pattern":"mereological","predicate":"part_of","slots":{{"part":"Hawaii","whole":"contiguous United States"}},"polarity":0,"source_text":"Hawaii is not part of the contiguous United States"}}]
Reasoning: explicit negation — polarity=0. Pattern remains mereological
because the surface form ("part of") names the relation type even when
negated.

# Multi-claim arithmetic example (preserved from v1)

Input: "Marie Curie was born in 1867 and died in 1934, so she lived 67 years."
Output: facts=[
  {{"pattern":"quantitative","predicate":"born_in_year","slots":{{"subject":"Marie Curie","property":"birth_year","value":1867}},"polarity":1,"source_text":"born in 1867"}},
  {{"pattern":"quantitative","predicate":"died_in_year","slots":{{"subject":"Marie Curie","property":"death_year","value":1934}},"polarity":1,"source_text":"died in 1934"}},
  {{"pattern":"quantitative","predicate":"lifespan_years","slots":{{"subject":"Marie Curie","property":"years_lived","value":67,"birth_year":1867,"death_year":1934}},"polarity":1,"source_text":"she lived 67 years"}}
]
Reasoning: three independent claims. The lifespan claim takes the dates as INPUTS — embed birth_year and death_year as slots so the LLM router can see the arithmetic is self-contained (1934 - 1867 = 67) and route to python. Without the embedded inputs the router would say "needs external data" and route to retrieval, defeating the multi-claim convention. The same pattern applies to ANY duration / diff / span claim that follows immediately after its inputs in the response (age from birth+now, term length from start+end years, distance from two coordinates).

Input: "The sunset was beautiful"
Output: facts=[]
Reasoning: aesthetic judgment, no pattern fits.

# Tautological is_a guards

Do NOT extract a categorical.is_a claim where the category is a
suffix of the entity (with one or more words preceding it), or where
the category exactly equals the entity. Such claims are vacuously
true ("waggle-dance communication system is a communication system")
and convey no information; storing them clutters the trace and gives
downstream consumers nothing to verify against. The extractor must
detect this shape and abstain — the verifier is not a substitute for
clean extraction.

Input (assistant's text): "The whole waggle-dance communication system enables foragers to share food locations."
Output: facts=[]
Reasoning: A noun phrase ("waggle-dance communication system") containing a head category ("communication system") as a suffix. Extracting `is_a(entity="waggle-dance communication system", category="communication system")` is vacuous — the entity's name already contains the category. Abstain. The sentence ALSO doesn't make any other extractable categorical claim; the verb "enables" is a causal/functional description, not a fact-stating clause that fits any pattern.

Input: "The waggle dance is a form of communication"
Output: facts=[{{"pattern":"categorical","predicate":"is_a","slots":{{"entity":"waggle dance","category":"communication"}},"polarity":1,"source_text":"The waggle dance is a form of communication"}}]
Reasoning: Real categorical claim. The category ("communication") is NOT a suffix of the entity ("waggle dance") and the two are distinct lexemes. Extract — the verifier can check whether this categorization is well-supported.

Input: "Tokyo is a city"
Output: facts=[{{"pattern":"categorical","predicate":"is_a","slots":{{"entity":"Tokyo","category":"city"}},"polarity":1,"source_text":"Tokyo is a city"}}]
Reasoning: Standard categorical extraction. "city" is not a suffix of "Tokyo".

Input: "The President of the United States is a President"
Output: facts=[{{"pattern":"categorical","predicate":"is_a","slots":{{"entity":"President of the United States","category":"President"}},"polarity":1,"source_text":"The President of the United States is a President"}}]
Reasoning: NOT a tautology under the suffix rule. The entity ends in "States", not "President"; the category is a substring but not a suffix. The claim is a real (if obvious) categorical assertion.

Input: "The European parliamentary system is a parliamentary system."
Output: facts=[]
Reasoning: Explicit-form tautology — the surface text is a literal "X is a Y" where Y is a suffix of X. Even though the surface form looks like a textbook categorical assertion, the suffix rule fires and the claim is vacuous. The extractor must abstain on these regardless of how forcefully the source asserts them. Compare with the President case above: there, "President" is a substring but NOT a suffix of "President of the United States" — different shape, different verdict.

Input: "Cats are cats."
Output: facts=[]
Reasoning: Single-token tautological is_a — entity equals category. Vacuous. Abstain.

Input: "I think I told you I was born in Williamsburg, Virginia. Is that right?"
Output: facts=[]
Reasoning: This is INTERROGATIVE-META — the user is asking whether they previously said something, not asserting a new fact about themselves. "I think I told you X" / "did I say X?" / "is it true I X?" / "you mentioned that I X — right?" are ALL questions, NOT assertions. The user is testing the model's recall, possibly adversarially. Extracting them as user assertions would (a) corrupt the user-fact store and (b) give the model a false confirmation to verify against. Always abstain on these forms.

Input: "Remember when I said I prefer black coffee?"
Output: facts=[]
Reasoning: Same family — the user is REFERRING to a prior assertion, not making a new one. If they want to confirm or update, they'll say so directly ("yes, I still prefer black coffee").

Input: "Who is Barron Trump? How many children does he have?"
Output: facts=[]
Reasoning: Pure information requests. Asking "Who is X?" or "How many Y does X have?" does NOT assert anything — including that the speaker is unaware of X. Never infer the speaker's knowledge state from a question. The user could be testing the model, refreshing memory, or genuinely curious; we don't know and won't guess. Questions about external entities yield no claims.

Input: "What's the population of Tokyo?"
Output: facts=[]
Reasoning: Information request, not assertion. Even though "Tokyo" is named, no fact is being asserted about it. Same for "Tell me about X", "Explain Y", "Describe Z" — instructions are not assertions.

Input: "How many r's are in 'strawberry'?"
Output: facts=[]
Reasoning: A counting question is still a question. Even though letter / character / word counting maps cleanly to a quantitative pattern when ASSERTED ("Strawberry has 3 r's"), the question form NEVER extracts. The user supplied no value; confabulating one to fill the `value` slot — picking 2 because that's a common wrong answer, or picking 3 because you "know" it's right — corrupts the user store with a fact the user never claimed. A subsequent verifier verdict that "matches" against this confabulated fact then masquerades as user-confirmed. This rule applies to ALL letter/word/character/digit-counting questions: "How many X are in Y?", "Count the X in Y", "What's the X count for Y?", "How many letters / characters / vowels / words / digits …?". The contrast is sharp: a question form yields nothing; a declarative with an explicit value ("Strawberry has 7 r's") DOES extract — extract 7 verbatim and let the verifier catch the discrepancy.

Input: "Strawberry has 7 r's"
Output: facts=[{{"pattern":"quantitative","predicate":"has_count","slots":{{"subject":"strawberry","property":"letter_r","value":7}},"polarity":1,"source_text":"Strawberry has 7 r's"}}]
Reasoning: Declarative with explicit value. Extract VERBATIM — value=7, not value=3. The verifier will compute the actual count (3) and produce a contradiction. Extraction is structural; correctness is downstream.

Input: "Why did the Soviet Union dissolve in 1991?"
Output: facts=[{{"pattern":"event","predicate":"dissolved","slots":{{"event_type":"dissolution","participants":["Soviet Union"],"occurred_at":"1991"}},"polarity":1,"source_text":"the Soviet Union dissolve in 1991"}}]
Reasoning: This is a question, BUT it embeds a factual premise ("the Soviet Union dissolved in 1991") presented as given. Extract the embedded premise. Distinguish between "asks about" (no fact) and "asks why X happened" where X is stated as background (extract X). Apply this only when the premise is unambiguously presented as fact, not when it's part of the speaker's own hypothetical.

Input: "It's 2:56 pm in New York right now. What time is it in Cairo?"
Output: facts=[
  {{"pattern":"quantitative","predicate":"current_time","slots":{{"subject":"New York","property":"time","value":"2:56 pm"}},"polarity":1,"source_text":"It's 2:56 pm in New York right now"}}
]
Reasoning: STATEMENT-THEN-QUESTION. The first sentence is a declarative assertion ("It's 2:56 pm in New York right now") — extract it as a fact even though the message ends with a question. Process clauses INDEPENDENTLY: a trailing "What time is it in Cairo?" does not retroactively turn the leading statement into a question. The fact is checkable; the user being wrong about NY's current time is exactly the kind of thing the verifier should catch. Common shape: "I'm in <situation/state>. Can you help me with <Y>?" → extract the situation, ignore the request.

Input: "I'm at JFK airport heading to Tokyo. What's the weather there?"
Output: facts=[
  {{"pattern":"spatial_temporal","predicate":"located_at","slots":{{"entity":"user","location":"JFK airport","relation_kind":"current"}},"polarity":1,"source_text":"I'm at JFK airport"}},
  {{"pattern":"event","predicate":"traveling_to","slots":{{"event_type":"travel","participants":["user"],"destination":"Tokyo"}},"polarity":1,"source_text":"heading to Tokyo"}}
]
Reasoning: Two declarative claims followed by a question. Extract both claims (user's current location + travel destination). The question about weather adds no fact and yields no claim — but it doesn't suppress the leading statements either.

# Information requests vs. assertions

A QUESTION ("Who/what/when/where/how/why ...?") or COMMAND ("Tell me…",
"Explain…", "Describe…", "Count…", "Show me…") is an information
REQUEST, not a fact-stating clause. Never extract user-asserted claims
about the speaker's knowledge, beliefs, or feelings just because they
asked a question. The ONLY claims to extract from a question are
factual PREMISES the question states as given (see "Why did the Soviet
Union dissolve in 1991?" above).

CRITICAL: process clauses independently. A message that contains BOTH
declarative statements AND a question is NOT a "question message." The
trailing question does not nullify the leading statements. If the user
says "X is true. What about Y?", extract X — then ignore the question.
The presence of a `?` at the end of the message must NOT cause you to
abstain on earlier declarative content.

# Output

Always call the `record_facts` tool exactly once. Never reply with prose."""


class ClaimExtractor:
    def __init__(self, llm: LLMClient, registry: PatternRegistry):
        self.llm = llm
        self.registry = registry
        # v0.14.3 — per-role tools so expected_verifier can be REQUIRED
        # for assistant-side extraction (high signal value for
        # downstream cross-checks) and OPTIONAL for user-side
        # (most user claims route to user_authoritative anyway).
        self._record_tool_assistant = _build_record_tool(registry, role="assistant")
        self._record_tool_user = _build_record_tool(registry, role="user")
        self._system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            patterns=registry.describe_for_prompt(),
        )

    def extract(
        self, text: str, role: str, *, context: str | None = None,
    ) -> ExtractionResult:
        if role not in ("user", "assistant"):
            raise ValueError(f"role must be 'user' or 'assistant', got {role!r}")
        if context:
            user_message = (
                f"Role of speaker: {role}\n"
                "Preceding speaker's message (use only to resolve "
                "self-references like 'this sentence', 'this word', or "
                "'the word you gave me' to literal text):\n"
                f"{context}\n\n"
                f"Speaker's text:\n{text}\n\n"
                "Extract every fact-stating clause from the speaker's text "
                "via the record_facts tool. When the speaker references "
                "'this sentence', 'this word', or similar, resolve to the "
                "literal text from the preceding message and embed it as "
                "the appropriate slot value (typically `subject` for "
                "quantitative claims). For hedged or conditional count "
                "claims (e.g. 'N if X, else M'; 'N — or M depending on "
                "interpretation'), extract the PRIMARY value — the one "
                "stated first, listed most prominently, or supported by an "
                "enumerated list — as a single fact. The hedge is the "
                "speaker's interpretive uncertainty, not a separate claim. "
                "Return [] only when no fact-stating clause appears."
            )
        else:
            user_message = (
                f"Role of speaker: {role}\n"
                f"Text:\n{text}\n\n"
                "Extract all facts via the record_facts tool. Return [] if none fit."
            )
        tool = (self._record_tool_assistant if role == "assistant"
                else self._record_tool_user)
        raw = self.llm.extract_with_tool(
            system=self._system_prompt,
            user_message=user_message,
            tool=tool,
            purpose=f"extractor:{role}",
        )
        result = self._validate(raw)
        self._flag_substitutions(result, text)
        return result

    def re_extract_after_rejection(
        self, original_claim: dict, source_text: str,
        rejection_reason: str, role: str = "assistant",
    ) -> ExtractionResult:
        """Re-extract a single claim that the validator rejected.

        v0.14.3 — feedback loop. When the validator marks a claim
        ``routing_anomaly``, this method calls the extractor a second
        time with a hint about why the first attempt was rejected.
        Bounded to one retry per claim by the caller (pipeline.py);
        this method just runs the re-classification call.

        The hint includes the original (rejected) classification, the
        validator's invariant + reason, the source text, and an
        instruction to re-classify into a different pattern OR confirm
        the rejection. The extractor returns its best second attempt
        as a normal ExtractionResult — caller applies the same
        validator to the result; if the re-classified claim ALSO
        fails validation, the caller accepts the rejection.
        """
        original_pattern = original_claim.get("pattern", "?")
        original_predicate = original_claim.get("predicate", "?")
        original_slots = original_claim.get("slots") or {}
        slot_lines = ", ".join(f"{k}={v!r}" for k, v in original_slots.items())
        user_message = (
            f"Role of speaker: {role}\n"
            f"Source text: {source_text!r}\n\n"
            "PREVIOUS EXTRACTION ATTEMPT (REJECTED):\n"
            f"  pattern: {original_pattern}\n"
            f"  predicate: {original_predicate}\n"
            f"  slots: {{{slot_lines}}}\n\n"
            "REJECTION REASON:\n"
            f"  {rejection_reason}\n\n"
            "Re-extract the source text into a DIFFERENT pattern that "
            "fits, OR confirm by returning [] if the claim genuinely "
            "doesn't fit any pattern. Common re-classifications:\n"
            "  * preference rejected for non-user agent → try relational\n"
            "    (animal behavior, organizational tendencies)\n"
            "  * propositional_attitude rejected for non-user agent → drop\n"
            "    (third-party mental states are unverifiable)\n"
            "  * mereological rejected for self-parthood → check whether the\n"
            "    claim is actually a categorical (X is_a Y where Y describes X)\n"
            "    or just an extraction error to drop\n"
            "  * categorical rejected for tautology → check for an actual\n"
            "    relational claim hiding in the source text\n\n"
            "If the source text contains MULTIPLE clauses, extract the one "
            "the previous attempt mis-classified; ignore others. "
            "Return [] only if no pattern fits the actual claim being made."
        )
        tool = (self._record_tool_assistant if role == "assistant"
                else self._record_tool_user)
        raw = self.llm.extract_with_tool(
            system=self._system_prompt,
            user_message=user_message,
            tool=tool,
            purpose=f"extractor:{role}:re_extract",
        )
        result = self._validate(raw)
        self._flag_substitutions(result, source_text)
        return result

    @staticmethod
    def _flag_substitutions(result: ExtractionResult, input_text: str) -> None:
        normalized_input = _normalize_for_substring(input_text)
        if not normalized_input:
            return
        for i, fact in enumerate(result.valid_facts):
            src = fact.get("source_text") or ""
            normalized_src = _normalize_for_substring(src)
            if not normalized_src:
                continue
            if normalized_src not in normalized_input:
                result.warnings.append({
                    "fact_index": i,
                    "kind": "source_text_not_in_input",
                    "detail": (
                        f"source_text {src!r} is not a substring of the "
                        f"input (after fuzzy normalization). Likely "
                        f"extractor rewrite — verify the slot values "
                        f"weren't substituted with the extractor's "
                        f"own world knowledge."
                    ),
                })

    # ---- validation -----------------------------------------------------

    def _validate(self, raw: dict[str, Any]) -> ExtractionResult:
        out = ExtractionResult(raw_tool_input=raw)
        facts = raw.get("facts") if isinstance(raw, dict) else None
        if not isinstance(facts, list):
            return out

        for f in facts:
            err = self._reject_reason(f)
            if err is None:
                out.valid_facts.append(self._normalize(f))
            else:
                out.rejected_facts.append({"fact": f, "reason": err})
        return out

    def _reject_reason(self, f: Any) -> str | None:
        if not isinstance(f, dict):
            return f"not a dict (got {type(f).__name__})"
        for k in ("pattern", "predicate", "slots", "polarity", "source_text"):
            if k not in f:
                return f"missing field {k!r}"
        pattern_name = f["pattern"]
        if not self.registry.has(pattern_name):
            return f"unknown pattern {pattern_name!r}"
        if not isinstance(f["slots"], dict):
            return f"slots must be a dict, got {type(f['slots']).__name__}"
        try:
            pol = int(f["polarity"])
        except (TypeError, ValueError):
            return "polarity must be an int"
        if pol not in (0, 1):
            return f"polarity must be 0 or 1, got {pol}"
        if not isinstance(f["predicate"], str) or not f["predicate"].strip():
            return "predicate must be a non-empty string"

        pattern = self.registry.get(pattern_name)
        present_slots = set(f["slots"].keys())
        missing_required = [
            s.name for s in pattern.slots if s.required and s.name not in present_slots
        ]
        if missing_required:
            return f"missing required slots {missing_required} for pattern {pattern_name!r}"
        return None

    def _normalize(self, f: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pattern": str(f["pattern"]),
            "predicate": str(f["predicate"]),
            "slots": dict(f["slots"]),
            "polarity": int(f["polarity"]),
            "source_text": str(f["source_text"]),
        }
        # v0.14.1 — anchor_entity is verification context (used by the
        # retrieval verifier to keep query construction on-topic when
        # the slots alone would lose the topical referent). Optional;
        # only carried through when the extractor set it.
        anchor = f.get("anchor_entity")
        if isinstance(anchor, str) and anchor.strip():
            out["anchor_entity"] = anchor.strip()
        # v0.14.3 — self-attesting fields. expected_verifier carries
        # the extractor's own routing expectation (cross-checked
        # downstream by triage + router); pattern_confidence flags
        # boundary cases for re-extraction consideration.
        ev = f.get("expected_verifier")
        if isinstance(ev, str) and ev.strip():
            out["expected_verifier"] = ev.strip()
        pc = f.get("pattern_confidence")
        if isinstance(pc, str) and pc.strip():
            out["pattern_confidence"] = pc.strip()
        return out
