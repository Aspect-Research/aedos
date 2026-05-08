"""Layer 3 substrate oracle: predicate distribution policy.

Decides, for a (pattern, predicate, polarity, taxonomy_relation_type)
4-tuple, whether the predicate's truth value PROPAGATES along chains
of the given taxonomy relation type. Four labels:

  * ``distributes_up``   — truth at the more-specific entity
                           propagates UP to the more-general. e.g.
                           ``(spatial_temporal, lives_in, p=1,
                           part_of)`` — lives in Williamstown ⇒
                           lives in Massachusetts. Living in a part
                           entails living in the whole.
  * ``distributes_down`` — truth at the more-general entity
                           propagates DOWN to the more-specific.
                           e.g. ``(preference, likes, p=1, is_a)`` —
                           likes animals ⇒ likes cheetahs. Liking a
                           category entails liking its instances.
  * ``both``             — propagates in both directions. Rare;
                           reserved for Phase 7+ discovery.
  * ``neither``          — no propagation in either direction. e.g.
                           ``(quantitative, weighs, p=1, is_a)`` —
                           an object's weight is a property of
                           itself, not category-inheritable.

The singleton-key shape — divergence from the symmetric oracles
==============================================================

predicate_equivalence and entity_equivalence work over unordered
PAIRS and use a canonical-pair helper to hide ordering from callers.
entity_taxonomy works over ordered (child, parent) pairs but still
has TWO entities as primary keys.

predicate_distribution has NO PAIRS at all. The key is a 4-tuple of
independent dimensions: pattern, predicate, polarity, taxonomy_
relation_type. There is nothing to canonicalize and nothing to
swap. Each row is a verdict about how a single predicate (in a
single pattern, at a single polarity) propagates across a single
taxonomy relation_type. The architectural commitment in
classifier_base.py: distribution is genuinely directional, and the
caller (Phase 7's walker) knows which direction it is asking about
because it is walking up or down a taxonomy chain.

Key columns and why each one is necessary
=========================================

  * **pattern** — predicates are pattern-scoped. ``has_count`` under
    quantitative is a different relation than a hypothetical
    ``has_count`` under categorical; their distribution policies
    can differ. Pattern-keying preserves the disambiguation.

  * **predicate** — the relation whose distribution we're asking
    about.

  * **polarity** — distribution behavior can differ across
    polarities. Positive ``dislikes`` distributes down is_a (if I
    dislike animals, I dislike cheetahs). Negative ``dislikes`` —
    "I don't dislike animals" — does NOT distribute down (it could
    go either way for any specific animal). Including polarity in
    the key prevents one polarity's verdict from leaking into the
    other's.

  * **taxonomy_relation_type** — is_a vs part_of. The two relations
    have different semantics (categorical class membership vs.
    constitutive parthood) so a predicate's distribution policy can
    differ between them. The CANONICAL HARD CASE: ``lives_in``
    distributes_up under part_of (lives in part ⇒ lives in whole)
    but NEITHER under is_a (lives in a city ≠ lives in all cities).
    The key column on relation_type is what lets the oracle learn
    those two policies independently.

Predicate normalization: lowercase + strip
==========================================

Predicates are case-folded and stripped (mirrors Phase 3's
predicate_equivalence convention). Distribution behavior is a
property of the predicate's semantics, not its capitalization. The
extractor occasionally varies capitalization without intending
different predicates; case-folding is signal-preserving.

Cost characteristics
====================

Phase 5 ships predicate_distribution DORMANT — no consumer wires
it. The inspector endpoints and tests are the only callers. Phase
7's derivation walker is the consumer that turns substrate rows
into derived verdicts.

When Phase 7 lands, walker cost on a chain involves one
predicate_distribution consult per hop where the walker is
considering propagation. Cold-start cost is one LLM call per new
4-tuple; warm cost is SQL only.

Pipeline events
===============

  * ``predicate_distribution_hit``                  — SQL cache hit,
                                                      no LLM call.
  * ``predicate_distribution_write``                — LLM ran, row
                                                      was UPSERTed.
  * ``predicate_distribution_classification_failed``
                                                    — LLM returned
                                                      malformed tool
                                                      output. No row
                                                      written.
                                                      Verdict
                                                      carries
                                                      ``classification_
                                                      failed=True``.
  * ``oracle_consulted``                            — emitted on
                                                      every call
                                                      with
                                                      ``oracle="predicate_
                                                      distribution"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.fact_store import FactStore
from src.layer3_substrate.classifier_base import (
    _ClassificationFailed,
    _now_iso,
    _safe_emit_event,
    confidence_from_counts,
)
from src.llm_client import LLMClient


# Public constants. Mirror the SQL CHECK constraints in the
# ``predicate_distribution`` table.
LABELS: tuple[str, ...] = (
    "distributes_up",
    "distributes_down",
    "both",
    "neither",
)

RELATION_TYPES: tuple[str, ...] = ("is_a", "part_of")

POLARITIES: tuple[int, ...] = (0, 1)


@dataclass(frozen=True)
class PredicateDistributionRow:
    """A row from the ``predicate_distribution`` table.

    Snapshot at read time. The 4-tuple
    ``(pattern, predicate, polarity, taxonomy_relation_type)`` is
    the primary key.
    """

    pattern: str
    predicate: str
    polarity: int
    taxonomy_relation_type: str
    label: str
    reason: Optional[str]
    affirmed_count: int
    contradicted_count: int
    created_at: str
    last_consulted_at: Optional[str]
    id: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "pattern": self.pattern,
            "predicate": self.predicate,
            "polarity": self.polarity,
            "taxonomy_relation_type": self.taxonomy_relation_type,
            "label": self.label,
            "reason": self.reason,
            "affirmed_count": self.affirmed_count,
            "contradicted_count": self.contradicted_count,
            "confidence": self.confidence(),
            "created_at": self.created_at,
            "last_consulted_at": self.last_consulted_at,
        }

    def confidence(self) -> float:
        return confidence_from_counts(
            self.affirmed_count, self.contradicted_count,
        )


@dataclass(frozen=True)
class PredicateDistributionVerdict:
    """The result a caller receives from ``consult``.

    The verdict is a singleton — the question is "does this predicate
    propagate, and if so in which direction" — so there is no caller-
    frame ambiguity to resolve.

    ``classification_failed`` is True only when the LLM produced
    malformed tool output. ``label`` is None and ``row_id`` is None
    in that case — no row was written. The caller (Phase 7's
    derivation walker) treats this as a propagation-policy MISS and
    falls through (typically: do not propagate, since the conservative
    bias for unknown is to refuse propagation).
    """

    label: Optional[str]
    reason: Optional[str]
    row_id: Optional[int]
    served_from_cache: bool
    confidence: float
    classification_failed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "reason": self.reason,
            "row_id": self.row_id,
            "served_from_cache": self.served_from_cache,
            "confidence": self.confidence,
            "classification_failed": self.classification_failed,
        }


def _normalize_predicate(predicate: str) -> str:
    """Lowercase + strip. Mirrors Phase 3's predicate_equivalence
    normalization. Distribution behavior is a property of the
    predicate's semantics, not its capitalization.

    Raises ValueError on empty input post-normalization.
    """
    p = (predicate or "").strip().lower()
    if not p:
        raise ValueError(
            f"predicate must be non-empty after strip+lower; "
            f"got {predicate!r}"
        )
    return p


def _validate_inputs(
    pattern: str,
    predicate: str,
    polarity: int,
    taxonomy_relation_type: str,
) -> tuple[str, str, int, str]:
    """Normalize predicate; validate pattern/polarity/relation_type.

    Returns the 4-tuple ready for SQL.

    Raises ValueError on:
      * empty pattern (after strip — pattern names are extractor-
        controlled and case-sensitive, so we do not lowercase).
      * predicate empty after strip+lower (handled by
        _normalize_predicate).
      * polarity not in {0, 1}.
      * taxonomy_relation_type not in {is_a, part_of}.
    """
    pat = (pattern or "").strip()
    if not pat:
        raise ValueError(
            f"pattern must be non-empty after strip; got {pattern!r}"
        )
    pred = _normalize_predicate(predicate)
    if polarity not in POLARITIES:
        raise ValueError(
            f"polarity must be 0 or 1, got {polarity!r}"
        )
    if taxonomy_relation_type not in RELATION_TYPES:
        raise ValueError(
            f"taxonomy_relation_type {taxonomy_relation_type!r} "
            f"not in {RELATION_TYPES}"
        )
    return pat, pred, polarity, taxonomy_relation_type


# ----- LLM tool definition --------------------------------------------------

_CLASSIFY_TOOL = {
    "name": "record_predicate_distribution",
    "description": (
        "Record whether a predicate (in a given pattern at a given "
        "polarity) propagates up or down chains of a given taxonomy "
        "relation type. Pick the label whose definition fits and "
        "write a one-sentence reason."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "label": {
                "type": "string",
                "enum": list(LABELS),
                "description": (
                    "distributes_up: truth at a more-specific entity "
                    "propagates UP to the more-general (lives in "
                    "Williamstown ⇒ lives in Massachusetts under "
                    "part_of). distributes_down: truth at the more-"
                    "general entity propagates DOWN to instances "
                    "(likes animals ⇒ likes cheetahs under is_a). "
                    "both: distributes in both directions (rare). "
                    "neither: no propagation in either direction — "
                    "when in doubt, prefer neither."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "One short sentence explaining the choice. "
                    "Surfaces in the trace UI and the inspector "
                    "endpoint."
                ),
            },
        },
        "required": ["label", "reason"],
    },
}


_CLASSIFIER_SYSTEM = """You decide whether a predicate's truth propagates along chains of a taxonomy relation.

