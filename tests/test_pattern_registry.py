"""Tests for src.pattern_registry."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.pattern_registry import (
    Pattern,
    PatternRegistry,
    PatternRegistryError,
    VerificationRule,
    load_default_registry,
    reset_cache,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    reset_cache()
    yield
    reset_cache()


# ---------- default registry ----------


EXPECTED_PATTERNS = {
    "role_assignment",
    "preference",
    "quantitative",
    "spatial_temporal",
    "categorical",
    "relational",
    "event",
    "propositional_attitude",
}


def test_default_registry_has_all_eight_patterns():
    reg = load_default_registry()
    assert set(reg.names()) == EXPECTED_PATTERNS


def test_each_pattern_has_required_metadata():
    reg = load_default_registry()
    for p in reg.all():
        assert p.description, f"{p.name} missing description"
        assert p.slots, f"{p.name} has no slots"
        assert p.verification_rules, f"{p.name} has no verification rules"
        assert p.example_extractions, f"{p.name} should have at least one worked example"


def test_retrieval_patterns_have_query_strategy():
    """Section 5 needs query_strategy on every retrieval-using pattern."""
    reg = load_default_registry()
    for p in reg.all():
        # If any rule resolves to retrieval, we need a strategy.
        uses_retrieval = any(r.method == "retrieval" for r in p.verification_rules)
        if uses_retrieval:
            assert p.query_strategy, f"{p.name} needs a query_strategy"


def test_user_authoritative_patterns_flag_non_user_anomaly():
    reg = load_default_registry()
    for name in ("preference", "propositional_attitude"):
        assert reg.get(name).flag_non_user_as_anomaly, (
            f"{name} should flag non-user agents as anomalies"
        )
    # spatial_temporal has user_auth branch but non-user agents are normal there.
    assert reg.get("spatial_temporal").flag_non_user_as_anomaly is False


# ---------- conditional verification rules ----------


def test_resolve_method_conditional_user_path():
    reg = load_default_registry()
    pref = reg.get("preference")
    assert pref.resolve_method({"agent": "user", "object": "pb"}) == "user_authoritative"
    assert pref.resolve_method({"agent": "Donald Trump", "object": "pb"}) == "unverifiable"


def test_resolve_method_default_string_form():
    reg = load_default_registry()
    role = reg.get("role_assignment")
    assert role.resolve_method({"agent": "x", "role": "y"}) == "retrieval"


def test_has_user_authoritative_branch():
    reg = load_default_registry()
    assert reg.get("preference").has_user_authoritative_branch()
    assert reg.get("propositional_attitude").has_user_authoritative_branch()
    assert reg.get("spatial_temporal").has_user_authoritative_branch()
    assert not reg.get("categorical").has_user_authoritative_branch()
    assert not reg.get("role_assignment").has_user_authoritative_branch()


# ---------- describe_for_prompt ----------


def test_describe_for_prompt_includes_every_pattern():
    reg = load_default_registry()
    text = reg.describe_for_prompt()
    for name in reg.names():
        assert name in text, f"{name} missing from prompt-formatted registry"
    assert "Verification:" in text
    assert "Slots:" in text


# ---------- error paths ----------


def _write_yaml(tmp_path: Path, body: dict) -> Path:
    p = tmp_path / "p.yaml"
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    return p


def test_missing_required_field(tmp_path):
    bad = _write_yaml(
        tmp_path,
        {"foo": {"description": "x", "verification_method": "retrieval"}},
    )
    with pytest.raises(PatternRegistryError, match="missing fields"):
        PatternRegistry.from_yaml(bad)


def test_unknown_verification_method(tmp_path):
    bad = _write_yaml(
        tmp_path,
        {
            "foo": {
                "description": "x",
                "slots": [{"name": "s", "type": "entity"}],
                "verification_method": "telepathy",
            }
        },
    )
    with pytest.raises(PatternRegistryError, match="verification_method"):
        PatternRegistry.from_yaml(bad)


def test_default_rule_must_be_last(tmp_path):
    bad = _write_yaml(
        tmp_path,
        {
            "foo": {
                "description": "x",
                "slots": [{"name": "s", "type": "entity"}],
                "verification_method": [
                    {"method": "retrieval"},  # default
                    {"when": {"agent": "user"}, "method": "user_authoritative"},
                ],
            }
        },
    )
    with pytest.raises(PatternRegistryError, match="last rule must be a default"):
        PatternRegistry.from_yaml(bad)


def test_unknown_slot_type(tmp_path):
    bad = _write_yaml(
        tmp_path,
        {
            "foo": {
                "description": "x",
                "slots": [{"name": "s", "type": "molecule"}],
                "verification_method": "retrieval",
            }
        },
    )
    with pytest.raises(PatternRegistryError, match="slot type"):
        PatternRegistry.from_yaml(bad)


def test_get_unknown_pattern_raises():
    reg = load_default_registry()
    with pytest.raises(PatternRegistryError):
        reg.get("not_a_real_pattern")


# ---------- VerificationRule.matches ----------


def test_verification_rule_matches_case_insensitive():
    rule = VerificationRule(method="x", when={"agent": "user"})
    assert rule.matches({"agent": "User"})
    assert rule.matches({"agent": " user "})
    assert not rule.matches({"agent": "Donald"})


def test_default_rule_always_matches():
    rule = VerificationRule(method="retrieval", when=None)
    assert rule.matches({})
    assert rule.matches({"agent": "anything"})
