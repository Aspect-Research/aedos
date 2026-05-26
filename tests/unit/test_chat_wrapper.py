"""Tests for ChatWrapper — intervention selection logic and response building."""

from __future__ import annotations

import pytest

from aedos.deployment.chat_wrapper import (
    ChatResponse,
    ChatWrapper,
    ClaimAction,
    ClaimActionType,
    InterventionPlan,
    InterventionType,
    build_response,
    select_interventions,
)
from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer4_sources.walker import BudgetConsumption, WalkResult
from aedos.layer5_result.aggregator import (
    Aggregator,
    ClaimVerdict,
    VerificationResult,
)
from aedos.layer5_result.trace import JustificationTrace, TraceNode
from aedos.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _claim(claim_id: str, subject: str = "Obama", predicate: str = "holds_role",
           obj: str = "President", polarity: int = 1) -> Claim:
    return Claim(
        claim_id=claim_id,
        subject=subject,
        predicate=predicate,
        object=obj,
        polarity=polarity,
        source_text="test",
        asserting_party="user_test",
        triage_decision=TriageDecision.VERIFY,
    )


def _make_claim_verdicts(verified: int = 0, contradicted: int = 0,
                          abstained: int = 0) -> list[ClaimVerdict]:
    """Build a synthetic list of ClaimVerdicts for select_interventions
    tests. Verdicts are the base shapes (verified / contradicted /
    no_grounding_found); the dual-designation collapse is tested
    separately."""
    out: list[ClaimVerdict] = []
    idx = 0
    for _ in range(verified):
        c = _claim(f"c{idx}")
        out.append(ClaimVerdict(claim_id=c.claim_id, claim=c, verdict="verified"))
        idx += 1
    for _ in range(contradicted):
        c = _claim(f"c{idx}", subject=f"Subject{idx}", predicate="is", obj="Wrong")
        out.append(ClaimVerdict(claim_id=c.claim_id, claim=c, verdict="contradicted"))
        idx += 1
    for _ in range(abstained):
        c = _claim(f"c{idx}", subject=f"Subject{idx}", predicate="is", obj="Unverifiable")
        out.append(ClaimVerdict(
            claim_id=c.claim_id, claim=c, verdict="no_grounding_found",
            abstention_reason="no_kb_path",
        ))
        idx += 1
    return out