You are given:
  * pattern (e.g. spatial_temporal, preference, quantitative)
  * predicate (e.g. lives_in, likes, weighs)
  * polarity (1 = positive, 0 = negated)
  * taxonomy_relation_type (is_a OR part_of)

You return ONE of four labels.

# Why this question matters

Phase 7 of the system uses a derivation walker that combines stored facts with taxonomy chains. If a user says "I like animals" and the system later evaluates "you like cheetahs" — knowing that cheetahs are_a animals — should the walker say MATCH? Only if the predicate `likes` distributes_down is_a chains. If yes (this case), the walker derives the verdict; if no, the walker falls through.

Distribution policies are semantic properties of predicates. They depend on what the predicate MEANS and what kind of taxonomy chain we're walking. Your job is to label each (predicate, polarity, relation_type) combination correctly.

# Labels

**distributes_up** — truth at the more-specific entity propagates UP to the more-general. "X holds for the part" implies "X holds for the whole." Canonical case: residence under part_of. Living in Williamstown is living in Massachusetts (because Williamstown IS part of Massachusetts; if you reside in the part, you reside in the whole). Same for `located_in`, `visited`, `born_in` under part_of chains.

**distributes_down** — truth at the more-general entity propagates DOWN to the more-specific. "X holds for the category" implies "X holds for any instance." Canonical case: preference under is_a. If you like animals (the category), you like cheetahs (an instance). Same for `dislikes`, `fears`, `loves`, `hates` under is_a chains — categorical attitudes inherit downward.

