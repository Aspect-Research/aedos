"""Store-backed verification (v0.3 — pattern + slots).

When the router needs to check whether a model-asserted claim matches
something the user previously said, it queries by ``(pattern, predicate,
key_slots)`` and looks for an exact polarity match (verified) or an
opposite-polarity match (contradicted).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from src.fact_store import DEFAULT_USER_ID, Fact, FactStore


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
    """Look for matching or contradicting prior facts.

    ``key_slot_names`` lists which slots define identity. For
    ``preference`` that's [agent, object]; for ``spatial_temporal`` it's
    [entity, location]; etc. The router supplies these from the pattern.

    ``user_id`` scopes the lookup so a user-asserted fact for one user
    doesn't satisfy another user's same-shaped claim.
    """
    slots = claim.get("slots", {})
    key_slots = {k: slots[k] for k in key_slot_names if k in slots}
    polarity = int(claim["polarity"])
    pattern = claim["pattern"]
    predicate = claim["predicate"]

    same = store.find_currently_valid(
        pattern, predicate=predicate, slot_match=key_slots,
        polarity=polarity, user_id=user_id,
    )
    if same:
        return StoreLookupResult(StoreLookupOutcome.MATCH, matching_fact=same[0])

    opposite = store.find_contradictions(
        pattern, predicate, key_slots, polarity, user_id=user_id,
    )
    if opposite:
        return StoreLookupResult(
            StoreLookupOutcome.CONTRADICTION, contradicting_fact=opposite[0]
        )
    return StoreLookupResult(StoreLookupOutcome.MISS)
