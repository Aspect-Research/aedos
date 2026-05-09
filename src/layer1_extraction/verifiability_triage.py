"""Layer 1.5 — verifiability triage (v0.14.1).

A pre-walker gate that decides whether each assistant claim warrants
the **expensive** verifier path (fresh dispatch — retrieval +
LLM-judge for world facts; code generation + sandbox for math).
Cheap verification (Tier U / Tier W / derivation lookups) ALWAYS
runs because those tiers are essentially free AND architecturally
load-bearing — the user's stored preferences must still contradict
the assistant when it gets them wrong.

  * ``VERIFY``       — full walker including fresh dispatch
  * ``PASS_THROUGH`` — walker runs (Tier U / W / derivation) but
                       fresh dispatch is suppressed. If U/W/derivation
                       resolve, that verdict stands. If they all miss,
                       no expensive retrieval fires; the claim's
                       walker outcome is MISS and the corrector
                       does nothing.

Architectural fit
=================

Principle 1 ("verification is upstream of memoization") is preserved:
PASS_THROUGH claims that miss U/W/derivation never enter the
verified store. They leave a ``verifiability_triage`` audit event so
the trace UI can show the operator what was triaged out.

The gate addresses two architectural pain points:
  * Indiscriminate retrieval — every extracted claim that misses the
    cheap tiers used to fall through to the expensive retrieval
    verifier even when the claim was vague enough to always come
    back inconclusive.
  * Cache pollution — Tier W stops accumulating verdicts for bare
    encyclopedic claims that didn't need verifying.

Decision rules (strict, rule-based, no LLM call)
================================================

A claim is VERIFY-eligible iff at least one of:

  1. **Numeric value present.** Any slot's value parses as int/float
     (excluding polarity). Numeric facts are the textbook checkable
     case ("baboons live ~30 years"; "Marie Curie was born in 1867").

  2. **Date / temporal scope present.** Any slot named ``valid_from``,
     ``valid_until``, ``occurred_at``, ``date``, or whose value
     matches a 4-digit-year pattern. Historical facts are checkable.

  3. **Multiple named-entity slots.** At least two slots whose values
     look like specific named entities (capitalized multi-word, or
     non-generic identifiers). Catches relations between specific
     things ("Paris is the capital of France"; "Trump defeated
     Harris").

  4. **Comparative / superlative claim.** The
     ``comparative.detect_comparative`` heuristic returns non-None
     (existing v0.7.9 detector for "tallest", "most", etc.).

  5. **Anchor + specific predicate.** The claim has an
     ``anchor_entity`` AND the predicate isn't a vague generic
     (``is``, ``has``, ``does``); the anchor signals the extractor
     pulled this from substantive content.

Everything else → PASS_THROUGH (cheap walker only, no fresh dispatch).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class TriageDecision(str, Enum):
    """Verifiability triage outcome.

    Values match the field name used in pipeline_events for the trace
    UI's badge styling. PASS_THROUGH replaces ``flow through Layer 2``;
    VERIFY is the existing default behavior.
    """

    VERIFY = "verify"
    PASS_THROUGH = "pass_through"


# Vague generic predicates that, by themselves, signal a non-falsifiable
# claim. The extractor should already reject these under the hard-claim
# discipline, but the triage gate is a defense-in-depth layer.
_VAGUE_PREDICATES: frozenset[str] = frozenset({
    "is", "has", "does", "exists", "occurs", "happens",
    "is_a_kind_of",  # without a specific category, this is empty
})

# DEPRECATED v0.14.3 — the source of truth for "predicates that always
# trigger triage VERIFY" is now the per-pattern
# ``triage_verify_predicates`` field in ``patterns.yaml``. Each
# pattern declares which of its predicates are
# computable / lookup-friendly. The triage gate looks the predicate
# up in the registry on each call. This constant remains as a flat
# back-compat fallback for callers that don't pass a registry; its
# contents are kept in sync with the schema by hand.
_COMPUTABLE_PREDICATES: frozenset[str] = frozenset({
    # Time / clock (quantitative)
    "current_time", "current_date", "current_day_of_week",
    "current_year", "time_difference_hours", "time_difference_days",
    # Counting (quantitative)
    "has_count", "letter_count", "word_count",
    "character_count", "vowel_count",
    # Locational (spatial_temporal)
    "located_in", "capital_of", "borders",
    "in_continent", "in_timezone",
    # String operations (relational)
    "contains_substring", "is_anagram_of", "reverse_of",
    "starts_with", "ends_with",
    # Lookup-friendly relations (relational)
    "founded_by", "married_to",
    "defeated_in_election", "authored_by",
})


def _registry_verify_predicates(
    registry: Any | None, pattern_name: str,
) -> frozenset[str]:
    """Read the per-pattern ``triage_verify_predicates`` allow-list
    from the registry. Falls back to the flat ``_COMPUTABLE_PREDICATES``
    constant when no registry is passed (back-compat for callers
    that haven't been updated)."""
    if registry is None or not pattern_name:
        return _COMPUTABLE_PREDICATES
    try:
        if not registry.has(pattern_name):
            return _COMPUTABLE_PREDICATES
        pattern = registry.get(pattern_name)
        return frozenset(pattern.triage_verify_predicates)
    except Exception:
        # Defensive: a malformed registry shouldn't break triage.
        return _COMPUTABLE_PREDICATES

# Slot names whose presence signals temporal scope.
_DATE_SLOT_NAMES: frozenset[str] = frozenset({
    "valid_from", "valid_until", "occurred_at", "date",
    "birth_year", "death_year", "year",
})

_YEAR_PATTERN = re.compile(r"\b(1[0-9]{3}|20[0-9]{2}|21[0-9]{2})\b")
# Heuristic for "specific named entity" — capitalized multi-word, or
# a single capitalized token that isn't a generic English noun.
_NAMED_ENTITY_PATTERN = re.compile(r"^[A-Z][A-Za-z0-9.\-']*( [A-Z][A-Za-z0-9.\-']*)+$")


@dataclass(frozen=True)
class TriageResult:
    decision: TriageDecision
    reason: str
    rule: str  # which rule fired ("numeric", "date", "named_entities", ...)
    # v0.14.3 — cross-check signal. Set when the extractor's
    # ``expected_verifier`` field disagrees with the triage decision.
    # PASS_THROUGH + extractor expected python/retrieval = mismatch
    # (extractor thought this was verifiable; triage didn't see the
    # signal). Trace UI can flag for operator review.
    expected_verifier: str | None = None
    extractor_triage_mismatch: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "rule": self.rule,
            "expected_verifier": self.expected_verifier,
            "extractor_triage_mismatch": self.extractor_triage_mismatch,
        }


