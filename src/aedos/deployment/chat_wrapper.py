from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from ..layer1_extraction.extractor import ExtractionContext
from ..layer1_extraction.triage import TriageDecision
from ..layer4_sources.walker import VerificationContext
from ..layer5_result.aggregator import VerificationResult
from ..llm.client import ChatMessage


class InterventionType(str, Enum):
    PASS_THROUGH = "pass_through"
    ABSTAIN = "abstain"
    CORRECT = "correct"
    DECLINE = "decline"


@dataclass
class ChatResponse:
    final_message: str
    intervention_type: str
    verification_result: VerificationResult
    verification_id: str
    draft_message: str = ""


def select_intervention(vr: VerificationResult) -> InterventionType:
    """Deterministic intervention selection from VerificationResult."""
    meta = vr.aggregate_metadata
    total = meta.get("claim_count", 0)
    if total == 0:
        return InterventionType.PASS_THROUGH

    contradicted = meta.get("contradicted", 0)
    abstained = meta.get("abstained", 0)

    if contradicted + abstained > total * 0.5:
        return InterventionType.DECLINE
    if contradicted > 0:
        return InterventionType.CORRECT
    if abstained > 0:
        return InterventionType.ABSTAIN
    return InterventionType.PASS_THROUGH


def build_response(draft: str, intervention_type: InterventionType, vr: VerificationResult) -> str:
    """Build the final response text based on intervention type."""
    if intervention_type == InterventionType.PASS_THROUGH:
        return draft
    if intervention_type == InterventionType.ABSTAIN:
        return draft + "\n\n[Note: some claims could not be verified.]"
    if intervention_type == InterventionType.CORRECT:
        return draft + "\n\n[Note: some claims were corrected based on verified sources.]"
    if intervention_type == InterventionType.DECLINE:
        return "I'm unable to provide a response I cannot verify."
    return draft


class ChatWrapper:
    def __init__(
        self,
        extractor,
        walker,
        aggregator,
        llm_client,
        tier_u=None,
        config: Optional[dict] = None,
    ) -> None:
        self._extractor = extractor
        self._walker = walker
        self._aggregator = aggregator
        self._llm = llm_client
        # Phase H Cluster 2 step 2: `tier_u` is the promotion target for
        # user-asserted claims. Optional for back-compat with tests that
        # construct ChatWrapper without it (those skip the user-message
        # extraction step; behavior matches pre-Cluster-2). The
        # build_pipeline shape passes it explicitly.
        self._tier_u = tier_u
        self._config = config or {}
        self._verification_store: dict[str, VerificationResult] = {}

    def respond(self, user_message: str, conversation_context: Optional[dict] = None) -> ChatResponse:
        ctx_dict = conversation_context or {}
        asserting_party = ctx_dict.get("asserting_party_id", "user")
        current_time = datetime.now(timezone.utc).isoformat()

        # Phase H Cluster 2 step 2 (Q-ChatWrapperSource): extract claims
        # from the user_message and promote them into Tier U BEFORE the
        # draft is generated. This is how "user-asserted claims
        # accumulate as premises" lands in the deployed pipeline — the
        # draft-extraction-and-walk loop later in this method then sees
        # the user's assertions as Tier U premises and can chain off them
        # (the chain produces a `*_given_assertion` verdict per step 3).
        #
        # Bounded extra cost: one additional extraction call per turn.
        # The user-message extraction reads from `user_message` (not
        # `draft`), so the asserting party is the user and the claims
        # are first-person canonicalized via the extractor's existing
        # logic.
        #
        # Skip the promotion path when `tier_u` was not wired (legacy
        # constructor shape) — the wrapper degrades cleanly to the
        # pre-Cluster-2 behavior.
        if self._tier_u is not None and self._extractor is not None and user_message:
            from ..layer4_sources.promotion import promote_assertions
            user_ctx = ExtractionContext(
                asserting_party=asserting_party,
                context_type="chat_user",
                turn_id=ctx_dict.get("conversation_id"),
            )
            user_claims = self._extractor.extract(user_message, user_ctx)
            user_claims = [c for c in user_claims if c.triage_decision == TriageDecision.VERIFY]
            if user_claims:
                promote_assertions(user_claims, self._tier_u)
                # The promotion's pre_verdicts (cross-source contradictions
                # on the user's own assertions vs. prior externally-verified
                # rows) are not surfaced to this turn's intervention — the
                # user spoke, we recorded what they said. The audit-log
                # entries (cross_source_contradiction) are the trail.
                # Phase 10.5 / a future deployment surface may consume them.

        # 1. Generate draft
        system = self._config.get("system_prompt", "You are a helpful assistant.")
        draft = self._llm.chat(
            system=system,
            messages=[ChatMessage(role="user", content=user_message)],
            purpose="chat",
        )

        # 2. Extract claims from the draft (the LLM's response — the
        # text whose factual content we intervene on).
        #
        # The extraction call is deliberately NOT wrapped in a broad
        # `except Exception`. A prior `except Exception: claims = []` here
        # silently swallowed a `TypeError` from a stale `extract` signature for
        # two release candidates, leaving `/chat` verification-inert. Letting an
        # unexpected extraction failure propagate is the honest behaviour: the
        # next such bug surfaces immediately instead of degrading silently.
        claims = []
        if self._extractor is not None and draft:
            extraction_context = ExtractionContext(
                asserting_party=asserting_party,
                context_type="chat_user",
                turn_id=ctx_dict.get("conversation_id"),
            )
            claims = self._extractor.extract(draft, extraction_context)
            # Only keep VERIFY-triaged claims for verification
            claims = [c for c in claims if c.triage_decision == TriageDecision.VERIFY]

        # 3. Verify each claim
        # Phase H D47: thread the draft (the text the extractor saw) as
        # source_text so the Wikipedia normalizer's Stage 2 has context
        # for disambiguating bare ambiguous references.
        verification_context = VerificationContext(
            current_time=current_time,
            asserting_party=asserting_party,
            source_text=draft,
        )
        walk_results = []
        for claim in claims:
            result = self._walker.walk(claim, verification_context)
            walk_results.append(result)

        # 4. Aggregate
        vr = self._aggregator.aggregate(
            claims=claims,
            per_claim_results=walk_results,
            text_input={"message": user_message, "draft": draft},
        )

        # 5. Intervention selection
        intervention = select_intervention(vr)

        # 6. Build response
        final = build_response(draft, intervention, vr)

        verification_id = str(uuid.uuid4())
        self._verification_store[verification_id] = vr

        return ChatResponse(
            final_message=final,
            intervention_type=intervention.value,
            verification_result=vr,
            verification_id=verification_id,
            draft_message=draft,
        )

    def get_verification(self, verification_id: str) -> Optional[VerificationResult]:
        return self._verification_store.get(verification_id)
