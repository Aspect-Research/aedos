"""Holistic response corrector (v0.14 Phase 8b).

Layer 5's rewrite step. Takes the assistant's draft, the user's
question, and the per-claim ``Intervention`` list from
``layer5_decision.intervention.plan_intervention``; produces a
rewritten draft via one LLM call.

Port-from-v1 status
===================

Prompt (``CORRECTOR_SYSTEM``) is copied **verbatim** from v1's
``src/corrector.py``. v1's prompt has been tuned across many turns;
the v2 architectural decision is to resist redesign during port.

The user-message ledger format is also the v1 shape:

    User's question:
    \"\"\"
    {user_message}
    \"\"\"

    Assistant's draft reply:
    \"\"\"
    {draft}
    \"\"\"

    Per-claim verification ledger:
    - {claim_inline}
      verdict: {verdict_label}
      verified value: {verified_value!r}     # only on REPLACE
      reason: {reason}
    ...

    Rewrite the assistant reply ...

Vocabulary translation at ledger time
=====================================

v1's CORRECTOR_SYSTEM prompt enumerates these verdict labels:
``verified``, ``contradicted``, ``retrieval_inconclusive``,
``unverifiable_in_principle``, ``unverifiable_pending_implementation``.

v2's eight verification statuses include three v1's prompt doesn't
mention:

  * ``user_asserted`` — v1 returned None from plan_one on this status
    so the LLM never saw it. v2 produces ``REPLACE`` when the user has
    asserted something different from the model's draft. **Translation:**
    render the verdict label as ``contradicted`` for the LLM (the
    rewriter treats it identically — the model claim is wrong; the
    verified_value is what to use). The Intervention's
    ``verification_status`` field stays ``user_asserted`` in the audit
    trail (principle 6: auditability by construction).

  * ``retrieval_failed`` — v2 produces ``NOOP``; filtered out before
    the LLM call.

  * ``routing_anomaly`` — v2 produces ``NOOP`` with ``flag_operator``;
    filtered out before the LLM call.

So the LLM only ever sees verdict labels its prompt enumerates.

Filtering
=========

The corrector filters ``pass_through`` and ``noop`` interventions out
of the LLM prompt. They don't require rewriting. If every intervention
is ``pass_through`` or ``noop``, the corrector returns the draft
unchanged without an LLM call (matches v1's
``if not interventions: return draft`` short-circuit).

The trace UI sees the full intervention list (all 5 types) at the
caller's site — this corrector does not own trace emission.
"""

from __future__ import annotations

from typing import Iterable, Optional

from src.layer5_decision.types import Intervention, InterventionType
from src.llm_client import LLMClient


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
    """Wrap an LLM client and apply v0.14 Phase 5 interventions to a draft.

    Single-method API: ``apply(draft, interventions, *, user_message)``.
    The trace UI / pipeline_events emission lives at the caller's site
    so the corrector stays focused on the rewrite.
    """

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def apply(
        self,
        draft: str,
        interventions: Iterable[Intervention],
        *,
        user_message: str = "",
    ) -> str:
        """Rewrite ``draft`` given Layer 5 interventions.

        Filters ``pass_through`` and ``noop`` out of the LLM prompt.
        If no actionable interventions remain, returns ``draft``
        unchanged without an LLM call (matches v1 short-circuit).
        """
        interventions = list(interventions)
        actionable = [
            iv for iv in interventions
            if iv.intervention_type not in (
                InterventionType.PASS_THROUGH,
                InterventionType.NOOP,
            )
        ]
        if not actionable:
            return draft

        user_msg = _format_user_message(
            draft, actionable, user_message=user_message,
        )
        return self.llm.rewrite(
            CORRECTOR_SYSTEM, user_msg, purpose="corrector",
        )


# ============================================================================
# Ledger formatting (port of v1's _format_user_message + helpers)
# ============================================================================


def _format_user_message(
    draft: str,
    interventions: list[Intervention],
    *,
    user_message: str = "",
) -> str:
    """Render the corrector's user message: question + draft + ledger.

    Mirrors v1's ``_format_user_message`` with v2 ``Intervention``
    inputs.
    """
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
    for iv in interventions:
        lines.append(_ledger_line(iv))
    lines.append("")
    lines.append(
        "Rewrite the assistant reply so it answers the user's question "
        "accurately, integrates every verified fact, gracefully handles "
        "any contradictions or unverified claims, and stays internally "
        "consistent end-to-end. Output ONLY the rewritten reply."
    )
    return "\n".join(lines)


def _ledger_line(iv: Intervention) -> str:
    """One ledger entry. Translates v2 status to v1-prompt verdict label.

    Translation rules:
      * user_asserted + REPLACE → 'contradicted' (LLM treats it the same
        way: model claim is wrong; verified_value is what to use)
      * everything else → status verbatim
    """
    claim_str = _claim_inline(iv.claim)
    verdict = _ledger_verdict_label(iv)
    parts = [f"- {claim_str}", f"  verdict: {verdict}"]
    if iv.verified_value is not None:
        parts.append(f"  verified value: {iv.verified_value!r}")
    if iv.reason:
        parts.append(f"  reason: {iv.reason}")
    return "\n".join(parts)


def _ledger_verdict_label(iv: Intervention) -> str:
    """Translate Intervention.verification_status to a v1-prompt-compatible
    verdict label for ledger rendering.

    See module docstring "Vocabulary translation at ledger time".
    """
    status = iv.verification_status
    if (
        status == "user_asserted"
        and iv.intervention_type is InterventionType.REPLACE
    ):
        return "contradicted"
    return status


def _claim_inline(claim: Optional[dict]) -> str:
    """One-line readable description of a claim. Mirrors v1's helper."""
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