def triage_claim(claim: dict, registry: Any | None = None) -> TriageResult:
    """Decide verifiability for one extracted claim.

    Pure function. No store access, no LLM call. The decision is
    deterministic from the claim's shape alone; same claim → same
    decision every time.

    ``registry`` (optional) — when supplied, the predicate allow-list
    for Rule 6 comes from the matched pattern's
    ``triage_verify_predicates`` field. When omitted, the flat
    ``_COMPUTABLE_PREDICATES`` constant is used as a back-compat
    fallback (with the union of all patterns' allow-lists).
    """
    result = _triage_decide(claim, registry)
    # v0.14.3 cross-check: stamp the extractor's expected_verifier and
    # flag mismatch with the triage decision. PASS_THROUGH while the
    # extractor expected python/retrieval is the canonical mismatch
    # — the extractor saw a verifiable claim that the rules didn't
    # recognize. Recorded for trace-UI surfacing; doesn't override
    # the decision (the gate stays conservative).
    expected = claim.get("expected_verifier")
    expected = expected.strip() if isinstance(expected, str) else None
    mismatch = False
    if expected:
        verifier_expected_paths = {"python", "python_with_canonical_constants",
                                   "retrieval"}
        if (result.decision is TriageDecision.PASS_THROUGH
            and expected in verifier_expected_paths):
            mismatch = True
    return TriageResult(
        decision=result.decision,
        reason=result.reason,
        rule=result.rule,
        expected_verifier=expected,
        extractor_triage_mismatch=mismatch,
    )


