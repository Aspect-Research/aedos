from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

from ..layer1_extraction.extractor import ExtractionContext
from ..layer1_extraction.triage import AbstentionReason
from ..layer4_sources.walker import VerificationContext
from ..layer5_result.aggregator import (
    ClaimVerdict,
    VerificationResult,
    base_verdict_of,
    is_given_assertion,
)
from ..llm.client import ChatMessage


class InterventionType(str, Enum):
    """Top-level intervention shape for the chat-wrapper response.

    The 3-value enum separates the *shape* of the response
    (pass-through, per-claim intervention, refuse) from the
    *per-claim actions* (carried in `InterventionPlan.per_claim_actions`).
    """
    PASS_THROUGH = "pass_through"
    INTERVENE = "intervene"
    DECLINE = "decline"


class ClaimActionType(str, Enum):
    """Per-claim action shape. Each problematic claim gets one action of
    one of these types when the overall intervention is INTERVENE.

    `CONFIRM_CONDITIONAL` (WS5) surfaces a `*_given_assertion` verified
    claim: the claim is verified only because it rests on the user's own
    (unverified) assertion, not on an independent source. It is made
    VISIBLE per the observability requirement but is NOT treated as a
    problem (it never escalates to DECLINE)."""
    CORRECT = "correct"
    ABSTAIN = "abstain"
    CONFIRM_CONDITIONAL = "confirm_conditional"


@dataclass(frozen=True)
class ClaimAction:
    """One per-claim intervention. `annotation` is the user-facing text
    the chat-wrapper composes into the response."""
    claim_id: str
    action_type: ClaimActionType
    annotation: str


@dataclass(frozen=True)
class InterventionPlan:
    """The full intervention shape: top-level direction + per-claim actions
    (empty when overall is PASS_THROUGH or DECLINE)."""
    overall: InterventionType
    per_claim_actions: tuple[ClaimAction, ...] = ()


@dataclass
class ChatResponse:
    final_message: str
    intervention_plan: InterventionPlan
    verification_result: VerificationResult
    verification_id: str
    draft_message: str = ""

    @property
    def intervention_type(self) -> str:
        """Backwards-compatibility accessor: returns the top-level
        intervention value (one of pass_through / intervene / decline).
        Callers that need the per-claim actions read `intervention_plan`
        directly."""
        return self.intervention_plan.overall.value


def _format_correction(cv: ClaimVerdict, label_fetcher=None) -> str:
    """Correction annotation for a contradicted claim. When the trace
    carried a contradicting value (`cv.contradicting_value`), emit
    '... the source indicates {value} instead.' Entity Q-ids are
    reverse-labeled via `label_fetcher` (when
    `cv.contradicting_value_type == 'entity'`); dates/quantities/literals
    pass through. Falls back to the generic form when no value was
    captured (§3.2-safe: only emit 'instead X' when a distinct value was
    genuinely captured). Reverse-label failure degrades to the raw Q-id —
    never crashes."""
    polarity_marker = "" if cv.claim.polarity == 1 else " (negated)"
    base = (
        f"Aedos found a contradicting source for: "
        f"{cv.claim.subject} {cv.claim.predicate} {cv.claim.object}{polarity_marker}"
    )
    value = cv.contradicting_value
    if value:
        display = value
        if (
            cv.contradicting_value_type == "entity"
            and isinstance(value, str)
            and value.startswith("Q")
            and label_fetcher is not None
        ):
            try:
                label = label_fetcher(value)
            except Exception:
                label = None
            if label:
                display = label
        return f"{base}; the source indicates {display} instead."
    return f"{base}."


def _format_conditional(cv: ClaimVerdict) -> str:
    """WS5: annotation for a `verified_given_assertion` claim — verified
    only because it rests on the user's own (unverified) assertion, with
    no independent source confirming it. Makes the conditional nature
    VISIBLE without treating it as a problem."""
    polarity_marker = "" if cv.claim.polarity == 1 else " (negated)"
    return (
        f"Aedos verified, contingent on your assertion that it holds "
        f"(no independent source confirms it): "
        f"{cv.claim.subject} {cv.claim.predicate} {cv.claim.object}{polarity_marker}."
    )


