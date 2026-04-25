"""Tests for src.corrector — intervention planning and rewrite batching."""

from __future__ import annotations

import pytest
pytestmark = pytest.mark.skip(
    reason="v0.3 migration: corrector recalibrated in Section 6; tests rewritten there"
)

from dataclasses import dataclass, field

import pytest

from src.corrector import (
    INTERVENTION_HEDGE,
    INTERVENTION_REPLACE,
    INTERVENTION_SOFTEN,
    Corrector,
    Intervention,
)
from src.router import Decision, RoutingOutcome


@dataclass
class FakeLLM:
    rewrite_responses: list[str] = field(default_factory=list)
    rewrite_calls: list[dict] = field(default_factory=list)

    def rewrite(self, system, user_message, max_tokens=2048):
        self.rewrite_calls.append({"system": system, "user_message": user_message})
        return self.rewrite_responses.pop(0)


def _decision(verification_status, *, confidence=0.5, claim=None, correction=None,
              outcome=RoutingOutcome.UNVERIFIED):
    claim = claim or {
        "subject": "x",
        "predicate": "likes",
        "object": "y",
        "object_type": "entity",
        "polarity": 1,
        "source_text": "x likes y",
    }
    return Decision(
        claim=claim,
        outcome=outcome,
        verification_status=verification_status,
        confidence=confidence,
        correction=correction,
    )


# ---------- intervention planning ----------


def test_verified_status_yields_no_intervention():
    c = Corrector(FakeLLM())
    out = c.plan_interventions([_decision("verified", confidence=0.95)])
    assert out == []


def test_user_asserted_status_yields_no_intervention():
    c = Corrector(FakeLLM())
    out = c.plan_interventions([_decision("user_asserted", confidence=0.95)])
    assert out == []


def test_routing_anomaly_yields_no_intervention():
    """Anomalies are content-irrelevant — pipeline logs them separately."""
    c = Corrector(FakeLLM())
    out = c.plan_interventions(
        [_decision("routing_anomaly", confidence=0.2, outcome=RoutingOutcome.ROUTING_ANOMALY)]
    )
    assert out == []


def test_contradicted_yields_replace_with_verified_value():
    c = Corrector(FakeLLM())
    decision = _decision(
        "contradicted",
        confidence=0.99,
        correction={
            "original_object": "wrong",
            "corrected_object": "right",
            "explanation": "verifier said so",
            "source_text": "wrong fact",
        },
        outcome=RoutingOutcome.CONTRADICTED,
    )
    out = c.plan_interventions([decision])
    assert len(out) == 1
    assert out[0].intervention_type == INTERVENTION_REPLACE
    assert out[0].verified_value == "right"


def test_pending_implementation_with_low_confidence_yields_hedge():
    c = Corrector(FakeLLM())
    out = c.plan_interventions([
        _decision("unverifiable_pending_implementation", confidence=0.4)
    ])
    assert len(out) == 1
    assert out[0].intervention_type == INTERVENTION_HEDGE


def test_pending_implementation_with_high_confidence_yields_no_intervention():
    """Threshold is < 0.5: anything at or above stays as-is (the model
    presumably knows what it's saying)."""
    c = Corrector(FakeLLM())
    out = c.plan_interventions([
        _decision("unverifiable_pending_implementation", confidence=0.7)
    ])
    assert out == []


def test_unverifiable_in_principle_yields_soften():
    c = Corrector(FakeLLM())
    out = c.plan_interventions([_decision("unverifiable_in_principle", confidence=0.3)])
    assert len(out) == 1
    assert out[0].intervention_type == INTERVENTION_SOFTEN


def test_mixed_decisions_yield_mixed_interventions():
    c = Corrector(FakeLLM())
    out = c.plan_interventions(
        [
            _decision("verified", confidence=0.95),
            _decision(
                "contradicted",
                confidence=0.99,
                correction={"corrected_object": "fixed", "explanation": "x"},
            ),
            _decision("unverifiable_pending_implementation", confidence=0.4),
            _decision("unverifiable_in_principle", confidence=0.3),
            _decision("routing_anomaly", confidence=0.2,
                      outcome=RoutingOutcome.ROUTING_ANOMALY),
        ]
    )
    types = sorted(i.intervention_type for i in out)
    assert types == sorted([INTERVENTION_REPLACE, INTERVENTION_HEDGE, INTERVENTION_SOFTEN])


# ---------- apply ----------


def test_apply_with_no_interventions_returns_draft_unchanged():
    llm = FakeLLM()
    c = Corrector(llm)
    out = c.apply("hello world", [])
    assert out == "hello world"
    assert llm.rewrite_calls == []  # no LLM call


def test_apply_with_interventions_calls_llm_once_for_batch():
    llm = FakeLLM(rewrite_responses=["rewritten text"])
    c = Corrector(llm)
    interventions = [
        Intervention(
            intervention_type=INTERVENTION_HEDGE,
            claim={"subject": "a", "predicate": "is_a", "object": "b", "polarity": 1},
            verification_status="unverifiable_pending_implementation",
            reason="not verified",
        ),
        Intervention(
            intervention_type=INTERVENTION_REPLACE,
            claim={"subject": "c", "predicate": "has_count", "object": "3", "polarity": 1},
            verification_status="contradicted",
            verified_value="0",
            reason="actual count is 0",
        ),
    ]
    out = c.apply("draft text with two claims", interventions)
    assert out == "rewritten text"
    assert len(llm.rewrite_calls) == 1
    user_msg = llm.rewrite_calls[0]["user_message"]
    # Both interventions appear in the prompt
    assert "[hedge]" in user_msg
    assert "[replace]" in user_msg
    assert "verified_value: '0'" in user_msg


def test_apply_user_message_includes_source_text():
    llm = FakeLLM(rewrite_responses=["x"])
    c = Corrector(llm)
    iv = Intervention(
        intervention_type=INTERVENTION_HEDGE,
        claim={
            "subject": "Donald Trump",
            "predicate": "holds_role",
            "object": "US President",
            "polarity": 1,
            "source_text": "Donald Trump is the US President",
        },
        verification_status="unverifiable_pending_implementation",
        reason="retrieval returned no results",
    )
    c.apply("Donald Trump is the US President.", [iv])
    msg = llm.rewrite_calls[0]["user_message"]
    assert "Donald Trump is the US President" in msg
    assert "retrieval returned no results" in msg


def test_corrector_system_prompt_lists_intervention_types():
    """Verify the system prompt actually documents the 4 intervention types."""
    from src.corrector import CORRECTOR_SYSTEM

    for kw in ("hedge", "replace", "soften", "remove"):
        assert kw in CORRECTOR_SYSTEM


# ---------- back-compat (v0.1 .correct() entry point) ----------


def test_legacy_correct_method_still_works():
    """v0.1 callers using .correct(text, corrections) keep working."""
    llm = FakeLLM(rewrite_responses=["rewritten"])
    c = Corrector(llm)
    out = c.correct(
        "There are 3 p's in strawberry.",
        [
            {
                "original_object": '{"item": "p", "count": 3}',
                "corrected_object": '{"item": "p", "count": 0}',
                "explanation": "actual count is 0",
                "source_text": "3 p's in strawberry",
            }
        ],
    )
    assert out == "rewritten"
    assert len(llm.rewrite_calls) == 1
