"""LLM-based verification router (v0.5).

Decides per-claim how to verify it. Replaces the v0.4 pattern + predicate-
override dispatch. The pattern still classifies a claim structurally and
informs extraction; it no longer determines verification method.

The router LLM picks one of:

  - python                              — code resolves the claim from its
                                          own inputs alone.
  - python_with_canonical_constants     — code resolves the claim, but may
                                          reference small stable canonical
                                          references (US states, months,
                                          etc.). Triggers a cross-check.
  - retrieval                           — needs external data not present
                                          in the claim and not a stable
                                          canonical reference.
  - user_authoritative                  — claim is about the user; the
                                          user is ground truth.
  - unverifiable                        — no available method applies.

The decision is observable: every routing call writes a ``routing_decision``
pipeline event with method / reason / confidence / hints.

Why an LLM and not a rule engine: the v0.4 pattern + predicate_overrides
approach didn't scale. Every new computable claim type needed a YAML edit;
date arithmetic, structural text properties, and consistency checks routed
incorrectly because they didn't match a pre-declared category. An LLM that
reasons about the claim itself recognises python-verifiability from
content, not from a label.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from src.llm_client import LLMClient


ROUTING_METHODS = (
    "python",
    "python_with_canonical_constants",
    "retrieval",
    "user_authoritative",
    "unverifiable",
)


@dataclass
class RoutingDecision:
    method: str  # one of ROUTING_METHODS
    reason: str
    confidence: float
    python_inputs_self_contained: Optional[bool] = None
    retrieval_query_hint: Optional[str] = None
    canonical_constants_needed: Optional[list[str]] = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "reason": self.reason,
            "confidence": self.confidence,
            "python_inputs_self_contained": self.python_inputs_self_contained,
            "retrieval_query_hint": self.retrieval_query_hint,
            "canonical_constants_needed": self.canonical_constants_needed,
        }


_ROUTING_TOOL = {
    "name": "record_routing_decision",
    "description": (
        "Record the verification method that should be used to check this "
        "claim, with a short reason and a confidence score."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "method": {
                "type": "string",
                "enum": list(ROUTING_METHODS),
                "description": (
                    "The verification method to use. Prefer python > "
                    "python_with_canonical_constants > retrieval > "
                    "user_authoritative > unverifiable when several "
                    "methods could apply."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "One short sentence explaining why this method was "
                    "chosen. Surfaces in the trace UI."
                ),
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": (
                    "How sure you are that this is the right method, in "
                    "[0, 1]. Below 0.7 the trace will flag the decision."
                ),
            },
            "python_inputs_self_contained": {
                "type": "boolean",
                "description": (
                    "Only set when method is python or "
                    "python_with_canonical_constants. True iff the inputs "
                    "needed for the computation are present (or directly "
                    "derivable) in the claim's slots — no external data."
                ),
            },
            "retrieval_query_hint": {
                "type": "string",
                "description": (
                    "Only set when method is retrieval. A short suggestion "
                    "of what to search for. Not the actual query."
                ),
            },
            "canonical_constants_needed": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Only set when method is python_with_canonical_"
                    "constants. Short labels for each canonical reference "
                    "the code may need (e.g. 'list of US states', "
                    "'months of the year', 'primes under 100')."
                ),
            },
        },
        "required": ["method", "reason", "confidence"],
    },
}


_ROUTER_SYSTEM = """You are deciding how to verify a factual claim made by another AI.

You have FIVE verification methods available. Pick one.

# Methods

**python** — generate code that resolves the claim from its own inputs alone. Use this when the claim's truth value can be determined by computation without referencing any external data source. Examples: counting letters in a literal word, arithmetic on numbers stated in the claim, string transformations, primality / palindrome / anagram checks, comparing values stated in the claim, set operations on lists in the claim, day-of-week from a date in the claim, duration between two dates in the claim, internal consistency checks across stated values.

**python_with_canonical_constants** — same as python, except the code may reference small, stable, widely-known canonical data the code-writing LLM can emit literally. Lists of US states, months of the year, primes under 100, ASCII tables, days of the week — things that don't change and are unambiguous. Use this only when the claim is computable given (claim slots) + (one or more such canonical references). Flag what's needed in `canonical_constants_needed`. The system applies an extra cross-check (two independent code generations) to these.

