"""Store-backed verification (v0.3 — pattern + slots).

When the router needs to check whether a model-asserted claim matches
something the user previously said, it queries by ``(pattern, predicate,
key_slots)`` and looks for an exact polarity match (verified) or an
opposite-polarity match (contradicted).

v0.14 Phase 8.6: lookups filter on ``asserted_by="user"``. The
architectural commitment is that Tier 2 (the user store) holds
*user-asserted* facts; corrected values from python verification
(``asserted_by="python_verifier"``) live in the same SQLite table for
historical reasons but DO NOT belong to the user microtheory. Without
this filter, a python_verifier-asserted fact matched on subsequent
turns and the trace mis-badged the result as "served from user_store
(matched user fact id=N)" — a fact the user never asserted. The
filter aligns the lookup with the architecture: only user-asserted
rows are eligible for Tier 2 matches; verifier outputs flow to Tier 3
(world cache) instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from src.legacy.fact_store import DEFAULT_USER_ID, Fact, FactStore


class StoreLookupOutcome(str, Enum):
    MATCH = "match"
    CONTRADICTION = "contradiction"
    MISS = "miss"


@dataclass
class StoreLookupResult:
    outcome: StoreLookupOutcome
    matching_fact: Optional[Fact] = None
    contradicting_fact: Optional[Fact] = None

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "matching_fact_id": self.matching_fact.id if self.matching_fact else None,
            "contradicting_fact_id": (
                self.contradicting_fact.id if self.contradicting_fact else None
            ),
        }


def store_lookup_verify(
    claim: dict,
    store: FactStore,
    *,
    key_slot_names: list[str],
    user_id: str = DEFAULT_USER_ID,
) -> StoreLookupResult:
    """Look for matching or contradicting prior USER-ASSERTED facts.

    ``key_slot_names`` lists which slots define identity. For
    ``preference`` that's [agent, object]; for ``spatial_temporal`` it's
    [entity, location]; etc. The router supplies these from the pattern.

    ``user_id`` scopes the lookup so a user-asserted fact for one user
    doesn't satisfy another user's same-shaped claim.

    **Phase 8.6 — asserted_by="user" filter.** This function defines
    Tier 2 of the verification stack (user store). It returns matches
    against user-asserted facts ONLY; rows authored by other paths
    (``python_verifier``, ``model``, ``external``) are invisible here.
    Those non-user-asserted rows live in the same table for storage
    reasons but belong to other tiers — python_verifier outputs flow
    to the verification cache (Tier 3); model claims are model
    bookkeeping. Pre-Phase-8.6 this filter was absent, and a
    python_verifier-asserted "corrected value" silently matched as if
    user-asserted, mis-badging downstream traces as "served from user
    store" when the user had never made the claim. See module
    docstring for the bug context.
    """
    slots = claim.get("slots", {})
    key_slots = {k: slots[k] for k in key_slot_names if k in slots}
    polarity = int(claim["polarity"])
    pattern = claim["pattern"]
    predicate = claim["predicate"]

    same = store.find_currently_valid(
        pattern, predicate=predicate, slot_match=key_slots,
        polarity=polarity, user_id=user_id,
        asserted_by="user",
    )
    if same:
        return StoreLookupResult(StoreLookupOutcome.MATCH, matching_fact=same[0])

    opposite = store.find_contradictions(
        pattern, predicate, key_slots, polarity, user_id=user_id,
        asserted_by="user",
    )
    if opposite:
        return StoreLookupResult(
            StoreLookupOutcome.CONTRADICTION, contradicting_fact=opposite[0]
        )
    return StoreLookupResult(StoreLookupOutcome.MISS)
