"""Calibration test for the v0.5 LLM router.

Runs the router against the worked examples from the system prompt and
checks that the LLM picks the expected method on at least 14/16 cases.

Gated behind ``RUN_API_TESTS=1`` because every case is a real API call.
A drift below 14/16 means the prompt or worked examples need attention.
"""

from __future__ import annotations

import os

import pytest

from src.llm_client import LLMClient
from src.llm_router import route_claim


# Each case: (label, claim, expected_method)
CALIBRATION_CASES: list[tuple[str, dict, str]] = [
    (
        "letter count in literal word",
        {
            "pattern": "quantitative", "predicate": "has_count",
            "slots": {"subject": "strawberry", "property": "letter_r", "value": 3},
            "polarity": 1, "source_text": "3 r's in strawberry",
        },
        "python",
    ),
    (
        "string reversal",
        {
            "pattern": "relational", "predicate": "reverse_of",
            "slots": {"subject": "nairatilage", "object": "egalitarian"},
            "polarity": 1, "source_text": "egalitarian backwards is nairatilage",
        },
        "python",
    ),
    (
        "arithmetic on stated numbers",
        {
            "pattern": "quantitative", "predicate": "product_equals",
            "slots": {"subject": "23 times 47", "property": "product", "value": 1081},
            "polarity": 1, "source_text": "23 * 47 = 1081",
        },
        "python",
    ),
    (
        "duration between dates in claim",
        {
            "pattern": "quantitative", "predicate": "term_duration",
            "slots": {
                "subject": "Trump first term", "property": "years", "value": 4,
                "valid_from": "2017", "valid_until": "2021",
            },
            "polarity": 1,
            "source_text": "Trump's first term lasted 4 years (2017-2021)",
        },
        "python",
    ),
    (
        # Phase-2 dogfood (turn 12, Marie Curie). Lifespan claim with
        # the dates as embedded slots — should route python, not retrieval.
        # Calibration commit added a worked example for this; this case
        # locks the routing in.
        "lifespan from embedded birth/death years",
        {
            "pattern": "quantitative", "predicate": "lifespan_years",
            "slots": {
                "subject": "Marie Curie", "property": "years_lived", "value": 67,
                "birth_year": 1867, "death_year": 1934,
            },
            "polarity": 1,
            "source_text": "Marie Curie was born in 1867 and died in 1934, so she lived 67 years",
        },
        "python",
    ),
    (
        "day of week from a literal date",
        {
            "pattern": "quantitative", "predicate": "day_of_week",
            "slots": {"subject": "2025-01-20", "property": "weekday", "value": "Monday"},
            "polarity": 1, "source_text": "January 20, 2025 was a Monday",
        },
        "python",
    ),
    (
        "perfect-cube test on a literal integer",
        {
            "pattern": "quantitative", "predicate": "is_perfect_cube",
            "slots": {"subject": "1729", "property": "is_cube", "value": False},
            "polarity": 1, "source_text": "1729 is a perfect cube",
        },
        "python",
    ),
    (
        "primality test (no canonical constants)",
        {
            "pattern": "quantitative", "predicate": "is_prime",
            "slots": {"subject": "73", "property": "is_prime", "value": True},
            "polarity": 1, "source_text": "73 is prime",
        },
        "python",
    ),
    (
        "Gettysburg opening — looks like python but actually retrieval",
        {
            "pattern": "relational", "predicate": "opens_with",
            "slots": {"subject": "Gettysburg Address",
                      "object": "Four score and seven years ago"},
            "polarity": 1,
            "source_text": "The Gettysburg Address opens with 'Four score and seven years ago.'",
        },
        "retrieval",
    ),
    (
        "US state count needs canonical reference",
        {
            "pattern": "quantitative", "predicate": "us_states_starting_with_letter",
            "slots": {"subject": "US states",
                      "property": "starting_with_A", "value": 4},
            "polarity": 1, "source_text": "Of the 50 US states, 4 begin with A",
        },
        "python_with_canonical_constants",
    ),
    (
        "current world-state — president",
        {
            "pattern": "role_assignment", "predicate": "holds_role",
            "slots": {"agent": "Donald Trump", "role": "47th President",
                      "org": "United States"},
            "polarity": 1, "source_text": "Donald Trump is the 47th US president",
        },
        "retrieval",
    ),
    (
        "specific historical date",
        {
            "pattern": "event", "predicate": "won_prize",
            "slots": {"participants": ["Marie Curie"],
                      "event_type": "Nobel Prize", "occurred_at": "1903"},
            "polarity": 1, "source_text": "Marie Curie won the Nobel Prize in 1903",
        },
        "retrieval",
    ),
    (
        "external population datum",
        {
            "pattern": "quantitative", "predicate": "population_of",
            "slots": {"subject": "Tokyo", "property": "population", "value": 14000000},
            "polarity": 1, "source_text": "Tokyo's population is 14 million",
        },
        "retrieval",
    ),
    (
        "user preference (about user)",
        {
            "pattern": "preference", "predicate": "likes",
            "slots": {"agent": "user", "object": "peanut butter"},
            "polarity": 1, "source_text": "I like peanut butter",
        },
        "user_authoritative",
    ),
    (
        "aesthetic judgment (non-user agent)",
        {
            "pattern": "propositional_attitude", "predicate": "feels",
            "slots": {"agent": "a critic", "attitude": "feels",
                      "proposition": "the novel is elegant"},
            "polarity": 1, "source_text": "The critic feels the novel is elegant",
        },
        "unverifiable",
    ),
    (
        "future-event prediction",
        {
            "pattern": "event", "predicate": "will_happen",
            "slots": {"event_type": "Fed rate cut", "occurred_at": "2026-05",
                      "participants": ["Fed"]},
            "polarity": 1, "source_text": "The Fed will cut rates next month",
        },
        "unverifiable",
    ),
    # Three time/timezone cases added when the router was misrouting
    # these to retrieval ("external real-time data") despite Python's
    # stdlib having a system clock + IANA tzdata via zoneinfo.
    (
        "current local time in a city",
        {
            "pattern": "quantitative", "predicate": "current_time",
            "slots": {"subject": "Cairo", "property": "time", "value": "9:56 am"},
            "polarity": 1, "source_text": "It's currently 9:56 am in Cairo",
        },
        "python",
    ),
    (
        "current local time in another city",
        {
            "pattern": "quantitative", "predicate": "current_time",
            "slots": {"subject": "New York", "property": "time", "value": "2:56 am"},
            "polarity": 1, "source_text": "It's 2:56 am in New York right now",
        },
        "python",
    ),
    (
        "time difference between two cities",
        {
            "pattern": "quantitative", "predicate": "time_difference",
            "slots": {"subject": "New York vs Cairo",
                      "property": "time_difference_hours", "value": 7},
            "polarity": 1,
            "source_text": "New York is typically 7 hours behind Cairo",
        },
        "python",
    ),
]

# A few of the cases above are deliberate boundary calls — at least
# 17 of 19 must match. The two acceptable misses are usually the
# Gettysburg edge and the canonical-constants vs. python boundary.
MIN_CORRECT = 17


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="real API calibration test gated behind RUN_API_TESTS=1",
)
def test_router_calibration_against_worked_examples():
    llm = LLMClient()
    correct = 0
    misses: list[str] = []
    for label, claim, expected in CALIBRATION_CASES:
        decision = route_claim(claim, llm)
        if decision.method == expected:
            correct += 1
        else:
            misses.append(
                f"  - {label}: expected {expected}, got "
                f"{decision.method} (conf={decision.confidence:.2f}) "
                f"— {decision.reason}"
            )
    msg = (
        f"{correct}/{len(CALIBRATION_CASES)} cases matched expected method."
    )
    if misses:
        msg += "\nMisses:\n" + "\n".join(misses)
    assert correct >= MIN_CORRECT, msg
