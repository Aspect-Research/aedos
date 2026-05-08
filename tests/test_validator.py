"""Tests for src.layer2_routing.validator.

Four invariants × representative cases each, plus the precedence test
that pins which invariant fires when two would fail simultaneously.

The validator short-circuits on the first failure (declared-precedence
order). Tests pin the exact ``invariant`` string and the ``slot`` /
``expected`` / ``actual`` payload so the trace UI's renderer doesn't
break silently if invariant identifiers drift.
"""

from __future__ import annotations

import pytest

from src.layer1_extraction.pattern_registry import (
    load_default_registry,
    reset_cache,
)
from src.layer2_routing.validator import (
    INVARIANT_CATEGORICAL_TAUTOLOGY,
    INVARIANT_EVENT_NO_PARTICIPANTS,
    INVARIANT_MEREOLOGICAL_SELF_PARTHOOD,
    INVARIANT_REQUIRED_SLOT_MISSING,
    INVARIANT_USER_SUBJECT_REQUIRED,
    validate,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_cache()
    yield
    reset_cache()


@pytest.fixture
def reg():
    return load_default_registry()


def _claim(pattern, predicate="dummy", slots=None, polarity=1):
    return {
        "pattern": pattern,
        "predicate": predicate,
        "slots": slots or {},
        "polarity": polarity,
        "source_text": "<src>",
    }


# ---- invariant 1: required slots present and non-empty ----


def test_pass_when_all_required_slots_present(reg):
    """Happy path — every required slot has a non-empty value."""
    claim = _claim(
        "preference",
        predicate="likes",
        slots={"agent": "user", "object": "peanut butter"},
    )
    result = validate(claim, reg)
    assert result.ok is True
    assert result.invariant is None


def test_missing_required_slot_flags_anomaly(reg):
    """preference requires `agent` and `object`. Missing `object` →
    anomaly with the slot named."""
    claim = _claim("preference", predicate="likes", slots={"agent": "user"})
    result = validate(claim, reg)
    assert result.ok is False
    assert result.invariant == INVARIANT_REQUIRED_SLOT_MISSING
    assert result.slot == "object"


def test_empty_string_required_slot_flags_anomaly(reg):
    """An empty string is missing for the validator's purposes."""
    claim = _claim(
        "preference",
        predicate="likes",
        slots={"agent": "user", "object": "   "},
    )
    result = validate(claim, reg)
    assert result.ok is False
    assert result.invariant == INVARIANT_REQUIRED_SLOT_MISSING
    assert result.slot == "object"


def test_none_required_slot_flags_anomaly(reg):
    claim = _claim(
        "spatial_temporal",
        predicate="lives_in",
        slots={"entity": "user", "location": None},
    )
    result = validate(claim, reg)
    assert result.ok is False
    assert result.slot == "location"


def test_optional_slot_missing_is_fine(reg):
    """role_assignment's `org` is optional. Omitting it must not flag."""
    claim = _claim(
        "role_assignment",
        predicate="holds_role",
        slots={"agent": "user", "role": "47th President"},
    )
    result = validate(claim, reg)
    assert result.ok is True


def test_falsy_but_present_values_are_present(reg):
    """The integer 0 and the boolean False count as present — the
    validator must not flag legitimate falsy values."""
    claim = _claim(
        "quantitative",
        predicate="has_count",
        slots={"subject": "x", "property": "y", "value": 0},
    )
    result = validate(claim, reg)
    assert result.ok is True


def test_unknown_pattern_flags_required_slot_missing(reg):
    """An unknown pattern is upstream of validation but the validator
    treats it as a malformed claim, not a crash. Surfaces the bad
    pattern name as the offending slot."""
    claim = {
        "pattern": "invented_pattern",
        "predicate": "x",
        "slots": {},
        "polarity": 1,
    }
    result = validate(claim, reg)
    assert result.ok is False
    assert result.invariant == INVARIANT_REQUIRED_SLOT_MISSING
    assert result.slot == "pattern"
    assert result.actual == "invented_pattern"


# ---- invariant 2: USER_SUBJECT_PATTERNS → agent ∈ {user, me, i} ----


def test_preference_user_agent_passes(reg):
    """Each of the canonical user tokens passes."""
    for token in ("user", "User", " me ", "I", "i", "ME"):
        claim = _claim(
            "preference",
            predicate="likes",
            slots={"agent": token, "object": "pb"},
        )
        result = validate(claim, reg)
        assert result.ok is True, f"token {token!r} should be a valid user agent"


def test_preference_with_non_user_agent_flags_anomaly(reg):
    claim = _claim(
        "preference",
        predicate="likes",
        slots={"agent": "Donald Trump", "object": "pb"},
    )
    result = validate(claim, reg)
    assert result.ok is False
    assert result.invariant == INVARIANT_USER_SUBJECT_REQUIRED
    assert result.slot == "agent"
    assert result.actual == "Donald Trump"


def test_propositional_attitude_with_non_user_agent_flags_anomaly(reg):
    claim = _claim(
        "propositional_attitude",
        predicate="believes",
        slots={
            "agent": "a critic",
            "attitude": "feels",
            "proposition": "the novel is elegant",
        },
    )
    result = validate(claim, reg)
    assert result.ok is False
    assert result.invariant == INVARIANT_USER_SUBJECT_REQUIRED


def test_user_subject_patterns_only_apply_to_those_two_patterns(reg):
    """quantitative claims with non-user subjects are perfectly fine —
    the user-subject invariant must NOT spread to unrelated patterns."""
    claim = _claim(
        "quantitative",
        predicate="population_of",
        slots={"subject": "Tokyo", "property": "population", "value": 14000000},
    )
    result = validate(claim, reg)
    assert result.ok is True


# ---- invariant 3: mereological → part != whole ----


def test_mereological_distinct_part_and_whole_passes(reg):
    claim = _claim(
        "mereological",
        predicate="part_of",
        slots={"part": "Williamstown", "whole": "Massachusetts"},
    )
    result = validate(claim, reg)
    assert result.ok is True


def test_mereological_self_parthood_flags_anomaly(reg):
    claim = _claim(
        "mereological",
        predicate="part_of",
        slots={"part": "Tokyo", "whole": "Tokyo"},
    )
    result = validate(claim, reg)
    assert result.ok is False
    assert result.invariant == INVARIANT_MEREOLOGICAL_SELF_PARTHOOD
    assert result.slot == "part"


def test_mereological_self_parthood_is_case_insensitive(reg):
    """'Tokyo' vs 'tokyo' is still self-parthood — the validator must
    not let casing typos through."""
    claim = _claim(
        "mereological",
        predicate="part_of",
        slots={"part": "Tokyo", "whole": "tokyo"},
    )
    result = validate(claim, reg)
    assert result.ok is False
    assert result.invariant == INVARIANT_MEREOLOGICAL_SELF_PARTHOOD


def test_mereological_self_parthood_strips_whitespace(reg):
    claim = _claim(
        "mereological",
        predicate="part_of",
        slots={"part": "Tokyo", "whole": "  Tokyo  "},
    )
    result = validate(claim, reg)
    assert result.ok is False


# ---- invariant 4: event → participants is non-empty list ----


def test_event_with_participants_passes(reg):
    claim = _claim(
        "event",
        predicate="was_inaugurated",
        slots={
            "event_type": "inauguration",
            "participants": ["Donald Trump"],
            "occurred_at": "2025-01-20",
        },
    )
    result = validate(claim, reg)
    assert result.ok is True


def test_event_with_empty_participants_list_flags_anomaly(reg):
    """A list-typed required slot that's empty is the universal
    invariant catching it (empty list fails _slot_value_is_present),
    BUT the event-specific invariant catches the same case too via
    the second check. Either way the trace is informative; pin which
    one fires (the universal runs first by precedence order)."""
    claim = _claim(
        "event",
        predicate="was_inaugurated",
        slots={
            "event_type": "inauguration",
            "participants": [],
            "occurred_at": "2025-01-20",
        },
    )
    result = validate(claim, reg)
    assert result.ok is False
    # Universal invariant fires first because participants=[] fails
    # _slot_value_is_present (empty list).
    assert result.invariant == INVARIANT_REQUIRED_SLOT_MISSING
    assert result.slot == "participants"


def test_event_with_non_list_participants_flags_event_invariant(reg):
    """When participants is a non-list value (e.g. a string), the
    universal check passes — _slot_value_is_present accepts non-empty
    strings — but the event-specific invariant catches it. This is
    where the event invariant actually earns its keep over the
    universal check."""
    claim = _claim(
        "event",
        predicate="was_inaugurated",
        slots={
            "event_type": "inauguration",
            "participants": "Donald Trump",  # string instead of list
            "occurred_at": "2025-01-20",
        },
    )
    result = validate(claim, reg)
    assert result.ok is False
    assert result.invariant == INVARIANT_EVENT_NO_PARTICIPANTS
    assert result.slot == "participants"


# ---- precedence: which invariant fires when multiple would ----


def test_precedence_required_slot_beats_user_subject(reg):
    """A preference claim with NO agent slot at all — both invariants
    1 and 2 would trip. Invariant 1 (universal) fires first."""
    claim = _claim(
        "preference",
        predicate="likes",
        slots={"object": "pb"},  # no `agent`
    )
    result = validate(claim, reg)
    assert result.ok is False
    assert result.invariant == INVARIANT_REQUIRED_SLOT_MISSING
    assert result.slot == "agent"


def test_precedence_required_slot_beats_mereological_self_parthood(reg):
    """A mereological claim missing `whole` would also trip the
    self-parthood check (both None == None). Universal first."""
    claim = _claim(
        "mereological",
        predicate="part_of",
        slots={"part": "Tokyo"},  # no `whole`
    )
    result = validate(claim, reg)
    assert result.ok is False
    assert result.invariant == INVARIANT_REQUIRED_SLOT_MISSING
    assert result.slot == "whole"


def test_precedence_required_slot_beats_event_no_participants(reg):
    """Event missing `participants` → invariant 1 (universal). The
    event-specific invariant 4 never gets to run."""
    claim = _claim(
        "event",
        predicate="was_inaugurated",
        slots={"event_type": "inauguration", "occurred_at": "2025-01-20"},
    )
    result = validate(claim, reg)
    assert result.ok is False
    assert result.invariant == INVARIANT_REQUIRED_SLOT_MISSING
    assert result.slot == "participants"


def test_short_circuit_returns_only_first_failure(reg):
    """A claim with two genuinely independent failures — only the
    first is reported. (Constructed with two missing slots; the
    validator iterates required_slot_names in pattern order and
    reports the first missing one.)"""
    claim = _claim(
        "role_assignment",
        predicate="holds_role",
        slots={},  # missing both `agent` and `role`
    )
    result = validate(claim, reg)
    assert result.ok is False
    assert result.invariant == INVARIANT_REQUIRED_SLOT_MISSING
    # The first required slot in the role_assignment pattern (per
    # patterns.yaml's slot order) is `agent`. Pin that.
    assert result.slot == "agent"


# ============================================================================
# Phase 8.6c — categorical tautology invariant (7-case discrimination matrix)
# ============================================================================


def _categorical_claim(entity, category):
    return _claim(
        "categorical",
        predicate="is_a",
        slots={"entity": entity, "category": category},
    )


def test_tautology_case1_waggle_dance_canonical(reg):
    """Case 1: waggle-dance communication system / communication system
    → FLAG. The canonical case from real chat testing — entity ends
    with `" " + category`, has at least one modifier ("waggle-dance")
    preceding the suffix."""
    claim = _categorical_claim(
        "waggle-dance communication system", "communication system",
    )
    result = validate(claim, reg)
    assert result.ok is False
    assert result.invariant == INVARIANT_CATEGORICAL_TAUTOLOGY
    assert result.slot == "category"


def test_tautology_case2_tokyo_no_overlap(reg):
    """Case 2: Tokyo / city → no flag. category is not a substring of
    entity. Standard categorical extraction must pass."""
    claim = _categorical_claim("Tokyo", "city")
    result = validate(claim, reg)
    assert result.ok is True


def test_tautology_case3_president_substring_not_suffix(reg):
    """Case 3: President of the United States / President → no flag.
    'President' IS a substring of the entity but NOT a suffix
    (entity ends with 'States'). The suffix rule is the discriminator
    that distinguishes vacuous tautologies from legitimate (if
    obvious) is_a relations."""
    claim = _categorical_claim("President of the United States", "President")
    result = validate(claim, reg)
    assert result.ok is True


def test_tautology_case4_small_cat_one_token_modifier(reg):
    """Case 4: small cat / cat → FLAG. Suffix match with one-token
    modifier. Vacuous claim (a small cat IS-A cat conveys nothing
    beyond what the name "small cat" already says); abstaining is
    architecturally correct. The system has nothing useful to verify."""
    claim = _categorical_claim("small cat", "cat")
    result = validate(claim, reg)
    assert result.ok is False
    assert result.invariant == INVARIANT_CATEGORICAL_TAUTOLOGY


def test_tautology_case5_marie_curie_no_relation(reg):
    """Case 5: Marie Curie / physicist → no flag. No substring relation
    at all between entity and category. Real categorical claim."""
    claim = _categorical_claim("Marie Curie", "physicist")
    result = validate(claim, reg)
    assert result.ok is True


def test_tautology_case6_exact_equality_multitoken(reg):
    """Case 6: communication system / communication system → FLAG.
    Exact equality is the degenerate suffix case (the "suffix" is the
    entire string with empty modifier prefix). The validator handles
    this branch separately from the leading-space-plus-category
    branch — entity == category is unambiguously vacuous."""
    claim = _categorical_claim("communication system", "communication system")
    result = validate(claim, reg)
    assert result.ok is False
    assert result.invariant == INVARIANT_CATEGORICAL_TAUTOLOGY


def test_tautology_case7_exact_equality_single_token(reg):
    """Case 7: cat / cat → FLAG. Exact equality with a single token.
    The entity IS its own category — vacuous regardless of token
    count. Same architectural reasoning as case 6."""
    claim = _categorical_claim("cat", "cat")
    result = validate(claim, reg)
    assert result.ok is False
    assert result.invariant == INVARIANT_CATEGORICAL_TAUTOLOGY


def test_tautology_case_insensitive_normalization(reg):
    """Bonus: capitalization differences don't unlock the tautology.
    'Communication System' / 'communication system' still flags, even
    though byte-level they differ. The validator normalizes both sides
    via lowercase + whitespace collapse before comparing."""
    claim = _categorical_claim(
        "Waggle-Dance Communication System", "communication system",
    )
    result = validate(claim, reg)
    assert result.ok is False
    assert result.invariant == INVARIANT_CATEGORICAL_TAUTOLOGY


def test_tautology_does_not_fire_on_other_patterns(reg):
    """The tautology invariant is scoped to ``categorical`` claims
    only. A spatial_temporal claim with the same entity/category
    pattern (entity='Tokyo metropolitan area', location='area')
    must not flag — the rule's architectural intent is noun-phrase
    is_a tautologies."""
    claim = _claim(
        "spatial_temporal",
        predicate="located_in",
        slots={
            "entity": "Tokyo metropolitan area", "location": "area",
            "relation_kind": "containment",
        },
    )
    result = validate(claim, reg)
    assert result.ok is True