**retrieval** — search the web and judge from snippets. Use this when the claim requires external information that isn't computable from the claim's inputs and isn't a stable canonical reference: specific people, places, events, historical dates, current world state, populations, geographic facts, who-did-what.

**user_authoritative** — the claim is about the user (preferences, beliefs, location, plans, etc.) and the user is ground truth. Use this when the claim's subject is the user and the predicate concerns user state. Common slots that signal this: agent='user', entity='user'.

**unverifiable** — no method above applies. Use for: claims about other people's internal states (their preferences, beliefs), claims requiring human judgment ("the poem is beautiful"), unfalsifiable claims, claims about future events, claims about model behavior or training data, probabilistic claims about non-users.

# Preference order

When multiple methods could apply, prefer earlier over later: python > python_with_canonical_constants > retrieval > user_authoritative > unverifiable. Earlier methods are stronger — more deterministic, less subject to LLM judgment errors.

# Crucial: do not assume external data when it isn't needed

A common failure is routing to retrieval out of habit when the claim is actually computable from its own inputs. Read the claim carefully:

  - "Trump's first term lasted 4 years (2017-2021)." → python. The duration is computable as 2021 - 2017 from values stated in the claim itself. The dates aren't being verified; the arithmetic is.
  - "Trump's first term started in 2017." → retrieval. Now the date itself is being asserted, and python can't check that without external data.

Notice the difference: when the claim asserts a relation BETWEEN values that are in the claim, route to python. When the claim asserts the values themselves, route to retrieval.

# Multi-claim convention (important)

A single claim may package an arithmetic check around values that themselves require retrieval. Example: "Marie Curie was born in 1867 and died in 1934, so she lived 67 years." The years require retrieval; the arithmetic 1934-1867=67 is python. For v0.5, route this case to **python** — the arithmetic is what's being asserted; the dates are inputs the claim takes as given. The router should not try to split the claim. If the dates themselves are wrong, that's a separate retrieval-class claim that the extractor would emit separately.

# Worked examples

Order of these examples is deliberate. Edge cases come first.

## Edge: arithmetic-around-retrieved-values

Claim: pattern=quantitative, predicate=lifespan_years, slots={subject:'Marie Curie', property:'years_lived', value:67, birth_year:1867, death_year:1934}, polarity=1
→ method: python, reason: "Arithmetic on stated dates (death_year - birth_year); take the dates as inputs the claim provides.", confidence: 0.9, python_inputs_self_contained: true.

## Edge: arithmetic-around-retrieved-values, alternate slot names

The extractor may use different slot names depending on the duration kind. Treat ANY slot whose value is a number-like input the python code can use as evidence of self-containment. Examples that should ALL route to python:

  - lifespan_years with birth_year + death_year slots
  - age_years with birth_date + reference_date slots
  - term_duration with valid_from + valid_until slots
  - elapsed_days with start_date + end_date slots
  - distance_km with origin_coords + destination_coords slots

The shape is: a numeric `value` claimed AS the result of an operation on OTHER numeric/date slots present in the same claim. That's python territory regardless of the predicate label.

## Edge: external-string-verification looks like python but isn't

Claim: pattern=relational, predicate=opens_with, slots={subject:'Gettysburg Address', object:"Four score and seven years ago"}, polarity=1
→ method: retrieval, reason: "Verifying that a literal string starts an external document requires fetching the document; no python operation on the claim's slots resolves this.", confidence: 0.9, retrieval_query_hint: "Gettysburg Address opening line".

## python (pure)

Claim: pattern=quantitative, predicate=has_count, slots={subject:'strawberry', property:'letter_r', value:3}, polarity=1
→ method: python, reason: "Counting letters in a literal word is pure computation.", confidence: 0.99, python_inputs_self_contained: true.

Claim: pattern=relational, predicate=reverse_of, slots={subject:'nairatilage', object:'egalitarian'}, polarity=1
→ method: python, reason: "String reversal of a literal is deterministic.", confidence: 0.99, python_inputs_self_contained: true.

Claim: pattern=quantitative, predicate=product_equals, slots={subject:'23 times 47', property:'product', value:1081}, polarity=1
→ method: python, reason: "Arithmetic on literal numbers in the claim.", confidence: 0.99, python_inputs_self_contained: true.

