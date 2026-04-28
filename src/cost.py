"""LLM cost accounting (v0.6).

Pricing is per million tokens, sourced from Anthropic's published
rates. Updated 2026-04. The numbers will drift; the model is the lookup
key so adding a new model is a one-line change.

Model pricing here. Modal/GLM is free until 2026-04-30, after which we
need to revisit (maybe move to per-second compute pricing).
"""

from __future__ import annotations

from dataclasses import dataclass


# Per-million-token pricing in USD, as of 2026-04.
# Format: model_id_prefix → (input_per_mtok, output_per_mtok)
# Prefix-match lets versioned IDs (claude-opus-4-7-20260101) hit the
# right tier.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-7":   (15.00, 75.00),
    "claude-opus-4-6":   (15.00, 75.00),
    "claude-opus-4-5":   (15.00, 75.00),
    "claude-sonnet-4-6": (3.00,  15.00),
    "claude-sonnet-4-5": (3.00,  15.00),
    "claude-haiku-4-5":  (1.00,  5.00),
    "claude-haiku-4-0":  (0.25,  1.25),
    # Modal/GLM-5.1-FP8 — free tier until 2026-04-30; after that we
    # need per-second compute pricing. Track as $0 for now.
    "zai-org/GLM-5.1":   (0.00,  0.00),
}


@dataclass
class CallCost:
    model: str
    input_tokens: int
    output_tokens: int
    input_usd: float
    output_usd: float
    total_usd: float
    pricing_known: bool

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "input_usd": round(self.input_usd, 6),
            "output_usd": round(self.output_usd, 6),
            "total_usd": round(self.total_usd, 6),
            "pricing_known": self.pricing_known,
        }


def _lookup_pricing(model: str) -> tuple[float, float] | None:
    """Prefix-match the model against the pricing table. Returns
    (input_per_mtok, output_per_mtok) or None if unknown."""
    if not model:
        return None
    # Exact match first.
    if model in _PRICING:
        return _PRICING[model]
    # Then prefix match (versioned IDs).
    for prefix, price in _PRICING.items():
        if model.startswith(prefix):
            return price
    return None


def cost_for_call(
    model: str, input_tokens: int, output_tokens: int,
) -> CallCost:
    """Compute the USD cost of a single LLM call.

    Unknown models report ``pricing_known=False`` and total_usd=0 so
    aggregations don't poison the per-turn total when a new model
    appears that we haven't priced yet."""
    pricing = _lookup_pricing(model)
    if pricing is None:
        return CallCost(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_usd=0.0,
            output_usd=0.0,
            total_usd=0.0,
            pricing_known=False,
        )
    input_per_mtok, output_per_mtok = pricing
    in_usd = (input_tokens / 1_000_000) * input_per_mtok
    out_usd = (output_tokens / 1_000_000) * output_per_mtok
    return CallCost(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_usd=in_usd,
        output_usd=out_usd,
        total_usd=in_usd + out_usd,
        pricing_known=True,
    )


def aggregate_costs(calls: list[CallCost]) -> dict:
    """Sum a list of CallCost entries into a turn-level summary."""
    total_in = sum(c.input_tokens for c in calls)
    total_out = sum(c.output_tokens for c in calls)
    total_usd = sum(c.total_usd for c in calls)
    by_model: dict[str, dict] = {}
    for c in calls:
        slot = by_model.setdefault(c.model, {
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "total_usd": 0.0,
        })
        slot["calls"] += 1
        slot["input_tokens"] += c.input_tokens
        slot["output_tokens"] += c.output_tokens
        slot["total_usd"] += c.total_usd
    # Round usd fields for display.
    for slot in by_model.values():
        slot["total_usd"] = round(slot["total_usd"], 6)
    return {
        "total_calls": len(calls),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_usd": round(total_usd, 6),
        "by_model": by_model,
        "any_unknown_pricing": any(not c.pricing_known for c in calls),
    }