def _format_abstention(cv: ClaimVerdict) -> str:
    """Generic abstention annotation for a claim without grounding. The
    `abstention_reason` (if present) gives operators a hook; the
    user-facing text remains concise."""
    polarity_marker = "" if cv.claim.polarity == 1 else " (negated)"
    return (
        f"Aedos could not verify: "
        f"{cv.claim.subject} {cv.claim.predicate} {cv.claim.object}{polarity_marker}."
    )


def select_interventions(
    claim_verdicts: list[ClaimVerdict], label_fetcher=None
) -> InterventionPlan:
    """Deterministic per-claim intervention selection.

    The policy:

    - Empty claim list → PASS_THROUGH (nothing to verify, nothing to
      intervene on; a draft with no extracted claims goes to the user
      unchanged).
    - All claims have a `verified`-family base verdict → PASS_THROUGH.
    - At least one problematic claim, AND at least one verified claim
      OR only one problematic claim → INTERVENE with per-claim
      annotations for each problematic claim.
    - Zero verified claims AND ≥ 2 problematic claims → DECLINE. This
      is the genuinely-dominated case: the draft contains nothing the
      system can vouch for, and per-claim annotations would amount to
      a long list of problems against zero verified content. The
      refusal is the honest response.

    Conditional verdicts (`*_given_assertion`, WS5): the base verdict still
    drives the COUNT buckets (via `base_verdict_of`), but the
    `_given_assertion` qualifier is NO LONGER erased at the user surface.
    A `verified_given_assertion` claim emits a `CONFIRM_CONDITIONAL` action
    (visible, but never problematic); `contradicted_given_assertion` /
    `abstained_given_assertion` get CORRECT / ABSTAIN with a suffix noting
    the contradiction/abstention rests on the user's own assertion.

    DECLINE policy: only CORRECT / ABSTAIN actions count as 'problematic'.
    A draft of only conditional confirmations surfaces via INTERVENE notes
    (visibility) and is never refused — a conditionally-verified claim is a
    (conditional) verification, not a problem.

    `label_fetcher` (optional) reverse-labels entity Q-ids in correction
    text; threaded down to `_format_correction`.
    """
    if not claim_verdicts:
        return InterventionPlan(InterventionType.PASS_THROUGH)

    actions: list[ClaimAction] = []
    verified_count = 0
    for cv in claim_verdicts:
        # v0.16 WS4 (4c): not_checkworthy claims are quiet — they are recorded
        # as ClaimVerdicts (observable in VerificationResult) but produce no
        # user-facing note and do not count toward the verified/problematic
        # tallies that drive PASS_THROUGH/DECLINE. This keeps an all-inert draft
        # → PASS_THROUGH (never a spurious DECLINE).
        if cv.abstention_reason == AbstentionReason.NOT_CHECKWORTHY.value:
            continue
        base = base_verdict_of(cv.verdict)
        conditional = is_given_assertion(cv.verdict)
        if base == "verified":
            verified_count += 1
            if conditional:
                # WS5: surface the conditional verification (observability) —
                # not independently grounded, but not a problem either.
                actions.append(ClaimAction(
                    claim_id=cv.claim_id,
                    action_type=ClaimActionType.CONFIRM_CONDITIONAL,
                    annotation=_format_conditional(cv),
                ))
            continue
        if base == "contradicted":
            actions.append(ClaimAction(
                claim_id=cv.claim_id,
                action_type=ClaimActionType.CORRECT,
                annotation=_format_correction(cv, label_fetcher=label_fetcher)
                + (" (this contradiction rests on your own prior assertion)" if conditional else ""),
            ))
        else:  # no_grounding_found (and its dual abstained_given_assertion)
            actions.append(ClaimAction(
                claim_id=cv.claim_id,
                action_type=ClaimActionType.ABSTAIN,
                annotation=_format_abstention(cv)
                + (" (your assertion alone is not independent grounding)" if conditional else ""),
            ))

    if not actions:
        return InterventionPlan(InterventionType.PASS_THROUGH)
    problematic = [
        a for a in actions
        if a.action_type in (ClaimActionType.CORRECT, ClaimActionType.ABSTAIN)
    ]
    if not problematic:
        # Only conditional confirmations — surface them via INTERVENE notes
        # (visibility), never DECLINE on a draft we conditionally verified.
        return InterventionPlan(InterventionType.INTERVENE, tuple(actions))
    if verified_count == 0 and len(problematic) >= 2:
        return InterventionPlan(InterventionType.DECLINE)
    return InterventionPlan(InterventionType.INTERVENE, tuple(actions))


