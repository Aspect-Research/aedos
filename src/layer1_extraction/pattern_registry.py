"""Pattern registry (v0.14).

Loads ``src/layer1_extraction/patterns.yaml`` and exposes
lookup + prompt-formatting helpers. The pattern catalog is bounded
(9 patterns in v0.14: the legacy 8 plus mereological); predicate
labels within a pattern are free-form.

Patterns do not carry verification routing — the LLM router (Phase 2)
decides per-claim. The pattern's role is purely structural
classification: it shapes extraction, defines slot identity for
store lookups, and supplies a query strategy for retrieval when
that's the chosen method.

Identical to the v0.13 module shape; the only delta is the
``_DEFAULT_PATH`` pointing at the v2 yaml under
``layer1_extraction/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SLOT_TYPES = {
    "entity",
    "string",
    "entity_or_string",
    "date",
    "list",
    "any",
}


class PatternRegistryError(ValueError):
    """Raised when patterns.yaml is malformed or references unknown items."""


@dataclass(frozen=True)
class Slot:
    name: str
    type: str
    required: bool


@dataclass(frozen=True)
class Pattern:
    name: str
    description: str
    slots: tuple[Slot, ...]
    example_predicates: tuple[str, ...]
    example_extractions: tuple[dict, ...] = field(default_factory=tuple)
    disambiguation_notes: str = ""
    query_strategy: tuple[str, ...] = field(default_factory=tuple)

    def slot(self, name: str) -> Slot | None:
        for s in self.slots:
            if s.name == name:
                return s
        return None

    def required_slot_names(self) -> list[str]:
        return [s.name for s in self.slots if s.required]


class PatternRegistry:
    def __init__(self, patterns: dict[str, Pattern]):
        self._patterns = patterns

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PatternRegistry":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not raw:
            raise PatternRegistryError(
                f"{path}: expected a non-empty mapping at the top level"
            )
        patterns: dict[str, Pattern] = {}
        for name, body in raw.items():
            patterns[name] = _build_pattern(name, body)
        return cls(patterns)

    def get(self, name: str) -> Pattern:
        if name not in self._patterns:
            raise PatternRegistryError(f"unknown pattern: {name!r}")
        return self._patterns[name]

    def has(self, name: str) -> bool:
        return name in self._patterns

    def all(self) -> list[Pattern]:
        return list(self._patterns.values())

    def names(self) -> list[str]:
        return list(self._patterns.keys())

    def describe_for_prompt(self) -> str:
        """LLM-readable summary of the pattern catalog. One section per pattern."""
        lines: list[str] = []
        for p in self._patterns.values():
            lines.append(f"## pattern: {p.name}")
            lines.append(p.description.strip())
            lines.append("")
            lines.append("Slots:")
            for s in p.slots:
                req = "required" if s.required else "optional"
                lines.append(f"  - {s.name} ({s.type}, {req})")
            if p.example_predicates:
                lines.append("")
                lines.append(
                    f"Example predicates (free-form, not exhaustive): "
                    f"{', '.join(p.example_predicates)}"
                )
            if p.disambiguation_notes:
                lines.append("")
                lines.append("Disambiguation:")
                for line in p.disambiguation_notes.strip().splitlines():
                    lines.append(f"  {line}")
            if p.example_extractions:
                lines.append("")
                lines.append("Examples:")
                for ex in p.example_extractions:
                    src = ex.get("input", "")
                    out = ex.get("output", {})
                    lines.append(
                        f"  - {src!r}\n    → pattern={out.get('pattern')}, "
                        f"predicate={out.get('predicate')}, slots={out.get('slots')}"
                    )
            lines.append("")
        return "\n".join(lines).strip()


def _build_pattern(name: str, body: object) -> Pattern:
    if not isinstance(body, dict):
        raise PatternRegistryError(
            f"{name}: entry must be a mapping, got {type(body).__name__}"
        )

    required_fields = {"description", "slots"}
    missing = required_fields - body.keys()
    if missing:
        raise PatternRegistryError(f"{name}: missing fields {sorted(missing)}")

    slots = tuple(_build_slot(name, s) for s in body["slots"])
    if not slots:
        raise PatternRegistryError(f"{name}: at least one slot required")

    example_predicates = tuple(body.get("example_predicates") or ())
    example_extractions = tuple(body.get("example_extractions") or ())
    query_strategy = tuple(body.get("query_strategy") or ())

    return Pattern(
        name=name,
        description=str(body["description"]),
        slots=slots,
        example_predicates=example_predicates,
        example_extractions=example_extractions,
        disambiguation_notes=str(body.get("disambiguation_notes", "") or ""),
        query_strategy=query_strategy,
    )


def _build_slot(pattern_name: str, raw: object) -> Slot:
    if not isinstance(raw, dict):
        raise PatternRegistryError(
            f"{pattern_name}: each slot must be a mapping, got {type(raw).__name__}"
        )
    for k in ("name", "type"):
        if k not in raw:
            raise PatternRegistryError(f"{pattern_name}: slot missing {k!r}")
    if raw["type"] not in SLOT_TYPES:
        raise PatternRegistryError(
            f"{pattern_name}: unknown slot type {raw['type']!r} "
            f"(allowed: {sorted(SLOT_TYPES)})"
        )
    return Slot(name=str(raw["name"]), type=str(raw["type"]),
                required=bool(raw.get("required", False)))


_DEFAULT_PATH = Path(__file__).resolve().parent / "patterns.yaml"
_cached: PatternRegistry | None = None


def load_default_registry() -> PatternRegistry:
    global _cached
    if _cached is None:
        _cached = PatternRegistry.from_yaml(_DEFAULT_PATH)
    return _cached


def reset_cache() -> None:
    global _cached
    _cached = None
