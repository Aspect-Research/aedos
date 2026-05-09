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

# v0.14.1 — predicates that name an operation the python verifier can
# settle deterministically against the system clock / sandbox. These
# always trigger VERIFY regardless of slot shape: the falsifiability
# lives in the predicate's *meaning* (zoneinfo, datetime, statistics,
# re), not in the slot's *shape* (which the other rules check).
#
# Conservative scope per the v0.14.1 approval — time/clock + counting
# only. Predicates the system has actually seen in real traces. Add
# arithmetic / calendar / string predicates here as we encounter them
# and confirm the python verifier can dispatch them cleanly.
_COMPUTABLE_PREDICATES: frozenset[str] = frozenset({
    # Time / clock
    "current_time",
    "current_date",
    "current_day_of_week",
    "current_year",
    "time_difference_hours",
    "time_difference_days",
    # Counting
    "has_count",
    "letter_count",
    "word_count",
    "character_count",
    "vowel_count",
})

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "rule": self.rule,
        }


def triage_claim(claim: dict) -> TriageResult:
    """Decide verifiability for one extracted claim.

    Pure function. No store access, no LLM call. The decision is
    deterministic from the claim's shape alone; same claim → same
    decision every time.
    """
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

    # Rule 6: computable-predicate allow-list. Predicates the python
    # verifier can settle deterministically against the system clock
    # or sandbox (zoneinfo, datetime, re, statistics). The
    # falsifiability lives in the predicate's meaning, not the slot's
    # shape — so a claim like "current_time(subject=Cairo, value='9:56
    # am')" hits VERIFY here even though "9:56 am" doesn't parse as a
    # number and Cairo alone isn't enough for the multi-named-entity
    # rule.
    if predicate in _COMPUTABLE_PREDICATES:
        return TriageResult(
            decision=TriageDecision.VERIFY,
            reason=(
                f"predicate {predicate!r} names a python-verifier "
                "operation (zoneinfo / datetime / re / statistics) → "
                "computable against the system clock or sandbox"
            ),
            rule="computable_predicate",
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
