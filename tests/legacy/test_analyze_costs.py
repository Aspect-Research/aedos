"""Tests for scripts/analyze_costs.py — the cost analysis script.

The script is read-only; we drive it by seeding a FactStore with
turn_cost events, running main(), and asserting on stdout.
"""

from __future__ import annotations

import json

import pytest

from src.legacy.fact_store import FactStore
from scripts.analyze_costs import main


def _seed(db_path):
    store = FactStore(db_path)
    # Two turns, one expensive, one cheap.
    user1 = store.insert_turn("user", "expensive prompt that costs a lot")
    asst1 = store.insert_turn("assistant", "long response")
    store.insert_pipeline_event(asst1, "turn_cost", {
        "total_calls": 5,
        "total_input_tokens": 5000,
        "total_output_tokens": 2000,
        "total_usd": 0.25,
        "by_model": {
            "claude-opus-4-7": {"calls": 4, "input_tokens": 4000,
                                "output_tokens": 1500, "total_usd": 0.20},
            "claude-sonnet-4-6": {"calls": 1, "input_tokens": 1000,
                                  "output_tokens": 500, "total_usd": 0.05},
        },
        "any_unknown_pricing": False,
    })

    user2 = store.insert_turn("user", "cheap prompt")
    asst2 = store.insert_turn("assistant", "short response")
    store.insert_pipeline_event(asst2, "turn_cost", {
        "total_calls": 2,
        "total_input_tokens": 100,
        "total_output_tokens": 50,
        "total_usd": 0.005,
        "by_model": {
            "claude-haiku-4-5": {"calls": 2, "input_tokens": 100,
                                 "output_tokens": 50, "total_usd": 0.005},
        },
        "any_unknown_pricing": False,
    })
    store.close()


def test_analyze_costs_summarizes_correctly(tmp_path, capsys):
    db = tmp_path / "c.db"
    _seed(db)
    rc = main(["analyze_costs", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    # Total cost is 0.25 + 0.005 = 0.255
    assert "$0.2550" in out
    # Per model
    assert "claude-opus-4-7" in out
    assert "claude-sonnet-4-6" in out
    assert "claude-haiku-4-5" in out
    # Avg per turn = 0.255 / 2 = 0.1275
    assert "$0.1275" in out
    # Most-expensive turn
    assert "expensive prompt" in out


def test_analyze_costs_handles_missing_db(tmp_path, capsys):
    rc = main(["analyze_costs", str(tmp_path / "nonexistent.db")])
    assert rc == 2


def test_analyze_costs_handles_empty_db(tmp_path, capsys):
    db = tmp_path / "empty.db"
    FactStore(db).close()  # creates schema but no events
    rc = main(["analyze_costs", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no turn_cost events" in out


def test_analyze_costs_top_flag_limits_output(tmp_path, capsys):
    """--top N caps how many expensive turns are shown."""
    db = tmp_path / "many.db"
    store = FactStore(db)
    for i in range(10):
        store.insert_turn("user", f"prompt {i}")
        asst = store.insert_turn("assistant", f"response {i}")
        store.insert_pipeline_event(asst, "turn_cost", {
            "total_calls": 1,
            "total_input_tokens": 100,
            "total_output_tokens": 50,
            "total_usd": float(i) * 0.01,
            "by_model": {
                "claude-opus-4-7": {"calls": 1, "input_tokens": 100,
                                    "output_tokens": 50, "total_usd": float(i) * 0.01},
            },
            "any_unknown_pricing": False,
        })
    store.close()

    rc = main(["analyze_costs", str(db), "--top", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    # Top 3 line shown; lower-cost turns not in the top section
    # (they ARE in the totals). Count "turn N: $" occurrences.
    top_lines = [line for line in out.splitlines()
                 if "turn " in line and "$" in line and " — " in line]
    assert len(top_lines) == 3
