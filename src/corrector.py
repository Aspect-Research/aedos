"""Holistic response corrector.

Pre-v0.11 the corrector took a draft + a list of per-claim "intervention"
edit instructions (REPLACE this value, HEDGE that claim, etc.) and asked
the LLM to apply them surgically. That worked for single-fact
corrections but fell apart on cascades:

  * Replacing a count without rewriting the enumeration that the count
    referred to ("there are 0 vowels: a, e, i, o, u").
  * Substituting a derived value with an unrelated lookup ("9:56 pm in
    Cairo → 11:13 am in Cairo" while the surrounding sentence still
    says "given NY is 2:56 pm, that means…").
  * Flipping a sign in a comparison without flipping the comparison
    direction ("7 hours ahead" → "-7 hours ahead").

v0.11 reframes the corrector. It now does ONE LLM call that sees:

  * The user's question (so the answer can be re-derived if needed).
  * The assistant's draft.
  * The full per-claim verification ledger (verdicts + verified values
    + verifier reasoning).

…and writes a fresh response that integrates the verifier's findings
holistically. The intervention planner still runs (its output drives
the trace UI's correction card and lets us skip the LLM call when
nothing was contradicted), but it no longer prescribes line edits.
The model is told to think about the whole answer, not to substitute.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from src.llm_client import LLMClient
from src.router import Decision

# Intervention type values are deliberately a fixed string set, not an enum,
# so the prompt can include them inline as keywords.
INTERVENTION_HEDGE = "hedge"
INTERVENTION_REPLACE = "replace"
INTERVENTION_SOFTEN = "soften"
INTERVENTION_REMOVE = "remove"


@dataclass
class Intervention:
    intervention_type: str
    claim: dict
    verification_status: str
    reason: str
    verified_value: Optional[Any] = None  # only meaningful for `replace`

    def to_dict(self) -> dict[str, Any]:
        return {
            "intervention_type": self.intervention_type,
            "claim": self.claim,
            "verification_status": self.verification_status,
            "reason": self.reason,
            "verified_value": self.verified_value,
        }


CORRECTOR_SYSTEM = """You rewrite an assistant's draft reply so it's accurate and coherent given pipeline verification feedback.

You will receive:

  1. The user's question (the message the assistant was responding to).
  2. The assistant's draft reply.
  3. A per-claim verification ledger — each factual claim the draft made,
     paired with the verifier's verdict (verified / contradicted /
     inconclusive / unverifiable) and, for contradicted claims, what the
     verifier actually computed.

Your job: write a NEW reply that answers the user's question correctly
given the verifier's findings. Treat this as a real rewrite, not as
applying surgical edits.

# Principles

- **Think holistically.** When a claim was contradicted, don't just
  swap the wrong value for the right one in place. Think about what
  the user asked, what's actually true, and what the right answer
  looks like end-to-end. If the draft's reasoning chain depends on a
  contradicted premise, REDERIVE the answer from the verified facts.

- **Internal consistency is non-negotiable.** If you change a number,
  also fix every list, enumeration, sub-claim, or follow-up sentence
  that built on the old number. A response that says "0 vowels in
  'apple'" and then enumerates "a, e" is worse than the original.

- **Conditional claims need their premises.** If the draft said "if X
  then Y" and the verifier returned a value for Y under different
  assumptions, the right move is usually to either (a) restate the
  conditional with the correct arithmetic, or (b) note the actual
  value alongside, NOT to silently replace Y with an unrelated value
  that breaks the if-then logic.

