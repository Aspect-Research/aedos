"""Layer 5 corrector tests (v0.14 Phase 8b).

Mocked-LLM tests covering:
  - filter behavior (pass_through + noop short-circuit)
  - ledger construction (one line per actionable intervention)
  - vocabulary translation (user_asserted + REPLACE → 'contradicted'
    label for the LLM)
  - verified_value rendering for each REPLACE source
  - user_message inclusion when provided
  - draft preservation when no actionable interventions
"""

from __future__ import annotations

from typing import Optional
import pytest

from src.layer4_lookup.types import LookupOutcome, WalkerDecision
from src.layer5_decision.corrector import (
    CORRECTOR_SYSTEM,
    Corrector,
    _format_user_message,
    _ledger_line,
    _ledger_verdict_label,
)
from src.layer5_decision.types import (
    DecisionConfidence,
    Intervention,
    InterventionType,
)


class _MockLLM:
    """Records the corrector call. Returns a fixed rewrite."""

    def __init__(self, reply: str = "REWRITTEN"):
        self.reply = reply
        self.last_system: Optional[str] = None
        self.last_user: Optional[str] = None
        self.last_purpose: Optional[str] = None
        self.call_count = 0

    def rewrite(self, system: str, user_message: str, *,
                purpose: Optional[str] = None, **kwargs) -> str:
        self.last_system = system
        self.last_user = user_message
        self.last_purpose = purpose
        self.call_count += 1
        return self.reply


def _conf(value: float = 0.9) -> DecisionConfidence:
    return DecisionConfidence(
        path_prior=1.0, chain_reliability=1.0, evidence_strength=1.0,
        value=value, explanation="test",
    )


def _iv(
    intervention_type: InterventionType,
    *,
    claim: Optional[dict] = None,
    verification_status: str = "verified",
    reason: str = "test reason",
    verified_value=None,
    flag_operator: bool = False,
) -> Intervention:
    return Intervention(
        intervention_type=intervention_type,
        claim=claim or {
            "pattern": "preference", "predicate": "likes",
            "polarity": 1, "slots": {"agent": "user", "object": "tea"},
            "source_text": "the user likes tea",
        },
        verification_status=verification_status,
        decision_confidence=_conf(),
        reason=reason,
        verified_value=verified_value,
        flag_operator=flag_operator,
    )


# ============================================================================
# Short-circuit (no actionable interventions)
# ============================================================================


def test_apply_no_interventions_returns_draft_unchanged():
    llm = _MockLLM()
    c = Corrector(llm)
    result = c.apply("hello world", [], user_message="anything")
    assert result == "hello world"
    assert llm.call_count == 0


def test_apply_only_pass_through_returns_draft_unchanged():
    llm = _MockLLM()
    c = Corrector(llm)
    result = c.apply(
        "hello world",
        [_iv(InterventionType.PASS_THROUGH)],
    )
    assert result == "hello world"
    assert llm.call_count == 0


def test_apply_only_noop_returns_draft_unchanged():
    llm = _MockLLM()
    c = Corrector(llm)
    result = c.apply(
        "hello world",
        [
            _iv(InterventionType.NOOP, verification_status="retrieval_failed"),
            _iv(InterventionType.NOOP, verification_status="routing_anomaly",
                flag_operator=True),
        ],
    )
    assert result == "hello world"
    assert llm.call_count == 0


def test_apply_mixed_pass_through_and_noop_returns_draft_unchanged():
    llm = _MockLLM()
    c = Corrector(llm)
    result = c.apply(
        "hello world",
        [
            _iv(InterventionType.PASS_THROUGH, verification_status="verified"),
            _iv(InterventionType.NOOP, verification_status="retrieval_failed"),
            _iv(InterventionType.PASS_THROUGH, verification_status="user_asserted"),
        ],
    )
    assert result == "hello world"
    assert llm.call_count == 0


# ============================================================================
# Actionable interventions trigger LLM call
# ============================================================================


def test_apply_with_hedge_calls_llm():
    llm = _MockLLM(reply="hedged version")
    c = Corrector(llm)
    result = c.apply(
        "draft",
        [_iv(InterventionType.HEDGE, verification_status="retrieval_inconclusive")],
    )
    assert result == "hedged version"
    assert llm.call_count == 1
    assert llm.last_system == CORRECTOR_SYSTEM
    assert llm.last_purpose == "corrector"


def test_apply_with_replace_calls_llm():
    llm = _MockLLM(reply="corrected")
    c = Corrector(llm)
    result = c.apply(
        "draft",
        [_iv(
            InterventionType.REPLACE,
            verification_status="contradicted",
            verified_value=42,
            reason="comparator computed 42",
        )],
    )
    assert result == "corrected"
    assert llm.call_count == 1
    assert "verified value: 42" in llm.last_user


def test_apply_with_soften_calls_llm():
    llm = _MockLLM(reply="softened")
    c = Corrector(llm)
    result = c.apply(
        "draft",
        [_iv(
            InterventionType.SOFTEN,
            verification_status="unverifiable_in_principle",
        )],
    )
    assert result == "softened"
    assert llm.call_count == 1


# ============================================================================
# Filter at LLM-prompt level: pass_through + noop dropped, others shown
# ============================================================================


