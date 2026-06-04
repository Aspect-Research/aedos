"""Assertion promotion.

The promotion step sits between Layer 1 extraction and Layer 4
verification. For every extracted user claim, it writes a Tier U row
with `status='asserted_unverified'`, so subsequent walker calls can
chain off that claim as a premise. The resulting `*_given_assertion`
verdict family preserves §3.2 soundness by making
the grounding source explicit.

`promote_assertions` is batch — it writes all rows
before returning, so the walker sees every extracted claim as a
candidate premise when verifying any one of them. Per-claim
ordering would otherwise make verdicts depend on the
extraction order, which is an implementation detail of the LLM
extractor.

§"KB wins" (cross-source contradiction): a promotion write whose
prior is `externally_verified` returns
`was_cross_source_contradicted=True` on the `WriteResult`. The
promotion step turns that into a `contradicted` verdict (NOT
`contradicted_given_assertion` — the contradiction is externally
grounded). The walker logic / aggregator threads this
pre-verdict through so the rest of the pipeline never tries to
verify a claim the KB has already refuted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..layer1_extraction.extractor import Claim
from ..layer4_sources.tier_u import TierU, WriteResult


def is_source_grounded(claim: Claim, source_text: str) -> bool:
    """v0.16.3: promotion-eligibility gate. A claim may become an
    `asserted_unverified` Tier U premise ONLY when the entities that define its
    relation — its SUBJECT and its OBJECT — are grounded in the SOURCE TEXT (what
    the party actually wrote).

    Motivation: the user-message extractor over-extracts a QUESTION into a fully
    answered assertion — "What is the capital of France?" → (France, capital,
    **Paris**) where the object "Paris" is the LLM's ANSWER, absent from the
    source. The pre-existing extractor guard (_passes_hard_claim_check) is an OR
    (subject OR object present), so a fabricated object slips through whenever the
    subject is named. Promoting that fabricated answer makes a later draft "ground"
    against the system's own guess (verified_given_assertion, KB bypassed). This
    gate is the AND: both the subject and the object must appear in the source, so
    only genuine stipulations ("France's capital is Paris" — both present) promote.

    - Checked against the REAL source text (the party's message), not the claim's
      LLM-emitted `source_text` span (which the model controls and could pad with
      the fabricated answer).
    - First-person subjects are canonicalized to the asserting party at extraction
      ("I prefer X" → subject == asserting_party); they refer to the speaker and
      are inherently grounded.
    - An EMPTY object is not a fabricated answer (no LLM-supplied entity), so it is
      not blocked here — such a claim cannot self-ground a later non-empty draft
      claim anyway (Tier U matches on the object).

    KNOWN LIMITATIONS (surfaced, all fail SAFE — feature-completeness loss only,
    never a soundness issue; the claim still verifies normally when it appears,
    and every case errs toward NOT promoting, the conservative direction). The
    object check is a verbatim case-insensitive substring, so a genuine assertion
    is NOT promoted whenever the LLM's OBJECT string diverges from the source span:
      - normalization/expansion: a decade "the 70s" → "1970s", an abbreviation
        "NYC" → "New York City" / "the US" → "United States", a currency "$5,000"
        → "5000";
      - demonym → country: "X is German" → has_nationality(X, "Germany") (the
        country name is not the demonym in the source);
      - first-person OBJECTS: "she praised me" canonicalizes the object to the
        asserting party id, which is not a substring of the source (the
        first-person exemption below covers the SUBJECT slot only).
    Conversely, this gate does NOT close a yes/no question that NAMES the answer
    ("Is Paris the capital of France?" — both entities present, so it promotes):
    distinguishing that from a stipulation needs speech-act detection, which is a
    deliberately broader change out of scope here. The resulting verdict still
    carries the honest `_given_assertion` contingency marker."""
    text = (source_text or "").lower()
    if not text:
        return False
    subj = (claim.subject or "").strip()
    obj = (claim.object or "").strip()
    subject_grounded = (
        bool(subj)
        and (subj == claim.asserting_party or subj.lower() in text)
    )
    # Empty object → not an LLM-supplied entity → not blocked. Non-empty object
    # must appear verbatim (case-insensitive) in the source.
    object_grounded = (not obj) or (obj.lower() in text)
    return subject_grounded and object_grounded


@dataclass
class PromotionResult:
    """One per claim promoted.

    `tier_u_row_id` — the row id created (or the existing row's id, if
                       the claim was idempotent with a prior).
    `pre_verdict`    — when the promotion step decides the verdict
                       without needing the walker:
                         - "contradicted" when §"KB wins" fired (an
                           externally_verified row contradicts this
                           assertion); plain `contradicted`, NOT
                           `contradicted_given_assertion`.
                         - None when the walker should verify
                           normally.
    `write_result`   — the underlying `WriteResult` for caller
                       inspection (audit, debugging, tests).
    """
    claim: Claim
    tier_u_row_id: int
    pre_verdict: Optional[str] = None
    write_result: Optional[WriteResult] = None


def promote_assertions(
    claims: list[Claim],
    tier_u: TierU,
) -> list[PromotionResult]:
    """Promote every extracted claim into Tier U as `asserted_unverified`.

    Returns a `PromotionResult` per claim, in input order. Callers
    that produce a verdict per claim (the corpus runner, the chat-
    wrapper, ad-hoc evaluations) consult `pre_verdict` first; if it is
    set, that is the verdict for the claim and the walker is not
    invoked. Otherwise the caller proceeds to walker verification.

    Q-MultiClaim: this is a single batch — all rows are written before
    the function returns, so subsequent walker calls see every
    promoted claim. A claim that idempotently matches an existing row
    is not re-written; its row id is returned and the `was_idempotent`
    flag on the WriteResult records the no-op.

    The `cross_source_contradiction` audit event is emitted by
    `TierU.write` itself (step 1); this function does not re-emit
    duplicate audit events.
    """
    results: list[PromotionResult] = []
    for claim in claims:
        wr = tier_u.write(claim, status="asserted_unverified")
        pre_verdict: Optional[str] = None
        if wr.was_cross_source_contradicted:
            # §"KB wins": the asserted claim conflicts with an
            # externally-verified Tier U row. The promotion still
            # wrote the row (for audit), but the row's effective
            # status is `contradicted_by_externally_verified` and the
            # verdict for the claim is plain `contradicted` — the
            # contradiction is externally grounded, not assertion-
            # contingent.
            pre_verdict = "contradicted"
        results.append(PromotionResult(
            claim=claim,
            tier_u_row_id=wr.row_id,
            pre_verdict=pre_verdict,
            write_result=wr,
        ))
    return results
