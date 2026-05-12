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

  3. **Multiple specific-referent slots** (v0.14.6). At least two
     slots whose values look like concrete referents ("Paris" /
     "France"; "cats" / "mice"). Replaces v0.14.5's
     `multiple_named_entities`, which required capitalized proper
     nouns and so missed common-knowledge claims with lowercase
     subjects.

  4. **Comparative / superlative claim.** The
     ``comparative.detect_comparative`` heuristic returns non-None
     (existing v0.7.9 detector for "tallest", "most", etc.).

  5. **Anchor + specific predicate.** The claim has an
     ``anchor_entity`` AND the predicate isn't a vague generic
     (``is``, ``has``, ``does``); the anchor signals the extractor
     pulled this from substantive content.

  6. **Verify-predicate allow-list** (v0.14.3). The claim's predicate
     appears in its pattern's ``triage_verify_predicates`` field
     (declared in patterns.yaml). Catches predicates whose
     verifiability is intrinsic to the predicate's meaning rather
     than the slot shape (``current_time``, ``has_count``,
     ``located_in``, etc.).

  7. **Specific subject + value slot** (v0.14.1). Any slot looks
     like a concrete referent AND a ``value`` slot exists.

  8. **Concrete categorical** (v0.14.6). A ``categorical.is_a``
     claim where BOTH ``entity`` and ``category`` clear the
     specificity check. Wikipedia is reliable on textbook is_a
     claims like "cats are mammals" / "pizza is Italian" — the
     v0.14.5 named-entity heuristic rejected these because both
     slots were lowercase common nouns.

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

# v0.14.6 — vague-noun stopword list. Replaces the v0.14.5
# orthography-biased _NAMED_ENTITY_PATTERN, which required
# capitalized multi-word phrases or 4+ char capitalized tokens.
# That heuristic systematically PASS_THROUGH'd textbook common-
# knowledge claims like "cats are mammals" / "pizza is Italian"
# whose slots are lowercase common nouns — Wikipedia handles those
# fine. The new heuristic asks "does this slot value name a concrete
# referent?" and rejects only an explicit list of vague placeholders.
#
# Keep this list FOCUSED. Each entry should be a noun (or
# noun-shaped adjective) that is so generic it can't anchor a
# Wikipedia query on its own. When in doubt, leave it OUT —
# false-positive specificity costs one inconclusive retrieval call;
# false-negative specificity (which the v0.14.5 heuristic produced
# en masse) means real facts never get checked.
_VAGUE_NOUNS: frozenset[str] = frozenset({
    # Pronouns / demonstratives / first-person placeholders
    "it", "this", "that", "these", "those", "they", "them",
    "he", "she", "him", "her", "i", "me", "we", "us", "user",
    # Generic placeholder nouns
    "thing", "things", "stuff", "something", "someone", "somewhere",
    "anything", "anyone", "anywhere",
    # Vague descriptor / category nouns
    "way", "ways", "type", "types", "kind", "kinds",
    "form", "forms", "sort", "sorts", "manner", "manners",
    # Vague qualitative slot fillers
    "behavior", "behaviors", "system", "systems", "environment",
    "environments", "level", "levels", "amount", "amounts",
    "nature", "world", "area", "areas", "topic", "topics",
    "concept", "concepts", "subject", "subjects", "aspect", "aspects",
    "feeling", "feelings", "state", "states",
    # Vague qualitative adjectives that creep into category slots
    "intelligent", "complex", "advanced", "important", "significant",
    "various", "diverse", "interesting", "simple", "general",
    "good", "bad", "high", "low",
})

