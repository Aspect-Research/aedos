"""Smoke corpus dispatcher (v0.14 Phase 7).

The smoke corpus at ``tests/v2/smoke_corpus.jsonl`` accretes entries
across phases. By Phase 7 the corpus has five distinct entry shapes,
each demonstrating a different layer of the v2 stack. The dispatcher's
job is to inspect each entry and route it to the right schema
validator (and, in Phase 9, to the right end-to-end runner).

Phase 7's deliverable is **schema validation + shape detection**.
End-to-end execution of corpus entries is Phase 9 territory; the
dispatcher is structured so Phase 9's parity check can plug in
``run_*`` runners alongside the validators without churn.

Five entry shapes
=================

The discriminators are listed in detection order — the first matching
rule wins. Order matters because some entries technically satisfy
multiple shapes' field sets.

  1. **SUBSTRATE_DIRECT** — entry has ``oracle_call`` (a dict naming
     one of the four substrate oracles plus its key columns) and
     ``expected_label``. The entry tests substrate behavior in
     isolation: no extraction, no routing, no Tier U/W. Phase 5
     introduced this shape; Phase 7+ entries with ``expected_via``
     populated remain in this shape.

  2. **TWO_TEXT_ORACLE** — entry has ``text_user`` AND
     ``text_assistant``. The two texts are extracted on consecutive
     turns; the entry's ``expected_oracle_classification`` (and
     optional ``expected_tier_u_outcome``) describes what the
     substrate should classify after the two extractions.

  3. **ROUTING_MEMO** — entry has ``text`` AND ``expected_memo_state``
     ∈ {"n/a", "write", "hit"}. Tests Layer 2's routing-memo behavior
     across the corpus's ordered sequence of (pattern, predicate)
     pairs.

  4. **ASSISTANT_LOOKUP** — entry has ``text`` AND ``role ==
     "assistant"`` (and typically expected_tier_u_outcome inside
     expected_facts). Tests Tier U lookup of the model's claim
     against prior user-asserted facts. Phase 7+ entries here may
     have ``expected_via`` populated with multi-oracle derivation
     chains.

  5. **USER_STORAGE** — fallback. Entry has ``text`` and (role missing
     OR role=="user"). Tests Layer 1 extraction and (if expectations
     reference session-locality or affirmed counts) Layer 2's storage
     path.

Shape detection is monotonically extensible: adding a new shape is
appending to ``_SHAPE_DETECTORS`` in priority order. A future shape
that introduces a new discriminator (e.g. ``derivation_call`` for a
Phase 8 mode) plugs in without re-validating existing entries.

What the dispatcher does NOT do
===============================

  * No end-to-end execution. Validation only. Phase 9's parity check
    builds runners on top.
  * No assertion on accreting state across entries. Each entry's
    schema is validated in isolation; cross-entry expectations like
    "p2-memo-hit-mereological depends on p2-memo-write-mereological"
    are documented as ``prerequisites: list[str]`` on the entry's
    schema and surfaced in cascading-failure formatting (see
    ``format_cascading_failure``), but the dispatcher itself does
    not run the corpus end-to-end.
  * No type coercion. ``polarity: 1`` (int) is not the same as
    ``polarity: "1"`` (str); the dispatcher reports the type mismatch.

Cascading-failure context
=========================

When the corpus is run end-to-end (Phase 9), entry N may fail not
because of its own validation but because entry N-K (an earlier
entry) failed to set up state that N depends on (e.g. a memo write
before a memo hit). ``format_cascading_failure`` produces a message
that names both entries, so debugging cascade failures doesn't
require manually walking the corpus order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


# ============================================================================
# Public types
# ============================================================================


class SmokeEntryShape(str, Enum):
    """The five entry shapes the corpus has accreted."""

    SUBSTRATE_DIRECT = "substrate_direct"
    TWO_TEXT_ORACLE = "two_text_oracle"
    ROUTING_MEMO = "routing_memo"
    ASSISTANT_LOOKUP = "assistant_lookup"
    USER_STORAGE = "user_storage"


@dataclass(frozen=True)
class FieldError:
    """One specific schema violation."""

    path: str            # dotted path into the entry, e.g. "expected_facts[0].polarity"
    expected: str        # human-readable expectation
    actual: Any          # the offending value (or a sentinel for "missing")

    MISSING = "<missing>"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "expected": self.expected,
            "actual": self.actual if self.actual is not FieldError.MISSING else None,
            "missing": self.actual is FieldError.MISSING,
        }


@dataclass(frozen=True)
class EntryValidationResult:
    """Schema-validation outcome for a single corpus entry.

    ``shape`` is None when no shape detector matched. ``errors`` is
    a list of specific violations; the entry is valid iff
    ``errors == []`` and ``shape is not None``.
    """

    entry_id: str
    shape: Optional[SmokeEntryShape]
    errors: list[FieldError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.shape is not None and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "shape": self.shape.value if self.shape else None,
            "ok": self.ok,
            "errors": [e.to_dict() for e in self.errors],
        }


@dataclass(frozen=True)
class CorpusValidationResult:
    """Aggregate validation outcome for an entire corpus file."""

    entries: list[EntryValidationResult]
    duplicate_ids: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.duplicate_ids and all(r.ok for r in self.entries)

    @property
    def failures(self) -> list[EntryValidationResult]:
        return [r for r in self.entries if not r.ok]


# ============================================================================
# Shape detection
# ============================================================================


def detect_shape(entry: dict) -> Optional[SmokeEntryShape]:
    """Return the entry's shape, or None if no detector matches.

    Detection order is the priority order encoded in
    ``_SHAPE_DETECTORS``; the first match wins. Order matters because
    some shapes share fields with others (e.g. session-aware
    ASSISTANT_LOOKUP entries also have ``text``, which would match
    ROUTING_MEMO if the latter's discriminator weren't more
    specific). Each detector below documents why its priority slot
    is what it is.
    """
    for detector in _SHAPE_DETECTORS:
        result = detector(entry)
        if result is not None:
            return result
    return None


def _detect_substrate_direct(entry: dict) -> Optional[SmokeEntryShape]:
    """``oracle_call`` is the most specific discriminator — entries
    that use it bypass extraction entirely and consult the substrate
    directly. No other shape uses this field.
    """
    if "oracle_call" in entry:
        return SmokeEntryShape.SUBSTRATE_DIRECT
    return None


def _detect_two_text_oracle(entry: dict) -> Optional[SmokeEntryShape]:
    """Two-text entries (``text_user`` + ``text_assistant``) are a
    multi-turn shape. They take precedence over single-text shapes
    because a single-text entry never has these fields.
    """
    if "text_user" in entry and "text_assistant" in entry:
        return SmokeEntryShape.TWO_TEXT_ORACLE
    return None


def _detect_routing_memo(entry: dict) -> Optional[SmokeEntryShape]:
    """``expected_memo_state`` is the routing-memo discriminator.
    Phase 2 introduced it; subsequent phases reuse the field name
    only when testing memo behavior. ASSISTANT_LOOKUP entries do
    not carry it.
    """
    if "expected_memo_state" in entry:
        return SmokeEntryShape.ROUTING_MEMO
    return None


def _detect_assistant_lookup(entry: dict) -> Optional[SmokeEntryShape]:
    """``role == "assistant"`` is the assistant-lookup discriminator.
    The entry is testing what happens when the model's draft is
    extracted into a claim and that claim is looked up in Tier U /
    Tier W / derivation. ``role`` defaults to "user" if absent.
    """
    if entry.get("role") == "assistant":
        return SmokeEntryShape.ASSISTANT_LOOKUP
    return None


def _detect_user_storage(entry: dict) -> Optional[SmokeEntryShape]:
    """The fallback shape. Any entry with ``text`` that didn't match
    a more specific shape lands here. The entry tests Layer 1
    extraction (and Layer 2 storage when expected_facts carries
    storage-related expectations).
    """
    if "text" in entry:
        return SmokeEntryShape.USER_STORAGE
    return None


# Order is priority order: most specific first.
_SHAPE_DETECTORS = (
    _detect_substrate_direct,
    _detect_two_text_oracle,
    _detect_routing_memo,
    _detect_assistant_lookup,
    _detect_user_storage,
)


# ============================================================================
# Schema validation
# ============================================================================


def validate_entry(entry: dict) -> EntryValidationResult:
    """Validate one corpus entry against its detected shape's schema.

    The ``entry_id`` field is required on all entries. Missing IDs
    produce an error with ``shape=None`` (we cannot validate further
    without an identifier).
    """
    entry_id = entry.get("id")
    if not isinstance(entry_id, str) or not entry_id:
        return EntryValidationResult(
            entry_id="<unknown>",
            shape=None,
            errors=[FieldError(
                path="id",
                expected="non-empty string",
                actual=entry_id if entry_id is not None else FieldError.MISSING,
            )],
        )

    shape = detect_shape(entry)
    if shape is None:
        return EntryValidationResult(
            entry_id=entry_id,
            shape=None,
            errors=[FieldError(
                path="<entry>",
                expected=(
                    "one of "
                    "{oracle_call, text_user+text_assistant, "
                    "text+expected_memo_state, "
                    "text+role=assistant, text}"
                ),
                actual=sorted(entry.keys()),
            )],
        )

    validator = _SHAPE_VALIDATORS[shape]
    errors = validator(entry)
    return EntryValidationResult(entry_id=entry_id, shape=shape, errors=errors)


def validate_corpus(path: Path | str) -> CorpusValidationResult:
    """Read the corpus file at ``path`` and validate every entry.

    Also detects duplicate IDs across the corpus (a duplicate would
    silently shadow a prior entry's outcomes during end-to-end runs).
    Skips blank lines; raises on invalid JSON.
    """
    path = Path(path)
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"{path}:{lineno}: malformed JSON: {e}"
                ) from e

    results = [validate_entry(r) for r in rows]

    seen: set[str] = set()
    duplicates: list[str] = []
    for r in results:
        if r.entry_id == "<unknown>":
            continue
        if r.entry_id in seen:
            duplicates.append(r.entry_id)
        seen.add(r.entry_id)

    return CorpusValidationResult(entries=results, duplicate_ids=duplicates)


# ============================================================================
# Per-shape validators
# ============================================================================


_VALID_PATTERNS = {
    "preference", "propositional_attitude", "spatial_temporal",
    "categorical", "role_assignment", "relational", "quantitative",
    "event", "mereological",
}

_VALID_ROUTING_METHODS = {
    "python", "python_with_canonical_constants", "retrieval",
    "user_authoritative", "unverifiable", "routing_anomaly",
}

_VALID_MEMO_STATES = {"n/a", "write", "hit"}

_VALID_TIER_U_OUTCOMES = {"match", "miss", "contradiction"}

_VALID_LOOKUP_OUTCOMES = {"match", "miss", "contradiction"}

# Phase 8g vocabulary — supersedes ``expected_tier_u_outcome`` for
# Phase 7+ entries. The walker is the unified Layer-4 dispatcher
# (Tier U → Tier W → derivation → fresh), so its overall outcome
# captures the per-claim decision regardless of which tier resolved.
_VALID_WALKER_OUTCOMES = {"match", "miss", "contradiction"}
_VALID_SERVED_FROM_TIERS = {
    "u", "w", "derivation", "fresh", "routing_anomaly",
}

_VALID_PE_LABELS = {"equivalent", "contradictory", "distinct"}
_VALID_PE_SLOT_REVERSALS = {
    "none", "subject_object_swap", "participant_reorder",
}
_VALID_EE_LABELS = {"same", "different"}
_VALID_ET_LABELS = {
    "child_subsumed_by_parent", "parent_subsumed_by_child",
    "equivalent", "neither",
}
_VALID_PD_LABELS = {
    "distributes_up", "distributes_down", "both", "neither",
}
_VALID_RELATION_TYPES = {"is_a", "part_of"}

_VALID_ORACLE_NAMES = {
    "predicate_equivalence", "entity_equivalence",
    "entity_taxonomy", "predicate_distribution",
}


def _validate_substrate_direct(entry: dict) -> list[FieldError]:
    """SUBSTRATE_DIRECT entries: ``oracle_call`` plus ``expected_label``,
    plus per-oracle key columns."""
    errors: list[FieldError] = []
    call = entry.get("oracle_call")
    if not isinstance(call, dict):
        errors.append(FieldError(
            path="oracle_call",
            expected="dict",
            actual=type(call).__name__ if call is not None else FieldError.MISSING,
        ))
        return errors

    oracle = call.get("oracle")
    if oracle not in _VALID_ORACLE_NAMES:
        errors.append(FieldError(
            path="oracle_call.oracle",
            expected=f"one of {sorted(_VALID_ORACLE_NAMES)}",
            actual=oracle if oracle is not None else FieldError.MISSING,
        ))
        # Continue validation against the requested key columns even on
        # an unknown oracle so the caller sees every issue at once.

    expected_label = entry.get("expected_label")
    label_set = _expected_label_set_for_oracle(oracle)
    if label_set is not None and expected_label not in label_set:
        errors.append(FieldError(
            path="expected_label",
            expected=f"one of {sorted(label_set)}",
            actual=expected_label if expected_label is not None else FieldError.MISSING,
        ))

    if oracle == "entity_taxonomy":
        errors.extend(_validate_oracle_call_entity_taxonomy(call))
    elif oracle == "predicate_distribution":
        errors.extend(_validate_oracle_call_predicate_distribution(call))
    elif oracle == "predicate_equivalence":
        errors.extend(_validate_oracle_call_predicate_equivalence(call))
    elif oracle == "entity_equivalence":
        errors.extend(_validate_oracle_call_entity_equivalence(call))

    if "expected_via" in entry:
        errors.extend(_validate_expected_via(
            entry["expected_via"], allow_null=True,
        ))

    return errors


def _expected_label_set_for_oracle(oracle: Any) -> Optional[set[str]]:
    if oracle == "entity_taxonomy":
        return _VALID_ET_LABELS
    if oracle == "predicate_distribution":
        return _VALID_PD_LABELS
    if oracle == "predicate_equivalence":
        return _VALID_PE_LABELS
    if oracle == "entity_equivalence":
        return _VALID_EE_LABELS
    return None


def _validate_oracle_call_entity_taxonomy(call: dict) -> list[FieldError]:
    errors: list[FieldError] = []
    for key in ("child", "parent"):
        v = call.get(key)
        if not isinstance(v, str) or not v.strip():
            errors.append(FieldError(
                path=f"oracle_call.{key}",
                expected="non-empty string",
                actual=v if v is not None else FieldError.MISSING,
            ))
    rt = call.get("relation_type")
    if rt not in _VALID_RELATION_TYPES:
        errors.append(FieldError(
            path="oracle_call.relation_type",
            expected=f"one of {sorted(_VALID_RELATION_TYPES)}",
            actual=rt if rt is not None else FieldError.MISSING,
        ))
    return errors


def _validate_oracle_call_predicate_distribution(call: dict) -> list[FieldError]:
    errors: list[FieldError] = []
    pat = call.get("pattern")
    if pat not in _VALID_PATTERNS:
        errors.append(FieldError(
            path="oracle_call.pattern",
            expected=f"one of {sorted(_VALID_PATTERNS)}",
            actual=pat if pat is not None else FieldError.MISSING,
        ))
    pred = call.get("predicate")
    if not isinstance(pred, str) or not pred.strip():
        errors.append(FieldError(
            path="oracle_call.predicate",
            expected="non-empty string",
            actual=pred if pred is not None else FieldError.MISSING,
        ))
    pol = call.get("polarity")
    if pol not in (0, 1):
        errors.append(FieldError(
            path="oracle_call.polarity",
            expected="0 or 1",
            actual=pol if pol is not None else FieldError.MISSING,
        ))
    rt = call.get("taxonomy_relation_type")
    if rt not in _VALID_RELATION_TYPES:
        errors.append(FieldError(
            path="oracle_call.taxonomy_relation_type",
            expected=f"one of {sorted(_VALID_RELATION_TYPES)}",
            actual=rt if rt is not None else FieldError.MISSING,
        ))
    return errors


def _validate_oracle_call_predicate_equivalence(call: dict) -> list[FieldError]:
    errors: list[FieldError] = []
    pat = call.get("pattern")
    if pat not in _VALID_PATTERNS:
        errors.append(FieldError(
            path="oracle_call.pattern",
            expected=f"one of {sorted(_VALID_PATTERNS)}",
            actual=pat if pat is not None else FieldError.MISSING,
        ))
    for key in ("predicate_a", "predicate_b"):
        v = call.get(key)
        if not isinstance(v, str) or not v.strip():
            errors.append(FieldError(
                path=f"oracle_call.{key}",
                expected="non-empty string",
                actual=v if v is not None else FieldError.MISSING,
            ))
    return errors


def _validate_oracle_call_entity_equivalence(call: dict) -> list[FieldError]:
    errors: list[FieldError] = []
    for key in ("entity_a", "entity_b"):
        v = call.get(key)
        if not isinstance(v, str) or not v.strip():
            errors.append(FieldError(
                path=f"oracle_call.{key}",
                expected="non-empty string",
                actual=v if v is not None else FieldError.MISSING,
            ))
    return errors


def _validate_two_text_oracle(entry: dict) -> list[FieldError]:
    """TWO_TEXT_ORACLE entries: ``text_user`` + ``text_assistant`` +
    ``expected_oracle_classification``. Optional
    ``expected_facts_user``, ``expected_tier_u_outcome``."""
    errors: list[FieldError] = []
    for key in ("text_user", "text_assistant"):
        v = entry.get(key)
        if not isinstance(v, str) or not v.strip():
            errors.append(FieldError(
                path=key,
                expected="non-empty string",
                actual=v if v is not None else FieldError.MISSING,
            ))

    classification = entry.get("expected_oracle_classification")
    if not isinstance(classification, dict):
        errors.append(FieldError(
            path="expected_oracle_classification",
            expected="dict",
            actual=type(classification).__name__ if classification is not None
                   else FieldError.MISSING,
        ))
    else:
        errors.extend(_validate_oracle_classification(classification))

    if "expected_facts_user" in entry:
        errors.extend(_validate_expected_facts(
            entry["expected_facts_user"], path="expected_facts_user",
        ))

    outcome = entry.get("expected_tier_u_outcome")
    if outcome is not None and outcome not in _VALID_TIER_U_OUTCOMES:
        errors.append(FieldError(
            path="expected_tier_u_outcome",
            expected=f"one of {sorted(_VALID_TIER_U_OUTCOMES)}",
            actual=outcome,
        ))

    return errors


def _validate_oracle_classification(c: dict) -> list[FieldError]:
    errors: list[FieldError] = []
    pat = c.get("pattern")
    if pat not in _VALID_PATTERNS:
        errors.append(FieldError(
            path="expected_oracle_classification.pattern",
            expected=f"one of {sorted(_VALID_PATTERNS)}",
            actual=pat if pat is not None else FieldError.MISSING,
        ))
    for key in ("predicate_a", "predicate_b"):
        v = c.get(key)
        if not isinstance(v, str) or not v.strip():
            errors.append(FieldError(
                path=f"expected_oracle_classification.{key}",
                expected="non-empty string",
                actual=v if v is not None else FieldError.MISSING,
            ))
    label = c.get("label")
    if label not in _VALID_PE_LABELS:
        errors.append(FieldError(
            path="expected_oracle_classification.label",
            expected=f"one of {sorted(_VALID_PE_LABELS)}",
            actual=label if label is not None else FieldError.MISSING,
        ))
    sr = c.get("slot_reversal")
    if sr not in _VALID_PE_SLOT_REVERSALS:
        errors.append(FieldError(
            path="expected_oracle_classification.slot_reversal",
            expected=f"one of {sorted(_VALID_PE_SLOT_REVERSALS)}",
            actual=sr if sr is not None else FieldError.MISSING,
        ))
    return errors


def _validate_routing_memo(entry: dict) -> list[FieldError]:
    """ROUTING_MEMO entries: ``text`` + ``expected_facts`` +
    ``expected_memo_state``. expected_facts[i].expected_routing
    must be a valid routing method or "routing_anomaly"."""
    errors: list[FieldError] = []
    text = entry.get("text")
    if not isinstance(text, str) or not text.strip():
        errors.append(FieldError(
            path="text",
            expected="non-empty string",
            actual=text if text is not None else FieldError.MISSING,
        ))

    state = entry.get("expected_memo_state")
    if state not in _VALID_MEMO_STATES:
        errors.append(FieldError(
            path="expected_memo_state",
            expected=f"one of {sorted(_VALID_MEMO_STATES)}",
            actual=state if state is not None else FieldError.MISSING,
        ))

    facts = entry.get("expected_facts")
    if facts is None:
        errors.append(FieldError(
            path="expected_facts",
            expected="list of expected fact dicts",
            actual=FieldError.MISSING,
        ))
    else:
        errors.extend(_validate_expected_facts(
            facts, path="expected_facts",
            require_routing=True,
        ))

    return errors


def _validate_assistant_lookup(entry: dict) -> list[FieldError]:
    """ASSISTANT_LOOKUP entries: ``text`` + ``role == "assistant"`` +
    ``expected_facts`` (with expected_tier_u_outcome inside) +
    ``expected_oracles_consulted``. Optional: session,
    expected_oracle_label, expected_polarity_flipped,
    expected_entity_oracle_label, expected_via, future_match_via."""
    errors: list[FieldError] = []
    text = entry.get("text")
    if not isinstance(text, str) or not text.strip():
        errors.append(FieldError(
            path="text",
            expected="non-empty string",
            actual=text if text is not None else FieldError.MISSING,
        ))

    role = entry.get("role")
    if role != "assistant":
        errors.append(FieldError(
            path="role",
            expected='"assistant"',
            actual=role if role is not None else FieldError.MISSING,
        ))

    facts = entry.get("expected_facts")
    if facts is None:
        errors.append(FieldError(
            path="expected_facts",
            expected="list of expected fact dicts",
            actual=FieldError.MISSING,
        ))
    else:
        errors.extend(_validate_expected_facts(
            facts, path="expected_facts",
            require_tier_u_outcome=True,
        ))

    consulted = entry.get("expected_oracles_consulted")
    if not isinstance(consulted, list):
        errors.append(FieldError(
            path="expected_oracles_consulted",
            expected="list of oracle names (may be empty)",
            actual=type(consulted).__name__ if consulted is not None
                   else FieldError.MISSING,
        ))
    else:
        for i, name in enumerate(consulted):
            if name not in _VALID_ORACLE_NAMES:
                errors.append(FieldError(
                    path=f"expected_oracles_consulted[{i}]",
                    expected=f"one of {sorted(_VALID_ORACLE_NAMES)}",
                    actual=name,
                ))

    if "expected_oracle_label" in entry:
        v = entry["expected_oracle_label"]
        if v not in _VALID_PE_LABELS:
            errors.append(FieldError(
                path="expected_oracle_label",
                expected=f"one of {sorted(_VALID_PE_LABELS)}",
                actual=v,
            ))

    if "expected_entity_oracle_label" in entry:
        v = entry["expected_entity_oracle_label"]
        if v not in _VALID_EE_LABELS:
            errors.append(FieldError(
                path="expected_entity_oracle_label",
                expected=f"one of {sorted(_VALID_EE_LABELS)}",
                actual=v,
            ))

    if "expected_polarity_flipped" in entry:
        v = entry["expected_polarity_flipped"]
        if not isinstance(v, bool):
            errors.append(FieldError(
                path="expected_polarity_flipped",
                expected="bool",
                actual=type(v).__name__,
            ))

    if "expected_via" in entry:
        errors.extend(_validate_expected_via(
            entry["expected_via"], allow_null=True,
        ))

    if "future_match_via" in entry:
        v = entry["future_match_via"]
        if v is not None and (not isinstance(v, str) or not v.strip()):
            errors.append(FieldError(
                path="future_match_via",
                expected="oracle name string or null",
                actual=v,
            ))

    if "session" in entry:
        v = entry["session"]
        if not isinstance(v, str) or not v.strip():
            errors.append(FieldError(
                path="session",
                expected="non-empty string",
                actual=v,
            ))

    return errors


def _validate_user_storage(entry: dict) -> list[FieldError]:
    """USER_STORAGE entries: ``text`` + ``expected_facts`` (no role
    or role=="user"). Optional: session, session-aware fields inside
    expected_facts[i] (expected_is_session_local,
    expected_session_ids_after, expected_affirmed_count_after)."""
    errors: list[FieldError] = []
    text = entry.get("text")
    if not isinstance(text, str) or not text.strip():
        errors.append(FieldError(
            path="text",
            expected="non-empty string",
            actual=text if text is not None else FieldError.MISSING,
        ))

    role = entry.get("role")
    if role is not None and role != "user":
        errors.append(FieldError(
            path="role",
            expected='"user" or absent',
            actual=role,
        ))

    facts = entry.get("expected_facts")
    if facts is None:
        errors.append(FieldError(
            path="expected_facts",
            expected="list of expected fact dicts",
            actual=FieldError.MISSING,
        ))
    else:
        errors.extend(_validate_expected_facts(facts, path="expected_facts"))

    if "session" in entry:
        v = entry["session"]
        if not isinstance(v, str) or not v.strip():
            errors.append(FieldError(
                path="session",
                expected="non-empty string",
                actual=v,
            ))

    return errors


def _validate_expected_facts(
    facts: Any, *, path: str,
    require_routing: bool = False,
    require_tier_u_outcome: bool = False,
) -> list[FieldError]:
    """Validate ``expected_facts`` (a list of fact-shape dicts).

    When ``require_routing`` is set (ROUTING_MEMO shape), each fact
    must carry ``expected_routing``. When ``require_tier_u_outcome``
    is set (ASSISTANT_LOOKUP shape), each fact must carry
    ``expected_tier_u_outcome``.

    Phase 6 session-aware fields (expected_is_session_local,
    expected_session_ids_after, expected_affirmed_count_after) are
    optional on every shape but type-checked when present.
    """
    errors: list[FieldError] = []
    if not isinstance(facts, list) or not facts:
        errors.append(FieldError(
            path=path,
            expected="non-empty list",
            actual=type(facts).__name__,
        ))
        return errors

    for i, fact in enumerate(facts):
        prefix = f"{path}[{i}]"
        if not isinstance(fact, dict):
            errors.append(FieldError(
                path=prefix,
                expected="dict",
                actual=type(fact).__name__,
            ))
            continue
        errors.extend(_validate_expected_fact(
            fact, prefix,
            require_routing=require_routing,
            require_tier_u_outcome=require_tier_u_outcome,
        ))
    return errors


def _validate_expected_fact(
    fact: dict, prefix: str,
    *,
    require_routing: bool,
    require_tier_u_outcome: bool,
) -> list[FieldError]:
    errors: list[FieldError] = []

    pat = fact.get("pattern")
    if pat not in _VALID_PATTERNS:
        errors.append(FieldError(
            path=f"{prefix}.pattern",
            expected=f"one of {sorted(_VALID_PATTERNS)}",
            actual=pat if pat is not None else FieldError.MISSING,
        ))

    pred_in = fact.get("predicate_in")
    if not isinstance(pred_in, list) or not pred_in:
        errors.append(FieldError(
            path=f"{prefix}.predicate_in",
            expected="non-empty list of predicate strings",
            actual=type(pred_in).__name__ if pred_in is not None
                   else FieldError.MISSING,
        ))
    else:
        for j, p in enumerate(pred_in):
            if not isinstance(p, str) or not p.strip():
                errors.append(FieldError(
                    path=f"{prefix}.predicate_in[{j}]",
                    expected="non-empty string",
                    actual=p,
                ))

    pol = fact.get("polarity")
    if pol not in (0, 1):
        errors.append(FieldError(
            path=f"{prefix}.polarity",
            expected="0 or 1",
            actual=pol if pol is not None else FieldError.MISSING,
        ))

    slots = fact.get("slots_subset")
    if not isinstance(slots, dict) or not slots:
        errors.append(FieldError(
            path=f"{prefix}.slots_subset",
            expected="non-empty dict",
            actual=type(slots).__name__ if slots is not None
                   else FieldError.MISSING,
        ))

    if require_routing:
        r = fact.get("expected_routing")
        if r not in _VALID_ROUTING_METHODS:
            errors.append(FieldError(
                path=f"{prefix}.expected_routing",
                expected=f"one of {sorted(_VALID_ROUTING_METHODS)}",
                actual=r if r is not None else FieldError.MISSING,
            ))
    elif "expected_routing" in fact:
        # Optional but type-check when present.
        r = fact["expected_routing"]
        if r not in _VALID_ROUTING_METHODS:
            errors.append(FieldError(
                path=f"{prefix}.expected_routing",
                expected=f"one of {sorted(_VALID_ROUTING_METHODS)}",
                actual=r,
            ))

    # Phase 8g: ``expected_tier_u_outcome`` is the legacy field
    # (Phase 0-6 corpus entries). ``expected_walker_outcome`` +
    # ``expected_served_from_tier`` are the Phase 7+ replacements.
    # When require_tier_u_outcome=True (assistant_lookup), at least
    # ONE of {expected_tier_u_outcome, expected_walker_outcome}
    # must be present. Validate whichever is provided. If both are
    # provided, walker fields take precedence and a deprecation
    # note is logged via FieldError; we don't error out, but the
    # validator surfaces the duplication so corpus authors can
    # clean up.
    legacy = fact.get("expected_tier_u_outcome")
    walker_outcome = fact.get("expected_walker_outcome")
    served_from = fact.get("expected_served_from_tier")

    if require_tier_u_outcome:
        if walker_outcome is None and legacy is None:
            errors.append(FieldError(
                path=(
                    f"{prefix}.expected_walker_outcome OR "
                    f"{prefix}.expected_tier_u_outcome"
                ),
                expected=(
                    "Phase 7+: expected_walker_outcome (one of "
                    f"{sorted(_VALID_WALKER_OUTCOMES)}); Phase 0-6 "
                    "legacy: expected_tier_u_outcome"
                ),
                actual=FieldError.MISSING,
            ))
        # Validate the field(s) that ARE present.
        if walker_outcome is not None:
            if walker_outcome not in _VALID_WALKER_OUTCOMES:
                errors.append(FieldError(
                    path=f"{prefix}.expected_walker_outcome",
                    expected=f"one of {sorted(_VALID_WALKER_OUTCOMES)}",
                    actual=walker_outcome,
                ))
        if legacy is not None and legacy not in _VALID_TIER_U_OUTCOMES:
            errors.append(FieldError(
                path=f"{prefix}.expected_tier_u_outcome",
                expected=f"one of {sorted(_VALID_TIER_U_OUTCOMES)}",
                actual=legacy,
            ))
    else:
        # Optional but type-check when present.
        if legacy is not None and legacy not in _VALID_TIER_U_OUTCOMES:
            errors.append(FieldError(
                path=f"{prefix}.expected_tier_u_outcome",
                expected=f"one of {sorted(_VALID_TIER_U_OUTCOMES)}",
                actual=legacy,
            ))
        if (
            walker_outcome is not None
            and walker_outcome not in _VALID_WALKER_OUTCOMES
        ):
            errors.append(FieldError(
                path=f"{prefix}.expected_walker_outcome",
                expected=f"one of {sorted(_VALID_WALKER_OUTCOMES)}",
                actual=walker_outcome,
            ))

    # served_from_tier is optional everywhere; type-check when present.
    if (
        served_from is not None
        and served_from not in _VALID_SERVED_FROM_TIERS
    ):
        errors.append(FieldError(
            path=f"{prefix}.expected_served_from_tier",
            expected=f"one of {sorted(_VALID_SERVED_FROM_TIERS)}",
            actual=served_from,
        ))

    if "expected_is_session_local" in fact:
        v = fact["expected_is_session_local"]
        if v not in (0, 1):
            errors.append(FieldError(
                path=f"{prefix}.expected_is_session_local",
                expected="0 or 1",
                actual=v,
            ))

    if "expected_session_ids_after" in fact:
        v = fact["expected_session_ids_after"]
        if not isinstance(v, list) or not all(isinstance(s, str) for s in v):
            errors.append(FieldError(
                path=f"{prefix}.expected_session_ids_after",
                expected="list of session id strings",
                actual=type(v).__name__,
            ))

    if "expected_affirmed_count_after" in fact:
        v = fact["expected_affirmed_count_after"]
        if not isinstance(v, int) or v < 0:
            errors.append(FieldError(
                path=f"{prefix}.expected_affirmed_count_after",
                expected="non-negative int",
                actual=v,
            ))

    return errors


def _validate_expected_via(via: Any, *, allow_null: bool) -> list[FieldError]:
    """``expected_via`` is either null (Phase 5 forward-ref) or a list
    of oracle names (Phase 7+ derivation chains)."""
    if via is None:
        if allow_null:
            return []
        return [FieldError(
            path="expected_via",
            expected="list of oracle names",
            actual=None,
        )]
    if not isinstance(via, list):
        return [FieldError(
            path="expected_via",
            expected="list of oracle names" + (" or null" if allow_null else ""),
            actual=type(via).__name__,
        )]
    errors: list[FieldError] = []
    for i, name in enumerate(via):
        if name not in _VALID_ORACLE_NAMES:
            errors.append(FieldError(
                path=f"expected_via[{i}]",
                expected=f"one of {sorted(_VALID_ORACLE_NAMES)}",
                actual=name,
            ))
    return errors


_SHAPE_VALIDATORS = {
    SmokeEntryShape.SUBSTRATE_DIRECT: _validate_substrate_direct,
    SmokeEntryShape.TWO_TEXT_ORACLE: _validate_two_text_oracle,
    SmokeEntryShape.ROUTING_MEMO: _validate_routing_memo,
    SmokeEntryShape.ASSISTANT_LOOKUP: _validate_assistant_lookup,
    SmokeEntryShape.USER_STORAGE: _validate_user_storage,
}


# ============================================================================
# Cascading-failure context
# ============================================================================


def format_cascading_failure(
    failed_entry_id: str,
    failure_message: str,
    *,
    prior_entry_id: Optional[str] = None,
    prior_entry_summary: Optional[str] = None,
) -> str:
    """Format a cross-entry failure message naming both the failing
    entry AND the prior entry whose state was expected.

    Used by the Phase 9 end-to-end runner when entry N fails because
    entry N-K's state didn't materialize. Writing this helper now
    (Phase 7) means Phase 9 doesn't need to re-derive the formatting.

    The ``prior_entry_summary`` field carries the specific
    expectation that didn't materialize, e.g. "expected memo write
    for (mereological, part_of)" — enough for an operator to know
    where to dig.
    """
    if prior_entry_id is None:
        return f"entry {failed_entry_id!r}: {failure_message}"
    summary = (
        f" ({prior_entry_summary})" if prior_entry_summary else ""
    )
    return (
        f"entry {failed_entry_id!r}: {failure_message}\n"
        f"  cascade source: prior entry {prior_entry_id!r}"
        f"{summary} did not materialize the expected state"
    )
