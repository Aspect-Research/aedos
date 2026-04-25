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
