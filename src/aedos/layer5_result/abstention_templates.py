"""v0.16.2 observability: templated human lines for the CLOSED abstain bucket-set.

Every abstain/abstention reason the engine can emit has a one-line, operator-
meaningful template here. Three layers contribute reasons, and this is their
union (the `test_abstention_templates.py` guard scans the source and fails CI if a
new reason appears without a template):

  - EXTRACTION (triage.AbstentionReason `.value`): a claim short-circuits before
    the walk (malformed / not-checkworthy). Surfaces as `Claim.abstention_reason`
    AND, via the walker short-circuit, as `WalkResult.abstention_reason`.
  - WALK (walker.py bare strings): the walk's own abstentions — budget exhaustion,
    depth exhaustion, an unresolvable/vague subject. This is the usual
    `WalkResult.abstention_reason` (hence `verification_claim.abstention_reason`).
  - KB-VERIFIER (kb_verifier.py bare strings, in the trace): why a specific KB
    binding did not ground. These live in the trace edges / per-binding trace, not
    usually as the top-level claim reason, but are templated here so any surface
    that renders them has a line.
  - AGGREGATOR: `circuit_breaker_triggered`.

§3.2-neutral: this is presentation only. It never reads or changes a verdict; an
unknown reason degrades to a generic line rather than raising.
"""
from __future__ import annotations

from typing import Any, Optional

# Reason code -> human line template. A template MAY reference {subject},
# {predicate}, {object} (filled from the claim when available); plain templates
# ignore them. Keep each line concise and operator-meaningful.
ABSTENTION_TEMPLATES: dict[str, str] = {
    # --- extraction layer (AbstentionReason.value) ---
    "self_referential": "Not externally checkable — the claim refers to itself.",
    "predicate_eq_object": "Vacuous — the predicate just restates the object.",
    "content_less_event": "No factual content to verify (deprecated reason, never emitted).",
    "subject_absent_from_source": "The subject “{subject}” does not appear in the source text.",
    "not_checkworthy": "Inert prose — nothing factual to verify.",
    # --- walk layer (walker.py) ---
    "user_subject_required": "Needs a user-scoped subject; no asserting-party context to ground against.",
    "vague_subject_existential": "The subject “{subject}” is too vague to resolve to a specific entity.",
    "depth_exhausted": "Search exhausted every grounding path without confirming or contradicting the claim.",
    "budget_wall_clock": "Abstained: the wall-clock budget was exhausted before grounding.",
    "budget_llm_calls": "Abstained: the LLM-call budget was exhausted before grounding.",
    "budget_kb_work": "Abstained: the knowledge-base round-trip budget was exhausted.",
    "budget_kb_neighbor_probes": "Abstained: the KB neighbor-probe budget was exhausted.",
    "budget_fanout": "Abstained: the discovery fan-out budget was exhausted.",
    # --- aggregator ---
    "circuit_breaker_triggered": "Abstained: the consistency circuit-breaker tripped on a repeated unresolvable cycle.",
    # --- KB-verifier layer (kb_verifier.py; usually in the trace, not the top reason) ---
    "unsupported_slot_to_qualifier": "The predicate's slot mapping is one the verifier cannot interpret.",
    "lookup_subject_unresolved": "Could not resolve “{subject}” to a knowledge-base entity.",
    "no_statements": "The resolved subject carries no knowledge-base statement for this property.",
    "value_type_incompatible_binding": "The object's type does not satisfy this property's value-type, so no contradiction can be drawn.",
    "value_type_unconfirmed_positive_gate": "Could not confirm the object's type for this candidate property, so it does not verify.",
    "value_unresolved": "Could not resolve the object “{object}” to a knowledge-base entity.",
    "no_matching_statement": "No knowledge-base statement matched the claimed value.",
    "multi_valued_single_valued_predicate": "The subject holds multiple values for a single-valued predicate; cannot adjudicate.",
    "value_type_object_type_mismatch": "The knowledge-base value's type does not match the claim's object type.",
    "entity_claim_vs_literal_value": "The claim names an entity but the knowledge-base value is a literal; not a like-for-like comparison.",
    "approximate_date_no_year_match": "The dates share no common year, but the comparison is too approximate to contradict.",
    "date_not_a_clean_mismatch": "The dates differ only below year precision (a placeholder), so this is not a clean mismatch.",
}


def abstention_line(
    reason: Any,
    *,
    subject: Optional[str] = None,
    predicate: Optional[str] = None,
    object: Optional[str] = None,
) -> Optional[str]:
    """Render the human abstain line for a reason code, or None when there is no
    reason (a non-abstaining verdict). Forward-safe: a reason without a template
    (the bucket-set is closed but the walk/KB layers add bare strings without
    ceremony) renders a generic line rather than raising. Never raises on a bad
    format field. §3.2-neutral."""
    if reason is None:
        return None
    # Normalize an AbstentionReason enum member to its string value.
    code = getattr(reason, "value", reason)
    if not isinstance(code, str):
        code = str(code)
    template = ABSTENTION_TEMPLATES.get(code)
    fields = {
        "subject": subject if subject is not None else "the subject",
        "predicate": predicate if predicate is not None else "the predicate",
        "object": object if object is not None else "the object",
    }
    if template is None:
        return f"Could not verify (reason: {code})."
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError):
        # A template referencing an unknown field must never crash the read path.
        return template
