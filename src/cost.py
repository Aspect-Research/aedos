"""LLM cost accounting (v0.9.x — cross-provider cache-aware).

Pricing is per million tokens, sourced from each provider's published
rates. The numbers will drift; the model is the lookup key so adding a
new model is a one-line change.

Cache tiers (the v0.9.x audit fix)
==================================
Both Anthropic and OpenAI bill cached input tokens differently from
uncached input tokens, with provider-specific multipliers. Earlier
versions of this module only counted ``input_tokens`` (Anthropic) /
``prompt_tokens`` (OpenAI) at the base rate, which:

  * Understated Anthropic cost when a cache write or hit occurred —
    the cache tokens were billed by the API but invisible in our ledger.
  * Overstated OpenAI cost when a cache hit occurred — ``prompt_tokens``
    INCLUDES cached tokens at the full count, so billing them at 1.0x
    double-charged the cached portion.

``cost_for_call`` now takes explicit ``cache_creation_tokens`` and
``cache_read_tokens`` and applies the right multiplier based on the
provider derived from the model prefix:

  Anthropic:  cache write × 1.25, cache read × 0.10
  OpenAI:     cache write × 1.00, cache read × 0.50

The Anthropic SDK exposes the relevant token counts as
``cache_creation_input_tokens`` and ``cache_read_input_tokens`` on the
usage object; ``input_tokens`` is the uncached remainder. The OpenAI
SDK exposes the cached portion as
``usage.prompt_tokens_details.cached_tokens`` while ``prompt_tokens``
is the *total* (uncached + cached). Callers must handle the OpenAI
subtraction before calling ``cost_for_call``.
"""

from __future__ import annotations

from dataclasses import dataclass


# Per-million-token INPUT/OUTPUT pricing in USD, as of 2026-04.
# Format: model_id_prefix → (input_per_mtok, output_per_mtok)
# Cache-tier prices are derived from input_per_mtok via the provider
# multipliers below — providers don't publish them as separate columns.
# Prefix-match lets versioned IDs (claude-opus-4-7-20260101) hit the
# right tier.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-7":   (15.00, 75.00),
    "claude-opus-4-6":   (15.00, 75.00),
    "claude-opus-4-5":   (15.00, 75.00),
    "claude-sonnet-4-6": (3.00,  15.00),
    "claude-sonnet-4-5": (3.00,  15.00),
    "claude-haiku-4-5":  (1.00,  5.00),
    # OpenAI — added v0.8.0. Per-MTok pricing as of 2026-04.
    "gpt-4.1":           (2.00,   8.00),
    "gpt-4.1-mini":      (0.40,   1.60),
    "gpt-4.1-nano":      (0.10,   0.40),
    "gpt-4o":            (2.50,  10.00),
    "gpt-4o-mini":       (0.15,   0.60),
}


# Cache-tier multipliers applied to the model's input_per_mtok rate.
# Sourced from each provider's pricing documentation. If we ever add an
# o1/o3-style reasoning model whose cached read rate differs from the
# rest of the OpenAI lineup, the simplest fix is a third bucket here
# keyed by a more specific prefix.
_OPENAI_PREFIXES = ("gpt-", "o1-", "o3-", "o4-")
_CACHE_MULTIPLIERS: dict[str, dict[str, float]] = {
    "anthropic": {"creation": 1.25, "read": 0.10},
    "openai":    {"creation": 1.00, "read": 0.50},
}


def _provider_for(model: str) -> str:
    """Map a model id to the provider whose cache multipliers apply.
    Defaults to ``anthropic`` for unknown / claude-* ids."""
    if any(model.startswith(p) for p in _OPENAI_PREFIXES):
        return "openai"
    return "anthropic"