def build_response(draft: str, plan: InterventionPlan) -> str:
    """Compose the user-facing response from the draft + intervention plan.

    Pass-through returns the
    draft unchanged; decline returns a generic refusal; intervene
    returns the draft followed by an "Aedos verification notes:"
    section listing each per-claim annotation as a bullet."""
    if plan.overall == InterventionType.PASS_THROUGH:
        return draft
    if plan.overall == InterventionType.DECLINE:
        return "I'm unable to provide a response I cannot verify."
    # INTERVENE
    notes = "\n".join(f"- {a.annotation}" for a in plan.per_claim_actions)
    return f"{draft}\n\n---\nAedos verification notes:\n{notes}"


def claim_observability(vr: VerificationResult, verbose: bool = False) -> list[dict]:
    """WS5: structured, inspectable per-claim view.

    Two surfaces, two depths (round-1 observability follow-up). The
    `verbose` flag selects between them:

    - verbose=False (LIGHTWEIGHT, for the PUBLIC POST /chat body): the
      verdict-level fields a caller needs to render the turn — verdict,
      base verdict, conditional flag, abstention reason, the
      contradicting value, and a human-readable trace string — but NOT
      the raw `provenance` term and NOT the full `trace` JSON. Both of
      those carry internal substrate identifiers (the trace edge metadata
      embeds tier_u_row_id / entity_resolution_cache_row_id /
      subsumption_row_id, and the provenance literals carry table+row_id
      pairs). Internal DB row ids are not part of the public contract; a
      `/chat` consumer that wants the full audit detail dereferences
      `verification_id` against GET /verification/{id}.
    - verbose=True (FULL, for the rich AUDIT endpoint GET
      /verification/{id}): everything, including `trace` (full
      trace_to_json with edge metadata) and `provenance` (the AND/OR
      term with its (table,row_id) literals) so the operator keeps the
      complete retraction-footprint view.

    `trace_human` is emitted on BOTH surfaces: it is row-id-free (see
    trace_to_human in trace.py — provenance renders as source+status+
    assertion-marker only), so it is safe in the public body.
    """
    from ..layer5_result.trace import trace_to_json, trace_to_human
    out: list[dict] = []
    for cv in vr.claim_verdicts:
        trace = vr.per_claim_traces.get(cv.claim_id)
        entry = {
            "claim_id": cv.claim_id,
            "subject": cv.claim.subject,
            "predicate": cv.claim.predicate,
            "object": cv.claim.object,
            "polarity": cv.claim.polarity,
            "verdict": cv.verdict,
            "base_verdict": base_verdict_of(cv.verdict),
            "conditional": is_given_assertion(cv.verdict),
            "abstention_reason": cv.abstention_reason,
            "contradicting_value": cv.contradicting_value,
            "contradicting_value_type": cv.contradicting_value_type,
            "trace_human": (
                trace_to_human(trace, claim=cv.claim, verdict=cv.verdict)
                if trace else None
            ),
        }
        if verbose:
            # FULL audit surface only: the row-id-bearing trace + provenance.
            trace_json = trace_to_json(trace) if trace else None
            entry["provenance"] = (
                trace_json.get("provenance") if trace_json else None
            )
            entry["trace"] = trace_json
        out.append(entry)
    return out


