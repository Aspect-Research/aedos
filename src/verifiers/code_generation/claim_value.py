"""Pull the asserted value from a claim by pattern + predicate.

Both the prompt builder (for leak detection) and the comparator (for the
equality check) need to know "what is this claim asserting the answer
to be?". The answer depends on the pattern and predicate:

    quantitative.*               → slots["value"]
    relational.reverse_of        → slots["subject"]   (subject IS the answer)
    relational.<boolean>         → True               (the relation either holds or doesn't;
                                                        polarity is applied at compare time)
"""

from __future__ import annotations

from typing import Any


# Relational predicates whose computed answer is a boolean (the relation
# holds or it doesn't). For these, the "claimed positive answer" is
# always True and polarity is applied separately by the comparator.
_RELATIONAL_BOOLEAN_PREDICATES = {
    "is_anagram_of",
    "contains_substring",
    "equals",
    "greater_than",
    "less_than",
    "starts_with",
    "ends_with",
}

# Relational predicates whose computed answer IS the subject slot. The
# code computes f(object) and compares to subject.
_RELATIONAL_SUBJECT_AS_ANSWER = {
    "reverse_of",
}


class UnknownClaimShape(ValueError):
    """Raised when extract_claimed_value can't determine the asserted slot."""


def extract_claimed_value(claim: dict) -> Any:
    """Return the value the claim asserts the computed answer should equal.

    For boolean relational predicates returns True (positive form);
    polarity is applied by the comparator, not here.
    """
    pattern = claim.get("pattern")
    predicate = claim.get("predicate") or ""
    slots = claim.get("slots") or {}

    if pattern == "quantitative":
        # The "value" slot is the asserted answer for every quantitative
        # claim — has_count, has_length, weighs, born_in_year, etc.
        return slots.get("value")

    if pattern == "relational":
        if predicate in _RELATIONAL_SUBJECT_AS_ANSWER:
            return slots.get("subject")
        # Default for relational predicates (boolean and unknown):
        # the relation either positively holds (True) or it doesn't.
        return True

    # Other patterns aren't expected to route through code generation,
    # but if they do, fall back to None so the comparator surfaces the
    # comparison_error rather than silently passing.
    return None