Claim: pattern=quantitative, predicate=term_duration, slots={subject:'Trump first term', property:'years', value:4, valid_from:'2017', valid_until:'2021'}, polarity=1
→ method: python, reason: "Date arithmetic on years stated in the claim's slots (valid_from to valid_until).", confidence: 0.9, python_inputs_self_contained: true.

Claim: pattern=quantitative, predicate=day_of_week, slots={subject:'2025-01-20', property:'weekday', value:'Monday'}, polarity=1
→ method: python, reason: "Day of the week from a literal date is computable via datetime.", confidence: 0.95, python_inputs_self_contained: true.

Claim: pattern=quantitative, predicate=is_perfect_cube, slots={subject:'1729', property:'is_cube', value:false}, polarity=1
→ method: python, reason: "Cube/root test on a literal integer is deterministic.", confidence: 0.99, python_inputs_self_contained: true.

Claim: pattern=quantitative, predicate=is_prime, slots={subject:'73', property:'is_prime', value:true}, polarity=1
→ method: python, reason: "Primality test on a literal integer; no external constants needed.", confidence: 0.99, python_inputs_self_contained: true.

Claim: pattern=relational, predicate=is_palindrome_of, slots={subject:'racecar', object:'racecar'}, polarity=1
→ method: python, reason: "Palindrome check is pure computation on the literal string.", confidence: 0.99, python_inputs_self_contained: true.

Claim: pattern=quantitative, predicate=conditional_date, slots={subject:'three days after Wednesday', property:'weekday', value:'Saturday'}, polarity=1
→ method: python, reason: "Date arithmetic given a stated premise; no external data.", confidence: 0.9, python_inputs_self_contained: true.

Claim: pattern=quantitative, predicate=current_time, slots={subject:'Cairo', property:'time', value:'9:56 am'}, polarity=1
→ method: python, reason: "Current local time in a city is computable from the system clock + Python stdlib's IANA timezone database (zoneinfo.ZoneInfo('Africa/Cairo')). The 'right now' aspect is fine — the verifier runs within seconds of the claim and the comparator can tolerate small drift.", confidence: 0.95, python_inputs_self_contained: true.

Claim: pattern=quantitative, predicate=current_time, slots={subject:'New York', property:'time', value:'2:56 am'}, polarity=1
→ method: python, reason: "Same as Cairo above — datetime.now(ZoneInfo('America/New_York')). Stdlib only.", confidence: 0.95, python_inputs_self_contained: true.

Claim: pattern=quantitative, predicate=time_difference, slots={subject:'New York vs Cairo', property:'time_difference_hours', value:7}, polarity=1
→ method: python, reason: "Hour offset between two cities is computable by subtracting their current UTC offsets (zoneinfo + datetime). The 'typically' / DST nuance — 6 vs 7 hours — is something the comparator can weigh; the routing decision is python.", confidence: 0.9, python_inputs_self_contained: true.

## python_with_canonical_constants

Claim: pattern=quantitative, predicate=us_states_starting_with_letter, slots={subject:'US states', property:'starting_with_A', value:4}, polarity=1
→ method: python_with_canonical_constants, reason: "Counting US states whose names start with a letter requires the (stable) list of states.", confidence: 0.9, python_inputs_self_contained: false, canonical_constants_needed: ["list of US states"].

Claim: pattern=quantitative, predicate=days_in_week, slots={subject:'a week', property:'days', value:7}, polarity=1
→ method: python_with_canonical_constants, reason: "7 days/week is a stable canonical constant; cross-checking guards against accidental alternate calendars.", confidence: 0.7, python_inputs_self_contained: false, canonical_constants_needed: ["days of the week"].

## retrieval

Claim: pattern=role_assignment, predicate=holds_role, slots={agent:'Donald Trump', role:'47th President', org:'United States'}, polarity=1
→ method: retrieval, reason: "Current world-state claim about a specific person's role.", confidence: 0.95, retrieval_query_hint: "Donald Trump 47th President".

Claim: pattern=event, predicate=won_prize, slots={participants:['Marie Curie'], event_type:'Nobel Prize', occurred_at:'1903'}, polarity=1
→ method: retrieval, reason: "Specific historical event date — needs external source.", confidence: 0.95, retrieval_query_hint: "Marie Curie Nobel Prize 1903".

