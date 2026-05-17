from __future__ import annotations

import uuid


def decompose_event(raw: dict) -> list[dict]:
    """Convert a raw LLM extraction dict into binary-relational claim dicts.

    If raw['participants'] is non-empty, produces multiple binary claims linked
    by a shared reified_event_id:
      - one has_participant claim per participant
      - one event_type claim if event_type is set
      - one target claim from the original object slot

    If participants is empty or absent, returns [raw] unchanged (already binary).
    """
    participants: list[str] = raw.get("participants") or []

    if not participants:
        return [raw]

    event_id: str = raw.get("reified_event_id") or f"event_{uuid.uuid4().hex[:8]}"

    # Fields inherited by all decomposed claims (strip decomposition-specific keys)
    base = {
        k: v for k, v in raw.items()
        if k not in ("participants", "subject", "object", "predicate", "reified_event_id", "event_type")
    }
    base["participants"] = []

    result: list[dict] = []

    for participant in participants:
        result.append({
            **base,
            "subject": event_id,
            "predicate": "has_participant",
            "object": participant,
            "reified_event_id": event_id,
        })

    event_type = raw.get("event_type")
    if event_type:
        result.append({
            **base,
            "subject": event_id,
            "predicate": "event_type",
            "object": event_type,
            "reified_event_id": event_id,
        })

    orig_object = raw.get("object")
    if orig_object:
        result.append({
            **base,
            "subject": event_id,
            "predicate": "target",
            "object": orig_object,
            "reified_event_id": event_id,
        })

    return result
