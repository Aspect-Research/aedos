"""Central-claim selection (v0.16.2 Phase D).

A chat draft can extract many claims, most tangential to the user's actual
question. This picks the subset CENTRAL to answering the prompt so only those are
verified — the efficiency win. Skipping a non-central claim emits NO verdict on it
(it passes through unflagged, like an abstain), so this never false-verifies or
false-contradicts: selection errors are safe in both directions.

FAIL-OPEN to "verify everything": a selector error, an empty result, or an
unparseable response all fall back to selecting ALL claims. The central claims
"should definitely be verified if any response is returned", so the system never
skips verification on a selector failure.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..llm.client import ChatMessage

_SYSTEM = (
    "You decide which extracted factual claims are CENTRAL to answering a user's "
    "question. A claim is central if it directly answers the question or is part "
    "of that answer. Background, tangential, or incidental claims (unrelated facts "
    "about the subject) are NOT central. Be INCLUSIVE: if a claim could plausibly "
    "be part of the answer, include it. ALWAYS include any claim that establishes "
    "WHO or WHAT the answer is about — its core identity, role, title, office, or "
    "type (e.g. 'X is the president', 'X holds the role pope', 'X is a river') — "
    "because the correctness of the rest of the answer depends on that being right. "
    "Respond with ONLY a JSON array of the central claim numbers, e.g. [1,3,4] — "
    "nothing else."
)


@dataclass
class Selection:
    central_ids: set
    applied: bool   # True iff the selector narrowed the set; False = verifying all
    reason: str


def _parse_selected_numbers(text: str) -> list[int] | None:
    """Parse the first JSON array of integers from the model's reply. Returns None
    when no clean array is found — we then FAIL OPEN to verifying everything rather
    than trust a loose integer scrape that could pick the wrong claims."""
    match = re.search(r"\[[^\[\]]*\]", text, re.DOTALL)
    if not match:
        return None
    try:
        arr = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(arr, list):
        return None
    nums: list[int] = []
    for x in arr:
        try:
            nums.append(int(x))
        except (ValueError, TypeError):
            continue
    return nums or None


def select_central_claims(
    llm,
    question: str,
    draft: str,
    claims: list,
    *,
    min_claims: int = 4,
    enabled: bool = True,
) -> Selection:
    """Choose the claims central to answering `question`. Fails open to ALL."""
    all_ids = {c.claim_id for c in claims}
    if not enabled:
        return Selection(all_ids, applied=False, reason="selection disabled — verifying all")
    if len(claims) <= min_claims:
        return Selection(
            all_ids, applied=False,
            reason=f"only {len(claims)} claim(s) — verifying all (nothing to narrow)",
        )

    numbered = "\n".join(
        f"{i + 1}. {c.subject} {c.predicate} {c.object}" for i, c in enumerate(claims)
    )
    user = (
        f"User question:\n{question}\n\n"
        f"Draft answer:\n{draft}\n\n"
        f"Claims extracted from the draft (one per line):\n{numbered}\n\n"
        "Return ONLY a JSON array of the numbers of the claims that are central to "
        "answering the user's question."
    )
    try:
        raw = llm.chat(
            system=_SYSTEM,
            messages=[ChatMessage(role="user", content=user)],
            max_tokens=256,
            purpose="deployment:claim_selection",
        )
    except Exception as exc:  # any LLM/transport failure -> verify all
        return Selection(all_ids, applied=False, reason=f"selector error ({type(exc).__name__}) — verifying all")

    nums = _parse_selected_numbers(raw or "")
    if not nums:
        return Selection(all_ids, applied=False, reason="unparseable selection — verifying all")

    central = {claims[i - 1].claim_id for i in nums if 1 <= i <= len(claims)}
    if not central:
        return Selection(all_ids, applied=False, reason="empty selection — verifying all")
    return Selection(
        central_ids=central, applied=True,
        reason=f"{len(central)} of {len(claims)} claim(s) central to the question",
    )
