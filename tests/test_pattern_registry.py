"""Tests for src.layer1_extraction.pattern_registry (v0.14).

Ports v1's test_pattern_registry.py with:
  * 9 expected patterns (legacy 8 + mereological).
  * Mereological-specific assertions: slots are part/whole, both required;
    example_predicates carry part_of/member_of/composed_of; member_of is
    NOT in categorical or relational example_predicates anymore.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.layer1_extraction.pattern_registry import (
    Pattern,
    PatternRegistry,
    PatternRegistryError,
    load_default_registry,
    reset_cache,
)

V2_YAML_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "layer1_extraction" / "patterns.yaml"
)


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
    "mereological",
}


def test_default_registry_has_all_nine_patterns():
    reg = load_default_registry()
    assert set(reg.names()) == EXPECTED_PATTERNS


def test_each_pattern_has_required_metadata():
    reg = load_default_registry()
    for p in reg.all():
        assert p.description, f"{p.name} missing description"
        assert p.slots, f"{p.name} has no slots"
        assert p.example_extractions, f"{p.name} should have at least one worked example"


def test_patterns_likely_to_use_retrieval_have_query_strategy():
    """Mereological joins the existing retrieval-routed patterns. The
    Phase 2 LLM router will route mereological claims to retrieval by
    default, so it needs a slot-aware query strategy.
    """
    reg = load_default_registry()
    for name in (
        "role_assignment", "categorical", "event", "spatial_temporal",
        "quantitative", "relational", "mereological",
    ):
        p = reg.get(name)
        assert p.query_strategy, f"{name} should declare a query strategy"


# ---------- mereological-specific shape ----------


def test_mereological_has_part_and_whole_slots():
    reg = load_default_registry()
    p = reg.get("mereological")
    slot_names = {s.name for s in p.slots}
    assert "part" in slot_names, "mereological must declare a 'part' slot"
    assert "whole" in slot_names, "mereological must declare a 'whole' slot"
    assert p.slot("part").required, "'part' must be required"
    assert p.slot("whole").required, "'whole' must be required"
    assert p.slot("part").type == "entity"
    assert p.slot("whole").type == "entity"


def test_mereological_required_slot_names():
    """The required-slot list is exactly part + whole (date slots are
    optional)."""
    reg = load_default_registry()
    p = reg.get("mereological")
    assert set(p.required_slot_names()) == {"part", "whole"}


def test_mereological_example_predicates_cover_constitutive_parthood():
    reg = load_default_registry()
    p = reg.get("mereological")
    expected = {"part_of", "member_of", "composed_of"}
    assert expected.issubset(set(p.example_predicates)), (
        f"mereological example_predicates {p.example_predicates} should cover {expected}"
    )


def test_member_of_moved_out_of_categorical():
    """Phase 1: member_of belongs to mereological, not categorical."""
    reg = load_default_registry()
    cat = reg.get("categorical")
    assert "member_of" not in cat.example_predicates, (
        "member_of should be in mereological's example_predicates, not categorical's"
    )


def test_member_of_not_in_relational_example_predicates():
    """Confirm relational doesn't carry member_of either."""
    reg = load_default_registry()
    rel = reg.get("relational")
    assert "member_of" not in rel.example_predicates


def test_mereological_disambiguation_contrasts_spatial_temporal():
    """The disambiguation block must mention both spatial_temporal and
    the constitutive/locational distinction so the extractor's prompt
    inherits the contrast verbatim.
    """
    reg = load_default_registry()
    p = reg.get("mereological")
    notes = p.disambiguation_notes.lower()
    assert "spatial_temporal" in notes
    assert "constitutive" in notes or "constitut" in notes
    assert "locational" in notes


def test_spatial_temporal_disambiguation_now_mentions_mereological():
    """The cross-reference goes both ways: spatial_temporal's notes
    must point at mereological so the extractor sees the contrast from
    either side."""
    reg = load_default_registry()
    p = reg.get("spatial_temporal")
    notes = p.disambiguation_notes.lower()
    assert "mereological" in notes


def test_mereological_examples_never_have_part_equals_whole():
    """Phase 1 enforces this only via example consistency. Phase 2's
    validator owns the runtime invariant; this test guards the YAML so
    the extractor never sees a self-parthood example."""
    reg = load_default_registry()
    p = reg.get("mereological")
    for ex in p.example_extractions:
        slots = ex.get("output", {}).get("slots", {})
        part = slots.get("part")
        whole = slots.get("whole")
        if part is not None and whole is not None:
            assert part != whole, (
                f"Self-parthood example in mereological YAML: part={part!r} == whole={whole!r}"
            )


# ---------- v0.5 cleanup: verification routing fields are gone ----------


def test_pattern_has_no_verification_routing_fields():
    """v0.5 cleanup, preserved into v0.14: patterns should NOT carry
    verification_rules, predicate_overrides, or
    flag_non_user_as_anomaly fields. Phase 2's router decides routing
    per-claim, not per-pattern.
    """
    reg = load_default_registry()
    p = reg.get("preference")
    for attr in ("verification_rules", "predicate_overrides",
                 "flag_non_user_as_anomaly", "resolve_method",
                 "fallback_method", "has_user_authoritative_branch"):
        assert not hasattr(p, attr), (
            f"Pattern.{attr!r} should not exist on the dataclass"
        )


def test_yaml_does_not_carry_verification_method():
    raw = yaml.safe_load(V2_YAML_PATH.read_text(encoding="utf-8"))
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


def test_describe_for_prompt_includes_mereological_slots():
    """The extractor's prompt must surface the part/whole slots."""
    reg = load_default_registry()
    text = reg.describe_for_prompt()
    # The pattern header must be present, and the slot lines beneath
    # it must list both part and whole as required.
    assert "## pattern: mereological" in text
    # Find the mereological section and assert both slots are listed
    # as required.
    section = text.split("## pattern: mereological", 1)[1]
    # The next section starts with another '## pattern:' header.
    if "## pattern:" in section:
        section = section.split("## pattern:", 1)[0]
    assert "part (entity, required)" in section
    assert "whole (entity, required)" in section


def test_describe_for_prompt_does_not_mention_verification():
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
    reg = load_default_registry()
    p = reg.get("preference")
    assert p.slot("agent") is not None
    assert p.slot("nonexistent") is None


def test_registry_has_works():
    reg = load_default_registry()
    assert reg.has("preference")
    assert reg.has("mereological")
    assert not reg.has("not_a_pattern")


def test_empty_yaml_raises(tmp_path):
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
            "slots": [{"type": "entity", "required": True}],
        }},
    )
    with pytest.raises(PatternRegistryError, match="slot missing 'name'"):
        PatternRegistry.from_yaml(bad)