**both** — distributes in BOTH directions. Rare. Reserved for predicates that are genuinely bidirectional under the relation. Most predicates are not.

**neither** — no propagation in either direction. The predicate is about a property of the specific entity that does not inherit categorically and does not aggregate compositionally. Canonical case: `weighs` under is_a. An animal's weight is a property of that specific animal; "animals weigh X kg" is meaningless as a category-level claim, and even if it were, "this cheetah weighs X kg" doesn't follow from any category-level weight.

# is_a chains vs part_of chains — the relation_type matters

This is the most important distinction in the prompt. The same predicate often has DIFFERENT distribution policies under is_a vs part_of, because the two relations describe DIFFERENT KINDS OF STRUCTURE:

  * is_a walks CATEGORICAL chains. Class membership. cheetah is_a animal; animal is_a living thing. A predicate distributes through is_a if its truth INHERITS through category membership — if the property is the kind of thing instances of a category share by virtue of being in the category. Preferences distribute down is_a (loving the category extends to instances). Properties of specific individuals (weighs, has_friend, owns) generally do not.

  * part_of walks COMPOSITIONAL chains. Constitutive parthood. Williamstown part_of Massachusetts; cell part_of tissue; chapter part_of book. A predicate distributes through part_of if its truth AGGREGATES across composition — if a property of a part transfers to the whole, or vice versa. Spatial-residence predicates distribute up part_of (residing in a part means residing in the whole). Properties tied to the whole-as-such (the book has 350 pages) do not distribute down to the parts.

