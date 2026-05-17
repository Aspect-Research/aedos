from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

BEFORE_PRESENT = "before_present"


@dataclass
class TemporalScope:
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    valid_during_ref: Optional[str] = None
    is_future: bool = False


def extract_temporal_scope(
    *,
    verb_tense: str,
    valid_from_raw: Optional[str] = None,
    valid_until_raw: Optional[str] = None,
    valid_during_ref: Optional[str] = None,
) -> TemporalScope:
    """Derive temporal scope from extraction signals.

    verb_tense: one of "past", "present", "future"
    valid_from_raw / valid_until_raw: explicit date strings from LLM extraction (may be None)
    valid_during_ref: claim_id of a reference claim for relative scope (may be None)
    """
    if verb_tense == "future":
        return TemporalScope(is_future=True)

    # Explicit scope overrides tense-based inference
    if valid_from_raw or valid_until_raw:
        return TemporalScope(
            valid_from=valid_from_raw,
            valid_until=valid_until_raw,
            valid_during_ref=valid_during_ref,
        )

    if valid_during_ref:
        return TemporalScope(valid_during_ref=valid_during_ref)

    # Implicit past tense without dates → claim ended at unspecified past time
    if verb_tense == "past":
        return TemporalScope(valid_until=BEFORE_PRESENT)

    # Present tense, no markers → currently valid, unscoped
    return TemporalScope()