def _make_vr(verified: int = 0, contradicted: int = 0, abstained: int = 0) -> VerificationResult:
    """Build a VerificationResult populated with both the legacy dict shapes
    and the new claim_verdicts list. Used by the response-composition and
    integration tests."""
    cvs = _make_claim_verdicts(verified, contradicted, abstained)
    verdicts = {cv.claim_id: cv.verdict for cv in cvs}
    traces = {cv.claim_id: JustificationTrace(root=TraceNode("claim")) for cv in cvs}
    total = verified + contradicted + abstained
    return VerificationResult(
        claims_extracted=[cv.claim for cv in cvs],
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
        claim_verdicts=cvs,
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
# InterventionType enum (Phase 10.5 Session 2 Item 1 redesign — 3 values)
# ---------------------------------------------------------------------------

class TestInterventionType:
    def test_values(self):
        assert InterventionType.PASS_THROUGH == "pass_through"
        assert InterventionType.INTERVENE == "intervene"
        assert InterventionType.DECLINE == "decline"
        assert {t.value for t in InterventionType} == {"pass_through", "intervene", "decline"}

    def test_claim_action_type_values(self):
        assert ClaimActionType.CORRECT == "correct"
        assert ClaimActionType.ABSTAIN == "abstain"


# ---------------------------------------------------------------------------
# select_interventions — per-claim deterministic logic
# ---------------------------------------------------------------------------

class TestSelectInterventionsPassThrough:
    def test_empty_claim_verdicts_pass_through(self):
        plan = select_interventions([])
        assert plan.overall == InterventionType.PASS_THROUGH
        assert plan.per_claim_actions == ()

    def test_all_verified_pass_through(self):
        plan = select_interventions(_make_claim_verdicts(verified=3))
        assert plan.overall == InterventionType.PASS_THROUGH
        assert plan.per_claim_actions == ()


class TestSelectInterventionsIntervene:
    def test_single_contradicted_intervene_correct(self):
        # Was DECLINE under the old 4-value rollup (1/1 > 50% problematic
        # → DECLINE). Under the per-claim policy a single problematic
        # claim produces INTERVENE with one CORRECT action.
        cvs = _make_claim_verdicts(contradicted=1)
        plan = select_interventions(cvs)
        assert plan.overall == InterventionType.INTERVENE
        assert len(plan.per_claim_actions) == 1
        assert plan.per_claim_actions[0].action_type == ClaimActionType.CORRECT
        assert plan.per_claim_actions[0].claim_id == cvs[0].claim_id

    def test_single_abstained_intervene_abstain(self):
        cvs = _make_claim_verdicts(abstained=1)
        plan = select_interventions(cvs)
        assert plan.overall == InterventionType.INTERVENE
        assert len(plan.per_claim_actions) == 1
        assert plan.per_claim_actions[0].action_type == ClaimActionType.ABSTAIN

    def test_mixed_contradicted_and_abstained_both_surface(self):
        # The new design's central case: a mixed-problem draft produces
        # one action per problematic claim — the abstained claim is no
        # longer silently dropped by a CORRECT-priority rollup.
        cvs = _make_claim_verdicts(verified=1, contradicted=1, abstained=1)
        plan = select_interventions(cvs)
        assert plan.overall == InterventionType.INTERVENE
        types = [a.action_type for a in plan.per_claim_actions]
        assert types.count(ClaimActionType.CORRECT) == 1
        assert types.count(ClaimActionType.ABSTAIN) == 1

    def test_intervene_with_verified_majority(self):
        cvs = _make_claim_verdicts(verified=3, contradicted=1)
        plan = select_interventions(cvs)
        assert plan.overall == InterventionType.INTERVENE
        assert len(plan.per_claim_actions) == 1
        assert plan.per_claim_actions[0].action_type == ClaimActionType.CORRECT

    def test_intervene_with_verified_below_50pct(self):
        # Old policy: 4/5 = 80% problematic, > 50% → DECLINE. New policy:
        # has a verified claim, so INTERVENE with 4 actions.
        cvs = _make_claim_verdicts(verified=1, contradicted=2, abstained=2)
        plan = select_interventions(cvs)
        assert plan.overall == InterventionType.INTERVENE
        assert len(plan.per_claim_actions) == 4

    def test_intervene_actions_preserve_claim_ids(self):
        cvs = _make_claim_verdicts(verified=1, contradicted=1, abstained=1)
        plan = select_interventions(cvs)
        # Each action references the specific claim_id it acts on.
        problematic_cids = {cv.claim_id for cv in cvs
                             if cv.verdict in ("contradicted", "no_grounding_found")}
        action_cids = {a.claim_id for a in plan.per_claim_actions}
        assert action_cids == problematic_cids


class TestSelectInterventionsDecline:
    def test_zero_verified_two_contradicted_decline(self):
        # The narrow DECLINE trigger: zero verified AND ≥ 2 problematic.
        plan = select_interventions(_make_claim_verdicts(contradicted=2))
        assert plan.overall == InterventionType.DECLINE
        assert plan.per_claim_actions == ()

    def test_zero_verified_two_abstained_decline(self):
        plan = select_interventions(_make_claim_verdicts(abstained=2))
        assert plan.overall == InterventionType.DECLINE

    def test_zero_verified_mixed_problems_decline(self):
        plan = select_interventions(_make_claim_verdicts(contradicted=1, abstained=1))
        assert plan.overall == InterventionType.DECLINE

    def test_zero_verified_one_problematic_intervene_not_decline(self):
        # Boundary: a single problematic claim with zero verified does NOT
        # decline — the per-claim annotation is still useful (e.g. "the
        # one claim you made is wrong, here's the correction").
        plan = select_interventions(_make_claim_verdicts(contradicted=1))
        assert plan.overall == InterventionType.INTERVENE

    def test_any_verified_disables_decline(self):
        # Boundary: even one verified claim shifts DECLINE → INTERVENE,
        # because the per-claim annotations now have a useful frame
        # (some content is verified; the listed claims are the issues).
        plan = select_interventions(_make_claim_verdicts(verified=1, contradicted=5))
        assert plan.overall == InterventionType.INTERVENE


class TestSelectInterventionsDualDesignation:
    def test_verified_given_assertion_treated_as_verified(self):
        c = _claim("c1")
        cvs = [ClaimVerdict(claim_id="c1", claim=c, verdict="verified_given_assertion")]
        plan = select_interventions(cvs)
        assert plan.overall == InterventionType.PASS_THROUGH

    def test_contradicted_given_assertion_treated_as_contradicted(self):
        c = _claim("c1")
        cvs = [ClaimVerdict(claim_id="c1", claim=c, verdict="contradicted_given_assertion")]
        plan = select_interventions(cvs)
        assert plan.overall == InterventionType.INTERVENE
        assert plan.per_claim_actions[0].action_type == ClaimActionType.CORRECT

    def test_abstained_given_assertion_treated_as_abstained(self):
        c = _claim("c1")
        cvs = [ClaimVerdict(claim_id="c1", claim=c, verdict="abstained_given_assertion")]
        plan = select_interventions(cvs)
        assert plan.overall == InterventionType.INTERVENE
        assert plan.per_claim_actions[0].action_type == ClaimActionType.ABSTAIN


# ---------------------------------------------------------------------------
# build_response — Format A composition (Phase 10.5 Session 2 Item 1c)
# ---------------------------------------------------------------------------

class TestBuildResponse:
    def test_pass_through_returns_draft_unchanged(self):
        draft = "Hello world."
        plan = InterventionPlan(InterventionType.PASS_THROUGH)
        assert build_response(draft, plan) == draft

    def test_decline_returns_refusal(self):
        draft = "Some unverifiable claim."
        plan = InterventionPlan(InterventionType.DECLINE)
        result = build_response(draft, plan)
        assert draft not in result
        assert "unable" in result.lower() or "cannot" in result.lower()

    def test_intervene_single_action_appends_one_bullet(self):
        draft = "Obama was the 45th President."
        c = _claim("c1", subject="Obama", predicate="holds_role", obj="45th President")
        plan = InterventionPlan(
            InterventionType.INTERVENE,
            (ClaimAction("c1", ClaimActionType.CORRECT,
                         "Aedos found a contradicting source for: Obama holds_role 45th President."),),
        )
        result = build_response(draft, plan)
        assert result.startswith(draft)
        assert "Aedos verification notes:" in result
        assert "- Aedos found a contradicting source for: Obama" in result
        # Single bullet → single newline-separated note line
        assert result.count("\n- ") == 1

    def test_intervene_multiple_actions_appends_multiple_bullets(self):
        draft = "Some statement."
        plan = InterventionPlan(
            InterventionType.INTERVENE,
            (
                ClaimAction("c1", ClaimActionType.CORRECT, "Aedos found a contradicting source for: X."),
                ClaimAction("c2", ClaimActionType.ABSTAIN, "Aedos could not verify: Y."),
                ClaimAction("c3", ClaimActionType.CORRECT, "Aedos found a contradicting source for: Z."),
            ),
        )
        result = build_response(draft, plan)
        assert result.startswith(draft)
        assert result.count("\n- ") == 3
        # All three annotations land in the output — none silently dropped.
        assert "Aedos found a contradicting source for: X." in result
        assert "Aedos could not verify: Y." in result
        assert "Aedos found a contradicting source for: Z." in result

    def test_intervene_uses_separator_before_notes(self):
        draft = "Draft."
        plan = InterventionPlan(
            InterventionType.INTERVENE,
            (ClaimAction("c1", ClaimActionType.ABSTAIN, "Aedos could not verify: X."),),
        )
        result = build_response(draft, plan)
        # Format A: blank line + "---" separator between draft and notes.
        assert "\n\n---\n" in result


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

    def test_response_exposes_intervention_plan(self):
        # Phase 10.5 Session 2 Item 1: ChatResponse carries the full
        # InterventionPlan, not just the top-level shape. Callers that
        # need per-claim actions read `response.intervention_plan`.
        wrapper = self._make_wrapper()
        response = wrapper.respond("Tell me something.")
        assert isinstance(response.intervention_plan, InterventionPlan)
        assert response.intervention_plan.overall in InterventionType
        # intervention_type stays as a backwards-compat property over
        # the overall shape.
        assert response.intervention_type == response.intervention_plan.overall.value
