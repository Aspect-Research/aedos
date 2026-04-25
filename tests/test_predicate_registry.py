"""Tests for src.predicate_registry."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.predicate_registry import (
    PredicateRegistry,
    PredicateRegistryError,
    load_default_registry,
    reset_cache,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _reset_registry_cache():
    reset_cache()
    yield
    reset_cache()


def test_default_registry_loads():
    reg = load_default_registry()
    assert len(reg.all()) >= 25, "registry should have a substantive vocabulary"

    # Key anchor predicates from the spec must exist.
    for name in (
        "likes",
        "dislikes",
        "lives_in",
        "has_count",
        "spelled_as",
        "capital_of",
        "will_happen",
    ):
        assert reg.has(name), f"expected {name!r} in default registry"


def test_every_method_represented():
    reg = load_default_registry()
    for method in ("user_authoritative", "python", "retrieval", "unverifiable"):
        assert reg.by_method(method), f"no predicates with verification_method={method}"


def test_python_predicates_have_verifier_names():
    reg = load_default_registry()
    for p in reg.by_method("python"):
        assert p.python_verifier, f"{p.name} must declare python_verifier"


def test_get_unknown_raises():
    reg = load_default_registry()
    with pytest.raises(PredicateRegistryError):
        reg.get("this_predicate_does_not_exist")


def test_describe_for_prompt_includes_all(tmp_path):
    reg = load_default_registry()
    text = reg.describe_for_prompt()
    for name in reg.names():
        assert name in text, f"{name} missing from prompt-formatted registry"


def _write_yaml(tmp_path: Path, body: dict) -> Path:
    p = tmp_path / "p.yaml"
    p.write_text(yaml.safe_dump(body), encoding="utf-8")
    return p


def test_missing_required_field(tmp_path):
    bad = _write_yaml(
        tmp_path,
        {"likes": {"object_type": "entity", "verification_method": "user_authoritative", "description": "x"}},
    )
    with pytest.raises(PredicateRegistryError, match="missing fields"):
        PredicateRegistry.from_yaml(bad)


def test_invalid_object_type(tmp_path):
    bad = _write_yaml(
        tmp_path,
        {
            "p": {
                "object_type": "banana",
                "verification_method": "user_authoritative",
                "description": "x",
                "example": "x",
            }
        },
    )
    with pytest.raises(PredicateRegistryError, match="object_type"):
        PredicateRegistry.from_yaml(bad)


def test_invalid_verification_method(tmp_path):
    bad = _write_yaml(
        tmp_path,
        {
            "p": {
                "object_type": "int",
                "verification_method": "telepathy",
                "description": "x",
                "example": "x",
            }
        },
    )
    with pytest.raises(PredicateRegistryError, match="verification_method"):
        PredicateRegistry.from_yaml(bad)


def test_python_method_requires_verifier_name(tmp_path):
    bad = _write_yaml(
        tmp_path,
        {
            "p": {
                "object_type": "int",
                "verification_method": "python",
                "description": "x",
                "example": "x",
            }
        },
    )
    with pytest.raises(PredicateRegistryError, match="python_verifier"):
        PredicateRegistry.from_yaml(bad)


def test_nonpython_method_rejects_verifier_name(tmp_path):
    bad = _write_yaml(
        tmp_path,
        {
            "p": {
                "object_type": "string",
                "verification_method": "user_authoritative",
                "description": "x",
                "example": "x",
                "python_verifier": "whatever",
            }
        },
    )
    with pytest.raises(PredicateRegistryError, match="only valid when"):
        PredicateRegistry.from_yaml(bad)


def test_empty_file_rejected(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("", encoding="utf-8")
    with pytest.raises(PredicateRegistryError):
        PredicateRegistry.from_yaml(p)


# ---------- Section 2: role predicates ----------


@pytest.mark.parametrize(
    "name", ["holds_role", "is_a", "headed_by", "member_of", "succeeded_by", "preceded_by"]
)
def test_role_predicate_loads_with_retrieval_method(name):
    reg = load_default_registry()
    p = reg.get(name)
    assert p.verification_method == "retrieval"
    assert p.example, f"{name} must have a non-empty example"
    assert p.description, f"{name} must have a non-empty description"
    assert p.retrieval_query_template, (
        f"{name} must declare a retrieval_query_template (Section 3 needs it)"
    )


def test_holds_role_distinct_from_is_a():
    reg = load_default_registry()
    holds_role = reg.get("holds_role")
    is_a = reg.get("is_a")
    # Both string-typed retrieval predicates, but their descriptions must
    # discriminate them — these two are the most-confused pair.
    assert "role" in holds_role.description.lower() or "position" in holds_role.description.lower()
    assert "category" in is_a.description.lower() or "profession" in is_a.description.lower()


def test_retrieval_query_template_only_on_retrieval_predicates(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "user_pred": {
                    "object_type": "string",
                    "verification_method": "user_authoritative",
                    "retrieval_query_template": "{subject}",
                    "description": "x",
                    "example": "x",
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PredicateRegistryError, match="retrieval_query_template"):
        PredicateRegistry.from_yaml(bad)


def test_existing_retrieval_predicates_now_have_templates():
    """v0.1 retrieval predicates need templates so they actually verify in v0.2."""
    reg = load_default_registry()
    for name in ("capital_of", "born_in_year", "located_in", "authored_by", "founded_in"):
        assert reg.get(name).retrieval_query_template, (
            f"{name} should declare a retrieval_query_template"
        )
