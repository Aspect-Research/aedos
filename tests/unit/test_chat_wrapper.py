"""Tests for ChatWrapper — intervention selection logic and response building."""

from __future__ import annotations

import pytest

from aedos.deployment.chat_wrapper import (
    ChatResponse,
    ChatWrapper,
    InterventionType,
    build_response,
    select_intervention,
)
from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer4_sources.walker import BudgetConsumption, WalkResult
from aedos.layer5_result.aggregator import Aggregator, VerificationResult
from aedos.layer5_result.trace import JustificationTrace, TraceNode
from aedos.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_vr(verified: int = 0, contradicted: int = 0, abstained: int = 0) -> VerificationResult:
    total = verified + contradicted + abstained
    verdicts = {}
    traces = {}
    idx = 0
    for _ in range(verified):
        cid = f"c{idx}"
        verdicts[cid] = "verified"
        traces[cid] = JustificationTrace(root=TraceNode("claim"))
        idx += 1
    for _ in range(contradicted):
        cid = f"c{idx}"
        verdicts[cid] = "contradicted"
        traces[cid] = JustificationTrace(root=TraceNode("claim"))
        idx += 1
    for _ in range(abstained):
        cid = f"c{idx}"
        verdicts[cid] = "no_grounding_found"
        traces[cid] = JustificationTrace(root=TraceNode("claim"))
        idx += 1
    return VerificationResult(
        claims_extracted=[],
        per_claim_verdicts=verdicts,
        per_claim_traces=traces,
        aggregate_metadata={
            "claim_count": total,
            "verified": verified,
            "contradicted": contradicted,
            "abstained": abstained,
        },
        audit_log_entries=[],
        text_input={},
    )


class MockTransport:
    def chat(self, *a, **kw):
        return "Obama was the 44th President of the United States."

    def extract_with_tool(self, *a, purpose=None, **kw):
        if purpose == "substrate:predicate_distribution":
            return {"verdict": "neither", "reason": "test"}
        if purpose == "substrate:subsumption":
            return {"verdict": "unrelated", "reason": "test"}
        return {
            "claims": [],
            "object_type": "entity",
            "user_subject_required": 0,
            "distinct_slots": None,
            "routing_hint": "user_authoritative",
            "kb_namespace": None,
            "kb_property": None,
            "slot_to_qualifier": None,
            "reason": "test",
        }


# ---------------------------------------------------------------------------
# InterventionType enum
# ---------------------------------------------------------------------------

class TestInterventionType:
    def test_values(self):
        assert InterventionType.PASS_THROUGH == "pass_through"
        assert InterventionType.ABSTAIN == "abstain"
        assert InterventionType.CORRECT == "correct"
        assert InterventionType.DECLINE == "decline"


# ---------------------------------------------------------------------------
# select_intervention — deterministic logic
# ---------------------------------------------------------------------------

class TestSelectIntervention:
    def test_all_verified_pass_through(self):
        vr = _make_vr(verified=3)
        assert select_intervention(vr) == InterventionType.PASS_THROUGH

    def test_zero_claims_pass_through(self):
        vr = _make_vr()
        assert select_intervention(vr) == InterventionType.PASS_THROUGH

    def test_abstained_only_abstain(self):
        vr = _make_vr(verified=2, abstained=1)
        assert select_intervention(vr) == InterventionType.ABSTAIN

    def test_all_abstained_under_50pct_abstain(self):
        vr = _make_vr(verified=3, abstained=2)
        # 2 abstained out of 5 = 40% → abstain
        assert select_intervention(vr) == InterventionType.ABSTAIN

    def test_any_contradicted_correct(self):
        vr = _make_vr(verified=2, contradicted=1)
        assert select_intervention(vr) == InterventionType.CORRECT

    def test_contradicted_below_50pct_correct(self):
        vr = _make_vr(verified=3, contradicted=1)
        assert select_intervention(vr) == InterventionType.CORRECT

    def test_contradicted_above_50pct_decline(self):
        vr = _make_vr(verified=1, contradicted=2)
        # 2 contradicted out of 3 = 67% → decline
        assert select_intervention(vr) == InterventionType.DECLINE

    def test_abstained_above_50pct_decline(self):
        vr = _make_vr(verified=1, abstained=3)
        # 3 abstained out of 4 = 75% → decline
        assert select_intervention(vr) == InterventionType.DECLINE

    def test_contradicted_plus_abstained_above_50pct_decline(self):
        vr = _make_vr(verified=1, contradicted=1, abstained=1)
        # 2 out of 3 = 67% → decline
        assert select_intervention(vr) == InterventionType.DECLINE

    def test_exactly_50pct_contradicted_abstained_no_decline(self):
        vr = _make_vr(verified=2, contradicted=2)
        # 2 out of 4 = 50%, NOT > 50% → correct
        assert select_intervention(vr) == InterventionType.CORRECT

    def test_contradicted_takes_priority_over_abstained(self):
        vr = _make_vr(verified=3, contradicted=1, abstained=1)
        # Only 2/5 = 40%, not decline; has contradicted → correct (not abstain)
        assert select_intervention(vr) == InterventionType.CORRECT


