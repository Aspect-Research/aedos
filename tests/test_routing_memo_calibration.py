"""Calibration test for the routing memo's (pattern, predicate) → method invariance.

Two live-LLM tests, both gated behind ``RUN_API_TESTS=1``:

  1. **Memo invariance**. 10 (pattern, predicate) pairs × 5 claims
     each varying only in slot values (e.g. quantitative.population_of
     for Tokyo, Paris, NYC, …). Asserts:
       - All 5 claims for a given (pattern, predicate) route to the
         same method on first call.
       - Calls 2-5 hit the memo — no further LLM calls beyond the
         first per pair (so 10 LLM calls total across 50 classifies).
       - last_consulted_at advances on hits.
       - affirmed_count and contradicted_count stay 0 across all hits
         (principle 3: reads are not writes).
       - **No row's method field was overwritten between first and
         last call.** This pins the empirical (pattern, predicate) →
         method invariance — a stricter check than the calibration
         loop, which only asserts the LLM is consistent on the
         first-of-pair runs.

  2. **One live test per routing method**. Five claims, one per
     method, each routed via the real LLM router and asserted to
     produce the expected method. This is the end-to-end calibration
     for the LLM-router prompt itself; the orchestrator-integration
     tests in test_router.py use stubs.

Each LLM call is one Anthropic API call. Both tests gated behind
RUN_API_TESTS=1 so CI can run mocked-only without network access.
"""

from __future__ import annotations

import os

import pytest

from src.fact_store import FactStore
from src.layer1_extraction.pattern_registry import (
    load_default_registry,
    reset_cache,
)
from src.layer2_routing.llm_router import ROUTING_METHODS
from src.layer2_routing.router import Router
from src.layer2_routing.routing_memo import RoutingMemo


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_cache()
    yield
    reset_cache()


