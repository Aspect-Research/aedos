"""Tests for the v0.6 cache TTL stability classifier.

Same structure as the scoping classifier tests — mock the LLM,
exercise the parsing/wiring; real-API calibration is gated.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import pytest

from src.cache.stability_classifier import (
    STABILITY_CLASSES,
    STABILITY_TTL_SECONDS,
    StabilityDecision,
    classify_stability,
)


@dataclass
class _MockLLM:
    canned: dict = field(default_factory=dict)

    def extract_with_tool(self, system, user_message, tool, max_tokens=2048, **_kwargs):
        return self.canned


def _claim(**kwargs):
    base = {
        "pattern": "spatial_temporal",
        "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"},
        "polarity": 1,
        "source_text": "Tokyo is in Japan",
    }
    base.update(kwargs)
    return base


def test_returns_decade_stable_with_correct_ttl():
    llm = _MockLLM(canned={
        "stability_class": "decade_stable",
        "reason": "geographic", "confidence": 0.95,
    })
    d = classify_stability(_claim(), llm)
    assert d.stability_class == "decade_stable"
    assert d.ttl_seconds == STABILITY_TTL_SECONDS["decade_stable"]
    # v0.7.8 — tightened from 10y → 1y so a multi-year-old verdict
    # never serves a fresh fact-verification request.
    assert d.ttl_seconds == 365 * 24 * 3600


def test_returns_immutable_with_none_ttl():
    llm = _MockLLM(canned={
        "stability_class": "immutable",
        "reason": "math", "confidence": 0.99,
    })
    d = classify_stability(_claim(), llm)
    assert d.stability_class == "immutable"
    assert d.ttl_seconds is None  # never expires


def test_returns_volatile_with_zero_ttl():
    llm = _MockLLM(canned={
        "stability_class": "volatile",
        "reason": "stock price", "confidence": 0.99,
    })
    d = classify_stability(_claim(), llm)
    assert d.stability_class == "volatile"
    assert d.ttl_seconds == 0  # don't cache


# ---- historical-period shortcut (deterministic, no LLM call) ----


def test_historical_period_shortcut_forces_immutable():
    """A claim whose ``valid_until`` slot is a year strictly in the
    past gets immutable + ttl=None without an LLM call. The role
    Trump held 2017–2021 is permanent now that 2021 has passed; the
    cache stores it forever.

    Pins the deterministic shortcut so future regressions don't push
    these into years_stable (where they'd expire after a year)."""
    # MockLLM with a CANNED wrong answer — if shortcut fires, this
    # never gets read. Test asserts shortcut wins.
    llm = _MockLLM(canned={
        "stability_class": "years_stable",
        "reason": "wrong",
        "confidence": 0.5,
    })
    claim = _claim(
        pattern="role_assignment",
        predicate="served_as",
        slots={
            "agent": "Donald Trump", "role": "45th President",
            "org": "United States",
            "valid_from": "2017", "valid_until": "2021",
        },
    )
    d = classify_stability(claim, llm)
    assert d.stability_class == "immutable"
    assert d.ttl_seconds is None
    assert "closed historical period" in d.reason
    # Shortcut marker — useful for trace inspection.
    assert d.raw == {"shortcut": "historical_period"}


def test_historical_period_shortcut_skipped_for_open_period():
    """When ``valid_until`` is the current year or later, the period
    isn't closed and the LLM is consulted normally."""
    from datetime import datetime
    llm = _MockLLM(canned={
        "stability_class": "years_stable",
        "reason": "still in office",
        "confidence": 0.9,
    })
    future_year = datetime.utcnow().year + 1
    claim = _claim(
        pattern="role_assignment",
        predicate="serves_as",
        slots={
            "agent": "X", "role": "Y", "org": "Z",
            "valid_until": str(future_year),
        },
    )
    d = classify_stability(llm=llm, claim=claim)
    assert d.stability_class == "years_stable"


def test_historical_period_shortcut_handles_missing_valid_until():
    """No valid_until → fall through to the LLM."""
    llm = _MockLLM(canned={
        "stability_class": "decade_stable",
        "reason": "geographic",
        "confidence": 0.95,
    })
    d = classify_stability(_claim(), llm)
    assert d.stability_class == "decade_stable"


def test_historical_period_shortcut_ignores_unparseable_year():
    """``valid_until`` of 'not-a-year' → fall through to LLM (don't
    crash, don't mis-shortcut)."""
    llm = _MockLLM(canned={
        "stability_class": "decade_stable",
        "reason": "fallback",
        "confidence": 0.5,
    })
    claim = _claim(slots={**_claim()["slots"], "valid_until": "not-a-year"})
    d = classify_stability(claim, llm)
    assert d.stability_class == "decade_stable"


def test_invalid_stability_class_raises():
    llm = _MockLLM(canned={
        "stability_class": "made_up", "reason": "junk", "confidence": 0.5,
    })
    with pytest.raises(RuntimeError, match="invalid class"):
        classify_stability(_claim(), llm)


def test_decision_to_dict_shape():
    d = StabilityDecision(
        stability_class="years_stable", reason="r",
        ttl_seconds=STABILITY_TTL_SECONDS["years_stable"],
    )
    assert d.to_dict() == {
        "stability_class": "years_stable",
        "reason": "r",
        # v0.7.8 — tightened from 1y → 90 days.
        "ttl_seconds": 90 * 24 * 3600,
    }


def test_all_classes_have_ttl_mapping():
    for cls in STABILITY_CLASSES:
        assert cls in STABILITY_TTL_SECONDS, f"missing TTL for {cls}"


# ---- real-API calibration (gated) --------------------------------------


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="real API stability classifier calibration gated behind RUN_API_TESTS=1",
)
def test_stability_calibration_against_worked_examples():
    """Smoke-check that the stability classifier picks the expected
    bin on its own worked examples. Real API; one call per case."""
    from src.llm_client import LLMClient

    cases = [
        # (claim, expected_class)
        ({"pattern": "quantitative", "predicate": "has_count",
          "slots": {"subject": "strawberry", "property": "letter_r", "value": 3},
          "polarity": 1, "source_text": "3 r's in strawberry"},
         "immutable"),
        ({"pattern": "spatial_temporal", "predicate": "located_in",
          "slots": {"entity": "Tokyo", "location": "Japan"},
          "polarity": 1, "source_text": "Tokyo is in Japan"},
         "decade_stable"),
        ({"pattern": "quantitative", "predicate": "stock_price",
          "slots": {"subject": "Apple", "property": "closing_price",
                    "value": 175.50, "unit": "USD"},
          "polarity": 1, "source_text": "Apple closed at 175.50"},
         "volatile"),
        ({"pattern": "quantitative", "predicate": "birth_year",
          "slots": {"subject": "Marie Curie", "property": "birth_year",
                    "value": 1867},
          "polarity": 1, "source_text": "Marie Curie was born in 1867"},
         "immutable"),
    ]

    llm = LLMClient()
    correct = 0
    misses: list[str] = []
    for claim, expected in cases:
        d = classify_stability(claim, llm)
        if d.stability_class == expected:
            correct += 1
        else:
            misses.append(f"  claim={claim['source_text']!r} expected="
                          f"{expected} got={d.stability_class} reason={d.reason}")
    assert correct >= 3, (
        f"stability classifier calibration: only {correct}/{len(cases)} correct\n"
        + "\n".join(misses)
    )
