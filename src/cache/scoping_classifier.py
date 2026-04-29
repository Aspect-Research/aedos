"""Cache-eligibility scoping classifier (v0.6, observation mode).

Decides per claim:

  user_specific    — about the user; never cache (Tier 1's job)
  session_specific — about this conversation / right now / unverifiable
                     in the abstract; don't cache
  world_fact       — about the world; cache eligible

The decision is one LLM call with a stable system prompt + worked
examples. In OBSERVATION MODE (initial deployment), the classifier
runs and writes a ``cache_scoping_decision`` pipeline event but DOES
NOT gate caching — there's no cache lookup or write yet. After two
sessions of observation we read the decisions, calibrate, and only
then wire it to actual cache writes.

The shape mirrors ``llm_router.RoutingDecision`` deliberately so
testing patterns can transfer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from src.llm_client import LLMClient


SCOPING_METHODS = ("user_specific", "session_specific", "world_fact")


@dataclass
class ScopingDecision:
    scope: str  # one of SCOPING_METHODS
    reason: str
    confidence: float
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "reason": self.reason,
            "confidence": self.confidence,
        }


_SCOPING_SYSTEM = """You are the cache-eligibility scoping classifier for a claim-verification system.

You receive a single structured claim. You decide whether the claim is:

  user_specific    — about the user themselves (preference, biography,
                     opinion, location). NEVER cacheable across users.
  session_specific — about THIS conversation, right now, or otherwise
                     true-only-in-context (e.g. "this sentence has 7
                     words", "it's raining outside", "the previous
                     turn said X"). NOT cacheable.
  world_fact       — a claim about the external world that's true the
                     same way for every user (Tokyo is in Japan, the
                     atomic number of carbon is 6, the 1969 moon
                     landing happened on July 20). CACHE ELIGIBLE.

# Decision rule

If the claim's truth depends on WHO is asking, scope is user_specific.
If the claim's truth depends on WHEN or in WHICH conversation, scope
is session_specific. Otherwise scope is world_fact.

# Worked examples

Claim: pattern=preference, predicate=likes, slots={agent:user, object:peanut butter}, polarity=1
→ scope: user_specific, reason: "agent is the user; preference is by definition user-specific.", confidence: 0.99

Claim: pattern=spatial_temporal, predicate=lives_in, slots={entity:user, location:Williamstown}, polarity=1
→ scope: user_specific, reason: "user's residence; varies per user.", confidence: 0.99

Claim: pattern=propositional_attitude, predicate=believes, slots={agent:user, proposition:'the Fed will cut rates'}, polarity=1
→ scope: user_specific, reason: "user's belief; only the user can confirm.", confidence: 0.99

Claim: pattern=quantitative, predicate=has_count, slots={subject:'the quick brown fox', property:'words_with_o', value:2}, polarity=1
→ scope: session_specific, reason: "subject is a literal sentence from this conversation; meaningful only in this turn.", confidence: 0.95

Claim: pattern=quantitative, predicate=has_count, slots={subject:'strawberry', property:'letter_r', value:3}, polarity=1
→ scope: world_fact, reason: "structural property of the literal word 'strawberry'; same answer for every user, every session.", confidence: 0.99

Claim: pattern=spatial_temporal, predicate=located_in, slots={entity:Tokyo, location:Japan}, polarity=1
→ scope: world_fact, reason: "geographic fact; stable across users and sessions.", confidence: 0.99

Claim: pattern=quantitative, predicate=born_in_year, slots={subject:'Marie Curie', property:'birth_year', value:1867}, polarity=1
→ scope: world_fact, reason: "biographical fact about a historical figure; immutable.", confidence: 0.99

Claim: pattern=role_assignment, predicate=holds_role, slots={agent:'Donald Trump', role:'47th President', org:'United States'}, polarity=1
→ scope: world_fact, reason: "current world-state — everyone shares the same answer right now. Cache with short TTL.", confidence: 0.95

Claim: pattern=event, predicate=will_happen, slots={event_type:'Fed rate cut', occurred_at:'2026-05'}, polarity=1
→ scope: session_specific, reason: "future-event prediction; hasn't happened yet, can't be verified, definitely not cacheable.", confidence: 0.9

Claim: pattern=relational, predicate=opens_with, slots={subject:'Gettysburg Address', object:'Four score and seven years ago'}, polarity=1
→ scope: world_fact, reason: "literal text of a fixed historical document; immutable.", confidence: 0.99

# Output

Call the ``record_scope`` tool exactly once. Provide a one-sentence
reason. confidence in [0.0, 1.0]. Never reply with prose."""


_SCOPING_TOOL = {
    "name": "record_scope",
    "description": (
        "Record the cache-eligibility scope of the claim. Choose one of "
        "user_specific / session_specific / world_fact."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": list(SCOPING_METHODS),
                "description": "The scope class.",
            },
            "reason": {
                "type": "string",
                "description": "One-sentence justification.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "0.0-1.0 confidence in the classification.",
            },
        },
        "required": ["scope", "reason", "confidence"],
    },
}


def classify_scope(claim: dict, llm: LLMClient) -> ScopingDecision:
    """One LLM call. Always returns a decision (raises on malformed)."""
    user_message = json.dumps(
        {
            "pattern": claim.get("pattern"),
            "predicate": claim.get("predicate"),
            "slots": claim.get("slots", {}),
            "polarity": claim.get("polarity", 1),
            "source_text": claim.get("source_text", "")[:200],
        },
        default=str,
    )
    raw = llm.extract_with_tool(
        _SCOPING_SYSTEM, user_message, _SCOPING_TOOL, max_tokens=512,
        purpose="cache_scoping",
    )
    scope = raw.get("scope")
    if scope not in SCOPING_METHODS:
        raise RuntimeError(
            f"scoping classifier returned invalid scope {scope!r}; "
            f"expected one of {SCOPING_METHODS}"
        )
    return ScopingDecision(
        scope=scope,
        reason=str(raw.get("reason", "")),
        confidence=float(raw.get("confidence", 0.0)),
        raw=dict(raw),
    )
