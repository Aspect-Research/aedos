"""LLM cost accounting (v0.6).

Pricing is per million tokens, sourced from Anthropic's published
rates. Updated 2026-04. The numbers will drift; the model is the lookup
key so adding a new model is a one-line change.
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
    # OpenAI — added v0.8.0. Per-MTok pricing as of 2026-04.
    "gpt-4.1":           (2.00,   8.00),
    "gpt-4.1-mini":      (0.40,   1.60),
    "gpt-4.1-nano":      (0.10,   0.40),
    "gpt-4o":            (2.50,  10.00),
    "gpt-4o-mini":       (0.15,   0.60),
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
    # v0.7.3: purpose label + wall-clock duration of the call. Lets the
    # UI show "extractor (Opus 4.7) · 0.5s · $0.0008" instead of just a
    # by-model aggregate. Optional so legacy call sites keep working.
    purpose: str | None = None
    duration_ms: float | None = None

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "input_usd": round(self.input_usd, 6),
            "output_usd": round(self.output_usd, 6),
            "total_usd": round(self.total_usd, 6),
            "pricing_known": self.pricing_known,
            "purpose": self.purpose,
            "duration_ms": (
                round(self.duration_ms, 1) if self.duration_ms is not None else None
            ),
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
    purpose: str | None = None, duration_ms: float | None = None,
) -> CallCost:
    """Compute the USD cost of a single LLM call.

    Unknown models report ``pricing_known=False`` and total_usd=0 so
    aggregations don't poison the per-turn total when a new model
    appears that we haven't priced yet.

    Tokens are clamped to non-negative integers — a malformed response
    that reported negative tokens shouldn't credit the operator with
    negative cost.
    """
    # Defensive: clamp to non-negative integers.
    in_toks = max(int(input_tokens or 0), 0)
    out_toks = max(int(output_tokens or 0), 0)
    pricing = _lookup_pricing(model)
    if pricing is None:
        return CallCost(
            model=model,
            input_tokens=in_toks,
            output_tokens=out_toks,
            input_usd=0.0,
            output_usd=0.0,
            total_usd=0.0,
            pricing_known=False,
            purpose=purpose,
            duration_ms=duration_ms,
        )
    input_per_mtok, output_per_mtok = pricing
    in_usd = (in_toks / 1_000_000) * input_per_mtok
    out_usd = (out_toks / 1_000_000) * output_per_mtok
    return CallCost(
        model=model,
        input_tokens=in_toks,
        output_tokens=out_toks,
        input_usd=in_usd,
        output_usd=out_usd,
        total_usd=in_usd + out_usd,
        pricing_known=True,
        purpose=purpose,
        duration_ms=duration_ms,
    )


def aggregate_costs(calls: list[CallCost]) -> dict:
    """Sum a list of CallCost entries into a turn-level summary.

    v0.7.3: includes a per-purpose breakdown and a temporal `calls`
    list (in-order per-call dicts) so the UI can show every LLM call
    individually with its purpose, model, duration, and cost.
    """
    total_in = sum(c.input_tokens for c in calls)
    total_out = sum(c.output_tokens for c in calls)
    total_usd = sum(c.total_usd for c in calls)
    by_model: dict[str, dict] = {}
    by_purpose: dict[str, dict] = {}
    for c in calls:
        slot = by_model.setdefault(c.model, {
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "total_usd": 0.0,
        })
        slot["calls"] += 1
        slot["input_tokens"] += c.input_tokens
        slot["output_tokens"] += c.output_tokens
        slot["total_usd"] += c.total_usd
        purpose_key = c.purpose or "unknown"
        pslot = by_purpose.setdefault(purpose_key, {
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "total_usd": 0.0, "duration_ms": 0.0,
        })
        pslot["calls"] += 1
        pslot["input_tokens"] += c.input_tokens
        pslot["output_tokens"] += c.output_tokens
        pslot["total_usd"] += c.total_usd
        if c.duration_ms is not None:
            pslot["duration_ms"] += c.duration_ms
    for slot in by_model.values():
        slot["total_usd"] = round(slot["total_usd"], 6)
    for slot in by_purpose.values():
        slot["total_usd"] = round(slot["total_usd"], 6)
        slot["duration_ms"] = round(slot["duration_ms"], 1)
    return {
        "total_calls": len(calls),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_usd": round(total_usd, 6),
        "by_model": by_model,
        "by_purpose": by_purpose,
        "calls": [c.to_dict() for c in calls],
        "any_unknown_pricing": any(not c.pricing_known for c in calls),
    }