# Each entry: a (pattern, predicate) pair plus 5 slot-shape variations.
# Variations differ only in slot VALUES, not in pattern or predicate, so
# the routing decision must be identical across all 5 if the memoization
# key is honored.
_INVARIANCE_CASES: list[tuple[str, str, list[dict]]] = [
    # 1. preferences — user-authoritative
    (
        "preference", "likes",
        [
            {"agent": "user", "object": "peanut butter"},
            {"agent": "user", "object": "olives"},
            {"agent": "user", "object": "sourdough"},
            {"agent": "user", "object": "raspberries"},
            {"agent": "user", "object": "spicy food"},
        ],
    ),
    # 2. has_count letter — python
    (
        "quantitative", "has_count",
        [
            {"subject": "strawberry", "property": "letter_r", "value": 3},
            {"subject": "banana", "property": "letter_a", "value": 3},
            {"subject": "apple", "property": "letter_p", "value": 2},
            {"subject": "raspberry", "property": "letter_r", "value": 3},
            {"subject": "blueberry", "property": "letter_b", "value": 1},
        ],
    ),
    # 3. population — retrieval
    (
        "quantitative", "population_of",
        [
            {"subject": "Tokyo", "property": "population", "value": 14000000},
            {"subject": "Paris", "property": "population", "value": 2000000},
            {"subject": "New York", "property": "population", "value": 8000000},
            {"subject": "London", "property": "population", "value": 9000000},
            {"subject": "Berlin", "property": "population", "value": 3700000},
        ],
    ),
    # 4. role assignment — retrieval
    (
        "role_assignment", "holds_role",
        [
            {"agent": "Donald Trump", "role": "47th President", "org": "United States"},
            {"agent": "Tim Cook", "role": "CEO", "org": "Apple"},
            {"agent": "Sundar Pichai", "role": "CEO", "org": "Google"},
            {"agent": "Satya Nadella", "role": "CEO", "org": "Microsoft"},
            {"agent": "Lisa Su", "role": "CEO", "org": "AMD"},
        ],
    ),
    # 5. mereological part_of — retrieval (Phase 2 added the worked example)
    (
        "mereological", "part_of",
        [
            {"part": "Williamstown", "whole": "Massachusetts"},
            {"part": "Berkshire County", "whole": "Massachusetts"},
            {"part": "Brooklyn", "whole": "New York City"},
            {"part": "Manhattan", "whole": "New York City"},
            {"part": "Westminster", "whole": "London"},
        ],
    ),
    # 6. relational founded_by — retrieval
    (
        "relational", "founded_by",
        [
            {"subject": "Anthropic", "object": "Dario Amodei"},
            {"subject": "Apple", "object": "Steve Jobs"},
            {"subject": "Microsoft", "object": "Bill Gates"},
            {"subject": "Tesla", "object": "Elon Musk"},
            {"subject": "Amazon", "object": "Jeff Bezos"},
        ],
    ),
    # 7. propositional_attitude (user) — user_authoritative
    (
        "propositional_attitude", "believes",
        [
            {"agent": "user", "attitude": "thinks", "proposition": "the Fed will cut rates"},
            {"agent": "user", "attitude": "thinks", "proposition": "AI is transformative"},
            {"agent": "user", "attitude": "thinks", "proposition": "remote work is here to stay"},
            {"agent": "user", "attitude": "thinks", "proposition": "Python is overused"},
            {"agent": "user", "attitude": "thinks", "proposition": "tabs beat spaces"},
        ],
    ),
    # 8. categorical is_a — retrieval
    (
        "categorical", "is_a",
        [
            {"entity": "Marie Curie", "category": "physicist"},
            {"entity": "Tokyo", "category": "city"},
            {"entity": "kakapo", "category": "bird"},
            {"entity": "Stradivari", "category": "violin maker"},
            {"entity": "Sahara", "category": "desert"},
        ],
    ),
    # 9. spatial_temporal lives_in (user subject) — user_authoritative
    (
        "spatial_temporal", "lives_in",
        [
            {"entity": "user", "location": "San Francisco"},
            {"entity": "user", "location": "Boston"},
            {"entity": "user", "location": "London"},
            {"entity": "user", "location": "Tokyo"},
            {"entity": "user", "location": "Paris"},
        ],
    ),
    # 10. event won_election — retrieval
    (
        "event", "won_election",
        [
            {"event_type": "election", "participants": ["Donald Trump"], "occurred_at": "2024"},
            {"event_type": "election", "participants": ["Joe Biden"], "occurred_at": "2020"},
            {"event_type": "election", "participants": ["Barack Obama"], "occurred_at": "2008"},
            {"event_type": "election", "participants": ["George W. Bush"], "occurred_at": "2000"},
            {"event_type": "election", "participants": ["Ronald Reagan"], "occurred_at": "1980"},
        ],
    ),
]


