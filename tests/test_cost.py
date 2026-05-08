"""Tests for the cost module."""

from __future__ import annotations

import pytest

from src.cost import CallCost, aggregate_costs, cost_for_call


def test_opus_pricing():
    c = cost_for_call("claude-opus-4-7", input_tokens=1_000_000, output_tokens=0)
    assert c.input_usd == pytest.approx(15.00)
    assert c.output_usd == 0.0
    assert c.total_usd == pytest.approx(15.00)
    assert c.pricing_known


def test_sonnet_pricing():
    c = cost_for_call("claude-sonnet-4-6", input_tokens=0, output_tokens=1_000_000)
    assert c.output_usd == pytest.approx(15.00)
    assert c.total_usd == pytest.approx(15.00)


def test_haiku_pricing():
    c = cost_for_call("claude-haiku-4-5", input_tokens=1_000_000,
                      output_tokens=1_000_000)
    assert c.input_usd == pytest.approx(1.00)
    assert c.output_usd == pytest.approx(5.00)
    assert c.total_usd == pytest.approx(6.00)


def test_versioned_model_prefix_match():
    """Versioned IDs (claude-opus-4-7-20260101) should hit the same
    pricing tier as the base model."""
    c = cost_for_call("claude-opus-4-7-20260101",
                      input_tokens=1_000_000, output_tokens=0)
    assert c.input_usd == pytest.approx(15.00)
    assert c.pricing_known


def test_unknown_model_zero_cost_and_flagged():
    c = cost_for_call("future-model-x", input_tokens=1_000_000,
                      output_tokens=1_000_000)
    assert c.total_usd == 0.0
    assert not c.pricing_known


def test_partial_million_tokens():
    """Linear in tokens — 100k input on opus = 15 * 0.1 = $1.50."""
    c = cost_for_call("claude-opus-4-7", input_tokens=100_000,
                      output_tokens=0)
    assert c.input_usd == pytest.approx(1.50)


def test_empty_model_string_unknown():
    c = cost_for_call("", input_tokens=100, output_tokens=100)
    assert not c.pricing_known
    assert c.total_usd == 0.0


def test_negative_tokens_clamped_to_zero():
    """Defensive: a malformed response with negative tokens shouldn't
    produce negative cost (would credit the operator with -$$)."""
    c = cost_for_call("claude-opus-4-7",
                      input_tokens=-100, output_tokens=-50)
    assert c.input_tokens == 0
    assert c.output_tokens == 0
    assert c.total_usd == 0.0


def test_float_tokens_truncated():
    """Float tokens (would only happen on a buggy upstream) get int()'d."""
    c = cost_for_call("claude-opus-4-7",
                      input_tokens=100.7, output_tokens=50.3)
    assert c.input_tokens == 100
    assert c.output_tokens == 50


def test_none_tokens_treated_as_zero():
    """None token counts (from getattr(.., default=None)) should not crash."""
    c = cost_for_call("claude-opus-4-7", input_tokens=None, output_tokens=None)
    assert c.input_tokens == 0
    assert c.output_tokens == 0
    assert c.total_usd == 0.0


# ---- aggregate_costs ----


def test_aggregate_empty_list():
    a = aggregate_costs([])
    assert a["total_calls"] == 0
    assert a["total_usd"] == 0.0
    assert a["by_model"] == {}
    assert not a["any_unknown_pricing"]


def test_aggregate_sums_correctly():
    a = aggregate_costs([
        cost_for_call("claude-opus-4-7", 1000, 500),
        cost_for_call("claude-sonnet-4-6", 2000, 1000),
        cost_for_call("claude-opus-4-7", 500, 200),
    ])
    assert a["total_calls"] == 3
    assert a["total_input_tokens"] == 3500
    assert a["total_output_tokens"] == 1700

    # Two opus calls aggregated.
    opus = a["by_model"]["claude-opus-4-7"]
    assert opus["calls"] == 2
    assert opus["input_tokens"] == 1500
    assert opus["output_tokens"] == 700

    sonnet = a["by_model"]["claude-sonnet-4-6"]
    assert sonnet["calls"] == 1
    assert sonnet["input_tokens"] == 2000


def test_aggregate_flags_unknown_pricing():
    a = aggregate_costs([
        cost_for_call("claude-opus-4-7", 1000, 500),
        cost_for_call("future-model-x", 1000, 500),
    ])
    assert a["any_unknown_pricing"]


def test_aggregate_no_unknown_when_all_known():
    a = aggregate_costs([
        cost_for_call("claude-opus-4-7", 1000, 500),
        cost_for_call("claude-sonnet-4-6", 1000, 500),
    ])
    assert not a["any_unknown_pricing"]


def test_call_cost_to_dict_shape():
    c = cost_for_call("claude-opus-4-7", input_tokens=10, output_tokens=20)
    d = c.to_dict()
    assert set(d.keys()) == {
        "model", "input_tokens", "output_tokens",
        "input_usd", "output_usd", "total_usd", "pricing_known",
        # v0.7.3: purpose label + per-call wall-clock duration
        "purpose", "duration_ms",
        # v0.9.x: cache-tier accounting
        "cache_creation_tokens", "cache_read_tokens",
        "cache_creation_usd", "cache_read_usd",
    }


# ---- cache-tier pricing (v0.9.x) ----


