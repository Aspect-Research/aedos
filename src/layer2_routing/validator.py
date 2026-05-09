"""Layer 2 step 1 — rule-based claim validation.

Phase 2 promotes v1's ``_maybe_anomaly`` from a Router internal into a
top-level layer-2 step. Every claim runs through ``validate(claim,
registry)`` BEFORE the LLM router is consulted. Validation failures
short-circuit the layer entirely: they emit a ``ROUTING_ANOMALY``
``Decision`` and skip both the memo lookup and the LLM call. This is
the architectural commitment in principle 7 (validate before
classifying, route before reasoning) — cheap rule-based checks catch
structural problems before expensive LLM work.

Five invariants, in declared precedence:

  1. **All required slots present and non-empty** (universal). Derived
     from ``registry.get(pattern).slots``. A claim missing one of its
     pattern's required slots is structurally malformed regardless of
     pattern.
  2. **USER_SUBJECT_PATTERNS → agent ∈ {user, me, i}**. Preference and
     propositional_attitude claims with a non-user agent are slot-
     binding bugs upstream — the extractor mis-bound a third party
     into a pattern reserved for the user.
  3. **Mereological → part != whole**. Self-parthood is invalid;
     constitutive parthood requires distinct entities. Comparison is
     case-insensitive on string values to catch ``"Tokyo"`` vs
     ``"tokyo"`` typos.
  4. **Event → participants is a non-empty list**. The slot's type is
     ``list`` per ``patterns.yaml``; an empty list (or non-list) means
     extraction failed to fill the slot.
  5. **Categorical → category is not a suffix of the entity** (Phase
     8.6). Vacuous tautologies — ``is_a("waggle-dance communication
     system", "communication system")`` — get rejected. The check
     fires when the lowercase-stripped entity equals the category, OR
     when the entity ends with `" " + category` (i.e. the category is
     the trailing token-or-tokens of the entity, with at least one
     modifier preceding it). Pure substring matches that aren't
     suffixes ("President of the United States" / "President") DO NOT
     flag — the architectural intent is to catch noun-phrase
     tautologies, not legitimate (if obvious) is_a relations. The
     extractor's prompt is the primary guard; this validator is the
     backstop against prompt drift.

The validator short-circuits on the first failure. The trace UI shows
one anomaly per claim — operators reason about a single root cause,
not about which of N reported failures is the real bug. If two
invariants would fail simultaneously, the earlier one wins per the
declared order above. The order itself is precedence-justified: the
universal "required slots present" invariant runs first because the
later invariants assume the slots they reference exist (e.g. the
mereological check would crash if ``part`` or ``whole`` is missing).

The validator does NOT call the LLM. It returns synchronously and
deterministically. Exceptions inside the validator are bugs in the
validator, not in the claim — they should propagate, not be caught.
"""

from __future__ import annotations

from typing import Any

from src.layer1_extraction.pattern_registry import PatternRegistry
from src.layer2_routing.constants import is_user
from src.layer2_routing.types import ValidationResult


# Invariant identifier strings — surfaced in ValidationResult.invariant
# and in pipeline events so the trace UI and tests can key off them.
# Keep these stable; tests pin the exact strings.
INVARIANT_REQUIRED_SLOT_MISSING = "required_slot_missing"
INVARIANT_USER_SUBJECT_REQUIRED = "user_subject_required"
INVARIANT_MEREOLOGICAL_SELF_PARTHOOD = "mereological_self_parthood"
INVARIANT_EVENT_NO_PARTICIPANTS = "event_no_participants"
INVARIANT_CATEGORICAL_TAUTOLOGY = "categorical_tautology"


def _slot_value_is_present(value: Any) -> bool:
    """Whether a slot value is considered "present and non-empty".

    None and the empty string fail; everything else (including the
    integer 0 and the boolean False) passes. The validator's job is
    to catch missing-slot extractor bugs, not to second-guess legitimate
    falsy values like ``polarity=0`` (which is the negation case) or
    ``value=0`` for a quantitative claim.

    Empty lists fail because the only required list-typed slot we have
    is ``event.participants``, which is independently checked by the
    event invariant. Catching empty lists here makes the universal
    check do the right thing for any future list-typed required slots
    too.
    """
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    if isinstance(value, list) and not value:
        return False
    return True


