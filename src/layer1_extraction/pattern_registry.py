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

    # v0.14.3 schema fields — each pattern self-declares the disciplines
    # downstream layers enforce. Single source of truth: validator,
    # triage, router, and extractor all read from the schema rather
    # than hard-coding their own copy.
    #
    # agent_constraint: when set to "must_be_user", the validator
    #   enforces that the slot named in subject_slot_for_constraint
    #   names the user (in {user, me, i}). Drives USER_SUBJECT_PATTERNS.
    # subject_slot_for_constraint: which slot the agent_constraint
    #   applies to (typically 'agent'). Defaults to 'agent' when
    #   agent_constraint is set.
    # distinct_slots: pair of slot names that must hold distinct
    #   values (case-insensitive on strings). Drives mereological's
    #   part != whole invariant. Format: [slot_a, slot_b].
    # default_routing_method: the dominant routing-method this
    #   pattern's claims should land on (python /
    #   python_with_canonical_constants / retrieval / user_authoritative
    #   / unverifiable). Used by the LLM router as a prior + by the
    #   triage cross-check.
    # triage_verify_predicates: predicates within this pattern that
    #   ALWAYS trigger triage VERIFY regardless of slot shape. Drives
    #   the per-pattern slice of _COMPUTABLE_PREDICATES.
    # boundary_examples: mis-classification examples — claims that
    #   look like this pattern but belong to another. Assembled into
    #   the extractor's prompt so the extractor sees the boundary
    #   explicitly.
    agent_constraint: str | None = None
    subject_slot_for_constraint: str | None = None
    distinct_slots: tuple[str, str] | None = None
    default_routing_method: str | None = None
    triage_verify_predicates: tuple[str, ...] = field(default_factory=tuple)
    boundary_examples: tuple[dict, ...] = field(default_factory=tuple)

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
            # v0.14.3 — boundary_examples surface the cases that
            # commonly mis-classify INTO this pattern (so the
            # extractor sees what's NOT this pattern alongside what
            # IS). This is the schema-driven version of "tell the
            # extractor about adversarial inputs"; previously it
            # lived only in disambiguation_notes prose.
            if p.boundary_examples:
                lines.append("")
                lines.append("Boundary cases (mis-classification examples):")
                for ex in p.boundary_examples:
                    src = ex.get("input", "")
                    is_this = ex.get("this_pattern")
                    correct = ex.get("correct_pattern")
                    reason = (ex.get("reason") or "").strip()
                    if is_this is False and correct:
                        lines.append(
                            f"  - {src!r} → NOT {p.name}, use {correct} instead"
                        )
                    elif is_this is True:
                        lines.append(f"  - {src!r} → IS {p.name} (canonical case)")
                    else:
                        lines.append(f"  - {src!r}")
                    if reason:
                        for rline in reason.splitlines():
                            lines.append(f"      {rline.strip()}")
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
    triage_verify_predicates = tuple(body.get("triage_verify_predicates") or ())
    boundary_examples = tuple(body.get("boundary_examples") or ())

    # distinct_slots: must be a 2-list of slot names if present.
    distinct_raw = body.get("distinct_slots")
    distinct_slots: tuple[str, str] | None = None
    if distinct_raw is not None:
        if (not isinstance(distinct_raw, list)
            or len(distinct_raw) != 2
            or not all(isinstance(s, str) for s in distinct_raw)):
            raise PatternRegistryError(
                f"{name}: distinct_slots must be a 2-list of slot "
                f"names, got {distinct_raw!r}"
            )
        distinct_slots = (distinct_raw[0], distinct_raw[1])

    agent_constraint = body.get("agent_constraint")
    if agent_constraint is not None and agent_constraint != "must_be_user":
        raise PatternRegistryError(
            f"{name}: agent_constraint, when set, must be "
            f"'must_be_user' (got {agent_constraint!r})"
        )
    subject_slot_for_constraint = body.get("subject_slot_for_constraint")
    if agent_constraint is not None and subject_slot_for_constraint is None:
        subject_slot_for_constraint = "agent"  # sensible default

    return Pattern(
        name=name,
        description=str(body["description"]),
        slots=slots,
        example_predicates=example_predicates,
        example_extractions=example_extractions,
        disambiguation_notes=str(body.get("disambiguation_notes", "") or ""),
        query_strategy=query_strategy,
        agent_constraint=agent_constraint,
        subject_slot_for_constraint=subject_slot_for_constraint,
        distinct_slots=distinct_slots,
        default_routing_method=body.get("default_routing_method"),
        triage_verify_predicates=triage_verify_predicates,
        boundary_examples=boundary_examples,
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