@dataclass
class CallCost:
    model: str
    input_tokens: int                      # uncached input
    output_tokens: int
    input_usd: float
    output_usd: float
    total_usd: float
    pricing_known: bool
    # v0.9.x — cache-tier accounting. cache_creation_tokens fires only
    # on Anthropic in practice (OpenAI doesn't charge a write premium
    # so the field stays 0); cache_read_tokens fires on both providers
    # whenever a cached prompt is reused.
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_usd: float = 0.0
    cache_read_usd: float = 0.0
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
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "input_usd": round(self.input_usd, 6),
            "output_usd": round(self.output_usd, 6),
            "cache_creation_usd": round(self.cache_creation_usd, 6),
            "cache_read_usd": round(self.cache_read_usd, 6),
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
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    purpose: str | None = None, duration_ms: float | None = None,
) -> CallCost:
    """Compute the USD cost of a single LLM call.

    ``input_tokens`` must be the UNCACHED portion. The cached portion
    is split into ``cache_creation_tokens`` (newly written) and
    ``cache_read_tokens`` (served from cache). Provider-specific
    multipliers (Anthropic: 1.25x / 0.10x; OpenAI: 1.00x / 0.50x)
    are applied based on the model's prefix.

    Anthropic callers can read all three counts directly from
    ``usage.input_tokens / cache_creation_input_tokens /
    cache_read_input_tokens``. OpenAI callers must subtract
    ``usage.prompt_tokens_details.cached_tokens`` from
    ``usage.prompt_tokens`` themselves — OpenAI's ``prompt_tokens``
    is the TOTAL, not the uncached remainder.

    Unknown models report ``pricing_known=False`` and total_usd=0 so
    aggregations don't poison the per-turn total when a new model
    appears that we haven't priced yet.

    Tokens are clamped to non-negative integers — a malformed response
    that reported negative tokens shouldn't credit the operator with
    negative cost.
    """
    in_toks = max(int(input_tokens or 0), 0)
    out_toks = max(int(output_tokens or 0), 0)
    cc_toks = max(int(cache_creation_tokens or 0), 0)
    cr_toks = max(int(cache_read_tokens or 0), 0)
    pricing = _lookup_pricing(model)
    if pricing is None:
        return CallCost(
            model=model,
            input_tokens=in_toks,
            output_tokens=out_toks,
            cache_creation_tokens=cc_toks,
            cache_read_tokens=cr_toks,
            input_usd=0.0,
            output_usd=0.0,
            cache_creation_usd=0.0,
            cache_read_usd=0.0,
            total_usd=0.0,
            pricing_known=False,
            purpose=purpose,
            duration_ms=duration_ms,
        )
    input_per_mtok, output_per_mtok = pricing
    mult = _CACHE_MULTIPLIERS[_provider_for(model)]
    in_usd = (in_toks / 1_000_000) * input_per_mtok
    out_usd = (out_toks / 1_000_000) * output_per_mtok
    cc_usd = (cc_toks / 1_000_000) * input_per_mtok * mult["creation"]
    cr_usd = (cr_toks / 1_000_000) * input_per_mtok * mult["read"]
    return CallCost(
        model=model,
        input_tokens=in_toks,
        output_tokens=out_toks,
        cache_creation_tokens=cc_toks,
        cache_read_tokens=cr_toks,
        input_usd=in_usd,
        output_usd=out_usd,
        cache_creation_usd=cc_usd,
        cache_read_usd=cr_usd,
        total_usd=in_usd + out_usd + cc_usd + cr_usd,
        pricing_known=True,
        purpose=purpose,
        duration_ms=duration_ms,
    )


def aggregate_costs(calls: list[CallCost]) -> dict:
    """Sum a list of CallCost entries into a turn-level summary.

    v0.7.3: includes a per-purpose breakdown and a temporal `calls`
    list (in-order per-call dicts) so the UI can show every LLM call
    individually with its purpose, model, duration, and cost.

    v0.9.x: surfaces cache-tier totals so the UI can display cache
    hit rate and the dollar value of cache-driven savings. The
    per-model and per-purpose slots get the same breakdown.
    """
    total_in = sum(c.input_tokens for c in calls)
    total_out = sum(c.output_tokens for c in calls)
    total_cc = sum(c.cache_creation_tokens for c in calls)
    total_cr = sum(c.cache_read_tokens for c in calls)
    total_usd = sum(c.total_usd for c in calls)
    by_model: dict[str, dict] = {}
    by_purpose: dict[str, dict] = {}
    for c in calls:
        slot = by_model.setdefault(c.model, {
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_creation_tokens": 0, "cache_read_tokens": 0,
            "total_usd": 0.0,
        })
        slot["calls"] += 1
        slot["input_tokens"] += c.input_tokens
        slot["output_tokens"] += c.output_tokens
        slot["cache_creation_tokens"] += c.cache_creation_tokens
        slot["cache_read_tokens"] += c.cache_read_tokens
        slot["total_usd"] += c.total_usd
        purpose_key = c.purpose or "unknown"
        pslot = by_purpose.setdefault(purpose_key, {
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_creation_tokens": 0, "cache_read_tokens": 0,
            "total_usd": 0.0, "duration_ms": 0.0,
        })
        pslot["calls"] += 1
        pslot["input_tokens"] += c.input_tokens
        pslot["output_tokens"] += c.output_tokens
        pslot["cache_creation_tokens"] += c.cache_creation_tokens
        pslot["cache_read_tokens"] += c.cache_read_tokens
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
        "total_cache_creation_tokens": total_cc,
        "total_cache_read_tokens": total_cr,
        "total_usd": round(total_usd, 6),
        "by_model": by_model,
        "by_purpose": by_purpose,
        "calls": [c.to_dict() for c in calls],
        "any_unknown_pricing": any(not c.pricing_known for c in calls),
    }
