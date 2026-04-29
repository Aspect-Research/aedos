"""Combined scoping + stability classifier (v0.7.16).

Pre-v0.7.16 the cache pipeline ran TWO LLM calls per claim during
classification: scoping (user_specific / session_specific / world_fact)
and, for world_fact claims, stability (immutable / decade_stable /
years_stable / months_stable / days_stable / volatile).

v0.7.16 merges both into a single forced-tool-use call. The model is
asked to first decide scope, and IF scope is world_fact, to also
classify stability. The response is one tool call:

    {
      "scope": "world_fact",
      "scope_reason": "...",
      "scope_confidence": 0.99,
      "stability_class": "decade_stable",     # required when scope=world_fact
      "stability_reason": "...",              # required when scope=world_fact
      "stability_confidence": 0.95            # required when scope=world_fact
    }

For non-world-fact claims, the stability fields are omitted (the tool
schema marks them optional). This halves cache-classifier LLM calls
on cache-eligible claims and cuts them by ⅓ on non-eligible ones
(scope-only).

The two old single-classifier modules (scoping_classifier,
stability_classifier) stay around for backward compat with tests and
for scenarios where you want to call one independently. CacheGate
prefers the combined classifier when wired.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from src.cache.scoping_classifier import (
    SCOPING_METHODS,
    ScopingDecision,
)
from src.cache.stability_classifier import (
    STABILITY_CLASSES,
    STABILITY_TTL_SECONDS,
    StabilityDecision,
    _historical_period_shortcut,
)
from src.llm_client import LLMClient


@dataclass
class CombinedDecision:
    """Result of one combined classifier call.

    ``stability`` is None when scope is not world_fact (no caching
    happens, no TTL needed) and when the historical-period shortcut
    didn't fire. The CacheGate consumer treats `(scoping, stability)`
    as the same shape it would have received from the two separate
    classifiers."""
    scoping: ScopingDecision
    stability: StabilityDecision | None


_COMBINED_SYSTEM = """You classify claims for the verification cache.
Answer TWO questions in one tool call:

(1) SCOPE — one of:

  user_specific      — about the user (preferences, beliefs, biographical
                       facts, locations, possessions, attitudes). Cannot
                       be cached because the answer depends on which
                       user is asking.
  session_specific   — about THIS conversation, transient context,
                       hypotheticals, future-event predictions, or
                       references to literal text from this turn.
                       Cannot be cached because it has no validity
                       outside this conversation.
  world_fact         — same answer for every user, every session.
                       Definitional, structural, geographic, historical,
                       biographical-about-public-figures, current-world-
                       state. CAN be cached.

