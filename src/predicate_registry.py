"""Predicate registry.

Loads predicates.yaml at startup, validates it, exposes lookups, and formats
the registry for the extractor prompt.

This is a bounded vocabulary on purpose — the extractor must never invent
predicates. If a claim doesn't fit an existing entry, it should be dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

VERIFICATION_METHODS = {
    "user_authoritative",
    "python",
    "store_lookup",
    "retrieval",
    "unverifiable",
}

OBJECT_TYPES = {"int", "string", "bool", "entity", "count"}


@dataclass(frozen=True)
class Predicate:
    name: str
    object_type: str
    verification_method: str
    description: str
    example: str
    python_verifier: str | None = None


class PredicateRegistryError(ValueError):
    """Raised when predicates.yaml is malformed or references an unknown entry."""


class PredicateRegistry:
    def __init__(self, predicates: dict[str, Predicate]):
        self._predicates = predicates

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PredicateRegistry":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not raw:
            raise PredicateRegistryError(
                f"{path}: expected a non-empty mapping at the top level"
            )
        predicates: dict[str, Predicate] = {}
        for name, body in raw.items():
            predicates[name] = _build_predicate(name, body)
        return cls(predicates)

    def get(self, name: str) -> Predicate:
        if name not in self._predicates:
            raise PredicateRegistryError(f"unknown predicate: {name!r}")
        return self._predicates[name]

    def has(self, name: str) -> bool:
        return name in self._predicates

    def all(self) -> list[Predicate]:
        return list(self._predicates.values())

    def names(self) -> list[str]:
        return list(self._predicates.keys())

    def by_method(self, method: str) -> list[Predicate]:
        return [p for p in self._predicates.values() if p.verification_method == method]

    def describe_for_prompt(self) -> str:
        """Return a compact, LLM-readable description of every predicate.

        Grouped by verification method so the extractor can see the shape of
        the vocabulary at a glance.
        """
        lines: list[str] = []
        for method in (
            "user_authoritative",
            "python",
            "store_lookup",
            "retrieval",
            "unverifiable",
        ):
            entries = self.by_method(method)
            if not entries:
                continue
            lines.append(f"## verification_method: {method}")
            for p in entries:
                lines.append(f"- {p.name} (object_type={p.object_type})")
                lines.append(f"    {p.description.strip()}")
                lines.append(f"    example: {p.example}")
            lines.append("")
        return "\n".join(lines).strip()


def _build_predicate(name: str, body: object) -> Predicate:
    if not isinstance(body, dict):
        raise PredicateRegistryError(f"{name}: entry must be a mapping, got {type(body).__name__}")

    required = {"object_type", "verification_method", "description", "example"}
    missing = required - body.keys()
    if missing:
        raise PredicateRegistryError(f"{name}: missing fields {sorted(missing)}")

    object_type = body["object_type"]
    if object_type not in OBJECT_TYPES:
        raise PredicateRegistryError(
            f"{name}: object_type must be one of {sorted(OBJECT_TYPES)}, got {object_type!r}"
        )

    method = body["verification_method"]
    if method not in VERIFICATION_METHODS:
        raise PredicateRegistryError(
            f"{name}: verification_method must be one of {sorted(VERIFICATION_METHODS)}, got {method!r}"
        )

    python_verifier = body.get("python_verifier")
    if method == "python" and not python_verifier:
        raise PredicateRegistryError(
            f"{name}: verification_method=python requires a 'python_verifier' field"
        )
    if method != "python" and python_verifier:
        raise PredicateRegistryError(
            f"{name}: 'python_verifier' is only valid when verification_method=python"
        )

    return Predicate(
        name=name,
        object_type=object_type,
        verification_method=method,
        description=str(body["description"]),
        example=str(body["example"]),
        python_verifier=python_verifier,
    )


_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "predicates.yaml"
_cached: PredicateRegistry | None = None


def load_default_registry() -> PredicateRegistry:
    """Load the registry from the repo-root predicates.yaml, cached."""
    global _cached
    if _cached is None:
        _cached = PredicateRegistry.from_yaml(_DEFAULT_PATH)
    return _cached


def reset_cache() -> None:
    """For tests — force the next load_default_registry() to re-read from disk."""
    global _cached
    _cached = None


def predicate_names(registry: PredicateRegistry | None = None) -> Iterable[str]:
    return (registry or load_default_registry()).names()