# ---------------------------------------------------------------------------
# build_response
# ---------------------------------------------------------------------------

class TestBuildResponse:
    def _vr(self):
        return _make_vr(verified=1)

    def test_pass_through_returns_draft_unchanged(self):
        draft = "Hello world."
        result = build_response(draft, InterventionType.PASS_THROUGH, self._vr())
        assert result == draft

    def test_abstain_appends_note(self):
        draft = "Some claim."
        result = build_response(draft, InterventionType.ABSTAIN, self._vr())
        assert result.startswith(draft)
        assert "could not be verified" in result.lower() or "note" in result.lower()

    def test_correct_appends_note(self):
        draft = "Obama was president."
        result = build_response(draft, InterventionType.CORRECT, self._vr())
        assert result.startswith(draft)
        assert "correct" in result.lower() or "note" in result.lower()

    def test_decline_returns_refusal(self):
        draft = "Some unverifiable claim."
        result = build_response(draft, InterventionType.DECLINE, self._vr())
        assert draft not in result or "unable" in result.lower() or "cannot" in result.lower()

    def test_decline_does_not_include_draft(self):
        draft = "This specific text should not appear."
        result = build_response(draft, InterventionType.DECLINE, self._vr())
        assert "This specific text should not appear." not in result


# ---------------------------------------------------------------------------
# ChatWrapper integration
# ---------------------------------------------------------------------------

class TestChatWrapperIntegration:
    def _make_wrapper(self) -> ChatWrapper:
        from aedos.database import open_memory_db
        from aedos.layer3_substrate import Substrate
        from aedos.layer3_substrate.predicate_distribution import PredicateDistributionOracle
        from aedos.layer3_substrate.predicate_translation import PredicateTranslation
        from aedos.layer3_substrate.resolver import EntityResolver
        from aedos.layer3_substrate.subsumption import SubsumptionOracle
        from aedos.layer4_sources.kb_protocol import ResolutionCandidate, SubsumptionResult
        from aedos.layer4_sources.kb_verifier import KBVerifier
        from aedos.layer4_sources.python_verifier import PythonVerifier
        from aedos.layer4_sources.tier_u import TierU
        from aedos.layer4_sources.walker import Walker

        db = open_memory_db()
        client = LLMClient(_transport=MockTransport())
        class StubKB:
            def resolve_entity(self, r, lc): return [ResolutionCandidate("Q76", score=0.9)]
            def lookup_statements(self, e, p): return []
            def subsumption(self, a, b, rt): return SubsumptionResult(verdict="unrelated")

        kb = StubKB()
        pt = PredicateTranslation(db=db, llm_client=client)
        resolver = EntityResolver(kb_protocol=kb, db=db)
        sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
        pd = PredicateDistributionOracle(db=db, llm_client=client)
        substrate = Substrate(resolver=resolver, predicate_translation=pt, subsumption=sub, predicate_distribution=pd)
        tier_u = TierU(db=db, predicate_translation=pt)
        kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
        py_verifier = PythonVerifier()
        walker = Walker(tier_u=tier_u, kb_verifier=kb_verifier, python_verifier=py_verifier, substrate=substrate)
        aggregator = Aggregator()
        return ChatWrapper(extractor=None, walker=walker, aggregator=aggregator, llm_client=client)

    def test_respond_returns_chat_response(self):
        wrapper = self._make_wrapper()
        response = wrapper.respond("Tell me about Obama.")
        assert isinstance(response, ChatResponse)
        assert response.final_message
        assert response.intervention_type in [t.value for t in InterventionType]

    def test_no_claims_extracted_gives_pass_through(self):
        # extractor=None means no claims extracted → pass_through
        wrapper = self._make_wrapper()
        response = wrapper.respond("Tell me something.")
        assert response.intervention_type == InterventionType.PASS_THROUGH.value

    def test_verification_id_stored(self):
        wrapper = self._make_wrapper()
        response = wrapper.respond("Tell me something.")
        assert response.verification_id
        vr = wrapper.get_verification(response.verification_id)
        assert vr is not None

    def test_get_verification_unknown_id_returns_none(self):
        wrapper = self._make_wrapper()
        assert wrapper.get_verification("nonexistent-id") is None

    def test_draft_message_populated(self):
        wrapper = self._make_wrapper()
        response = wrapper.respond("Tell me about Obama.")
        assert response.draft_message