# Articles / determiners stripped before the vague-noun lookup so
# "the cat" / "a cat" / "an apple" tokenize down to the head noun.
_ARTICLES: frozenset[str] = frozenset({"a", "an", "the"})


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

    # Rule 7: specific subject + value slot. When a slot value
    # looks like a concrete referent AND a `value` slot exists (any
    # type), VERIFY. Catches structured-but-non-numeric value claims
    # that the multi-specific rule misses ("Cairo current time is
    # 2:56 am" — Cairo is specific, value=2:56 am is the falsifiable
    # assertion). The presence of an explicit `value` slot is the
    # falsifiability signal regardless of whether it's a number.
    if "value" in slots:
        specific_subject_present = any(
            _looks_specific(v) for v in slots.values()
        )
        if specific_subject_present:
            return TriageResult(
                decision=TriageDecision.VERIFY,
                reason=(
                    "specific subject + explicit `value` slot → "
                    "structured value claim, checkable"
                ),
                rule="specific_subject_with_value",
            )

    # Rule 8 (v0.14.6) — checked BEFORE Rule 3 so categorical claims
    # surface the more semantic rule name in the audit event. A
    # `categorical.is_a` claim where BOTH ``entity`` and ``category``
    # clear the specificity check is VERIFY-eligible. Catches
    # textbook common-knowledge classifications — "cats are mammals"
    # / "pizza is Italian" / "the kakapo is a bird" — that the
    # v0.14.5 multi-named-entity rule rejected because the slots
    # were lowercase common nouns. Wikipedia is reliable on these.
    #
    # Functionally Rule 8 overlaps with Rule 3 (which would also fire
    # on the same shape) — running it first is a labeling choice so
    # the trace UI shows the semantic-shape reason rather than the
    # generic multi-slot reason. The vague-noun stopword filter on
    # both slots is what keeps "X is intelligent" / "X is a thing"
    # from sneaking through.
    if pattern_name == "categorical":
        entity = slots.get("entity")
        category = slots.get("category")
        if _looks_specific(entity) and _looks_specific(category):
            return TriageResult(
                decision=TriageDecision.VERIFY,
                reason=(
                    f"concrete categorical: entity {entity!r} and "
                    f"category {category!r} are both specific "
                    "referents → checkable"
                ),
                rule="concrete_categorical",
            )

    # Rule 3: multiple specific slots. v0.14.6 — formerly
    # `multiple_named_entities`, restricted to capitalized proper
    # nouns. The new check accepts any concrete referent (lowercase
    # common nouns included) so claims like "cats hunt mice" qualify.
    specific_slots = [
        (k, v) for k, v in slots.items()
        if _looks_specific(v)
    ]
    if len(specific_slots) >= 2:
        keys = [k for k, _ in specific_slots]
        return TriageResult(
            decision=TriageDecision.VERIFY,
            reason=(
                f"≥2 specific-referent slots ({keys}) → relation between "
                "concrete things"
            ),
            rule="multiple_specific_slots",
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
            "no numeric / date / multi-specific / comparative / anchored-"
            "specific / concrete-categorical signal — claim shape is too "
            "vague to verify reliably; trusting the chat model"
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


def _looks_specific(s: Any) -> bool:
    """Heuristic: looks like a CONCRETE referent rather than a vague
    placeholder. v0.14.6 replacement for the v0.14.5
    ``_looks_named_entity`` orthographic check.

    The triage gate is asking "is this slot value verifiable?", not
    "is this a proper noun?". A lowercase common noun like ``cat`` /
    ``pizza`` / ``mammal`` is a fine retrieval target — Wikipedia has
    articles on all three. The orthographic bias was dropping
    textbook categoricals into PASS_THROUGH ("cats are mammals" got
    silently skipped because both slots were lowercase common nouns).

    Reject only:
      * non-string / empty / whitespace-only values
      * single-token values shorter than 3 chars (catches articles,
        short pronouns, common stop tokens that slip through the
        leading-article strip)
      * single-token values (case-insensitive, after stripping
        leading articles ``a`` / ``an`` / ``the``) that appear in
        the ``_VAGUE_NOUNS`` stopword list

    Multi-word phrases bypass the stopword check unconditionally:
    "vague behavior" or "the United States" is more specific than
    its head noun alone, because at minimum the modifier carries
    information. This is intentionally permissive — false-positive
    specificity costs one inconclusive retrieval call; false-negative
    specificity is what the v0.14.5 heuristic was producing in bulk.
    """
    if not isinstance(s, str):
        return False
    s = s.strip()
    if not s:
        return False
    parts = s.split()
    # Strip a single leading article so "the cat" → "cat" for the
    # vague-noun check; "the United States" → "United States" still
    # qualifies via the multi-word path.
    if parts and parts[0].lower() in _ARTICLES:
        parts = parts[1:]
    if not parts:
        return False
    if len(parts) >= 2:
        return True
    word = parts[0].lower()
    if len(word) < 3:
        return False
    if word in _VAGUE_NOUNS:
        return False
    return True
