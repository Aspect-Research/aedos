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
    _format_conditional,
    _format_correction,
    build_response,
    select_interventions,
)
from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import AbstentionReason, TriageDecision
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
        # WS5 (part d): the conditional-confirmation action for
        # `verified_given_assertion` claims.
        assert ClaimActionType.CONFIRM_CONDITIONAL == "confirm_conditional"
        assert {t.value for t in ClaimActionType} == {
            "correct", "abstain", "confirm_conditional",
        }


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
    """WS5 (part d): the `*_given_assertion` qualifier is NO LONGER collapsed
    to its base verdict at the user surface. A `verified_given_assertion`
    claim now surfaces a CONFIRM_CONDITIONAL action (visible, but not a
    problem → never DECLINE); the contradicted/abstained duals keep their
    CORRECT/ABSTAIN action but with a suffix noting the
    contradiction/abstention rests on the user's own assertion."""

    def test_verified_given_assertion_surfaces_conditional(self):
        # Inverted from the v0.15 collapse-to-base (was PASS_THROUGH): a
        # single conditionally-verified claim is now made VISIBLE via an
        # INTERVENE note carrying a CONFIRM_CONDITIONAL action — never a
        # problem, never DECLINE.
        c = _claim("c1")
        cvs = [ClaimVerdict(claim_id="c1", claim=c, verdict="verified_given_assertion")]
        plan = select_interventions(cvs)
        assert plan.overall == InterventionType.INTERVENE
        assert len(plan.per_claim_actions) == 1
        action = plan.per_claim_actions[0]
        assert action.action_type == ClaimActionType.CONFIRM_CONDITIONAL
        assert action.claim_id == "c1"
        # The annotation makes the conditional (assertion-resting) nature plain.
        assert "contingent on your assertion" in action.annotation

    def test_only_conditional_confirmations_never_decline(self):
        # WS5 (part d.2): a draft of ONLY conditionally-verified claims —
        # even two of them, which under the base policy would be two
        # verified claims (PASS_THROUGH) — surfaces via INTERVENE notes,
        # and must NEVER escalate to DECLINE (a conditional verification is
        # not a problem). This is the policy guard the collapse removal needs.
        c1 = _claim("c1")
        c2 = _claim("c2")
        cvs = [
            ClaimVerdict(claim_id="c1", claim=c1, verdict="verified_given_assertion"),
            ClaimVerdict(claim_id="c2", claim=c2, verdict="verified_given_assertion"),
        ]
        plan = select_interventions(cvs)
        assert plan.overall == InterventionType.INTERVENE
        assert len(plan.per_claim_actions) == 2
        assert all(
            a.action_type == ClaimActionType.CONFIRM_CONDITIONAL
            for a in plan.per_claim_actions
        )

    def test_contradicted_given_assertion_correct_with_conditional_suffix(self):
        c = _claim("c1")
        cvs = [ClaimVerdict(claim_id="c1", claim=c, verdict="contradicted_given_assertion")]
        plan = select_interventions(cvs)
        assert plan.overall == InterventionType.INTERVENE
        action = plan.per_claim_actions[0]
        assert action.action_type == ClaimActionType.CORRECT
        # The dual designation is no longer erased — the correction notes the
        # contradiction rests on the user's own prior assertion.
        assert "rests on your own prior assertion" in action.annotation

    def test_abstained_given_assertion_abstain_with_conditional_suffix(self):
        c = _claim("c1")
        cvs = [ClaimVerdict(claim_id="c1", claim=c, verdict="abstained_given_assertion")]
        plan = select_interventions(cvs)
        assert plan.overall == InterventionType.INTERVENE
        action = plan.per_claim_actions[0]
        assert action.action_type == ClaimActionType.ABSTAIN
        assert "your assertion alone is not independent grounding" in action.annotation

    def test_conditional_confirmation_does_not_block_real_problem_intervene(self):
        # A mix of one conditional confirmation and one genuinely
        # contradicted claim → INTERVENE; the contradicted claim is the only
        # 'problematic' one, but with a verified_given_assertion present
        # (counts as verified) DECLINE never triggers regardless.
        c1 = _claim("c1")
        c2 = _claim("c2", subject="Subject2", predicate="is", obj="Wrong")
        cvs = [
            ClaimVerdict(claim_id="c1", claim=c1, verdict="verified_given_assertion"),
            ClaimVerdict(claim_id="c2", claim=c2, verdict="contradicted"),
        ]
        plan = select_interventions(cvs)
        assert plan.overall == InterventionType.INTERVENE
        types = {a.action_type for a in plan.per_claim_actions}
        assert ClaimActionType.CONFIRM_CONDITIONAL in types
        assert ClaimActionType.CORRECT in types


