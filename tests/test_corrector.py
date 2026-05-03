"""Tests for src.corrector (v0.3 — calibrated against retrieval_failed)."""

from __future__ import annotations

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

    def rewrite(self, system, user_message, max_tokens=2048, **_kwargs):
        self.rewrite_calls.append({"system": system, "user_message": user_message})
        return self.rewrite_responses.pop(0)


def _decision(verification_status, *, confidence=0.5, claim=None, correction=None,
              outcome=RoutingOutcome.UNVERIFIED):
    claim = claim or {
        "pattern": "preference",
        "predicate": "likes",
        "slots": {"agent": "user", "object": "x"},
        "polarity": 1,
        "source_text": "x",
    }
    return Decision(
        claim=claim,
        outcome=outcome,
        verification_status=verification_status,
        confidence=confidence,
        correction=correction,
    )


# ---------- intervention planning ----------


def test_verified_no_intervention():
    c = Corrector(FakeLLM())
    out = c.plan_interventions([_decision("verified", confidence=0.95)])
    assert out == []


def test_user_asserted_no_intervention():
    c = Corrector(FakeLLM())
    out = c.plan_interventions([_decision("user_asserted", confidence=0.95)])
    assert out == []


def test_routing_anomaly_no_intervention():
    c = Corrector(FakeLLM())
    out = c.plan_interventions(
        [_decision("routing_anomaly", confidence=0.2,
                   outcome=RoutingOutcome.ROUTING_ANOMALY)]
    )
    assert out == []


def test_contradicted_yields_replace():
    c = Corrector(FakeLLM())
    d = _decision(
        "contradicted",
        confidence=0.99,
        correction={"corrected_object": "right", "explanation": "verifier said so"},
        outcome=RoutingOutcome.CONTRADICTED,
    )
    out = c.plan_interventions([d])
    assert len(out) == 1
    assert out[0].intervention_type == INTERVENTION_REPLACE
    assert out[0].verified_value == "right"


# ---------- v0.3 split: retrieval_inconclusive vs retrieval_failed ----


def test_retrieval_inconclusive_yields_hedge():
    """Verifier ran, judge said insufficient evidence — hedge the claim."""
    c = Corrector(FakeLLM())
    out = c.plan_interventions([_decision("retrieval_inconclusive", confidence=0.4)])
    assert len(out) == 1
    assert out[0].intervention_type == INTERVENTION_HEDGE


def test_retrieval_failed_does_NOT_hedge():
    """The v0.2 bug: retrieval failure (no signal) was hedging true claims.
    v0.3: do NOT add hedge — there's no positive evidence of uncertainty."""
    c = Corrector(FakeLLM())
    out = c.plan_interventions([_decision("retrieval_failed", confidence=0.4)])
    assert out == [], (
        "verifier failure must not trigger a hedge — adding 'I think' to "
        "a possibly-true claim is worse than leaving it"
    )


def test_retrieval_inconclusive_vs_failed_diff():
    """Two decisions identical except for status produce different intervention sets."""
    c = Corrector(FakeLLM())
    out_inc = c.plan_interventions([_decision("retrieval_inconclusive", confidence=0.4)])
    out_fail = c.plan_interventions([_decision("retrieval_failed", confidence=0.4)])
    assert len(out_inc) == 1
    assert len(out_fail) == 0


def test_unverifiable_pending_low_confidence_hedges():
    c = Corrector(FakeLLM())
    out = c.plan_interventions([
        _decision("unverifiable_pending_implementation", confidence=0.4)
    ])
    assert len(out) == 1
    assert out[0].intervention_type == INTERVENTION_HEDGE


def test_unverifiable_pending_high_confidence_no_intervention():
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
            _decision("retrieval_inconclusive", confidence=0.4),
            _decision("retrieval_failed", confidence=0.4),  # NOT hedged
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
    assert llm.rewrite_calls == []


def test_apply_with_interventions_calls_llm_once_for_batch():
    llm = FakeLLM(rewrite_responses=["rewritten text"])
    c = Corrector(llm)
    interventions = [
        Intervention(
            intervention_type=INTERVENTION_HEDGE,
            claim={"pattern": "categorical", "predicate": "is_a",
                   "slots": {"entity": "x", "category": "y"}, "polarity": 1,
                   "source_text": "src1"},
            verification_status="retrieval_inconclusive",
            reason="inconclusive",
        ),
        Intervention(
            intervention_type=INTERVENTION_REPLACE,
            claim={"pattern": "quantitative", "predicate": "has_count",
                   "slots": {"subject": "strawberry", "property": "p", "value": 3},
                   "polarity": 1, "source_text": "src2"},
            verification_status="contradicted",
            verified_value=0,
            reason="actual is 0",
        ),
    ]
    out = c.apply("draft text", interventions)
    assert out == "rewritten text"
    assert len(llm.rewrite_calls) == 1
    user_msg = llm.rewrite_calls[0]["user_message"]
    # v0.11 holistic format: the user message embeds the draft +
    # a per-claim ledger with verdicts. No more "[hedge]/[replace]"
    # action tags — the corrector decides what to do per claim.
    assert "draft text" in user_msg
    assert "verdict: retrieval_inconclusive" in user_msg
    assert "verdict: contradicted" in user_msg