The CANONICAL HARD CASE this oracle exists to handle: the same predicate gets DIFFERENT labels under is_a vs part_of. Take `lives_in`:

  * (spatial_temporal, lives_in, p=1, is_a) → neither. Living in a city does NOT mean living in all cities; living in a city does NOT mean living in some sub-thing of "city" the category — categorical chains do not preserve residence, because "city" is a category and residence is about specific locations.

  * (spatial_temporal, lives_in, p=1, part_of) → distributes_up. Living in Williamstown means living in Massachusetts, because Williamstown is constitutively part of Massachusetts and residing in a part means residing in the whole.

The reason these get different labels is structural: is_a chains do not preserve location-based propositions because the chain is about category membership, not about spatial composition; part_of chains DO preserve location-based propositions because the chain IS about spatial composition. Apply this principle: ASK whether the predicate is about category-membership behavior (then is_a is the relevant chain) or about composition behavior (then part_of is the relevant chain) — and answer accordingly.

# Polarity matters

Distribution behavior can differ between positive and negative polarity. The clean case:

  * (preference, likes, p=1, is_a) → distributes_down. "I like animals" entails "I like cheetahs" (a specific instance).

  * (preference, likes, p=0, is_a) → neither. "I don't like animals" does NOT entail "I don't like cheetahs." (You might love this one cheetah anyway. The negative-categorical-preference is consistent with positive-individual-preferences.)

The same asymmetry holds for `dislikes`, `loves`, `hates`, `fears`. Positive categorical attitudes distribute down; negated ones do not, because "I don't have attitude X toward the category" is consistent with having or lacking attitude X toward any specific instance.

# Conservative bias

When uncertain, prefer **neither**. Wrong-distribution calls let Phase 7's derivation walker propagate facts across taxonomy links it shouldn't propagate through, contaminating derived verdicts. Wrong-`neither` calls just fail to derive — fall-through to fresh verification.

The asymmetry favors `neither` on uncertainty.

# Worked examples

The order is deliberate. Edge cases come first — directional asymmetry and polarity sensitivity are the failure modes this oracle exists to handle.

## Edge: directional asymmetry — same predicate, different relation_type, different label

(spatial_temporal, lives_in, p=1, is_a) → label: neither, reason: "Living in a specific location does not propagate down or up an is_a chain — categorical class membership does not preserve residence."

(spatial_temporal, lives_in, p=1, part_of) → label: distributes_up, reason: "Residing in a part means residing in the whole — Williamstown part_of Massachusetts implies a Williamstown resident is a Massachusetts resident."

(spatial_temporal, located_in, p=1, is_a) → label: neither, reason: "Location is not a category-membership property; an entity's location does not propagate along is_a chains."