# ---------------------------------------------------------------------------
# TestSelectInterventionsNotCheckworthy — v0.16 WS4 (4c.2): a not_checkworthy
# claim is QUIET — recorded as a ClaimVerdict (observable) but produces no
# user-facing action and does not count toward the verified/problematic
# tallies that drive PASS_THROUGH/DECLINE.
# ---------------------------------------------------------------------------

def _not_checkworthy_cv(claim_id: str) -> ClaimVerdict:
    c = _claim(claim_id, subject="weather", predicate="is_nice", obj="pleasant")
    return ClaimVerdict(
        claim_id=claim_id, claim=c, verdict="no_grounding_found",
        abstention_reason=AbstentionReason.NOT_CHECKWORTHY.value,
    )


class TestSelectInterventionsNotCheckworthy:
    def test_not_checkworthy_suppressed_from_notes(self):
        # One verified + one not_checkworthy claim. The not_checkworthy claim
        # must NOT produce an ABSTAIN action (it is quiet), even though its
        # base verdict is no_grounding_found. With only a verified claim
        # remaining and no actions, the plan is PASS_THROUGH.
        c_ok = _claim("c0")
        cvs = [
            ClaimVerdict(claim_id="c0", claim=c_ok, verdict="verified"),
            _not_checkworthy_cv("c1"),
        ]
        plan = select_interventions(cvs)
        assert plan.overall == InterventionType.PASS_THROUGH
        # No ABSTAIN action was emitted for the not_checkworthy claim.
        action_cids = {a.claim_id for a in plan.per_claim_actions}
        assert "c1" not in action_cids
        assert all(
            a.action_type != ClaimActionType.ABSTAIN for a in plan.per_claim_actions
        )

    def test_not_checkworthy_alongside_real_problem_only_problem_surfaces(self):
        # A not_checkworthy claim must not be conflated with a genuine
        # abstention: only the real contradicted claim surfaces an action.
        c_ok = _claim("c0")
        c_bad = _claim("c2", subject="Subject2", predicate="is", obj="Wrong")
        cvs = [
            ClaimVerdict(claim_id="c0", claim=c_ok, verdict="verified"),
            _not_checkworthy_cv("c1"),
            ClaimVerdict(claim_id="c2", claim=c_bad, verdict="contradicted"),
        ]
        plan = select_interventions(cvs)
        assert plan.overall == InterventionType.INTERVENE
        action_cids = {a.claim_id for a in plan.per_claim_actions}
        assert action_cids == {"c2"}

    def test_all_inert_draft_passes_through(self):
        # An all-not_checkworthy draft (e.g. "That's a great question!") would,
        # without the tally-skip, yield N≥2 abstain actions + 0 verified →
        # spurious DECLINE. The skip removes them from actions AND verified_count
        # → empty effective set → PASS_THROUGH (never DECLINE).
        cvs = [_not_checkworthy_cv("c0"), _not_checkworthy_cv("c1"), _not_checkworthy_cv("c2")]
        plan = select_interventions(cvs)
        assert plan.overall == InterventionType.PASS_THROUGH
        assert plan.per_claim_actions == ()


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
# _format_correction — contradicting value + reverse-label (WS5 part c)
# ---------------------------------------------------------------------------