def test_unknown_verification_status_returns_no_intervention():
    """plan_intervention returns None for an unknown status — be
    conservative, don't intervene if we don't know what the verifier
    meant. Locks in the catch-all branch."""
    from src.corrector import Corrector

    c = Corrector(FakeLLM(rewrite_responses=[]))
    decisions = [_decision(verification_status="some_new_status_we_dont_know")]
    interventions = c.plan_interventions(decisions)
    assert interventions == []


def test_corrector_system_prompt_frames_holistic_rewrite():
    """v0.11: the corrector reframed from 'apply per-claim edits' to
    'rewrite holistically given the verifier ledger'. The system
    prompt no longer enumerates intervention-type keywords (the model
    decides per-claim what to do); it does still call out internal
    consistency + the no-narration constraint."""
    from src.corrector import CORRECTOR_SYSTEM
    # Holistic framing — naming the inputs the corrector is given.
    assert "user's question" in CORRECTOR_SYSTEM
    assert "draft" in CORRECTOR_SYSTEM
    assert "verification ledger" in CORRECTOR_SYSTEM.lower()
    # Internal consistency + enumeration-cascade lesson preserved.
    assert "internal consistency" in CORRECTOR_SYSTEM.lower()
    assert "vowels" in CORRECTOR_SYSTEM
    # Conditional-claim guidance ("if X then Y" verifier mismatch).
    assert "conditional" in CORRECTOR_SYSTEM.lower()
    # No "MINIMAL CHANGES" framing — that's what produced the bugs.
    assert "MINIMAL CHANGES" not in CORRECTOR_SYSTEM
    # No narration / no apologies.
    assert "narrate" in CORRECTOR_SYSTEM.lower() or "apolog" in CORRECTOR_SYSTEM.lower()


def test_corrector_user_message_renders_verification_ledger():
    """v0.11: every contradicted claim shows up in the per-claim
    ledger with its verified value + reason, so the corrector's
    rewrite has the full picture (no separate 'Verified values'
    checklist any more — the ledger IS the checklist)."""
    from src.corrector import (
        Corrector, INTERVENTION_REPLACE, INTERVENTION_HEDGE, Intervention,
    )

    @dataclass
    class _LLM:
        rewrite_calls: list = field(default_factory=list)
        corrector_model: str = "mock"

        def rewrite(self, system, user_message, max_tokens=2048,
                    temperature=None, **_kwargs):
            self.rewrite_calls.append({"user_message": user_message})
            return "ok"

    llm = _LLM()
    c = Corrector(llm)
    interventions = [
        Intervention(
            intervention_type=INTERVENTION_REPLACE,
            claim={"pattern": "quantitative", "predicate": "has_count",
                   "slots": {"subject": "prompt",
                             "property": "words_with_more_than_two_vowels",
                             "value": 2},
                   "polarity": 1,
                   "source_text": "Count: 2 words"},
            verification_status="contradicted",
            verified_value=0,
            reason="actual is 0",
        ),
        Intervention(
            intervention_type=INTERVENTION_HEDGE,
            claim={"pattern": "categorical", "predicate": "is_a",
                   "slots": {"entity": "x", "category": "y"},
                   "polarity": 1, "source_text": "src"},
            verification_status="retrieval_inconclusive",
            reason="hedge",
        ),
    ]
    c.apply("draft", interventions, user_message="how many words?")

    msg = llm.rewrite_calls[0]["user_message"]
    # User question relayed.
    assert "how many words?" in msg
    # Per-claim ledger header.
    assert "verification ledger" in msg.lower()
    # Contradicted entry shows verified value + reason.
    assert "verdict: contradicted" in msg
    assert "verified value: 0" in msg
    assert "actual is 0" in msg
    # Inconclusive entry shows up (every claim, not just contradictions).
    assert "verdict: retrieval_inconclusive" in msg


def test_corrector_user_message_includes_user_question():
    """The user's question is the input the corrector needs to derive
    a coherent reply when the draft's reasoning chain depended on a
    contradicted premise. Make sure it's threaded through."""
    from src.corrector import (
        Corrector, INTERVENTION_REPLACE, Intervention,
    )

    @dataclass
    class _LLM:
        rewrite_calls: list = field(default_factory=list)
        corrector_model: str = "mock"

        def rewrite(self, system, user_message, max_tokens=2048,
                    temperature=None, **_kwargs):
            self.rewrite_calls.append({"user_message": user_message})
            return "ok"

    llm = _LLM()
    c = Corrector(llm)
    iv = Intervention(
        intervention_type=INTERVENTION_REPLACE,
        claim={"pattern": "quantitative", "predicate": "current_time",
               "slots": {"subject": "Cairo", "property": "time", "value": "9:56 pm"},
               "polarity": 1, "source_text": "9:56 pm in Cairo"},
        verification_status="contradicted",
        verified_value="11:13 am",
        reason="zoneinfo says 11:13 am",
    )
    c.apply("draft about cairo time", [iv],
            user_message="What time is it in Cairo right now?")
    msg = llm.rewrite_calls[0]["user_message"]
    assert "User's question:" in msg
    assert "What time is it in Cairo right now?" in msg


