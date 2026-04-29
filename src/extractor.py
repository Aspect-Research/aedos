"""Claim extractor (v0.3 — pattern-based).

The extractor sees the full pattern catalog with descriptions, slots,
verification methods, and disambiguation notes. Per source-text it:

    1. Decides whether the text contains any fact-stating clause.
    2. For each one, picks the BEST-FIT pattern.
    3. Fills in the pattern's slots from the text.
    4. Picks a predicate label (preferred from example_predicates, but
       free to invent — predicates are descriptive, not authoritative).
    5. Sets polarity (1 normally, 0 for explicit negation).
    6. Returns each as a structured pattern instance.

Predicates are NOT validated against a closed list — verification
semantics belong to the pattern, not the predicate. Within a pattern,
adding a new predicate is a no-op.

Abstention is preferred over poor fits. The pattern set is broad; if
nothing fits, the claim is probably outside scope (aesthetic judgments,
counterfactuals, complex causal claims).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.llm_client import LLMClient
from src.pattern_registry import PatternRegistry


# Punctuation strip for the source_text-in-input substring check.
# We compare lowercase, whitespace-collapsed, punctuation-stripped
# forms — only genuine rewrites should trip the warning, not trailing
# periods or quote escaping differences. Keep word chars + whitespace
# only.
_PUNCT_STRIP_RE = re.compile(r"[^\w\s]")


def _normalize_for_substring(s: str) -> str:
    if not s:
        return ""
    s = _PUNCT_STRIP_RE.sub(" ", s.lower())
    return " ".join(s.split())


@dataclass
class ExtractionResult:
    valid_facts: list[dict[str, Any]] = field(default_factory=list)
    rejected_facts: list[dict[str, Any]] = field(default_factory=list)  # each: {fact, reason}
    raw_tool_input: dict[str, Any] | None = None
    # Defense-in-depth signals from the validator. Each entry:
    #   {fact_index, kind, detail}
    # Currently the only kind is "source_text_not_in_input" — a strong
    # signal that the extractor rewrote the source_text (often because
    # it substituted a "correct" value for what the chat model said).
    # The fact still appears in valid_facts; this is a flag, not a
    # rejection. Pipeline emits an extractor_substitution_warning
    # pipeline event for each entry.
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid_facts": self.valid_facts,
            "rejected_facts": self.rejected_facts,
            "warnings": self.warnings,
        }


def _build_record_tool(registry: PatternRegistry) -> dict[str, Any]:
    """Tool spec for forced extraction. Pattern names are enumerated."""
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
                                    "Object whose keys come from the chosen "
                                    "pattern's slot list. Required slots must "
                                    "be populated."
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
                        },
                        "required": [
                            "pattern", "predicate", "slots", "polarity", "source_text",
                        ],
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

- The pattern set is closed. PREDICATE labels within a pattern are FREE-FORM —
  prefer the example_predicates listed in each pattern, but invent a more
  specific label when none of the examples capture the relation precisely
  (e.g. `is_obsessed_with` for an extreme `preference`).
- If a clause does not fit any pattern, do NOT extract it. An empty list is
  a valid and common answer. The pattern set covers role assignment,
  preferences, quantitative facts, spatial/temporal facts, categorical
  membership, binary relations, events, and propositional attitudes — claims
  outside this scope (aesthetic judgments, counterfactuals, complex causal
  claims, scientific process descriptions) are out of scope.
- Pattern decision aids:
    * preference vs propositional_attitude: object vs proposition.
    * categorical vs role_assignment: intrinsic vs conferrable.
    * relational vs role_assignment: relation between entities vs binding to a role.
    * event vs role_assignment: discrete moment vs ongoing tenure.
- One sentence can yield MULTIPLE facts. Extract them all
  (e.g. "Tokyo is a city in Japan" → categorical AND spatial_temporal).

# Slot rules

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

# Polarity

- 1 = positive ("I like X", "Trump is the president").
- 0 = explicit negation ("I don't like X", "Trump was NOT the 45th").
- Implicit negations or absences do NOT extract.

# Few-shot examples

Input: "Photosynthesis converts sunlight into chemical energy."
Output: facts=[]
Reasoning: scientific process. None of the eight patterns capture
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
Reasoning: one sentence yields two facts in two different patterns.

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

Input: "I think I told you I was born in Williamsburg, Virginia. Is that right?"
Output: facts=[]
Reasoning: This is INTERROGATIVE-META — the user is asking whether they previously said something, not asserting a new fact about themselves. "I think I told you X" / "did I say X?" / "is it true I X?" / "you mentioned that I X — right?" are ALL questions, NOT assertions. The user is testing the model's recall, possibly adversarially. Extracting them as user assertions would (a) corrupt the user-fact store and (b) give the model a false confirmation to verify against. Always abstain on these forms.

Input: "Remember when I said I prefer black coffee?"
Output: facts=[]
Reasoning: Same family — the user is REFERRING to a prior assertion, not making a new one. If they want to confirm or update, they'll say so directly ("yes, I still prefer black coffee").

# Output

Always call the `record_facts` tool exactly once. Never reply with prose."""


