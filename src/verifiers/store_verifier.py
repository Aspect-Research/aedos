"""Store-backed verification.

When a model asserts a fact whose verification method is ``store_lookup`` or
``user_authoritative``, we check the fact store — the user is the ground
truth for their own preferences/identity, and anything they previously
asserted sits in the store as ``user_asserted``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.fact_store import Fact, FactStore


class StoreLookupOutcome(str, Enum):
    MATCH = "match"  # an existing currently-valid fact matches exactly
    CONTRADICTION = "contradiction"  # existing fact has opposite polarity
    MISS = "miss"  # nothing on record


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


def store_lookup_verify(claim: dict, store: FactStore) -> StoreLookupResult:
    """Look for facts that match or contradict ``claim``.

    A ``match`` means same subject+predicate+object+polarity and currently valid.
    A ``contradiction`` means same subject+predicate+object but opposite polarity.
    """
    subject = claim["subject"]
    predicate = claim["predicate"]
    obj = claim["object"]
    polarity = int(claim["polarity"])

    same = store.find_currently_valid(subject, predicate, obj, polarity)
    if same:
        return StoreLookupResult(StoreLookupOutcome.MATCH, matching_fact=same[0])

    opposite = store.find_contradictions(subject, predicate, obj, polarity)
    if opposite:
        return StoreLookupResult(
            StoreLookupOutcome.CONTRADICTION, contradicting_fact=opposite[0]
        )

    return StoreLookupResult(StoreLookupOutcome.MISS)