def _triage_decide(claim: dict, registry: Any | None) -> TriageResult:
    """Inner decision logic. Returns a TriageResult without the
    cross-check fields populated. Wrapped by triage_claim which adds
    the cross-check stamp."""
    pattern_name = claim.get("pattern", "")
    predicate = (claim.get("predicate") or "").strip().lower()
    slots = claim.get("slots") or {}
    anchor = (claim.get("anchor_entity") or "").strip()

    # Rule 2 (checked first because date-shaped values often parse as
    # numbers — '2017' is a valid float): date / temporal scope present.
    for k, v in slots.items():
        if k in _DATE_SLOT_NAMES:
            return TriageResult(
                decision=TriageDecision.VERIFY,
                reason=f"date slot {k!r} present → checkable historical fact",
                rule="date_slot",
            )
        if isinstance(v, str) and _YEAR_PATTERN.search(v):
            return TriageResult(
                decision=TriageDecision.VERIFY,
                reason=(
                    f"year-like value in slot {k!r}={v!r} → checkable "
                    "historical fact"
                ),
                rule="year_in_value",
            )

    # Rule 6: per-pattern verify-predicate allow-list. Each pattern in
    # patterns.yaml declares which of its predicates the system knows
    # how to verify (computable by the python verifier OR
    # lookup-friendly for the retrieval verifier). The falsifiability
    # lives in the predicate's *meaning*, not the slot's *shape* — so
    # a claim like ``current_time(subject=Cairo, value='9:56 am')``
    # hits VERIFY here even though "9:56 am" doesn't parse as a
    # number and Cairo alone isn't enough for the multi-named-entity
    # rule.
    pattern_predicates = _registry_verify_predicates(registry, pattern_name)
    if predicate in pattern_predicates:
        return TriageResult(
            decision=TriageDecision.VERIFY,
            reason=(
                f"predicate {predicate!r} is a verify-predicate "
                f"declared by pattern {pattern_name!r} → known "
                "verifier path"
            ),
            rule="verify_predicate",
        )

    # Rule 1: numeric value present in any slot.
    for k, v in slots.items():
        if k == "polarity":
            continue
        if _is_numeric(v):
            return TriageResult(
                decision=TriageDecision.VERIFY,
                reason=f"numeric value in slot {k!r} → falsifiable",
                rule="numeric_slot",
            )

    # Rule 7: named-entity subject + value slot. When a slot value
    # looks named-entity-like AND a `value` slot exists (any type),
    # VERIFY. Catches structured-but-non-numeric value claims that
    # the multi-named-entity rule misses ("Cairo current time is
    # 2:56 am" — Cairo is named, value=2:56 am is the falsifiable
    # assertion). The presence of an explicit `value` slot is the
    # falsifiability signal regardless of whether it's a number.
    if "value" in slots:
        named_subject_present = any(
            isinstance(v, str) and _looks_named_entity(v)
            for v in slots.values()
        )
        if named_subject_present:
            return TriageResult(
                decision=TriageDecision.VERIFY,
                reason=(
                    "named-entity subject + explicit `value` slot → "
                    "structured value claim, checkable"
                ),
                rule="named_subject_with_value",
            )

    # Rule 3: multiple named-entity slots.
    named_entity_slots = [
        (k, v) for k, v in slots.items()
        if isinstance(v, str) and _looks_named_entity(v)
    ]
    if len(named_entity_slots) >= 2:
        keys = [k for k, _ in named_entity_slots]
        return TriageResult(
            decision=TriageDecision.VERIFY,
            reason=(
                f"≥2 named-entity slots ({keys}) → relation between "
                "specific things"
            ),
            rule="multiple_named_entities",
        )

    # Rule 4: comparative / superlative.
    try:
        from src.verifiers.comparative import detect_comparative
        if detect_comparative(claim) is not None:
            return TriageResult(
                decision=TriageDecision.VERIFY,
                reason="comparative / superlative claim — checkable against rankings",
                rule="comparative",
            )
    except Exception:
        # If the comparative detector errors, fall through; don't block
        # triage on a defensive failure.
        pass

    # Rule 5: anchor + specific predicate.
    if anchor and predicate and predicate not in _VAGUE_PREDICATES:
        return TriageResult(
            decision=TriageDecision.VERIFY,
            reason=(
                f"anchor_entity={anchor!r} present and predicate "
                f"{predicate!r} is specific"
            ),
            rule="anchored_specific_predicate",
        )

    # Default: PASS_THROUGH. The claim doesn't have a falsifiable surface
    # area we can hand to the retrieval verifier with confidence. The
    # corrector leaves the chat draft's text unchanged.
    return TriageResult(
        decision=TriageDecision.PASS_THROUGH,
        reason=(
            "no numeric / date / multi-entity / comparative / anchored-"
            "specific signal — claim shape is too vague to verify "
            "reliably; trusting the chat model"
        ),
        rule="no_falsifiability_signal",
    )


# ---- helpers -------------------------------------------------------


def _is_numeric(v: Any) -> bool:
    """True iff ``v`` is a number or a string that parses to one.
    Booleans (which inherit from int in Python) are excluded — polarity
    is the only bool we'd see and it's already excluded by key."""
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return False
        try:
            float(s)
            return True
        except ValueError:
            return False
    return False


def _looks_named_entity(s: str) -> bool:
    """Heuristic: looks like a specific named entity rather than a
    generic noun. Multi-word capitalized phrases ("Marie Curie",
    "United States"); or a single capitalized token longer than a
    short generic noun ("Anthropic", "Apple", "Tokyo").

    This is a specificity heuristic, not a real NER — it overfits a
    bit to English orthography but is good enough for the triage
    gate's binary decision.
    """
    s = s.strip()
    if not s:
        return False
    # Multi-word capitalized.
    if _NAMED_ENTITY_PATTERN.match(s):
        return True
    # Single capitalized token of 4+ chars: probably a proper noun.
    if (len(s) >= 4
        and s[0].isupper()
        and s[1:].isalnum()
        and " " not in s
        and not s.isupper()):  # exclude shouted COMMON nouns
        return True
    return False