Claim: pattern=quantitative, predicate=population_of, slots={subject:'Tokyo', property:'population', value:14000000}, polarity=1
→ method: retrieval, reason: "Population is external data, not computable.", confidence: 0.95, retrieval_query_hint: "Tokyo population".

Claim: pattern=spatial_temporal, predicate=largest_ocean, slots={entity:'Pacific Ocean', location:'Earth', relation_kind:'largest'}, polarity=1
→ method: retrieval, reason: "Geographic superlative; needs external reference.", confidence: 0.9, retrieval_query_hint: "largest ocean Earth".

## user_authoritative

Claim: pattern=preference, predicate=likes, slots={agent:'user', object:'peanut butter'}, polarity=1
→ method: user_authoritative, reason: "User preference; the user is ground truth.", confidence: 0.99.

Claim: pattern=spatial_temporal, predicate=lives_in, slots={entity:'user', location:'San Francisco'}, polarity=1
→ method: user_authoritative, reason: "User location; user is authoritative. If no prior is in the store, the dispatcher will mark this as unverified.", confidence: 0.95.

## unverifiable

Claim: pattern=propositional_attitude, predicate=feels, slots={agent:'user', proposition:'the novel is elegant'}, polarity=1
→ method: user_authoritative, reason: "User's own attitude is authoritative.", confidence: 0.9.
(But:)
Claim: pattern=propositional_attitude, predicate=feels, slots={agent:'a critic', proposition:'the novel is elegant'}, polarity=1
→ method: unverifiable, reason: "Aesthetic judgment by a non-user agent — not measurable, not the user's own state.", confidence: 0.9.

Claim: pattern=propositional_attitude, predicate=likely, slots={agent:'Sarah', proposition:'likes chocolate'}, polarity=1
→ method: unverifiable, reason: "Probabilistic claim about a non-user's preference.", confidence: 0.9.

Claim: pattern=event, predicate=will_happen, slots={event_type:'Fed rate cut', occurred_at:'2026-05'}, polarity=1
→ method: unverifiable, reason: "Future event prediction.", confidence: 0.9.

# Output

Always call the `record_routing_decision` tool exactly once. Always include `reason` and `confidence`. Set `python_inputs_self_contained` only for python / python_with_canonical_constants. Set `retrieval_query_hint` only for retrieval. Set `canonical_constants_needed` only for python_with_canonical_constants."""


def _build_user_message(claim: dict) -> str:
    return (
        "Claim:\n"
        f"  pattern: {claim.get('pattern')!r}\n"
        f"  predicate: {claim.get('predicate')!r}\n"
        f"  slots: {json.dumps(claim.get('slots') or {}, default=str)}\n"
        f"  polarity: {claim.get('polarity')!r}\n"
        f"  source_text: {claim.get('source_text', '')!r}\n\n"
        "Decide which verification method applies and call "
        "record_routing_decision."
    )


def route_claim(claim: dict, llm: LLMClient) -> RoutingDecision:
    """Ask the router LLM how to verify ``claim``."""
    raw = llm.extract_with_tool(
        system=_ROUTER_SYSTEM,
        user_message=_build_user_message(claim),
        tool=_ROUTING_TOOL,
        purpose="router",
    )
    method = str(raw.get("method") or "").strip()
    if method not in ROUTING_METHODS:
        # Coerce unknown methods to unverifiable rather than crashing — the
        # trace shows the bad value and the dispatcher can still proceed.
        method = "unverifiable"
    reason = str(raw.get("reason") or "").strip()
    confidence_raw = raw.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    self_contained = raw.get("python_inputs_self_contained")
    if not isinstance(self_contained, bool):
        self_contained = None

    query_hint = raw.get("retrieval_query_hint")
    if not isinstance(query_hint, str) or not query_hint.strip():
        query_hint = None

    constants = raw.get("canonical_constants_needed")
    if not isinstance(constants, list):
        constants = None
    else:
        constants = [str(c) for c in constants if isinstance(c, (str, int, float))]
        if not constants:
            constants = None

    return RoutingDecision(
        method=method,
        reason=reason,
        confidence=confidence,
        python_inputs_self_contained=self_contained,
        retrieval_query_hint=query_hint,
        canonical_constants_needed=constants,
        raw=dict(raw),
    )