class TestFormatCorrection:
    def test_generic_form_when_no_contradicting_value(self):
        # No value captured (polarity-conflict / subsumption-fallback path)
        # → the §3.2-safe generic form, no spurious "instead X".
        c = _claim("c1", subject="Obama", predicate="holds_role", obj="45th President")
        cv = ClaimVerdict(claim_id="c1", claim=c, verdict="contradicted")
        text = _format_correction(cv)
        assert text == (
            "Aedos found a contradicting source for: "
            "Obama holds_role 45th President."
        )
        assert "instead" not in text

    def test_literal_value_passes_through(self):
        c = _claim("c1", subject="France", predicate="capital", obj="Lyon")
        cv = ClaimVerdict(
            claim_id="c1", claim=c, verdict="contradicted",
            contradicting_value="Paris", contradicting_value_type="literal",
        )
        text = _format_correction(cv)
        assert "the source indicates Paris instead." in text

    def test_quantity_value_passes_through(self):
        c = _claim("c1", subject="France", predicate="population", obj="10 million")
        cv = ClaimVerdict(
            claim_id="c1", claim=c, verdict="contradicted",
            contradicting_value="67000000", contradicting_value_type="quantity",
        )
        text = _format_correction(cv)
        assert "the source indicates 67000000 instead." in text

    def test_entity_qid_reverse_labeled_via_fetcher(self):
        # WS5 (part c): an entity-typed contradicting value is a Q-id; the
        # label_fetcher reverse-resolves it to a human label.
        c = _claim("c1", subject="Obama", predicate="birthplace", obj="Chicago")
        cv = ClaimVerdict(
            claim_id="c1", claim=c, verdict="contradicted",
            contradicting_value="Q18094", contradicting_value_type="entity",
        )
        labels = {"Q18094": "Honolulu"}
        text = _format_correction(cv, label_fetcher=lambda q: labels.get(q))
        assert "the source indicates Honolulu instead." in text
        assert "Q18094" not in text

    def test_entity_qid_fail_open_to_raw_when_no_fetcher(self):
        # No fetcher → the raw Q-id is shown (still informative), never crash.
        c = _claim("c1", subject="Obama", predicate="birthplace", obj="Chicago")
        cv = ClaimVerdict(
            claim_id="c1", claim=c, verdict="contradicted",
            contradicting_value="Q18094", contradicting_value_type="entity",
        )
        text = _format_correction(cv)
        assert "the source indicates Q18094 instead." in text

    def test_entity_qid_fail_open_when_fetcher_raises(self):
        # A throwing label_fetcher degrades to the raw Q-id, never propagates.
        c = _claim("c1", subject="Obama", predicate="birthplace", obj="Chicago")
        cv = ClaimVerdict(
            claim_id="c1", claim=c, verdict="contradicted",
            contradicting_value="Q18094", contradicting_value_type="entity",
        )

        def _boom(_q):
            raise RuntimeError("kb down")

        text = _format_correction(cv, label_fetcher=_boom)
        assert "the source indicates Q18094 instead." in text

    def test_entity_qid_fail_open_when_fetcher_returns_none(self):
        c = _claim("c1", subject="Obama", predicate="birthplace", obj="Chicago")
        cv = ClaimVerdict(
            claim_id="c1", claim=c, verdict="contradicted",
            contradicting_value="Q18094", contradicting_value_type="entity",
        )
        text = _format_correction(cv, label_fetcher=lambda q: None)
        assert "the source indicates Q18094 instead." in text


class TestFormatConditional:
    def test_conditional_annotation_is_contingent_and_visible(self):
        c = _claim("c1", subject="Asa", predicate="lives_in", obj="Paris")
        cv = ClaimVerdict(claim_id="c1", claim=c, verdict="verified_given_assertion")
        text = _format_conditional(cv)
        assert "contingent on your assertion" in text
        assert "no independent source confirms it" in text
        assert "Asa lives_in Paris" in text

    def test_conditional_annotation_marks_negation(self):
        c = _claim("c1", subject="Asa", predicate="lives_in", obj="Paris", polarity=0)
        cv = ClaimVerdict(claim_id="c1", claim=c, verdict="verified_given_assertion")
        text = _format_conditional(cv)
        assert "(negated)" in text


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
