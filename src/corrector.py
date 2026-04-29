"""Response corrector with per-claim intervention planning.

The corrector now decides, per claim, what kind of edit (if any) the
assistant draft needs:

    verified / user_asserted   → noop          (no intervention)
    contradicted               → REPLACE       (use the verified value)
    unverifiable_pending_…     → HEDGE         (insert a verification hedge)
    unverifiable_in_principle  → SOFTEN        (predictive language)
    routing_anomaly            → noop          (logged separately by pipeline)

Multiple interventions on the same draft are batched into a single LLM
rewrite. If every claim is verified or otherwise needs no intervention,
the LLM is not called at all and the draft is returned verbatim.

The decision logic is deterministic and testable. Only the rewrite step
calls the LLM.
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


CORRECTOR_SYSTEM = """You apply targeted edits to an assistant response.

Each intervention names a specific claim and the kind of edit needed.

Intervention types:
- hedge: the claim was not verified by any source. Add a hedge near it
  ("I believe...", "as of my last training data...", "you may want to
  verify with a current source"). Do NOT delete the claim itself.
- replace: the claim is wrong. Replace the wrong value with the verified
  one wherever it appears in the response.
- soften: the claim is an unverifiable prediction stated as if certain.
  Soften with words like "might", "could", "is expected to". If the
  source text is already adequately hedged, leave it alone.
- remove: rare; only delete the claim if the instruction explicitly says
  remove. Otherwise prefer hedge.

CRITICAL — internal consistency for ``replace`` interventions:

When you change a verified_value, you MUST also fix every adjacent
sentence in the response that contradicts the new value. The wrong
value often gets restated, expanded, or used as a premise nearby —
those sentences become internally inconsistent the moment you change
the number/name/list at the named source_text.

Concrete example: original draft says "there are 2 words with 3+
vowels in your prompt. These are likely: 'Donald,' 'children,'
'prompt,' 'vowels'." The verified_value is 0. You must rewrite BOTH
the count AND the listing — leaving "These are likely: ..." untouched
produces a response that says "0" and then immediately enumerates 4
words. That's worse than no correction.

When in doubt, rewrite the smallest enclosing paragraph (or section,
if the contradiction spans paragraphs) so the verified value is the
single source of truth.

Other rules:
- Preserve voice, tone, and structure where you can. Don't restructure
  unaffected sections.
- Do NOT apologize, narrate the correction, or add "actually" / "to be
  precise" preludes.
- Output ONLY the rewritten response. No preamble, no explanation.
- If multiple interventions apply, do them all in one rewritten
  response."""


class Corrector:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    # ---- planning -------------------------------------------------------

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

    def apply(self, draft: str, interventions: Iterable[Intervention]) -> str:
        interventions = list(interventions)
        if not interventions:
            return draft

        return self.llm.rewrite(CORRECTOR_SYSTEM, _format_user_message(draft, interventions),
                                 purpose="corrector")

def _format_user_message(draft: str, interventions: list[Intervention]) -> str:
    lines = [
        "Original response:",
        '"""',
        draft,
        '"""',
        "",
        f"Apply these {len(interventions)} intervention(s) in a single rewrite:",
        "",
    ]
    for i, iv in enumerate(interventions, 1):
        c = iv.claim
        triple = (
            f"({c.get('subject', '?')}, "
            f"{c.get('predicate', '?')}, "
            f"{c.get('object', '?')}, "
            f"polarity={c.get('polarity', '?')})"
        )
        src = (c.get("source_text") or "").strip() or "(no source text recorded)"
        lines.append(f"{i}. [{iv.intervention_type}] claim={triple}")
        lines.append(f"   verification_status: {iv.verification_status}")
        lines.append(f"   source_text: {src!r}")
        if iv.intervention_type == INTERVENTION_REPLACE and iv.verified_value is not None:
            lines.append(f"   verified_value: {iv.verified_value!r}")
        lines.append(f"   reason: {iv.reason}")
        lines.append("")
    # If any intervention is a replace, prominently restate the verified
    # values at the bottom so the model has a checklist to consult while
    # scanning the rest of the response for contradicting prose.
    replaces = [iv for iv in interventions
                if iv.intervention_type == INTERVENTION_REPLACE
                and iv.verified_value is not None]
    if replaces:
        lines.append("Verified values that the rewritten response must agree with:")
        for iv in replaces:
            c = iv.claim
            slot_subj = (c.get("slots") or {}).get("subject") or c.get("subject")
            slot_prop = (c.get("slots") or {}).get("property") or c.get("predicate")
            descriptor = (
                f"{slot_subj}.{slot_prop}" if slot_subj and slot_prop
                else c.get("predicate", "?")
            )
            lines.append(f"  - {descriptor} = {iv.verified_value!r}")
        lines.append("")
    lines.append(
        "Apply the interventions and ensure the response is internally "
        "consistent with each verified value. Fix adjacent sentences "
        "that would contradict the new value (counts, lists, examples, "
        "premises). Preserve voice and structure where you can. Return "
        "ONLY the rewritten response."
    )
    return "\n".join(lines)