(spatial_temporal, located_in, p=1, part_of) → label: distributes_up, reason: "Location aggregates compositionally — being located in a part means being located in the whole."

## Edge: polarity sensitivity

(preference, likes, p=1, is_a) → label: distributes_down, reason: "Liking a category propagates to its instances — likes animals ⇒ likes cheetahs."

(preference, likes, p=0, is_a) → label: neither, reason: "Not liking the category does not entail not liking specific instances — a negated categorical preference is silent on individual instances."

(preference, dislikes, p=1, is_a) → label: distributes_down, reason: "Disliking a category propagates to its instances."

(preference, dislikes, p=0, is_a) → label: neither, reason: "Not disliking the category is silent on whether any specific instance is disliked or not."

## distributes_down (categorical attitudes under is_a)

(preference, fears, p=1, is_a) → label: distributes_down, reason: "Fear of a category propagates to its instances — fears predators ⇒ fears wolves."

(preference, loves, p=1, is_a) → label: distributes_down, reason: "Loving a category propagates to its instances."

(preference, hates, p=1, is_a) → label: distributes_down, reason: "Hating a category propagates to its instances."

## distributes_up (location-like predicates under part_of)

(spatial_temporal, born_in, p=1, part_of) → label: distributes_up, reason: "Birth location is constitutive — born in Williamstown means born in Massachusetts."

(spatial_temporal, visited, p=1, part_of) → label: distributes_up, reason: "Visiting a part counts as visiting the whole."

## neither (predicates that don't propagate)

(quantitative, weighs, p=1, is_a) → label: neither, reason: "Weight is a property of a specific individual; categorical chains do not preserve quantitative properties of individuals."

(quantitative, weighs, p=1, part_of) → label: neither, reason: "Weight does not distribute compositionally without further information — a part can weigh less than the whole, and the whole's weight is the sum of its parts (under standard part-of), not propagated as identity."

(quantitative, has_count, p=1, is_a) → label: neither, reason: "Count is a property of the specific entity; categorical chains don't preserve it."

(relational, has_friend, p=1, is_a) → label: neither, reason: "Having a specific friend does not propagate along category chains."

(relational, owns, p=1, is_a) → label: neither, reason: "Ownership is a property of a specific owner toward a specific item; categorical chains do not preserve it."

# Output