(2) STABILITY (only required if scope is world_fact) — how long a cached
verdict for this claim should be trusted before re-verification:

  immutable      — mathematical, definitional, or completed historical
                   facts that cannot change (1+1=2, the Berlin Wall fell
                   in 1989, atomic numbers, the Constitution was signed
                   in 1787).
  decade_stable  — geographic and demographic facts that change very
                   slowly (Tokyo is in Japan, Mt Everest's height,
                   Bhutan's capital, longest-river rankings).
  years_stable   — political offices, sitting heads of state, current
                   sports records, current CEOs, current top-3 in any
                   ranked list. Re-verify within months.
  months_stable  — recent pop-culture facts, recent best-sellers,
                   recent awards. Re-verify within weeks.
  days_stable    — current news headlines, recurring news topics,
                   today's market state. Re-verify within hours.
  volatile       — stock prices, sports scores in progress; never
                   useful to cache.

# Output

Call the ``record_classification`` tool exactly once.
- Always provide scope, scope_reason, scope_confidence.
- If scope is world_fact: ALSO provide stability_class, stability_reason,
  stability_confidence.
- If scope is user_specific or session_specific: OMIT the stability_*
  fields entirely.

Reasons must be one sentence each. Confidences in [0.0, 1.0].
Never reply with prose.

# Examples

Claim: {pattern: 'preference', predicate: 'likes', slots: {agent: 'user', object: 'tea'}}
→ scope: user_specific, scope_reason: "agent is the user; preference is by definition user-specific.", scope_confidence: 0.99
  (no stability fields)

Claim: {pattern: 'spatial_temporal', predicate: 'located_in', slots: {entity: 'Tokyo', location: 'Japan'}}
→ scope: world_fact, scope_reason: "geographic fact; same for every user.", scope_confidence: 0.99
  stability_class: decade_stable, stability_reason: "geographic fact; stable on multi-decade timescale.", stability_confidence: 0.95

Claim: {pattern: 'event', predicate: 'happened_in', slots: {event: 'Berlin Wall fell', year: 1989}}
→ scope: world_fact, scope_reason: "historical event; same answer for every observer.", scope_confidence: 0.99
  stability_class: immutable, stability_reason: "completed historical event with a fixed date.", stability_confidence: 0.99

Claim: {pattern: 'quantitative', predicate: 'has_count', slots: {subject: 'this sentence', property: 'words', value: 5}}
→ scope: session_specific, scope_reason: "subject is a literal sentence from this conversation; meaningful only in this turn.", scope_confidence: 0.95
  (no stability fields)
"""


_COMBINED_TOOL = {
    "name": "record_classification",
    "description": (
        "Record both the cache-scope (always) and the stability class "
        "(only when scope=world_fact)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": list(SCOPING_METHODS),
                "description": "The scope class.",
            },
            "scope_reason": {
                "type": "string",
                "description": "One-sentence justification for the scope choice.",
            },
            "scope_confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "0.0-1.0 confidence in the scope classification.",
            },
            "stability_class": {
                "type": "string",
                "enum": list(STABILITY_CLASSES),
                "description": (
                    "Required when scope=world_fact; omit otherwise."
                ),
            },
            "stability_reason": {
                "type": "string",
                "description": (
                    "Required when scope=world_fact; one-sentence justification."
                ),
            },
            "stability_confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": (
                    "Required when scope=world_fact; 0.0-1.0 confidence in "
                    "the stability classification."
                ),
            },
        },
        "required": ["scope", "scope_reason", "scope_confidence"],
    },
}


def classify_for_cache(
    claim: dict, llm: LLMClient,
) -> CombinedDecision:
    """One LLM call returns both scope and (when applicable) stability.

    Mirrors the historical-period shortcut from the standalone
    stability_classifier — claims with valid_until strictly in the
    past skip the LLM call entirely and resolve to immutable.

    The user_message is the full claim payload (truncated source_text)
    so the model sees the same context the two-call path saw.
    """
    # Historical-period shortcut: don't even ask the model. Falls
    # through to the LLM call when the shortcut doesn't apply.
    short = _historical_period_shortcut(claim)
    if short is not None:
        # The shortcut implies world_fact + immutable — no LLM call
        # needed. Surface a synthetic ScopingDecision so the gate's
        # downstream logic works.
        return CombinedDecision(
            scoping=ScopingDecision(
                scope="world_fact",
                reason="historical period strictly in the past — world fact by construction",
                confidence=0.99,
                raw={"shortcut": "historical_period"},
            ),
            stability=short,
        )

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
        _COMBINED_SYSTEM, user_message, _COMBINED_TOOL, max_tokens=768,
        purpose="cache_classify",
    )

    scope = raw.get("scope")
    if scope not in SCOPING_METHODS:
        raise RuntimeError(
            f"combined classifier returned invalid scope {scope!r}; "
            f"expected one of {SCOPING_METHODS}"
        )
    scoping = ScopingDecision(
        scope=scope,
        reason=str(raw.get("scope_reason", "")),
        confidence=float(raw.get("scope_confidence", 0.0)),
        raw=dict(raw),
    )

    stability: StabilityDecision | None = None
    if scope == "world_fact":
        cls = raw.get("stability_class")
        if cls not in STABILITY_CLASSES:
            raise RuntimeError(
                f"combined classifier returned world_fact but stability_class "
                f"{cls!r} is invalid; expected one of {STABILITY_CLASSES}"
            )
        stability = StabilityDecision(
            stability_class=cls,
            reason=str(raw.get("stability_reason", "")),
            confidence=float(raw.get("stability_confidence", 0.0)),
            ttl_seconds=STABILITY_TTL_SECONDS.get(cls),
            raw=dict(raw),
        )
    return CombinedDecision(scoping=scoping, stability=stability)
