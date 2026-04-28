"""Cache TTL stability classifier (v0.6, observation mode).

For claims the scoping classifier marked ``world_fact``, decide the
TTL class — how long the cached verdict is trustworthy for. Six bins:

    immutable       — math, definitions, fixed historical events
    decade_stable   — geography, demographics that change slowly
    years_stable    — political offices, executive roles, sports records
    months_stable   — pop-culture facts, recent records
    days_stable     — news headlines, current events that recur
    volatile        — prices, weather, real-time data — usually NOT cacheable

The classifier returns a stability class plus a TTL in seconds, plus a
reason. Observation mode: log only, no cache writes yet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from src.llm_client import LLMClient


STABILITY_CLASSES = (
    "immutable",
    "decade_stable",
    "years_stable",
    "months_stable",
    "days_stable",
    "volatile",
)

# Recommended TTL per class. immutable → None (no expiry). volatile →
# 0 (don't cache). The classifier returns a class; the caller maps to
# expires_at via this table. Caller can override for special cases.
STABILITY_TTL_SECONDS: dict[str, int | None] = {
    "immutable": None,
    "decade_stable": 10 * 365 * 24 * 3600,    # 10 years
    "years_stable": 365 * 24 * 3600,          # 1 year
    "months_stable": 30 * 24 * 3600,          # 30 days
    "days_stable": 24 * 3600,                 # 1 day
    "volatile": 0,                            # don't cache
}


@dataclass
class StabilityDecision:
    stability_class: str  # one of STABILITY_CLASSES
    reason: str
    confidence: float
    ttl_seconds: int | None = None  # None = no expiry; 0 = don't cache
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stability_class": self.stability_class,
            "reason": self.reason,
            "confidence": self.confidence,
            "ttl_seconds": self.ttl_seconds,
        }


_STABILITY_SYSTEM = """You classify the temporal stability of a verified WORLD FACT.

You receive a single structured claim (already classified as world_fact
by an upstream classifier). You decide how long a cached verdict for
this claim should be trusted before re-verification:

  immutable      — mathematical, definitional, or completed historical
                   facts that cannot change (1+1=2, the Berlin Wall fell
                   in 1989, the atomic number of carbon is 6, the
                   Constitution was signed in 1787).
  decade_stable  — geographic and demographic facts that change very
                   slowly (Tokyo is in Japan, Mt Everest is 8848.86m,
                   Bhutan's capital is Thimphu, the Mississippi is the
                   longest river in the US).
  years_stable   — political offices, sitting heads of state, current
                   sports records, current CEOs, current top-3 in any
                   ranked list. Re-verify yearly.
  months_stable  — recent pop-culture facts, recent best-sellers, recent
                   awards. Re-verify monthly.
  days_stable    — current news headlines, recurring news topics,
                   short-cycle facts ('Apple's stock price closed
                   Friday at X' — but those usually go to volatile).
  volatile       — prices, weather, exchange rates, anything in real
                   time. Don't cache; re-verify every time.

# Decision rule

Ask: "If a user asked this question 6 months from now, would the answer
be the same?" If yes, decade_stable or higher. If 'maybe', months_stable.
If 'probably not', days_stable. If 'definitely not', volatile.

Bias toward SHORTER TTLs when uncertain. A cache miss costs one extra
retrieval; a stale cached verdict serves a wrong answer for the entire
TTL window. Wrong-and-confident is worse than slow-and-correct.

# Worked examples

Claim: pattern=quantitative, predicate=has_count, slots={subject:'strawberry', property:'letter_r', value:3}, polarity=1
→ stability_class: immutable, reason: "structural property of a fixed string; cannot change.", confidence: 0.99

Claim: pattern=spatial_temporal, predicate=located_in, slots={entity:Tokyo, location:Japan}, polarity=1
→ stability_class: decade_stable, reason: "geographic fact; stable on multi-decade timescale.", confidence: 0.95

Claim: pattern=quantitative, predicate=birth_year, slots={subject:'Marie Curie', property:'birth_year', value:1867}, polarity=1
→ stability_class: immutable, reason: "biographical fact about a deceased historical figure; immutable.", confidence: 0.99

Claim: pattern=role_assignment, predicate=holds_role, slots={agent:'Donald Trump', role:'47th President', org:'United States'}, polarity=1
→ stability_class: years_stable, reason: "political office held until next presidential term; stable for ~4 years but changes.", confidence: 0.9

Claim: pattern=quantitative, predicate=stock_price, slots={subject:'Apple', property:'closing_price', value:175.50, unit:'USD'}, polarity=1
→ stability_class: volatile, reason: "stock prices change continuously; do not cache.", confidence: 0.99

Claim: pattern=quantitative, predicate=population_of, slots={subject:'Tokyo', property:'population', value:14000000}, polarity=1
→ stability_class: decade_stable, reason: "city population changes slowly; rough figure stable across years.", confidence: 0.85

Claim: pattern=quantitative, predicate=us_states_count, slots={subject:'United States', property:'state_count', value:50}, polarity=1
→ stability_class: decade_stable, reason: "stable since 1959; could change but unlikely on multi-year horizon.", confidence: 0.95

Claim: pattern=event, predicate=launched, slots={event_type:'iPhone 15', occurred_at:'2023-09'}, polarity=1
→ stability_class: immutable, reason: "completed historical event with a fixed date.", confidence: 0.99

# Output

Call the ``record_stability`` tool exactly once. Reason should explain
WHY this TTL class fits. confidence in [0.0, 1.0]. Never reply with
prose."""


_STABILITY_TOOL = {
    "name": "record_stability",
    "description": (
        "Record the stability class for caching this verified world-fact "
        "claim. Choose one of immutable / decade_stable / years_stable / "
        "months_stable / days_stable / volatile."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "stability_class": {
                "type": "string",
                "enum": list(STABILITY_CLASSES),
                "description": "TTL class.",
            },
            "reason": {
                "type": "string",
                "description": "One-sentence rationale.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "0.0-1.0 confidence in the classification.",
            },
        },
        "required": ["stability_class", "reason", "confidence"],
    },
}


def classify_stability(claim: dict, llm: LLMClient) -> StabilityDecision:
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
        _STABILITY_SYSTEM, user_message, _STABILITY_TOOL, max_tokens=512,
    )
    cls = raw.get("stability_class")
    if cls not in STABILITY_CLASSES:
        raise RuntimeError(
            f"stability classifier returned invalid class {cls!r}; "
            f"expected one of {STABILITY_CLASSES}"
        )
    return StabilityDecision(
        stability_class=cls,
        reason=str(raw.get("reason", "")),
        confidence=float(raw.get("confidence", 0.0)),
        ttl_seconds=STABILITY_TTL_SECONDS.get(cls),
        raw=dict(raw),
    )