def validate(claim: dict, registry: PatternRegistry) -> ValidationResult:
    """Validate a claim against the four Phase 2 invariants.

    Returns ``ValidationResult.passed()`` on success, or
    ``ValidationResult.anomaly(...)`` on the first failed invariant.
    Short-circuits on the first failure (see module docstring).

    The caller is the routing orchestrator; the validator does not
    emit pipeline events itself (the orchestrator emits
    ``routing_validation_failed`` with the result's payload so the
    event-emission decision lives next to the rest of the routing
    flow).
    """
    pattern_name = claim.get("pattern", "")
    if not pattern_name or not registry.has(pattern_name):
        # An unknown pattern is upstream of validation — the extractor
        # should have rejected it. Treat as a required-slot anomaly so
        # downstream consumers don't have to special-case it.
        return ValidationResult.anomaly(
            invariant=INVARIANT_REQUIRED_SLOT_MISSING,
            slot="pattern",
            expected="a known pattern name",
            actual=pattern_name,
        )
    pattern = registry.get(pattern_name)
    slots = claim.get("slots") or {}

    # Invariant 1 (universal) — every required slot present and non-empty.
    for slot_name in pattern.required_slot_names():
        if not _slot_value_is_present(slots.get(slot_name)):
            return ValidationResult.anomaly(
                invariant=INVARIANT_REQUIRED_SLOT_MISSING,
                slot=slot_name,
                expected=f"non-empty value on required slot {slot_name!r}",
                actual=slots.get(slot_name),
            )

    # Invariant 2 — agent_constraint (schema-driven). Patterns that
    # declare agent_constraint="must_be_user" require their named
    # subject slot to identify the chatting user. v0.14.3: this lives
    # in patterns.yaml's `agent_constraint` field instead of a
    # hardcoded USER_SUBJECT_PATTERNS dict.
    if pattern.agent_constraint == "must_be_user":
        user_slot = pattern.subject_slot_for_constraint or "agent"
        actual = slots.get(user_slot)
        if not is_user(actual):
            return ValidationResult.anomaly(
                invariant=INVARIANT_USER_SUBJECT_REQUIRED,
                slot=user_slot,
                expected="user (one of {'user', 'me', 'i'})",
                actual=actual,
            )

    # Invariant 3 — distinct_slots (schema-driven). Patterns that
    # declare distinct_slots=[a, b] require slots a and b to hold
    # distinct values (case-insensitive on strings). v0.14.3: this
    # lives in patterns.yaml's `distinct_slots` field instead of a
    # hardcoded mereological branch.
    if pattern.distinct_slots is not None:
        slot_a, slot_b = pattern.distinct_slots
        val_a = slots.get(slot_a)
        val_b = slots.get(slot_b)
        if _values_equal_ci(val_a, val_b):
            # Mereological is the canonical (and currently only) user
            # of distinct_slots; the invariant string keeps its name
            # for trace-UI / test stability.
            return ValidationResult.anomaly(
                invariant=INVARIANT_MEREOLOGICAL_SELF_PARTHOOD,
                slot=slot_a,
                expected=f"distinct from {slot_b}",
                actual=val_a,
            )

    # Invariant 4 — event: participants is a non-empty list.
    if pattern_name == "event":
        participants = slots.get("participants")
        if not isinstance(participants, list) or not participants:
            return ValidationResult.anomaly(
                invariant=INVARIANT_EVENT_NO_PARTICIPANTS,
                slot="participants",
                expected="non-empty list of participant entities",
                actual=participants,
            )

    # Invariant 5 (Phase 8.6) — categorical: category is not a suffix
    # of the entity (or equal to it). Catches vacuous tautologies that
    # the extractor prompt may emit on regression. See module docstring.
    if pattern_name == "categorical":
        entity = slots.get("entity")
        category = slots.get("category")
        if _is_categorical_tautology(entity, category):
            return ValidationResult.anomaly(
                invariant=INVARIANT_CATEGORICAL_TAUTOLOGY,
                slot="category",
                expected="distinct from entity (not a suffix or equal)",
                actual=category,
            )

    return ValidationResult.passed()


def _is_categorical_tautology(entity: Any, category: Any) -> bool:
    """True when the categorical claim is a vacuous tautology under
    the suffix rule. Both inputs must be strings; non-string inputs
    fall through to False (treated as legitimate, since the
    invariant only applies to natural-language tautologies).

    Normalization: lowercase + leading/trailing whitespace strip on
    both sides, then collapse internal whitespace to single spaces.
    The tautology fires on either of:

      * ``entity == category`` (degenerate equality case)
      * ``entity`` ends with ``" " + category`` (category is the last
        token-or-tokens of entity, with at least one modifier preceding)

    Pure substring matches that are NOT suffixes (e.g. "President of
    the United States" / "President") do NOT fire. The leading-space
    requirement enforces "entity has at least one token before the
    category" for non-equal cases."""
    if not isinstance(entity, str) or not isinstance(category, str):
        return False
    e = " ".join(entity.strip().lower().split())
    c = " ".join(category.strip().lower().split())
    if not e or not c:
        return False
    if e == c:
        return True
    return e.endswith(" " + c)


def _values_equal_ci(a: Any, b: Any) -> bool:
    """Case-insensitive equality for slot values that may be strings.

    Strings compare lower-cased and stripped; non-strings fall back to
    plain equality. The mereological check uses this so "Tokyo" vs
    "tokyo" both flag as self-parthood. Non-string equality (e.g. two
    integers, two lists) is exact.
    """
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b