class ClaimExtractor:
    def __init__(self, llm: LLMClient, registry: PatternRegistry):
        self.llm = llm
        self.registry = registry
        self._record_tool = _build_record_tool(registry)
        self._system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            patterns=registry.describe_for_prompt(),
        )

    def extract(
        self, text: str, role: str, *, context: str | None = None,
    ) -> ExtractionResult:
        """Extract facts from ``text``.

        ``context`` is an optional preceding turn used ONLY for reference
        resolution — when the speaker says "this sentence" / "the word
        you gave me", the extractor needs to know what those phrases
        point to so it can embed the literal referent as a slot value.
        Used by the pipeline when extracting from an assistant draft, so
        claims like "the sentence has 7 words with 'e'" get a useful
        ``subject`` (the literal sentence) instead of a description.

        ``context`` is for reference only — facts must NOT be extracted
        from it. The system prompt enforces this.
        """
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
        raw = self.llm.extract_with_tool(
            system=self._system_prompt,
            user_message=user_message,
            tool=self._record_tool,
            purpose=f"extractor:{role}",
        )
        result = self._validate(raw)
        # Defense-in-depth: flag any fact whose source_text isn't a
        # substring of the input. Strong signal of extractor rewriting.
        self._flag_substitutions(result, text)
        return result

    @staticmethod
    def _flag_substitutions(result: ExtractionResult, input_text: str) -> None:
        """Append warnings for likely extractor-substitution failures.

        Single check: ``source_text_not_in_input``. The extractor's
        source_text should be a verbatim slice of the input the model
        produced — if it isn't, the extractor probably rewrote it
        (the bug class behind the Saturn-moons false-positive
        catches earlier in development).

        Comparison is FUZZY by design — case-folded, whitespace-
        collapsed, and punctuation-stripped. Without the punctuation
        strip, near-misses like ``'reality TV show "The Apprentice"'``
        vs the input's ``'reality TV show "The Apprentice."'`` (one
        trailing period) tripped the warning constantly. We only
        want to catch genuine rewrites, not trivia.

        The earlier ``value_not_in_source_text`` check was removed:
        it kept misfiring on natural human number forms ("five" vs
        slot value 5) and the extractor's verbatim system-prompt
        rule plus the downstream verification path already catch the
        underlying bug class without this redundant tripwire.
        """
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

        # Required-slot enforcement
        pattern = self.registry.get(pattern_name)
        present_slots = set(f["slots"].keys())
        missing_required = [
            s.name for s in pattern.slots if s.required and s.name not in present_slots
        ]
        if missing_required:
            return f"missing required slots {missing_required} for pattern {pattern_name!r}"
        return None

    def _normalize(self, f: dict[str, Any]) -> dict[str, Any]:
        return {
            "pattern": str(f["pattern"]),
            "predicate": str(f["predicate"]),
            "slots": dict(f["slots"]),
            "polarity": int(f["polarity"]),
            "source_text": str(f["source_text"]),
        }
