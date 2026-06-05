from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

from ..layer1_extraction.extractor import ExtractionContext
from ..layer1_extraction.triage import AbstentionReason
from ..layer4_sources.parallel_verify import DEFAULT_MAX_WORKERS, walk_claims_parallel
from ..layer4_sources.walker import VerificationContext
from .claim_selection import select_central_claims
from ..layer5_result.aggregator import (
    ClaimVerdict,
    VerificationResult,
    base_verdict_of,
    is_given_assertion,
)
from ..llm.client import ChatMessage

_log = logging.getLogger(__name__)


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
    # v0.16.2 Phase D: claims extracted from the draft but NOT central to the
    # user's question — passed through unverified (each a {claim_id, subject,
    # predicate, object, polarity} dict), plus a one-line selection summary.
    not_assessed_claims: list = field(default_factory=list)
    selection_summary: str = ""

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


def _format_temporal_caveat(cv: ClaimVerdict) -> str:
    """v0.16.4: annotation for a present-tense fact that verified, but whose
    claimed start date ("since <year>") could not be confirmed (it precedes the
    value's actual KB start). The present fact is stated as confirmed; the date is
    explicitly flagged as unconfirmed so it is never presented as verified."""
    return (
        f"Aedos verified this is currently true, but could NOT confirm the claimed "
        f"start date / 'since' — that time bound is unconfirmed: "
        f"{cv.claim.subject} {cv.claim.predicate} {cv.claim.object}."
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


def _reconcile_for_composition(claim_verdicts):
    """Collapse claims that share a (subject, predicate, object, polarity) triple
    to ONE representative for USER-FACING COMPOSITION (the intervention plan + the
    editor instructions). Raw per-claim observability is untouched — this only
    governs what the final reply says.

    Motivation: the extractor emits temporal variants of the same fact as separate
    claims with the SAME triple — e.g. "X is president" (unscoped → verified) and
    "X took office in May 2022" → holds_role(X, president) with valid_from=2022-05
    (a date that doesn't ground → no_grounding). Composing per-claim then handed
    the editor CONTRADICTORY instructions for one triple ("keep X" AND "remove X"),
    so it struck the very fact it had verified — an over-refusal. Reconciling by
    triple with a verdict precedence (a verified base fact is NOT struck by a
    same-triple temporal abstention) fixes it; the distinct temporal detail (the
    role_started/date claim) is a different triple and is preserved.

    Precedence (most→least authoritative for what to tell the user about a triple):
    contradicted > verified > verified_given_assertion > abstained. A contradiction
    in ANY scope wins (conservative: never assert a triple some scope refutes); a
    plain verification beats a mere absence-of-grounding."""
    def _rank(cv) -> int:
        base = base_verdict_of(cv.verdict)
        if base == "contradicted":
            return 3
        if base == "verified":
            # A CLEAN verified (independent, fully in-scope) outranks a caveated
            # one — assertion-conditional or temporal-scope-unconfirmed — for the
            # same triple, so the clean assertion represents the group. A caveated
            # verified still outranks an abstention (it confirms the present fact).
            caveated = is_given_assertion(cv.verdict) or getattr(
                cv, "temporal_scope_unconfirmed", False
            )
            return 1 if caveated else 2
        return 0  # no_grounding_found / abstained / not_checkworthy

    groups: dict = {}
    order: list = []
    for cv in claim_verdicts:
        key = (
            (cv.claim.subject or "").strip().lower(),
            cv.claim.predicate,
            (cv.claim.object or "").strip().lower(),
            cv.claim.polarity,
        )
        if key not in groups:
            groups[key] = cv
            order.append(key)
        elif _rank(cv) > _rank(groups[key]):
            groups[key] = cv
    return [groups[k] for k in order]


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
            if getattr(cv, "temporal_scope_unconfirmed", False):
                # v0.16.4: present base fact verified, but the claimed start date
                # ("since <year>") could not be confirmed. Surface as a caveat
                # (observability + notes fallback) — verified, not a problem, but
                # the date is flagged so it is never presented as confirmed.
                actions.append(ClaimAction(
                    claim_id=cv.claim_id,
                    action_type=ClaimActionType.CONFIRM_CONDITIONAL,
                    annotation=_format_temporal_caveat(cv),
                ))
            elif conditional:
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


# --------------------------------------------------------------------------- #
# v0.16.4: inline verified-edit. Instead of appending an "Aedos verification
# notes" section, a final constrained-rewrite step folds the per-claim verdicts
# INTO the reply once every claim is processed — correcting wrong facts to the
# verified value, removing unverifiable claims, and caveating
# assertion-conditional ones — producing one coherent message. The editor is
# bounded (it may only apply the listed instructions and may NOT introduce new
# facts); any failure falls back to the deterministic draft+notes composition,
# and the structured observability still carries the true per-claim verdicts, so
# the audit trail stays honest regardless of the prose.
# --------------------------------------------------------------------------- #

_REVISE_SYSTEM_PROMPT = """\
You are the final editor of a fact-verified assistant. You receive a draft reply
and a list of verification instructions from a system that has checked the
draft's factual claims against authoritative sources. Produce ONE natural,
coherent revised reply that applies EVERY instruction.

Rules — follow them exactly:
1. CORRECT: where an instruction gives a verified value, state THAT value; never
   repeat the original wrong value.
2. REMOVE: drop the named claim entirely — do not mention it, hedge it, or hint
   at it.
3. CAVEAT: keep the claim but add a brief, natural note that it rests on the
   user's own assertion and is not independently confirmed.
4. VERIFIED or unmentioned content: keep it; you may rephrase for flow.
5. NEVER introduce any new fact, name, number, date, place, or claim that is not
   in the draft or the instructions. Do not fill gaps from your own knowledge.
6. If, after applying the instructions, there is essentially nothing verified
   left to say, reply with a SHORT honest sentence that you could not verify
   enough to answer — do not pad it.
7. The verification was run against CURRENT authoritative sources, not your
   training data. So do NOT hedge a VERIFIED fact as possibly stale: drop "as of
   my last update / as of <date>" cutoff framing and "this may have changed / I'd
   recommend checking a recent source to confirm" currency disclaimers about facts
   the instructions VERIFIED — they contradict a fact just confirmed against live
   data. State verified facts plainly and confidently. (Keep honest uncertainty
   ONLY where an instruction marks a claim unconfirmed, caveated, or removed — e.g.
   a present fact whose start date could not be confirmed: assert the fact, omit
   the date, and do not add a stale-knowledge hedge in its place.)

Output ONLY the revised reply text. No preamble, no notes, no headers.
"""


def _is_blank(text: Optional[str]) -> bool:
    """True when editor output has no substantive content — empty, whitespace, or
    only punctuation/markdown noise ('.', '- ', '—'). Treated as a
    strip-to-nothing edit so a near-empty fragment can't slip through as the
    answer (adversarial-review D2)."""
    return not any(ch.isalnum() for ch in (text or ""))


def _corrected_value_display(cv: ClaimVerdict, label_fetcher=None):
    """The verified value to substitute for a CONTRADICTED claim, reverse-labeling
    an entity Q-id to its name when possible (mirrors _format_correction). Returns
    None when no distinct contradicting value was captured (§3.2-safe: we only
    assert a correction when a concrete value was genuinely found)."""
    value = cv.contradicting_value
    if not value:
        return None
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
    return display


def _revision_instructions(claim_verdicts, label_fetcher=None) -> list[str]:
    """Build the per-claim editor instructions from the verdicts (richer than the
    annotation strings: carries the concrete corrected value). not_checkworthy
    claims are quiet — no instruction, exactly as they are absent from the notes."""
    lines: list[str] = []
    for cv in claim_verdicts:
        if cv.abstention_reason == AbstentionReason.NOT_CHECKWORTHY.value:
            continue
        triple = f"{cv.claim.subject} {cv.claim.predicate} {cv.claim.object}"
        if cv.claim.polarity == 0:
            triple += " (negated)"
        base = base_verdict_of(cv.verdict)
        conditional = is_given_assertion(cv.verdict)
        if base == "verified":
            if getattr(cv, "temporal_scope_unconfirmed", False):
                lines.append(
                    f"PRESENT-ONLY — \"{triple}\": this is CURRENTLY true and "
                    f"confirmed — assert it, but do NOT state the claimed start "
                    f"date or 'since <year>'; that time bound could not be confirmed."
                )
            elif conditional:
                lines.append(
                    f"CAVEAT — \"{triple}\": keep this, but note it rests on the "
                    f"user's own assertion, not an independent source."
                )
            else:
                lines.append(f"VERIFIED — \"{triple}\": confirmed; keep it.")
        elif base == "contradicted":
            corrected = _corrected_value_display(cv, label_fetcher)
            if corrected:
                lines.append(
                    f"CORRECT — \"{triple}\": this is WRONG; the verified value is "
                    f"\"{corrected}\". State the correct value, not the original."
                )
            else:
                lines.append(
                    f"REMOVE — \"{triple}\": contradicted by a source and cannot be "
                    f"corrected; remove it."
                )
        else:  # no_grounding_found / abstained_given_assertion
            lines.append(
                f"REMOVE — \"{triple}\": could not be verified; remove it (do not "
                f"assert it)."
            )
    return lines


def revise_response(
    user_message: str,
    draft: str,
    claim_verdicts,
    llm,
    label_fetcher=None,
) -> Optional[str]:
    """v0.16.4 constrained final edit. Returns the revised reply (stripped; may be
    "" after a strip-to-nothing edit), or None on any LLM error so the caller can
    fall back to the deterministic composition. Never raises."""
    instructions = _revision_instructions(claim_verdicts, label_fetcher)
    instr_block = (
        "\n".join(f"- {line}" for line in instructions)
        if instructions else "- (no changes required)"
    )
    user = (
        f"User's question:\n{user_message}\n\n"
        f"Draft reply:\n{draft}\n\n"
        f"Verification instructions (apply EVERY one):\n{instr_block}"
    )
    try:
        revised = llm.chat(
            system=_REVISE_SYSTEM_PROMPT,
            messages=[ChatMessage(role="user", content=user)],
            purpose="chat:revise",
        )
    except Exception:
        return None
    return revised.strip() if isinstance(revised, str) else None


def walk_result_observability(claim, walk_result) -> dict:
    """Per-claim observability built from a raw WalkResult (pre-aggregation), for
    STREAMING each claim's verdict + reasoning trace the moment its walk
    completes. Mirrors the lightweight `claim_observability` entry: verdict, base
    verdict, given-assertion flag, abstention reason, and the row-id-free human
    trace (`trace_human`) — the latter is what answers "how did it conclude
    `no_grounding_found`" (sources/edges tried + why it abstained)."""
    from ..layer5_result.trace import trace_to_human

    verdict = walk_result.verdict
    trace = getattr(walk_result, "trace", None)
    return {
        "claim_id": claim.claim_id,
        "subject": claim.subject,
        "predicate": claim.predicate,
        "object": claim.object,
        "polarity": claim.polarity,
        "verdict": verdict,
        "base_verdict": base_verdict_of(verdict),
        "conditional": is_given_assertion(verdict),
        "abstention_reason": getattr(walk_result, "abstention_reason", None),
        "trace_human": (
            trace_to_human(trace, claim=claim, verdict=verdict) if trace else None
        ),
    }


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
        verification_store=None,
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
        # v0.16.2 observability: optional durable SQLite-backed store. When None
        # (legacy/test constructions), behavior is exactly as before — pure
        # in-memory, lost on restart. When wired, every turn's full result is
        # persisted so GET /verification/{id} survives restart with no re-walk.
        self._vstore = verification_store

    def respond(
        self,
        user_message: str,
        conversation_context: Optional[dict] = None,
        progress: Optional[Callable[[dict], None]] = None,
        verify_workers: int = DEFAULT_MAX_WORKERS,
        select_central: bool = True,
        select_min_claims: int = 4,
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
        #
        # v0.16.5: the user-message premises are EXTRACTED + filtered here but
        # PROMOTED (written to Tier U) only AFTER the draft walk below — so the
        # draft cannot self-ground on the message it is answering. See the deferred
        # `promote_assertions` call after the walk for the full rationale.
        pending_user_premises: list = []
        if self._tier_u is not None and self._extractor is not None and user_message:
            from ..layer4_sources.promotion import is_source_grounded
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
            #
            # v0.16.3: ALSO require SOURCE-GROUNDING — both the subject AND the
            # object must appear in the user's message. The extractor's own guard
            # is an OR (subject OR object present), so it over-promotes a QUESTION
            # answered by the LLM: "What is the capital of France?" extracts
            # (France, capital, Paris) where "Paris" is the model's answer, not in
            # the source. Promoting that fabricated premise lets a later draft
            # self-ground against it (verified_given_assertion, KB bypassed). The
            # AND gate blocks the fabricated answer while preserving genuine
            # stipulations ("France's capital is Paris" — both entities present).
            # Checked against `user_message` (the real source the model cannot
            # pad), so it is robust to a fabricated claim-level source_text span.
            # Defer the WRITE: collect the grounded premises and promote them only
            # AFTER the draft walk (below), so the draft never self-grounds on this
            # turn's message. The promotion's pre_verdicts (cross-source
            # contradictions vs. prior externally-verified rows) are not surfaced to
            # this turn's intervention anyway — the audit log carries the trail — so
            # deferring the write does not lose anything this turn.
            pending_user_premises = [
                c for c in user_claims
                if c.abstention_reason is None and is_source_grounded(c, user_message)
            ]

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
        _emit("extracted", f"found {len(claims)} claim(s) in the draft")
        # 2.5 Phase D: select the claims CENTRAL to the user's question and verify
        # ONLY those. Non-central claims pass through the draft unverified — no
        # verdict is emitted on them (like an abstain), so this never false-
        # verifies/contradicts; they are shown transparently as "not assessed".
        # Fails open to ALL claims (select_central_claims handles the fallbacks).
        will_select = select_central and len(claims) > select_min_claims
        if will_select:
            _emit("selecting", "deciding which claims are central to your question")
        selection = select_central_claims(
            self._llm, user_message, draft, claims,
            min_claims=select_min_claims, enabled=select_central,
        )
        central = [c for c in claims if c.claim_id in selection.central_ids]
        peripheral = [c for c in claims if c.claim_id not in selection.central_ids]
        if selection.applied:
            _emit("selected", selection.reason)
            for c in peripheral:
                _emit(
                    "skipped",
                    f"not assessed (not central): {c.subject} {c.predicate} {c.object}",
                    claim_id=c.claim_id, subject=c.subject, predicate=c.predicate,
                    object=c.object, polarity=c.polarity, verdict="not_assessed",
                )
        not_assessed_claims = [
            {"claim_id": c.claim_id, "subject": c.subject, "predicate": c.predicate,
             "object": c.object, "polarity": c.polarity}
            for c in peripheral
        ]

        # 3. Verify the CENTRAL claims CONCURRENTLY — each `verdict` event (with its
        # reasoning trace) is emitted the moment that claim's walk completes.
        # Verdicts are identical to serial (claims independent; per-walk state
        # thread-local); walk_results come back in claim order for aggregation.
        total = len(central)
        for i, claim in enumerate(central):
            _emit(
                "verifying",
                f"verifying: {claim.subject} {claim.predicate} {claim.object}",
                index=i + 1, total=total, claim_id=claim.claim_id,
            )

        def _on_result(index, claim, result):
            _emit("verdict", str(result.verdict),
                  index=index + 1, total=total,
                  **walk_result_observability(claim, result))

        walk_results = walk_claims_parallel(
            self._walker, central, verification_context,
            max_workers=verify_workers, on_result=_on_result,
        )

        # v0.16.5: promote THIS turn's user-message premises NOW — AFTER the draft
        # walk, not before it. Promotion accumulates user-asserted knowledge across
        # a session (future turns), but the current draft must never ground against
        # the very message it is answering. Before this, a yes/no QUESTION ("Is the
        # Eiffel Tower taller than the Statue of Liberty?") promoted
        # `taller_than(Eiffel, Statue)` (both entities are in the question, so the
        # source-grounding gate admits it) and the draft's matching claim then
        # self-grounded as `verified_given_assertion` ("according to your assertion")
        # at depth 0 — crediting the user with an assertion they never made. Deferring
        # the WRITE leaves the draft walk to see only PRIOR-turn standing premises.
        if pending_user_premises:
            from ..layer4_sources.promotion import promote_assertions
            promote_assertions(pending_user_premises, self._tier_u)
            _emit("premises",
                  f"recorded {len(pending_user_premises)} premise(s) from your message "
                  f"as session context")

        # 4. Aggregate (central claims only; peripheral pass through the draft)
        _emit("composing", "composing the final reply")
        vr = self._aggregator.aggregate(
            claims=central,
            per_claim_results=walk_results,
            text_input={"message": user_message, "draft": draft},
        )

        # 5. Intervention selection (per-claim plan). WS5: thread the KB
        # adapter's fetch_label so corrections can reverse-label entity Q-ids
        # ("the source indicates {label} instead"). getattr-guarded: a kb
        # without fetch_label (or no kb) yields None → raw value / generic form.
        label_fetcher = getattr(self._kb, "fetch_label", None)
        # Reconcile temporal/duplicate variants of the SAME triple to one
        # representative for composition, so a verified base fact is never struck
        # by a same-triple temporal abstention (the over-refusal bug). Raw
        # vr.claim_verdicts (the persisted per-claim observability) is unchanged.
        composed_verdicts = _reconcile_for_composition(vr.claim_verdicts)
        plan = select_interventions(composed_verdicts, label_fetcher=label_fetcher)

        # 6. Final reply (v0.16.4): a constrained rewrite that folds the per-claim
        # verdicts INTO the message (correct wrong facts to the verified value,
        # remove unverifiable claims, caveat assertion-conditional ones), instead
        # of appending an "Aedos verification notes" section. The structured
        # observability / per_claim_actions below are UNCHANGED, so the audit trail
        # carries every true per-claim verdict regardless of how the prose reads.
        if plan.overall == InterventionType.PASS_THROUGH:
            # Every claim verified — nothing to apply; the draft stands verbatim.
            final = draft
        elif plan.overall == InterventionType.DECLINE:
            # §3.2: DECLINE means ZERO independently-verified claims. There is no
            # verified spine to anchor a rewrite, and the edited prose is NOT
            # re-verified — so a free LLM rewrite here could ship unverified text
            # as the answer. The only sound coherent reply is an honest one. The
            # per-claim detail (including any corrections) remains in the
            # structured observability / per_claim_actions for the UI.
            final = "I couldn't verify enough of this to answer confidently."
        else:
            # INTERVENE: a verified spine is present. Fold the verdicts in. The
            # editor is FAIL-SAFE — error / non-str / blank output falls back to
            # the deterministic draft+notes composition (`_is_blank` catches a
            # near-empty strip-to-nothing, not just exact ""), so a reply is never
            # broken or silently blanked.
            _emit("revising", "revising the reply to reflect verification")
            revised = revise_response(
                user_message, draft, composed_verdicts, self._llm, label_fetcher
            )
            final = revised if (revised and not _is_blank(revised)) else build_response(draft, plan)

        verification_id = str(uuid.uuid4())
        self._verification_store[verification_id] = vr
        # Durable persist (observability): the full per-claim walk result, so the
        # audit endpoint survives restart with no re-walk. walk_results is claim-
        # ordered and aligned with vr.claim_verdicts (both from the same zip in
        # aggregate). Best-effort: a store failure must never break the turn.
        if self._vstore is not None:
            try:
                self._vstore.persist(
                    verification_id, asserting_party, vr,
                    source_kind="chat", created_at=current_time,
                    walk_results=walk_results,
                    chat_extras={
                        "final_message": final,
                        "intervention_type": plan.overall.value,
                        "not_assessed_claims": not_assessed_claims,
                        "selection_summary": selection.reason,
                        # Per-claim intervention actions (action_type + the composed
                        # correction/abstention/conditional annotation) so the audit
                        # record reproduces the per-claim notes the turn showed live.
                        "per_claim_actions": [
                            {"claim_id": a.claim_id,
                             "action_type": a.action_type.value,
                             "annotation": a.annotation}
                            for a in plan.per_claim_actions
                        ],
                    },
                    # EVERY draft-extracted claim (incl. extraction-abstained ones
                    # not walked) so the durable record reflects the full extraction.
                    extracted_claims=[
                        {"claim_id": c.claim_id, "subject": c.subject,
                         "predicate": c.predicate, "object": c.object,
                         "polarity": c.polarity,
                         "abstention_reason": c.abstention_reason}
                        for c in claims
                    ],
                )
            except Exception:
                _log.exception("verification_store.persist (chat) failed")

        return ChatResponse(
            final_message=final,
            intervention_plan=plan,
            verification_result=vr,
            verification_id=verification_id,
            draft_message=draft,
            not_assessed_claims=not_assessed_claims,
            selection_summary=selection.reason,
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
        # NOTE: the durable store intentionally holds the VERIFY-TIME record (the
        # faithful historical audit). We do NOT re-persist the re-derived result
        # here: persist() would clobber the core presentation fields (final_message
        # / selection_summary) the re-walk doesn't carry. The audit endpoint serves
        # the verify-time record; staleness is a separate live-query concern.
        return refreshed
