"""Pattern registry.

Loads patterns.yaml at startup and exposes lookup + prompt-formatting
helpers. The pattern catalog is bounded (8 patterns); predicate labels
within a pattern are free-form.

Verification method may be a plain string ("retrieval") or a list of
conditional rules — each rule has an optional ``when`` slot-match clause
and a ``method``. First matching rule wins; the last rule (no ``when``)
is the default. The router calls ``resolve_method(slots)`` to pick.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

VERIFICATION_METHODS = {
    "user_authoritative",
    "python",
    "store_lookup",
    "retrieval",
    "unverifiable",
}

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
class VerificationRule:
    """A single rule in a pattern's verification_method.

    A rule with ``when=None`` is a catch-all default and must come last.
    A rule's ``when`` is a dict of slot-name → expected value (string).
    """

    method: str
    when: dict[str, str] | None = None

    def matches(self, slots: dict[str, Any]) -> bool:
        if self.when is None:
            return True
        for slot_name, expected in self.when.items():
            actual = slots.get(slot_name)
            if isinstance(actual, str) and actual.strip().lower() != str(expected).strip().lower():
                return False
            if not isinstance(actual, str) and actual != expected:
                return False
        return True


@dataclass(frozen=True)
class Pattern:
    name: str
    description: str
    slots: tuple[Slot, ...]
    verification_rules: tuple[VerificationRule, ...]
    example_predicates: tuple[str, ...]
    example_extractions: tuple[dict, ...] = field(default_factory=tuple)
    disambiguation_notes: str = ""
    query_strategy: tuple[str, ...] = field(default_factory=tuple)
    flag_non_user_as_anomaly: bool = False
    # v0.4: per-predicate verification_method overrides. Maps a specific
    # predicate label to a method that supersedes the rule list. The
    # router checks this BEFORE walking ``verification_rules``.
    predicate_overrides: dict[str, str] = field(default_factory=dict)

    def slot(self, name: str) -> Slot | None:
        for s in self.slots:
            if s.name == name:
                return s
        return None

    def required_slot_names(self) -> list[str]:
        return [s.name for s in self.slots if s.required]

    def resolve_method(self, slots: dict[str, Any], *, predicate: str | None = None) -> str:
        """Pick the verification_method for this fact's slots.

        ``predicate`` is consulted against ``predicate_overrides`` before
        the rule list. v0.4: routes ``relational.reverse_of`` etc. to
        python without changing the pattern's default retrieval rule.
        """
        if predicate is not None and predicate in self.predicate_overrides:
            return self.predicate_overrides[predicate]
        for rule in self.verification_rules:
            if rule.matches(slots):
                return rule.method
        # Should be unreachable — registry validation ensures a default rule.
        raise PatternRegistryError(
            f"pattern {self.name!r} has no matching verification rule for slots={slots!r}"
        )

    def fallback_method(self, slots: dict[str, Any]) -> str:
        """Pick the next non-python rule that matches.

        Used by the router when a python verification stage returns
        ``not_python_verifiable`` and we need to fall through.
        """
        for rule in self.verification_rules:
            if rule.method == "python":
                continue
            if rule.matches(slots):
                return rule.method
        return "unverifiable"

    def has_user_authoritative_branch(self) -> bool:
        """True if any rule resolves to user_authoritative for some slot value."""
        return any(r.method == "user_authoritative" for r in self.verification_rules)


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

    def by_method(self, method: str) -> list[Pattern]:
        """Patterns whose default verification method is the given string.

        For conditional rules, this matches the LAST (default) rule.
        """
        out: list[Pattern] = []
        for p in self._patterns.values():
            if p.verification_rules and p.verification_rules[-1].method == method:
                out.append(p)
        return out

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
            lines.append("")
            lines.append(f"Verification: {_describe_rules(p.verification_rules)}")
            if p.example_predicates:
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


def _describe_rules(rules: tuple[VerificationRule, ...]) -> str:
    parts: list[str] = []
    for rule in rules:
        if rule.when:
            cond = " AND ".join(f"{k}={v!r}" for k, v in rule.when.items())
            parts.append(f"if {cond} → {rule.method}")
        else:
            parts.append(f"default → {rule.method}")
    return "; ".join(parts)


def _build_pattern(name: str, body: object) -> Pattern:
    if not isinstance(body, dict):
        raise PatternRegistryError(
            f"{name}: entry must be a mapping, got {type(body).__name__}"
        )

    required_fields = {"description", "slots", "verification_method"}
    missing = required_fields - body.keys()
    if missing:
        raise PatternRegistryError(f"{name}: missing fields {sorted(missing)}")

    slots = tuple(_build_slot(name, s) for s in body["slots"])
    if not slots:
        raise PatternRegistryError(f"{name}: at least one slot required")

    rules = _build_verification_rules(name, body["verification_method"])

    example_predicates = tuple(body.get("example_predicates") or ())
    example_extractions = tuple(body.get("example_extractions") or ())
    query_strategy = tuple(body.get("query_strategy") or ())

    flag = bool(body.get("flag_non_user_as_anomaly", False))

    overrides_raw = body.get("predicate_overrides") or {}
    if not isinstance(overrides_raw, dict):
        raise PatternRegistryError(
            f"{name}: predicate_overrides must be a mapping, "
            f"got {type(overrides_raw).__name__}"
        )
    overrides: dict[str, str] = {}
    for predicate, method in overrides_raw.items():
        if method not in VERIFICATION_METHODS:
            raise PatternRegistryError(
                f"{name}: predicate_overrides[{predicate!r}] = {method!r} "
                f"not in {sorted(VERIFICATION_METHODS)}"
            )
        overrides[str(predicate)] = str(method)

    return Pattern(
        name=name,
        description=str(body["description"]),
        slots=slots,
        verification_rules=rules,
        example_predicates=example_predicates,
        example_extractions=example_extractions,
        disambiguation_notes=str(body.get("disambiguation_notes", "") or ""),
        query_strategy=query_strategy,
        flag_non_user_as_anomaly=flag,
        predicate_overrides=overrides,
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


def _build_verification_rules(
    pattern_name: str, raw: object
) -> tuple[VerificationRule, ...]:
    # Plain string shorthand: a single default rule.
    if isinstance(raw, str):
        if raw not in VERIFICATION_METHODS:
            raise PatternRegistryError(
                f"{pattern_name}: verification_method {raw!r} "
                f"not in {sorted(VERIFICATION_METHODS)}"
            )
        return (VerificationRule(method=raw, when=None),)

    if not isinstance(raw, list) or not raw:
        raise PatternRegistryError(
            f"{pattern_name}: verification_method must be a string or a non-empty "
            f"list of rules, got {type(raw).__name__}"
        )

    rules: list[VerificationRule] = []
    for r in raw:
        if not isinstance(r, dict):
            raise PatternRegistryError(
                f"{pattern_name}: each rule must be a mapping, got {type(r).__name__}"
            )
        method = r.get("method")
        if method not in VERIFICATION_METHODS:
            raise PatternRegistryError(
                f"{pattern_name}: rule has bad method {method!r} "
                f"(allowed: {sorted(VERIFICATION_METHODS)})"
            )
        when_raw = r.get("when")
        if when_raw is not None and not isinstance(when_raw, dict):
            raise PatternRegistryError(
                f"{pattern_name}: rule 'when' must be a mapping, "
                f"got {type(when_raw).__name__}"
            )
        when = {str(k): str(v) for k, v in when_raw.items()} if when_raw else None
        rules.append(VerificationRule(method=method, when=when))

    if rules[-1].when is not None:
        raise PatternRegistryError(
            f"{pattern_name}: the last rule must be a default (no 'when' clause)"
        )
    return tuple(rules)


_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "patterns.yaml"
_cached: PatternRegistry | None = None


def load_default_registry() -> PatternRegistry:
    global _cached
    if _cached is None:
        _cached = PatternRegistry.from_yaml(_DEFAULT_PATH)
    return _cached


def reset_cache() -> None:
    global _cached
    _cached = None
