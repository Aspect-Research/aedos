"""Tests for src.pattern_registry (v0.5).

Patterns no longer carry verification routing in v0.5. The registry
just supplies structural metadata (slots, examples, query strategy).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.legacy.pattern_registry import (
    Pattern,
    PatternRegistry,
    PatternRegistryError,
    load_default_registry,
    reset_cache,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


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
        assert p.example_extractions, f"{p.name} should have at least one worked example"


def test_patterns_likely_to_use_retrieval_have_query_strategy():
    """Patterns that the LLM router will typically route to retrieval
    (categorical, role_assignment, event, …) need a slot-aware query
    strategy. quantitative and relational also have one because the
    router falls back to retrieval for non-computable instances.
    """
    reg = load_default_registry()
    # Patterns that routinely produce retrieval-routed claims:
    for name in ("role_assignment", "categorical", "event", "spatial_temporal",
                 "quantitative", "relational"):
        p = reg.get(name)
        assert p.query_strategy, f"{name} should declare a query strategy"


# ---------- v0.5 cleanup: verification routing fields are gone ----------


def test_pattern_has_no_verification_routing_fields():
    """v0.5 §9 cleanup. Patterns should NOT carry verification_rules,
    predicate_overrides, or flag_non_user_as_anomaly fields.
    """
    reg = load_default_registry()
    p = reg.get("preference")
    for attr in ("verification_rules", "predicate_overrides",
                 "flag_non_user_as_anomaly", "resolve_method",
                 "fallback_method", "has_user_authoritative_branch"):
        assert not hasattr(p, attr), (
            f"Pattern.{attr!r} should be gone in v0.5"
        )


def test_yaml_does_not_carry_verification_method():
    """Sanity-check the on-disk patterns.yaml — the cleanup must remove
    the routing fields for real, not just from the dataclass.
    """
    raw = yaml.safe_load((REPO_ROOT / "patterns.yaml").read_text(encoding="utf-8"))
    for name, body in raw.items():
        for field_name in ("verification_method", "predicate_overrides",
                           "flag_non_user_as_anomaly"):
            assert field_name not in body, (
                f"patterns.yaml::{name} still carries {field_name!r}"
            )


# ---------- describe_for_prompt ----------


def test_describe_for_prompt_includes_every_pattern():
    reg = load_default_registry()
    text = reg.describe_for_prompt()
    for name in reg.names():
        assert name in text, f"{name} missing from prompt-formatted registry"
    assert "Slots:" in text


def test_describe_for_prompt_does_not_mention_verification():
    """The extractor's prompt no longer needs to know about routing."""
    reg = load_default_registry()
    text = reg.describe_for_prompt()
    assert "Verification:" not in text


# ---------- error paths ----------


def _write_yaml(tmp_path: Path, body: dict) -> Path:
    p = tmp_path / "p.yaml"
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    return p


def test_missing_required_field(tmp_path):
    bad = _write_yaml(
        tmp_path,
        {"foo": {"description": "x"}},
    )
    with pytest.raises(PatternRegistryError, match="missing fields"):
        PatternRegistry.from_yaml(bad)


def test_unknown_slot_type(tmp_path):
    bad = _write_yaml(
        tmp_path,
        {
            "foo": {
                "description": "x",
                "slots": [{"name": "s", "type": "molecule"}],
            }
        },
    )
    with pytest.raises(PatternRegistryError, match="slot type"):
        PatternRegistry.from_yaml(bad)


def test_get_unknown_pattern_raises():
    reg = load_default_registry()
    with pytest.raises(PatternRegistryError):
        reg.get("not_a_real_pattern")


def test_minimal_pattern_loads(tmp_path):
    """A pattern with just description + slots should load — no
    verification fields are required in v0.5.
    """
    minimal = _write_yaml(
        tmp_path,
        {
            "foo": {
                "description": "an example pattern",
                "slots": [{"name": "subject", "type": "entity", "required": True}],
            }
        },
    )
    reg = PatternRegistry.from_yaml(minimal)
    p = reg.get("foo")
    assert isinstance(p, Pattern)
    assert p.required_slot_names() == ["subject"]


# ---- coverage gaps ----


def test_pattern_slot_lookup_returns_none_for_unknown():
    """Pattern.slot(name) returns the Slot when found, None otherwise."""
    reg = load_default_registry()
    p = reg.get("preference")
    assert p.slot("agent") is not None
    assert p.slot("nonexistent") is None


def test_registry_has_works():
    reg = load_default_registry()
    assert reg.has("preference")
    assert not reg.has("not_a_pattern")


def test_empty_yaml_raises(tmp_path):
    """from_yaml rejects empty/malformed top-level mappings."""
    bad = tmp_path / "empty.yaml"
    bad.write_text("", encoding="utf-8")
    with pytest.raises(PatternRegistryError, match="non-empty mapping"):
        PatternRegistry.from_yaml(bad)


def test_yaml_top_level_list_raises(tmp_path):
    bad = tmp_path / "list.yaml"
    bad.write_text("- foo\n- bar\n", encoding="utf-8")
    with pytest.raises(PatternRegistryError, match="non-empty mapping"):
        PatternRegistry.from_yaml(bad)


def test_pattern_body_must_be_mapping(tmp_path):
    bad = _write_yaml(tmp_path, {"foo": "not a dict"})
    with pytest.raises(PatternRegistryError, match="must be a mapping"):
        PatternRegistry.from_yaml(bad)


def test_pattern_with_no_slots_raises(tmp_path):
    bad = _write_yaml(
        tmp_path,
        {"foo": {"description": "x", "slots": []}},
    )
    with pytest.raises(PatternRegistryError, match="at least one slot"):
        PatternRegistry.from_yaml(bad)


def test_slot_must_be_mapping(tmp_path):
    bad = _write_yaml(
        tmp_path,
        {"foo": {"description": "x", "slots": ["not a dict"]}},
    )
    with pytest.raises(PatternRegistryError, match="each slot must be a mapping"):
        PatternRegistry.from_yaml(bad)


def test_slot_must_have_name_and_type(tmp_path):
    bad = _write_yaml(
        tmp_path,
        {"foo": {
            "description": "x",
            "slots": [{"type": "entity", "required": True}],  # no name
        }},
    )
    with pytest.raises(PatternRegistryError, match="slot missing 'name'"):
        PatternRegistry.from_yaml(bad)