Always call the `record_predicate_distribution` tool exactly once with `label` and `reason`."""


def _build_user_message(
    pattern: str,
    predicate: str,
    polarity: int,
    taxonomy_relation_type: str,
) -> str:
    """Render the per-call user message. The 4-tuple is presented
    verbatim — the oracle does not transform inputs (predicate
    is already lowercased + stripped by the validator)."""
    return (
        "Classify this predicate's distribution policy.\n\n"
        f"  pattern: {pattern!r}\n"
        f"  predicate: {predicate!r}\n"
        f"  polarity: {polarity!r}  ({'positive' if polarity == 1 else 'negated'})\n"
        f"  taxonomy_relation_type: {taxonomy_relation_type!r}\n\n"
        "Decide whether the predicate's truth propagates along "
        "chains of this relation_type — distributes_up, "
        "distributes_down, both, or neither — and call "
        "record_predicate_distribution."
    )


# ----- the oracle ----------------------------------------------------------


class PredicateDistribution:
    """Memoized LLM classifier for predicate distribution policy.

    One instance per FactStore. The constructor takes only the store;
    an LLM is supplied per ``consult`` call.

    Phase 5 ships the oracle DORMANT — no Tier U / W / derivation
    consumer wires it. The inspector endpoints and tests populate
    the table. Phase 7's derivation walker is the consumer.
    """

    ORACLE_NAME = "predicate_distribution"

    def __init__(self, store: FactStore):
        self._store = store

    # ---- public API --------------------------------------------------------

    def consult(
        self,
        pattern: str,
        predicate: str,
        polarity: int,
        taxonomy_relation_type: str,
        *,
        llm: Optional[LLMClient] = None,
        source_turn_id: Optional[int] = None,
    ) -> PredicateDistributionVerdict:
        """Classify the (pattern, predicate, polarity, relation_type)
        4-tuple.

        Lookup-then-write-through. On SQL hit, returns cached verdict
        (no LLM call) and bumps last_consulted_at. On miss, calls the
        LLM, UPSERTs the row with counts at (0, 0). On malformed LLM
        output, emits ``predicate_distribution_classification_failed``
        and returns ``classification_failed=True``.

        SINGLETON KEY — there is no canonical-pair swap. Each unique
        4-tuple is its own row. In particular, the same (pattern,
        predicate, polarity) under different relation_types is two
        distinct rows that may have different labels.
        """
        pat, pred, pol, rt = _validate_inputs(
            pattern, predicate, polarity, taxonomy_relation_type,
        )
        existing = self._lookup_validated(pat, pred, pol, rt)
        if existing is not None:
            return self._serve_hit(existing, source_turn_id)

        if llm is None:
            raise RuntimeError(
                f"predicate_distribution consult miss for "
                f"({pattern!r}, {predicate!r}, {polarity!r}, "
                f"{taxonomy_relation_type!r}) with no LLM provided; "
                f"supply llm= or warm the cache via record() first"
            )

        try:
            label, reason = self._classify_via_llm(
                pat, pred, pol, rt, llm,
            )
        except _ClassificationFailed as failure:
            _safe_emit_event(
                self._store, source_turn_id,
                "predicate_distribution_classification_failed",
                {
                    "pattern": pat,
                    "predicate": pred,
                    "polarity": pol,
                    "taxonomy_relation_type": rt,
                    "raw": failure.raw,
                    "reason": failure.reason,
                },
            )
            _safe_emit_event(
                self._store, source_turn_id,
                "oracle_consulted",
                {
                    "oracle": self.ORACLE_NAME,
                    "outcome": "classification_failed",
                    "pattern": pat,
                    "predicate": pred,
                    "polarity": pol,
                    "taxonomy_relation_type": rt,
                },
            )
            return PredicateDistributionVerdict(
                label=None,
                reason=failure.reason,
                row_id=None,
                served_from_cache=False,
                confidence=confidence_from_counts(0, 0),
                classification_failed=True,
            )

        row = self._record_validated(pat, pred, pol, rt, label, reason)
        _safe_emit_event(
            self._store, source_turn_id,
            "predicate_distribution_write",
            {
                "id": row.id,
                "pattern": pat,
                "predicate": pred,
                "polarity": pol,
                "taxonomy_relation_type": rt,
                "label": label,
                "reason": reason,
            },
        )
        _safe_emit_event(
            self._store, source_turn_id,
            "oracle_consulted",
            {
                "oracle": self.ORACLE_NAME,
                "outcome": "write",
                "pattern": pat,
                "predicate": pred,
                "polarity": pol,
                "taxonomy_relation_type": rt,
                "label": label,
                "row_id": row.id,
            },
        )
        return PredicateDistributionVerdict(
            label=row.label,
            reason=row.reason,
            row_id=row.id,
            served_from_cache=False,
            confidence=row.confidence(),
            classification_failed=False,
        )

    def lookup(
        self,
        pattern: str,
        predicate: str,
        polarity: int,
        taxonomy_relation_type: str,
    ) -> Optional[PredicateDistributionRow]:
        """Pure read. Returns the row at this exact 4-tuple or None.
        Predicate normalization (lowercase + strip) is applied to
        the lookup arguments so callers don't need to pre-normalize.

        Raises ValueError on empty pattern, empty predicate (after
        normalization), polarity ∉ {0, 1}, or unknown relation_type.
        """
        pat, pred, pol, rt = _validate_inputs(
            pattern, predicate, polarity, taxonomy_relation_type,
        )
        return self._lookup_validated(pat, pred, pol, rt)

    def list_rows(
        self,
        pattern: Optional[str] = None,
        polarity: Optional[int] = None,
    ) -> list[PredicateDistributionRow]:
        """List rows, optionally filtered by pattern and/or polarity.
        Used by the inspector endpoint at
        /v2/api/substrate/predicate-distribution. Order is
        (pattern, predicate, polarity, taxonomy_relation_type).
        """
        clauses: list[str] = []
        params: list[Any] = []
        if pattern is not None:
            clauses.append("pattern = ?")
            params.append(pattern.strip())
        if polarity is not None:
            if polarity not in POLARITIES:
                raise ValueError(
                    f"polarity must be 0 or 1, got {polarity!r}"
                )
            clauses.append("polarity = ?")
            params.append(polarity)
        sql = (
            "SELECT id, pattern, predicate, polarity, "
            "taxonomy_relation_type, label, reason, affirmed_count, "
            "contradicted_count, created_at, last_consulted_at "
            "FROM predicate_distribution"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += (
            " ORDER BY pattern, predicate, polarity, "
            "taxonomy_relation_type"
        )
        rows = self._store._conn.execute(sql, params).fetchall()
        return [_row_to_dataclass(r) for r in rows]

    def record(
        self,
        pattern: str,
        predicate: str,
        polarity: int,
        taxonomy_relation_type: str,
        label: str,
        reason: Optional[str],
    ) -> PredicateDistributionRow:
        """Direct UPSERT — bypasses the LLM. For tests and calibration
        warmup. Counts are PRESERVED across overwrites; only label,
        reason, and last_consulted_at update on conflict.

        Validates label at the Python layer so callers get a clearer
        error than the SQL CHECK.
        """
        if label not in LABELS:
            raise ValueError(f"label {label!r} not in {LABELS}")
        pat, pred, pol, rt = _validate_inputs(
            pattern, predicate, polarity, taxonomy_relation_type,
        )
        return self._record_validated(pat, pred, pol, rt, label, reason)

    # ---- internals ---------------------------------------------------------

    def _lookup_validated(
        self,
        pattern: str,
        predicate: str,
        polarity: int,
        taxonomy_relation_type: str,
    ) -> Optional[PredicateDistributionRow]:
        row = self._store._conn.execute(
            "SELECT id, pattern, predicate, polarity, "
            "taxonomy_relation_type, label, reason, affirmed_count, "
            "contradicted_count, created_at, last_consulted_at "
            "FROM predicate_distribution "
            "WHERE pattern = ? AND predicate = ? AND polarity = ? "
            "AND taxonomy_relation_type = ?",
            (pattern, predicate, polarity, taxonomy_relation_type),
        ).fetchone()
        if row is None:
            return None
        return _row_to_dataclass(row)

    def _serve_hit(
        self,
        row: PredicateDistributionRow,
        source_turn_id: Optional[int],
    ) -> PredicateDistributionVerdict:
        self._touch_consulted(
            row.pattern, row.predicate, row.polarity,
            row.taxonomy_relation_type,
        )
        _safe_emit_event(
            self._store, source_turn_id,
            "predicate_distribution_hit",
            {
                "id": row.id,
                "pattern": row.pattern,
                "predicate": row.predicate,
                "polarity": row.polarity,
                "taxonomy_relation_type": row.taxonomy_relation_type,
                "label": row.label,
                "affirmed_count": row.affirmed_count,
                "contradicted_count": row.contradicted_count,
                "created_at": row.created_at,
            },
        )
        _safe_emit_event(
            self._store, source_turn_id,
            "oracle_consulted",
            {
                "oracle": self.ORACLE_NAME,
                "outcome": "hit",
                "pattern": row.pattern,
                "predicate": row.predicate,
                "polarity": row.polarity,
                "taxonomy_relation_type": row.taxonomy_relation_type,
                "label": row.label,
                "row_id": row.id,
            },
        )
        return PredicateDistributionVerdict(
            label=row.label,
            reason=row.reason,
            row_id=row.id,
            served_from_cache=True,
            confidence=row.confidence(),
            classification_failed=False,
        )

    def _record_validated(
        self,
        pattern: str,
        predicate: str,
        polarity: int,
        taxonomy_relation_type: str,
        label: str,
        reason: Optional[str],
    ) -> PredicateDistributionRow:
        """UPSERT a row at the 4-tuple key. Counts preserved on
        conflict; only label, reason, and last_consulted_at update.
        """
        now = _now_iso()
        self._store._conn.execute(
            """
            INSERT INTO predicate_distribution (
                pattern, predicate, polarity, taxonomy_relation_type,
                label, reason, affirmed_count, contradicted_count,
                created_at, last_consulted_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
            ON CONFLICT (pattern, predicate, polarity,
                         taxonomy_relation_type) DO UPDATE SET
                label = excluded.label,
                reason = excluded.reason,
                last_consulted_at = excluded.last_consulted_at
            """,
            (
                pattern, predicate, polarity, taxonomy_relation_type,
                label, reason, now, now,
            ),
        )
        self._store._conn.commit()
        row = self._lookup_validated(
            pattern, predicate, polarity, taxonomy_relation_type,
        )
        assert row is not None  # we just wrote it
        return row

    def _touch_consulted(
        self,
        pattern: str,
        predicate: str,
        polarity: int,
        taxonomy_relation_type: str,
    ) -> None:
        """Bump last_consulted_at; observability metadata only. NOT
        a reinforcement signal."""
        self._store._conn.execute(
            "UPDATE predicate_distribution SET last_consulted_at = ? "
            "WHERE pattern = ? AND predicate = ? AND polarity = ? "
            "AND taxonomy_relation_type = ?",
            (
                _now_iso(), pattern, predicate, polarity,
                taxonomy_relation_type,
            ),
        )
        self._store._conn.commit()

    def _classify_via_llm(
        self,
        pattern: str,
        predicate: str,
        polarity: int,
        taxonomy_relation_type: str,
        llm: LLMClient,
    ) -> tuple[str, str]:
        try:
            raw = llm.extract_with_tool(
                system=_CLASSIFIER_SYSTEM,
                user_message=_build_user_message(
                    pattern, predicate, polarity,
                    taxonomy_relation_type,
                ),
                tool=_CLASSIFY_TOOL,
                purpose="predicate_distribution",
            )
        except Exception as exc:
            raise _ClassificationFailed(
                reason=f"LLM call raised: {type(exc).__name__}: {exc}",
                raw={},
            )
        if not isinstance(raw, dict):
            raise _ClassificationFailed(
                reason=(
                    f"tool response was not a dict "
                    f"(got {type(raw).__name__})"
                ),
                raw={"received": str(raw)[:200]},
            )
        label = raw.get("label")
        if not isinstance(label, str) or label not in LABELS:
            raise _ClassificationFailed(
                reason=f"label {label!r} not in {LABELS}",
                raw=dict(raw),
            )
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise _ClassificationFailed(
                reason="reason missing or empty",
                raw=dict(raw),
            )
        return label, reason.strip()


def _row_to_dataclass(row: Any) -> PredicateDistributionRow:
    return PredicateDistributionRow(
        id=int(row["id"]),
        pattern=row["pattern"],
        predicate=row["predicate"],
        polarity=int(row["polarity"]),
        taxonomy_relation_type=row["taxonomy_relation_type"],
        label=row["label"],
        reason=row["reason"],
        affirmed_count=int(row["affirmed_count"] or 0),
        contradicted_count=int(row["contradicted_count"] or 0),
        created_at=row["created_at"],
        last_consulted_at=row["last_consulted_at"],
    )