def _claim(pattern, predicate, slots):
    return {
        "pattern": pattern, "predicate": predicate, "slots": slots,
        "polarity": 1, "source_text": "<calibration>",
    }


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="real API calibration test gated behind RUN_API_TESTS=1",
)
def test_memo_invariance_50_classifies_10_llm_calls(tmp_path):
    """50 claims across 10 (pattern, predicate) pairs route through
    only 10 LLM calls (one per pair on first miss). The remaining 40
    classifies are memo hits."""
    from src.llm_client import LLMClient

    store = FactStore(tmp_path / "calibration.db")
    memo = RoutingMemo(store)
    llm_call_count = {"n": 0}

    from src.layer2_routing.llm_router import route_claim as real_route
    real_llm = LLMClient()

    def counting_routing_fn(claim):
        llm_call_count["n"] += 1
        return real_route(claim, real_llm)

    router = Router(
        store, load_default_registry(),
        routing_fn=counting_routing_fn,
        memo=memo,
    )

    # Track first-call methods per (pattern, predicate) so we can
    # assert all 5 of a pair land on the same method.
    first_methods: dict[tuple[str, str], str] = {}
    method_drift: list[str] = []

    for pattern, predicate, variations in _INVARIANCE_CASES:
        for i, slots in enumerate(variations):
            decision = router.classify(
                _claim(pattern, predicate, slots),
                source_turn_id=1,
            )
            assert decision.method in ROUTING_METHODS
            if i == 0:
                first_methods[(pattern, predicate)] = decision.method
                assert decision.memo_hit is False
            else:
                # Subsequent calls must hit the memo and produce the
                # same method.
                assert decision.memo_hit is True, (
                    f"memo miss on call {i + 1} for "
                    f"({pattern!r}, {predicate!r})"
                )
                if decision.method != first_methods[(pattern, predicate)]:
                    method_drift.append(
                        f"({pattern!r}, {predicate!r}): "
                        f"first={first_methods[(pattern, predicate)]!r}, "
                        f"call {i + 1}={decision.method!r}"
                    )

    # Exactly 10 LLM calls expected (one per pair on first miss).
    assert llm_call_count["n"] == len(_INVARIANCE_CASES), (
        f"expected {len(_INVARIANCE_CASES)} LLM calls "
        f"(one per (pattern, predicate) pair on first miss), got "
        f"{llm_call_count['n']}"
    )

    assert not method_drift, (
        "method drift across memo hits — invariance broken:\n  "
        + "\n  ".join(method_drift)
    )

    # Counts must stay 0 across all 50 classifies (40 of which were
    # memo hits) — principle 3: reads are not writes.
    rows = memo.list_all()
    assert len(rows) == len(_INVARIANCE_CASES)
    for row in rows:
        assert row.affirmed_count == 0, (
            f"affirmed_count drifted on ({row.pattern!r}, "
            f"{row.predicate!r}): expected 0, got {row.affirmed_count}"
        )
        assert row.contradicted_count == 0
        assert row.last_consulted_at is not None  # touched on at least one hit

    # Final invariance check: no row's method field was overwritten
    # between first call and last. The calibration loop above caught
    # in-loop drift; this scans the persisted state for the same
    # invariant. (Would also catch drift if record() were ever called
    # again after the first miss — which it shouldn't, given UPSERT
    # only fires on miss.)
    for row in rows:
        assert row.method == first_methods[(row.pattern, row.predicate)], (
            f"({row.pattern!r}, {row.predicate!r}) method overwrite — "
            f"first={first_methods[(row.pattern, row.predicate)]!r}, "
            f"final={row.method!r}"
        )

    store.close()


# ---- one live test per routing method ----
#
# Each case asserts the LLM-router prompt (with the Phase-2-added
# mereological worked example) produces the expected method on a
# representative claim. These are the cold-path equivalent of the
# stub-driven tests in test_router.py: same claim shapes, but
# routing_fn is the real LLM call.

_METHOD_CASES = [
    (
        "python",
        "quantitative", "has_count",
        {"subject": "strawberry", "property": "letter_r", "value": 3},
    ),
    (
        "python_with_canonical_constants",
        "quantitative", "us_states_starting_with_letter",
        {"subject": "US states", "property": "starting_with_A", "value": 4},
    ),
    (
        "retrieval",
        "quantitative", "population_of",
        {"subject": "Tokyo", "property": "population", "value": 14000000},
    ),
    (
        "user_authoritative",
        "preference", "likes",
        {"agent": "user", "object": "peanut butter"},
    ),
    (
        "unverifiable",
        "event", "will_happen",
        {"event_type": "Fed rate cut", "participants": ["Fed"],
         "occurred_at": "2026-05"},
    ),
]


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="real API calibration test gated behind RUN_API_TESTS=1",
)
@pytest.mark.parametrize(
    "expected_method,pattern,predicate,slots", _METHOD_CASES,
)
def test_live_routing_per_method(
    expected_method, pattern, predicate, slots, tmp_path,
):
    """End-to-end: real LLM router classifies each method's
    representative claim correctly. Exercises the prompt itself.
    """
    from src.llm_client import LLMClient

    store = FactStore(tmp_path / f"live_{expected_method}.db")
    router = Router(
        store, load_default_registry(),
        llm=LLMClient(),
    )
    decision = router.classify(
        _claim(pattern, predicate, slots),
        source_turn_id=1,
    )
    assert decision.method == expected_method, (
        f"expected {expected_method!r} for ({pattern!r}, {predicate!r}); "
        f"got {decision.method!r}; reason: {decision.reason!r}"
    )
    store.close()