- **Verdicts are signals, not commands.** `verified` and
  `contradicted` verdicts are load-bearing — treat them as ground
  truth. But `retrieval_inconclusive`, `unverifiable_in_principle`,
  and `unverifiable_pending_implementation` mean the verifier
  couldn't reach a conclusion either way. They're a suggestion to
  hedge, not a command. Apply your own judgment:

  - **If the claim is widely-accepted common knowledge** — medical
    basics ("excess water consumption can cause hyponatremia"),
    definitional or lexical relationships ("sipping is a drinking
    method"), established physiological / scientific facts ("a
    typical human can drink roughly 0.5–1 gallons of water per
    minute"), well-known causal chains — keep the original
    phrasing. Stacking hedges on every soft verdict ("I think",
    "approximately", "you may want to verify") on top of claims
    every reader would accept makes a correct reply read as wrong
    and erodes trust faster than the occasional uncorrected
    near-miss.
  - **If the claim is genuinely uncertain to you** — a specific
    number you don't recognize, an obscure entity, an unfamiliar
    causal chain, or a fact you'd want to look up yourself — then
    hedge it ("I think", "roughly", "you may want to confirm").
    Don't drop it silently; don't present it as confirmed.

  Reserve hedges for meaningful unknowns. The verdict tells you
  what UPSTREAM couldn't check; YOU decide whether the claim is
  actually uncertain in the rewritten reply. The reason line on
  each ledger entry exists so you can judge that.

- **Preserve voice and structure** where the verifier didn't
  contradict anything. The user wrote a question expecting a certain
  shape of answer; don't restructure unaffected sections.

- **Don't narrate the correction.** No "actually", no "to be
  precise", no "I made an error in my previous draft". The user
  doesn't see the draft — only your rewrite. Just write the right
  answer.

- **Don't reference the verifier itself.** No "the comparator says",
  no "external sources confirm". The pipeline's mechanics are
  invisible to the user. State the facts directly.

# Special case: verified-only ledger

If every claim in the ledger is `verified` (or `user_asserted`), the
draft is fine — return it unchanged. You'll still have been called,
so just echo the draft verbatim.

# Output

Return ONLY the rewritten reply. No preamble, no explanation, no
markdown fences. The first line of your output is the first line of
the new reply."""


class Corrector:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    # ---- planning -------------------------------------------------------
    #
    # The intervention planner is unchanged — it still classifies each
    # Decision into hedge / replace / soften / noop. v0.11 doesn't use
    # the result to drive line edits any more, but the trace UI still
    # renders an "interventions applied" list so the operator can see
    # what the corrector reacted to. The list is also a fast skip
    # signal: when it's empty, we don't bother calling the LLM.

    def plan_interventions(self, decisions: Iterable[Decision]) -> list[Intervention]:
        """Decide what edit (if any) each Decision implies.

        Routing-anomaly decisions return None here — the pipeline logs
        them separately as their own pipeline_event.
        """
        out: list[Intervention] = []
        for d in decisions:
            intervention = self._plan_one(d)
            if intervention is not None:
                out.append(intervention)
        return out

    def _plan_one(self, d: Decision) -> Intervention | None:
        status = d.verification_status

        # Verified or user-asserted: keep as written.
        if status in ("verified", "user_asserted"):
            return None

        # Routing anomaly: logged separately by the pipeline as a warning.
        # NOT a content edit — the bug is upstream, not in the response.
        if status == "routing_anomaly":
            return None

        # Verifier failure: the verifier did not produce useful signal
        # (network error, no results, judge couldn't parse). DO NOT hedge —
        # adding "I think" to a possibly-true claim is worse than leaving
        # it as-is. The pipeline logs this as a verifier_failure event.
        if status == "retrieval_failed":
            return None

        # Genuinely unconfirmed: the verifier RAN, found evidence, and the
        # judge said "insufficient". This is positive signal of uncertainty.
        if status == "retrieval_inconclusive":
            return Intervention(
                intervention_type=INTERVENTION_HEDGE,
                claim=d.claim,
                verification_status=status,
                reason="retrieval found evidence but judge couldn't confirm; hedge",
            )

        if status == "contradicted":
            corrected = (d.correction or {}).get("corrected_object")
            explanation = (d.correction or {}).get(
                "explanation", "a verifier contradicted this claim"
            )
            return Intervention(
                intervention_type=INTERVENTION_REPLACE,
                claim=d.claim,
                verification_status=status,
                verified_value=corrected,
                reason=explanation,
            )

        if status == "unverifiable_pending_implementation":
            # Catch-all for python verifier inconclusive / store-lookup miss /
            # similar runtime failures. Confidence threshold gives us a knob.
            if d.confidence < 0.5:
                return Intervention(
                    intervention_type=INTERVENTION_HEDGE,
                    claim=d.claim,
                    verification_status=status,
                    reason="verifier returned no conclusive evidence",
                )
            return None

        if status == "unverifiable_in_principle":
            return Intervention(
                intervention_type=INTERVENTION_SOFTEN,
                claim=d.claim,
                verification_status=status,
                reason=(
                    "predicate is unverifiable by design; soften any "
                    "definite framing"
                ),
            )

        return None  # unknown status — be conservative, don't intervene

    # ---- application ----------------------------------------------------

    def apply(
        self,
        draft: str,
        interventions: Iterable[Intervention],
        *,
        user_message: str = "",
        decisions: Iterable[Decision] | None = None,
    ) -> str:
        """Holistic rewrite given the user's question, the draft, and
        the per-claim verification ledger.

        The legacy positional signature ``apply(draft, interventions)``
        still works — the new ``user_message`` and ``decisions``
        parameters are keyword-only with safe defaults. When
        ``decisions`` is None, the ledger is reconstructed from the
        interventions (a degraded view, since verified claims aren't
        in the intervention list — but enough to drive the rewrite).
        """
        interventions = list(interventions)
        if not interventions:
            return draft
        decisions_list = list(decisions) if decisions is not None else None
        user_msg = _format_user_message(
            draft, interventions,
            user_message=user_message,
            decisions=decisions_list,
        )
        return self.llm.rewrite(CORRECTOR_SYSTEM, user_msg, purpose="corrector")


def _claim_inline(claim: dict | None) -> str:
    """One-line readable description of a claim for the ledger."""
    if not claim:
        return "(no claim payload)"
    src = (claim.get("source_text") or "").strip()
    if src:
        return src
    pattern = claim.get("pattern", "?")
    predicate = claim.get("predicate", "?")
    slots = claim.get("slots") or {}
    slot_str = ", ".join(f"{k}={v!r}" for k, v in slots.items())
    return f"[{pattern}] {predicate}({slot_str})"


def _ledger_line_for_decision(d: Decision) -> str:
    """Render one verification-ledger entry for the rewrite prompt.

    Each line names the claim, its verdict, the verified value (when
    contradicted), and a SPECIFIC reason — pulled from upstream
    artifacts where available so the corrector can apply judgment
    instead of treating every soft verdict identically. v0.12.x
    Phase-3 change: surface the actual router reason (e.g. "Vacuous
    lexical tautology") and judge justification (e.g. "snippets
    describe X and Y separately") rather than the generic
    placeholders the older code used."""
    status = d.verification_status
    claim_str = _claim_inline(d.claim)
    parts = [f"- {claim_str}", f"  verdict: {status}"]
    if status == "contradicted":
        corrected = (d.correction or {}).get("corrected_object")
        if corrected is not None:
            parts.append(f"  verified value: {corrected!r}")
        explanation = (d.correction or {}).get("explanation")
        if explanation:
            parts.append(f"  reason: {explanation}")
    elif status == "retrieval_inconclusive":
        # The judge's justification names the SPECIFIC gap — use it
        # so the corrector can decide whether the gap matters for
        # this claim or whether it's common knowledge worth keeping.
        justification = _judge_justification(d)
        if justification:
            parts.append(f"  reason: judge said: {justification}")
        else:
            parts.append(
                "  reason: retrieval found evidence but couldn't confirm"
            )
    elif status == "unverifiable_pending_implementation":
        parts.append("  reason: verifier returned no conclusive result")
    elif status == "unverifiable_in_principle":
        # The router's reason distinguishes "vacuous tautology" from
        # "future prediction" from "aesthetic judgment" — each calls
        # for a different rewrite behavior. Surface it.
        router_reason = (d.routing_decision or {}).get("reason", "").strip()
        if router_reason:
            parts.append(f"  reason: router said: {router_reason}")
        else:
            parts.append(
                "  reason: this predicate isn't verifiable in principle"
            )
    return "\n".join(parts)


def _judge_justification(d: Decision) -> str:
    """Pull the judge's verdict justification out of the retrieval
    result, when available. Tolerates both the RetrievalResult dataclass
    and the plain-dict shape the cache-as-evidence path uses."""
    rr = d.retrieval_result
    if rr is None:
        return ""
    verdict = getattr(rr, "verdict", None)
    if verdict is None and isinstance(rr, dict):
        verdict = rr.get("verdict")
    if verdict is None:
        return ""
    justification = getattr(verdict, "justification", None)
    if justification is None and isinstance(verdict, dict):
        justification = verdict.get("justification")
    return (justification or "").strip()


def _ledger_line_from_intervention(iv: Intervention) -> str:
    """Fallback when no Decision objects were threaded through. Builds
    the same shape of line directly from an Intervention so the prompt
    stays uniform."""
    claim_str = _claim_inline(iv.claim)
    status = iv.verification_status
    parts = [f"- {claim_str}", f"  verdict: {status}"]
    if iv.verified_value is not None:
        parts.append(f"  verified value: {iv.verified_value!r}")
    if iv.reason:
        parts.append(f"  reason: {iv.reason}")
    return "\n".join(parts)


def _format_user_message(
    draft: str,
    interventions: list[Intervention],
    *,
    user_message: str = "",
    decisions: list[Decision] | None = None,
) -> str:
    """Render the corrector's user message: question + draft + ledger.

    Ledger rows come from the full Decision list when available (so
    verified claims are visible too — the LLM should know what's
    confirmed, not just what needs fixing). Falls back to the
    intervention list when the caller didn't pass decisions."""
    lines: list[str] = []
    if user_message.strip():
        lines.append("User's question:")
        lines.append('"""')
        lines.append(user_message.strip())
        lines.append('"""')
        lines.append("")
    lines.append("Assistant's draft reply:")
    lines.append('"""')
    lines.append(draft)
    lines.append('"""')
    lines.append("")
    lines.append("Per-claim verification ledger:")
    if decisions is not None and decisions:
        for d in decisions:
            lines.append(_ledger_line_for_decision(d))
    else:
        for iv in interventions:
            lines.append(_ledger_line_from_intervention(iv))
    lines.append("")
    lines.append(
        "Rewrite the assistant reply so it answers the user's question "
        "accurately, integrates every verified fact, gracefully handles "
        "any contradictions or unverified claims, and stays internally "
        "consistent end-to-end. Output ONLY the rewritten reply."
    )
    return "\n".join(lines)
