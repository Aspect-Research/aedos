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


def test_glm_free_tier():
    c = cost_for_call("zai-org/GLM-5.1-FP8", input_tokens=10_000,
                      output_tokens=10_000)
    assert c.total_usd == 0.0
    assert c.pricing_known


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
    }
