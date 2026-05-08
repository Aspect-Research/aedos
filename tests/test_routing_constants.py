"""Tests for src.layer2_routing.constants.

Phase 0 shipped ``confidence_from_counts``; Phase 1 added the pattern-
shape maps; Phase 2 added ``USER_SUBJECT_PATTERNS``,
``UNIQUE_VALUE_SLOTS``, and the helpers ``is_user`` /
``is_self_attribute``. The drift tests (each dict's keys ⊆
registry.names()) are load-bearing: if a pattern is added or removed
in patterns.yaml without updating the maps, Layer 2 will silently
miss claims under the orphaned pattern.
"""

from __future__ import annotations

import pytest

from src.layer1_extraction.pattern_registry import (
    load_default_registry,
    reset_cache,
)
from src.layer2_routing.constants import (
    KEY_SLOTS_BY_PATTERN,
    SUBJECT_SLOT_BY_PATTERN,
    UNIQUE_VALUE_SLOTS,
    USER_SUBJECT_PATTERNS,
    confidence_from_counts,
    is_self_attribute,
    is_user,
    unique_value_slots_enabled,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_cache()
    yield
    reset_cache()


# ---- confidence_from_counts (Phase 0 contract) ----


def test_confidence_no_evidence_is_uniform_prior():
    assert confidence_from_counts(0, 0) == pytest.approx(0.5)


def test_confidence_climbs_with_affirmations():
    a = confidence_from_counts(0, 0)
    b = confidence_from_counts(1, 0)
    c = confidence_from_counts(5, 0)
    assert a < b < c
    assert b == pytest.approx((1 + 1) / (1 + 0 + 2))
    assert c == pytest.approx((5 + 1) / (5 + 0 + 2))


def test_confidence_drops_with_contradictions():
    a = confidence_from_counts(5, 0)
    b = confidence_from_counts(5, 1)
    c = confidence_from_counts(5, 5)
    assert a > b > c


def test_confidence_handles_negatives_and_none():
    """Defensive coercion — negative or None counts read as 0."""
    assert confidence_from_counts(-3, 0) == pytest.approx(0.5)
    assert confidence_from_counts(0, -3) == pytest.approx(0.5)
    assert confidence_from_counts(None, None) == pytest.approx(0.5)  # type: ignore[arg-type]


# ---- pattern-shape maps (Phase 1 additions) ----


def test_subject_slot_by_pattern_keys_match_registry():
    """Drift test: SUBJECT_SLOT_BY_PATTERN keys exactly equal
    registry.names(). If a pattern is added/removed in patterns.yaml,
    this fails until the map is updated."""
    reg = load_default_registry()
    assert set(SUBJECT_SLOT_BY_PATTERN.keys()) == set(reg.names())


def test_key_slots_by_pattern_keys_match_registry():
    """Drift test mirror for KEY_SLOTS_BY_PATTERN."""
    reg = load_default_registry()
    assert set(KEY_SLOTS_BY_PATTERN.keys()) == set(reg.names())


def test_mereological_subject_is_part():
    assert SUBJECT_SLOT_BY_PATTERN["mereological"] == "part"


def test_mereological_key_slots_are_part_and_whole():
    assert KEY_SLOTS_BY_PATTERN["mereological"] == ["part", "whole"]


def test_subject_slot_values_exist_as_pattern_slots():
    """Each pattern's named subject slot must actually be declared on
    the pattern. Catches typos like 'subjeect'. Exception: `event`
    uses 'participants' which is a list-typed slot — still declared
    on the pattern.
    """
    reg = load_default_registry()
    for pattern_name, slot_name in SUBJECT_SLOT_BY_PATTERN.items():
        pattern = reg.get(pattern_name)
        declared = {s.name for s in pattern.slots}
        assert slot_name in declared, (
            f"SUBJECT_SLOT_BY_PATTERN[{pattern_name!r}]={slot_name!r} "
            f"is not a declared slot on the pattern (declared: {declared})"
        )


def test_key_slots_values_exist_as_pattern_slots():
    """Each KEY_SLOTS entry must reference real declared slot names."""
    reg = load_default_registry()
    for pattern_name, slot_names in KEY_SLOTS_BY_PATTERN.items():
        pattern = reg.get(pattern_name)
        declared = {s.name for s in pattern.slots}
        for slot_name in slot_names:
            assert slot_name in declared, (
                f"KEY_SLOTS_BY_PATTERN[{pattern_name!r}] references "
                f"{slot_name!r} which is not declared on the pattern "
                f"(declared: {declared})"
            )


# ---- USER_SUBJECT_PATTERNS (Phase 2 addition) ----


def test_user_subject_patterns_keys_subset_of_registry():
    """USER_SUBJECT_PATTERNS keys must be valid pattern names — but
    they're a SUBSET of registry.names() (only preference and
    propositional_attitude qualify). Drift here is silent: a pattern
    added to USER_SUBJECT_PATTERNS that isn't in the registry breaks
    the validator's invariant 2."""
    reg = load_default_registry()
    assert set(USER_SUBJECT_PATTERNS.keys()) <= set(reg.names())
    # Phase 2's commitment: exactly these two.
    assert set(USER_SUBJECT_PATTERNS.keys()) == {
        "preference", "propositional_attitude",
    }


def test_user_subject_patterns_values_exist_as_pattern_slots():
    """Each USER_SUBJECT_PATTERNS slot name must be declared on the
    pattern."""
    reg = load_default_registry()
    for pattern_name, slot_name in USER_SUBJECT_PATTERNS.items():
        declared = {s.name for s in reg.get(pattern_name).slots}
        assert slot_name in declared


# ---- is_user (Phase 2 helper) ----


def test_is_user_canonical_tokens():
    """The three canonical first-person tokens must pass."""
    assert is_user("user")
    assert is_user("me")
    assert is_user("i")


def test_is_user_normalization():
    """Case + whitespace folding."""
    assert is_user("User")
    assert is_user(" ME ")
    assert is_user("I")
    assert is_user("\tme\n")


def test_is_user_rejects_non_user_strings():
    assert not is_user("Donald Trump")
    assert not is_user("the user")  # extractor must canonicalize first
    assert not is_user("you")
    assert not is_user("")
    assert not is_user("   ")


def test_is_user_rejects_non_strings():
    assert not is_user(None)
    assert not is_user(42)
    assert not is_user(["user"])
    assert not is_user({"agent": "user"})


# ---- is_self_attribute (Phase 2 helper, consumed by Phase 4+) ----


def test_is_self_attribute_preference_with_user():
    """preference.agent == 'user' → self-attribute."""
    claim = {
        "pattern": "preference",
        "slots": {"agent": "user", "object": "pb"},
    }
    assert is_self_attribute(claim) is True


def test_is_self_attribute_preference_with_third_party():
    claim = {
        "pattern": "preference",
        "slots": {"agent": "Donald Trump", "object": "pb"},
    }
    assert is_self_attribute(claim) is False


def test_is_self_attribute_quantitative_user_is_self():
    """quantitative.subject == 'user' is unusual but possible (the
    user is the subject of a self-quantitative claim — e.g. their
    own age)."""
    claim = {
        "pattern": "quantitative",
        "slots": {"subject": "user", "property": "age", "value": 30},
    }
    assert is_self_attribute(claim) is True


def test_is_self_attribute_quantitative_world_subject():
    claim = {
        "pattern": "quantitative",
        "slots": {"subject": "Tokyo", "property": "population",
                  "value": 14000000},
    }
    assert is_self_attribute(claim) is False


def test_is_self_attribute_event_user_in_participants_list():
    """Event's participants is a list — user appearing anywhere in it
    counts as a self-attribute."""
    claim = {
        "pattern": "event",
        "slots": {
            "event_type": "attendance",
            "participants": ["user", "Olympics"],
            "occurred_at": "2024",
        },
    }
    assert is_self_attribute(claim) is True


def test_is_self_attribute_event_no_user_in_participants():
    claim = {
        "pattern": "event",
        "slots": {
            "event_type": "inauguration",
            "participants": ["Donald Trump"],
            "occurred_at": "2025-01-20",
        },
    }
    assert is_self_attribute(claim) is False


def test_is_self_attribute_unknown_pattern_returns_false():
    """An unknown pattern can't be a self-attribute; the validator's
    upstream invariant catches the bad pattern, but this helper must
    not crash on it."""
    claim = {
        "pattern": "invented",
        "slots": {"agent": "user"},
    }
    assert is_self_attribute(claim) is False


def test_is_self_attribute_mereological_part_user():
    """mereological.part is the subject; 'user is part of X' is rare
    but the helper must classify it consistently."""
    claim = {
        "pattern": "mereological",
        "slots": {"part": "user", "whole": "the team"},
    }
    assert is_self_attribute(claim) is True


# ---- UNIQUE_VALUE_SLOTS (port-only in Phase 2) ----


def test_unique_value_slots_shape():
    """Phase 2 ports the constant unchanged from v1; Phase 6 consumes
    it. Pin the v1-equivalent shape so the port is faithful."""
    assert UNIQUE_VALUE_SLOTS == {
        ("spatial_temporal", "was_born_in", "entity", "location"): True,
    }


def test_unique_value_slots_env_flag_default_off(monkeypatch):
    """Opt-in via env var; default off."""
    monkeypatch.delenv("AEDOS_UNIQUE_VALUE_SLOTS", raising=False)
    assert unique_value_slots_enabled() is False
    monkeypatch.setenv("AEDOS_UNIQUE_VALUE_SLOTS", "1")
    assert unique_value_slots_enabled() is True
    monkeypatch.setenv("AEDOS_UNIQUE_VALUE_SLOTS", "0")
    assert unique_value_slots_enabled() is False