def test_apply_filters_pass_through_and_noop_from_prompt():
    """The LLM only sees actionable interventions in the ledger."""
    llm = _MockLLM()
    c = Corrector(llm)
    c.apply(
        "draft",
        [
            _iv(InterventionType.PASS_THROUGH,
                verification_status="verified",
                claim={"source_text": "filter me out 1"}),
            _iv(InterventionType.HEDGE,
                verification_status="retrieval_inconclusive",
                claim={"source_text": "this should appear"},
                reason="judge said insufficient"),
            _iv(InterventionType.NOOP,
                verification_status="retrieval_failed",
                claim={"source_text": "filter me out 2"}),
        ],
    )
    user_msg = llm.last_user
    assert "filter me out 1" not in user_msg
    assert "filter me out 2" not in user_msg
    assert "this should appear" in user_msg


# ============================================================================
# Vocabulary translation: user_asserted + REPLACE → contradicted
# ============================================================================


def test_user_asserted_replace_renders_as_contradicted_in_ledger():
    iv = _iv(
        InterventionType.REPLACE,
        verification_status="user_asserted",
        verified_value={"source": "user_assertion", "predicate": "dislikes"},
    )
    line = _ledger_line(iv)
    assert "verdict: contradicted" in line
    assert "verdict: user_asserted" not in line


def test_user_asserted_pass_through_keeps_status_label():
    iv = _iv(
        InterventionType.PASS_THROUGH,
        verification_status="user_asserted",
    )
    label = _ledger_verdict_label(iv)
    assert label == "user_asserted"


def test_other_statuses_render_verbatim():
    for status in (
        "verified", "contradicted", "retrieval_inconclusive",
        "unverifiable_in_principle", "unverifiable_pending_implementation",
    ):
        iv = _iv(InterventionType.HEDGE, verification_status=status)
        assert _ledger_verdict_label(iv) == status


# ============================================================================
# Audit trail preservation: verification_status NOT mutated by translation
# ============================================================================


def test_translation_doesnt_mutate_intervention_status():
    """Vocabulary translation is at ledger-rendering time; the
    Intervention's verification_status field MUST stay user_asserted
    in the audit trail."""
    iv = _iv(
        InterventionType.REPLACE,
        verification_status="user_asserted",
        verified_value={"source": "user_assertion"},
    )
    _ledger_line(iv)  # trigger translation
    assert iv.verification_status == "user_asserted"


# ============================================================================
# Ledger format
# ============================================================================


def test_ledger_includes_source_text_when_present():
    iv = _iv(
        InterventionType.HEDGE,
        verification_status="retrieval_inconclusive",
        claim={"source_text": "the population of Tokyo is 14 million"},
    )
    line = _ledger_line(iv)
    assert "the population of Tokyo is 14 million" in line


def test_ledger_falls_back_to_pattern_predicate_when_no_source_text():
    iv = _iv(
        InterventionType.HEDGE,
        verification_status="retrieval_inconclusive",
        claim={
            "pattern": "spatial_temporal", "predicate": "located_in",
            "polarity": 1, "slots": {"entity": "Tokyo", "location": "Japan"},
        },
    )
    line = _ledger_line(iv)
    assert "[spatial_temporal]" in line
    assert "located_in" in line


def test_ledger_includes_verified_value_only_when_present():
    iv_with = _iv(
        InterventionType.REPLACE,
        verification_status="contradicted",
        verified_value=42,
    )
    iv_without = _iv(
        InterventionType.HEDGE,
        verification_status="retrieval_inconclusive",
        verified_value=None,
    )
    assert "verified value:" in _ledger_line(iv_with)
    assert "verified value:" not in _ledger_line(iv_without)


def test_ledger_includes_reason():
    iv = _iv(
        InterventionType.HEDGE,
        verification_status="retrieval_inconclusive",
        reason="judge said snippets describe X separately",
    )
    line = _ledger_line(iv)
    assert "reason: judge said snippets describe X separately" in line


# ============================================================================
# user_message
# ============================================================================


def test_user_message_appears_in_prompt_when_provided():
    llm = _MockLLM()
    c = Corrector(llm)
    c.apply(
        "draft",
        [_iv(InterventionType.HEDGE,
             verification_status="retrieval_inconclusive")],
        user_message="What's the population of Tokyo?",
    )
    assert "User's question:" in llm.last_user
    assert "What's the population of Tokyo?" in llm.last_user


def test_user_message_omitted_when_blank():
    llm = _MockLLM()
    c = Corrector(llm)
    c.apply(
        "draft",
        [_iv(InterventionType.HEDGE,
             verification_status="retrieval_inconclusive")],
    )
    assert "User's question:" not in llm.last_user


# ============================================================================
# Format helper structure
# ============================================================================


def test_format_user_message_structure():
    iv = _iv(
        InterventionType.REPLACE,
        verification_status="contradicted",
        verified_value=99,
        reason="comparator computed 99",
        claim={"source_text": "the answer is 42"},
    )
    msg = _format_user_message(
        "draft body", [iv], user_message="what is the answer?",
    )
    assert "User's question:" in msg
    assert "what is the answer?" in msg
    assert "Assistant's draft reply:" in msg
    assert "draft body" in msg
    assert "Per-claim verification ledger:" in msg
    assert "the answer is 42" in msg
    assert "verdict: contradicted" in msg
    assert "verified value: 99" in msg
    assert "reason: comparator computed 99" in msg
    assert "Rewrite the assistant reply" in msg
