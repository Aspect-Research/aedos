from __future__ import annotations

import re

_CANONICAL_MAP: dict[str, str] = {
    "is employed by": "employed_by",
    "works for": "employed_by",
    "works at": "employed_by",
    "was employed by": "employed_by",
    "is employed at": "employed_by",
    "was employed at": "employed_by",
    "is born in": "born_in",
    "was born in": "born_in",
    "was born": "born_in",
    "born in": "born_in",
    "died in": "died_in",
    "passed away in": "died_in",
    "is located in": "located_in",
    "lives in": "located_in",
    "resides in": "located_in",
    "is in": "located_in",
    "is situated in": "located_in",
    "was awarded": "received_award",
    "was awarded the": "received_award",
    "received the award": "received_award",
    "received award": "received_award",
    "won": "received_award",
    "won the": "received_award",
    "awarded": "received_award",
    "studied at": "educated_at",
    "attended": "educated_at",
    "graduated from": "graduated_from",
    "served as": "holds_role",
    "is a member of": "member_of",
    "is member of": "member_of",
    "is affiliated with": "affiliated_with",
    "is part of": "part_of",
    "belongs to": "part_of",
    "is made of": "made_of",
    "is composed of": "composed_of",
    "is the founder of": "founded",
    "co-founded": "co_founded",
    "co founded": "co_founded",
    "is the capital of": "is_capital_of",
    "is the president of": "is_president_of",
    "is the ceo of": "is_ceo_of",
    "is the author of": "authored",
    "has written": "authored",
    "wrote": "authored",
    "is a": "instance_of",
    "is an": "instance_of",
    "was a": "instance_of",
    "was an": "instance_of",
    "is the": "holds_role",
    "was the": "holds_role",
}

_AUX_PREFIX = re.compile(
    r"^(is|was|were|has|have|had|will|would|does|did)\s+",
    re.IGNORECASE,
)


def normalize_predicate(raw: str) -> str:
    """Normalize a predicate to canonical snake_case, tense-neutral, voice-neutral form."""
    stripped = raw.strip().lower()

    if stripped in _CANONICAL_MAP:
        return _CANONICAL_MAP[stripped]

    # Try stripping a leading auxiliary verb once
    no_aux = _AUX_PREFIX.sub("", stripped).strip()
    if no_aux != stripped:
        if no_aux in _CANONICAL_MAP:
            return _CANONICAL_MAP[no_aux]
        stripped = no_aux

    # Remove trailing definite/indefinite article
    stripped = re.sub(r"\s+(the|a|an)$", "", stripped)

    # snake_case: remove non-word/non-space chars, collapse spaces to underscores
    result = re.sub(r"[^\w\s]", "_", stripped)
    result = re.sub(r"\s+", "_", result.strip())
    result = re.sub(r"_+", "_", result).strip("_")

    return result or "unknown_predicate"