def test_anthropic_cache_creation_billed_at_125_percent():
    """Anthropic charges 1.25× the input rate for tokens written into the
    prompt cache. Opus input is $15/MTok → cache write is $18.75/MTok."""
    c = cost_for_call(
        "claude-opus-4-7", input_tokens=0, output_tokens=0,
        cache_creation_tokens=1_000_000,
    )
    assert c.cache_creation_usd == pytest.approx(18.75)
    assert c.cache_read_usd == 0.0
    assert c.input_usd == 0.0
    assert c.total_usd == pytest.approx(18.75)


def test_anthropic_cache_read_billed_at_10_percent():
    """Cache hits cost 0.10× the input rate on Anthropic. Opus = $1.50/MTok read."""
    c = cost_for_call(
        "claude-opus-4-7", input_tokens=0, output_tokens=0,
        cache_read_tokens=1_000_000,
    )
    assert c.cache_read_usd == pytest.approx(1.50)
    assert c.cache_creation_usd == 0.0
    assert c.total_usd == pytest.approx(1.50)


def test_openai_cache_read_billed_at_50_percent():
    """OpenAI's gpt-4.1 family discounts cached prompt tokens by 50%.
    gpt-4.1-mini input is $0.40/MTok → cache read is $0.20/MTok."""
    c = cost_for_call(
        "gpt-4.1-mini", input_tokens=0, output_tokens=0,
        cache_read_tokens=1_000_000,
    )
    assert c.cache_read_usd == pytest.approx(0.20)
    assert c.total_usd == pytest.approx(0.20)


def test_openai_cache_creation_no_premium():
    """OpenAI doesn't charge a write premium — cache_creation_tokens is
    always 0 in practice on the OpenAI side, but if we were ever passed
    a non-zero value it should price at 1.0× input (no extra)."""
    c = cost_for_call(
        "gpt-4.1-mini", input_tokens=0, output_tokens=0,
        cache_creation_tokens=1_000_000,
    )
    # 1.0 multiplier × $0.40/MTok = $0.40
    assert c.cache_creation_usd == pytest.approx(0.40)


def test_mixed_uncached_and_cached_input_sum_correctly():
    """A typical Anthropic call: 500 uncached + 2000 cache_read tokens.
    Total = 500 × $15/MTok + 2000 × $1.50/MTok (= 0.10 × $15)."""
    c = cost_for_call(
        "claude-opus-4-7",
        input_tokens=500, output_tokens=0,
        cache_read_tokens=2000,
    )
    assert c.input_usd == pytest.approx(500 / 1_000_000 * 15.00)
    assert c.cache_read_usd == pytest.approx(2000 / 1_000_000 * 1.50)
    assert c.total_usd == pytest.approx(c.input_usd + c.cache_read_usd)


def test_unknown_model_zero_cost_even_with_cache_tokens():
    """Unknown model → all USD fields zero, cache fields included."""
    c = cost_for_call(
        "future-model-x", input_tokens=100,
        output_tokens=100,
        cache_creation_tokens=100,
        cache_read_tokens=100,
    )
    assert not c.pricing_known
    assert c.total_usd == 0.0
    assert c.cache_creation_usd == 0.0
    assert c.cache_read_usd == 0.0
    # Token counts still preserved for telemetry.
    assert c.cache_creation_tokens == 100
    assert c.cache_read_tokens == 100


def test_negative_cache_tokens_clamped():
    c = cost_for_call(
        "claude-opus-4-7", input_tokens=0, output_tokens=0,
        cache_creation_tokens=-50, cache_read_tokens=-50,
    )
    assert c.cache_creation_tokens == 0
    assert c.cache_read_tokens == 0
    assert c.total_usd == 0.0


def test_aggregate_includes_cache_token_totals():
    from src.cost import aggregate_costs
    a = aggregate_costs([
        cost_for_call("claude-opus-4-7", 100, 50, cache_creation_tokens=1000),
        cost_for_call("claude-opus-4-7", 100, 50, cache_read_tokens=2000),
        cost_for_call("gpt-4.1-mini",     200, 100, cache_read_tokens=500),
    ])
    assert a["total_cache_creation_tokens"] == 1000
    assert a["total_cache_read_tokens"] == 2500
    opus = a["by_model"]["claude-opus-4-7"]
    assert opus["cache_creation_tokens"] == 1000
    assert opus["cache_read_tokens"] == 2000
    mini = a["by_model"]["gpt-4.1-mini"]
    assert mini["cache_read_tokens"] == 500


def test_call_cost_to_dict_includes_purpose_and_duration():
    c = cost_for_call("claude-opus-4-7", 10, 20, purpose="extractor:user", duration_ms=420.7)
    d = c.to_dict()
    assert d["purpose"] == "extractor:user"
    assert d["duration_ms"] == 420.7


def test_aggregate_costs_includes_per_purpose_and_calls_list():
    calls = [
        cost_for_call("claude-opus-4-7", 100, 200, purpose="extractor:assistant", duration_ms=350.0),
        cost_for_call("claude-sonnet-4-6", 50, 100, purpose="router", duration_ms=180.0),
        cost_for_call("claude-sonnet-4-6", 50, 100, purpose="router", duration_ms=200.0),
    ]
    from src.cost import aggregate_costs
    agg = aggregate_costs(calls)
    assert agg["total_calls"] == 3
    assert "by_purpose" in agg
    assert agg["by_purpose"]["router"]["calls"] == 2
    assert agg["by_purpose"]["extractor:assistant"]["calls"] == 1
    assert agg["by_purpose"]["router"]["duration_ms"] == 380.0
    assert "calls" in agg
    assert len(agg["calls"]) == 3
    assert agg["calls"][0]["purpose"] == "extractor:assistant"