class ChatWrapper:
    def __init__(
        self,
        extractor,
        walker,
        aggregator,
        llm_client,
        tier_u=None,
        config: Optional[dict] = None,
        kb=None,
    ) -> None:
        self._extractor = extractor
        self._walker = walker
        self._aggregator = aggregator
        self._llm = llm_client
        # WS5: the KB adapter (WikidataAdapter), used only to reverse-label
        # contradicting entity Q-ids when composing corrections. Optional and
        # accessed via getattr(kb, "fetch_label", None) so mock/stub adapters
        # without fetch_label keep working. None for legacy/test constructions.
        self._kb = kb
        # `tier_u` is the promotion target for
        # user-asserted claims. Optional for back-compat with tests that
        # construct ChatWrapper without it (those skip the user-message
        # extraction step; behavior matches a wrapper built without Tier U).
        # The build_pipeline shape passes it explicitly.
        self._tier_u = tier_u
        self._config = config or {}
        self._verification_store: dict[str, VerificationResult] = {}

    def respond(
        self,
        user_message: str,
        conversation_context: Optional[dict] = None,
        progress: Optional[Callable[[dict], None]] = None,
    ) -> ChatResponse:
        ctx_dict = conversation_context or {}
        asserting_party = ctx_dict.get("asserting_party_id", "user")
        current_time = datetime.now(timezone.utc).isoformat()

        # v0.16.2 Phase B: optional progress sink for streaming the turn's steps
        # to a deployment UI (transparent process). `progress=None` reproduces the
        # prior behavior exactly; a throwing sink can never break verification.
        def _emit(phase: str, detail: str, **extra) -> None:
            if progress is None:
                return
            try:
                progress({"phase": phase, "detail": detail, **extra})
            except Exception:
                pass

        _emit("reading", "reading your message")

        # Extract claims
        # from the user_message and promote them into Tier U BEFORE the
        # draft is generated. This is how "user-asserted claims
        # accumulate as premises" lands in the deployed pipeline — the
        # draft-extraction-and-walk loop later in this method then sees
        # the user's assertions as Tier U premises and can chain off them
        # (the chain produces a `*_given_assertion` verdict).
        #
        # Bounded extra cost: one additional extraction call per turn.
        # The user-message extraction reads from `user_message` (not
        # `draft`), so the asserting party is the user and the claims
        # are first-person canonicalized via the extractor's existing
        # logic.
        #
        # Skip the promotion path when `tier_u` was not wired (legacy
        # constructor shape) — the wrapper degrades cleanly to the
        # behavior of a wrapper built without Tier U.
        if self._tier_u is not None and self._extractor is not None and user_message:
            from ..layer4_sources.promotion import promote_assertions
            user_ctx = ExtractionContext(
                asserting_party=asserting_party,
                context_type="chat_user",
                turn_id=ctx_dict.get("conversation_id"),
            )
            user_claims = self._extractor.extract(user_message, user_ctx)
            # v0.16 WS4 (4c): promote only checkworthy, well-formed assertions
            # as premises — exclude any claim carrying an extraction-layer
            # abstention_reason (not_checkworthy, self_referential,
            # predicate_eq_object, subject_absent_from_source). A malformed or
            # not-checkworthy user assertion must not become a Tier U premise.
            user_claims = [c for c in user_claims if c.abstention_reason is None]
            if user_claims:
                promote_assertions(user_claims, self._tier_u)
                _emit("premises",
                      f"recorded {len(user_claims)} premise(s) from your message as session context")
                # The promotion's pre_verdicts (cross-source contradictions
                # on the user's own assertions vs. prior externally-verified
                # rows) are not surfaced to this turn's intervention — the
                # user spoke, we recorded what they said. The audit-log
                # entries (cross_source_contradiction) are the trail.
                # A future deployment surface may consume them.

        # 1. Generate draft
        _emit("draft", "composing a draft reply")
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
            # v0.16 WS4 (4c): do NOT drop INERT_PROSE claims here. They now
            # carry abstention_reason='not_checkworthy' (extractor) and the
            # walker short-circuits them to no_grounding_found (4b) with zero
            # external/LLM calls; the aggregator records a ClaimVerdict so the
            # designation is observable, and select_interventions suppresses any
            # not_checkworthy claim from the user-facing notes and the tallies.

        # 3. Verify each claim
        # Thread the draft (the text the extractor saw) as
        # source_text so the Wikipedia normalizer's Stage 2 has context
        # for disambiguating bare ambiguous references.
        verification_context = VerificationContext(
            current_time=current_time,
            asserting_party=asserting_party,
            source_text=draft,
        )
        _emit("extracted", f"found {len(claims)} claim(s) to verify in the draft")
        walk_results = []
        for i, claim in enumerate(claims):
            _emit(
                "verifying",
                f"verifying claim {i + 1}/{len(claims)}: "
                f"{claim.subject} {claim.predicate} {claim.object}",
                index=i + 1, total=len(claims), claim_id=claim.claim_id,
            )
            result = self._walker.walk(claim, verification_context)
            walk_results.append(result)
            _emit(
                "verdict", str(result.verdict),
                claim_id=claim.claim_id, verdict=str(result.verdict),
                subject=claim.subject, predicate=claim.predicate, object=claim.object,
            )

        # 4. Aggregate
        _emit("composing", "composing the final reply")
        vr = self._aggregator.aggregate(
            claims=claims,
            per_claim_results=walk_results,
            text_input={"message": user_message, "draft": draft},
        )

        # 5. Intervention selection (per-claim plan). WS5: thread the KB
        # adapter's fetch_label so corrections can reverse-label entity Q-ids
        # ("the source indicates {label} instead"). getattr-guarded: a kb
        # without fetch_label (or no kb) yields None → raw value / generic form.
        label_fetcher = getattr(self._kb, "fetch_label", None)
        plan = select_interventions(vr.claim_verdicts, label_fetcher=label_fetcher)

        # 6. Build response (Format A: draft + per-claim notes)
        final = build_response(draft, plan)

        verification_id = str(uuid.uuid4())
        self._verification_store[verification_id] = vr

        return ChatResponse(
            final_message=final,
            intervention_plan=plan,
            verification_result=vr,
            verification_id=verification_id,
            draft_message=draft,
        )

    def get_verification(self, verification_id: str) -> Optional[VerificationResult]:
        """Return the stored VerificationResult, lazily re-deriving any stale
        verdict first (v0.16 WS3 §3E).

        A verdict goes STALE when a Tier U premise it rested on is later closed
        or retracted (TierU.write/retract → propagate_retraction). Staleness is
        scoped to *_given_assertion verdicts. On next reference here, re-walk
        each stale claim (the walk IS the re-derivation), re-aggregate the whole
        result so the stored shape stays consistent, clear the stale flags, and
        return the refreshed result. No propagator (test paths) → return as-is.
        """
        vr = self._verification_store.get(verification_id)
        if vr is None:
            return None
        propagator = getattr(self._aggregator, "_propagator", None)
        if propagator is None:
            return vr

        stale_ids = [
            cv.claim_id
            for cv in vr.claim_verdicts
            if propagator.is_stale(cv.claim_id)
        ]
        if not stale_ids:
            return vr

        # Re-walk EVERY claim and re-aggregate — the cheapest correct path that
        # keeps per_claim_verdicts / claim_verdicts / aggregate_metadata mutually
        # consistent (a partial re-walk would desync the aggregate counts).
        # v0.16 WS3 §3E: re-derivation needs a real VerificationContext —
        # Walker.walk dereferences context.current_time. Rebuild one from the
        # stored claims (they share the turn's asserting party) and the draft
        # that was verified, with a fresh timestamp.
        rederive_context = VerificationContext(
            current_time=datetime.now(timezone.utc).isoformat(),
            asserting_party=(
                vr.claims_extracted[0].asserting_party
                if vr.claims_extracted else "user"
            ),
            source_text=vr.text_input.get("draft") if vr.text_input else None,
        )
        walk_results = [
            self._walker.walk(c, rederive_context) for c in vr.claims_extracted
        ]
        refreshed = self._aggregator.aggregate(
            claims=vr.claims_extracted,
            per_claim_results=walk_results,
            text_input=vr.text_input,
        )
        for cid in stale_ids:
            propagator.clear_stale(cid)
        self._verification_store[verification_id] = refreshed
        return refreshed
