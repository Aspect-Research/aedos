"""Layer 3 substrate oracle: entity taxonomy.

Decides, for an ordered (child, parent) pair under a given
``relation_type`` ∈ {is_a, part_of}, which of four labels applies:

  * ``child_subsumed_by_parent`` — natural direction. The entity in
                                   the ``child`` column is subsumed
                                   by (is_a / part_of) the entity in
                                   the ``parent`` column. e.g.
                                   ``(golden retriever, dog, is_a)``
                                   or ``(Williamstown, Massachusetts,
                                   part_of)``.
  * ``parent_subsumed_by_child`` — caller passed them in inverted
                                   order. e.g. ``(mammal, golden
                                   retriever, is_a)``. Same fact;
                                   the position labels are reversed
                                   relative to the natural reading.
  * ``equivalent``                — same level under the relation.
                                   ``(Holland, Netherlands, is_a)``;
                                   ``(Burma, Myanmar, part_of)``.
                                   Rare but real.
  * ``neither``                   — no taxonomic relation under this
                                   relation_type. ``(Apple, fruit,
                                   is_a)`` (the company is not a
                                   fruit); ``(doctor, hospital,
                                   is_a)`` (functional relation, not
                                   categorical).

Pattern-independence — same architectural decision as
entity_equivalence
================================================================

Taxonomy is pattern-independent. Williamstown's relationship to
Massachusetts holds regardless of which pattern referred to either
entity. The table has NO pattern column. Pattern-keying would force
re-classification of the same chain across patterns and miss the
cross-pattern subsumption Phase 7's derivation walker relies on.

Directional storage — the divergence from the symmetric oracles
================================================================

predicate_equivalence and entity_equivalence are symmetric — the
verdict is unchanged under swap of the pair, so a canonical-pair
helper (lex-smaller first) lets one row cover both orderings.

entity_taxonomy is DIRECTIONAL. ``(child=Williamstown,
parent=Massachusetts, part_of)`` is a different proposition from
``(child=Massachusetts, parent=Williamstown, part_of)``. The first
is true (with label ``child_subsumed_by_parent``); the second is
also true (with label ``parent_subsumed_by_child``) — they describe
the same fact about the world but the column positions encode
which entity the caller framed as more-specific.

The architectural decision (locked in the Phase 5 plan): store
EITHER ordering the caller provided. Different orderings produce
different rows. The label tells the consumer the direction. There
is NO canonical-pair swap. ``UNIQUE (child, parent, relation_type)``
plus ``CHECK (child != parent)`` are the storage invariants.

Why: the consumer (Phase 7's derivation walker) walks specific
directions through the taxonomy and asks oracle questions in those
directions. Forcing it to interpret a label-flip on a swapped
lookup adds machinery in the consumer that the simple shape doesn't
need. Cost: at most 2× LLM calls to fully populate a node-pair.
With memoization that's bounded. If Phase 7 finds the doubling
wasteful, a ``consult_either_direction()`` helper can live in the
walker; the oracle's clean semantics stay.

Case-sensitivity — same contract as entity_equivalence
======================================================

Strip-only normalization. NO lowercase. Case is semantic for
entities: ``apple`` (the fruit) is an instance of ``fruit``, but
``Apple`` (the company) is not. The taxonomy must respect that
distinction.

Cost characteristics
====================

Phase 5 ships entity_taxonomy DORMANT — no consumer wires it. The
inspector endpoints and tests are the only callers. Phase 7's
derivation walker is the consumer that turns substrate rows into
derived verdicts.

When Phase 7 lands, derivation-walk cost on a cold-start chain of
length N is O(N) oracle consults (each new (child, parent,
relation_type) triple pays one LLM call once, then memoizes).
Warm-cache cost approaches zero (SQL hits, no LLM).

Pipeline events
===============

  * ``entity_taxonomy_hit``                  — SQL cache hit, no
                                               LLM call.
  * ``entity_taxonomy_write``                — LLM ran, row was
                                               UPSERTed.
  * ``entity_taxonomy_classification_failed``
                                             — LLM returned a
                                               malformed tool
                                               response. No row
                                               written. Verdict
                                               carries
                                               ``classification_
                                               failed=True``.
  * ``oracle_consulted``                     — emitted on every
                                               call with
                                               ``oracle="entity_
                                               taxonomy"``.
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
# ``entity_taxonomy`` table; kept here as Python tuples so consumers
# can validate without round-tripping through the DB.
LABELS: tuple[str, ...] = (
    "child_subsumed_by_parent",
    "parent_subsumed_by_child",
    "equivalent",
    "neither",
)

RELATION_TYPES: tuple[str, ...] = ("is_a", "part_of")


@dataclass(frozen=True)
class EntityTaxonomyRow:
    """A row from the ``entity_taxonomy`` table.

    The (child, parent, relation_type) triple is positional and
    meaningful — column order encodes which entity the caller framed
    as more-specific. Snapshot at read time.
    """

    child: str
    parent: str
    relation_type: str
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
            "child": self.child,
            "parent": self.parent,
            "relation_type": self.relation_type,
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
class EntityTaxonomyVerdict:
    """The result a caller receives from ``consult``.

    The verdict is REPORTED IN THE CALLER'S FRAME — i.e. the label
    refers to the column positions the caller passed in. If the
    caller asked ``consult(Williamstown, Massachusetts, part_of)``,
    a label of ``child_subsumed_by_parent`` means Williamstown is
    subsumed by Massachusetts. If the same caller (or a different
    one) asks ``consult(Massachusetts, Williamstown, part_of)``,
    that's a separate row and the natural verdict is
    ``parent_subsumed_by_child``.

    ``classification_failed`` is True only when the LLM produced
    malformed tool output. ``label`` is None and ``row_id`` is None
    in that case — no row was written.
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


def _normalize_inputs(
    child: str, parent: str, relation_type: str,
) -> tuple[str, str, str]:
    """Strip whitespace from child / parent; validate relation_type.

    **Architectural contract:** ``strip()`` only. NO lowercase. Case
    is semantic for entities (Apple the company vs. apple the fruit).
    Mirrors entity_equivalence's normalization contract.

    Raises ValueError on:
      * empty child or parent (after strip)
      * child equals parent (after strip) — self-pairs not in domain
      * relation_type not in {is_a, part_of}
    """
    c = (child or "").strip()
    p = (parent or "").strip()
    if not c or not p:
        raise ValueError(
            f"both child and parent must be non-empty after strip; "
            f"got ({child!r}, {parent!r}) -> ({c!r}, {p!r})"
        )
    if c == p:
        raise ValueError(
            f"entity_taxonomy does not classify self-pairs; "
            f"got {c!r} for both child and parent"
        )
    if relation_type not in RELATION_TYPES:
        raise ValueError(
            f"relation_type {relation_type!r} not in {RELATION_TYPES}"
        )
    return c, p, relation_type


# ----- LLM tool definition --------------------------------------------------

_CLASSIFY_TOOL = {
    "name": "record_entity_taxonomy",
    "description": (
        "Record the taxonomic relationship between a (child, parent) "
        "pair under a specified relation_type (is_a or part_of). "
        "Pick the label whose definition fits and write a one-"
        "sentence reason."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "label": {
                "type": "string",
                "enum": list(LABELS),
                "description": (
                    "child_subsumed_by_parent: the entity in the "
                    "child column is subsumed by (is_a/part_of) the "
                    "entity in the parent column — natural direction. "
                    "parent_subsumed_by_child: caller passed the "
                    "arguments in inverted order; the entity in the "
                    "parent column is actually the more specific one. "
                    "equivalent: same level under the relation (Holland/"
                    "Netherlands; Burma/Myanmar). neither: no taxonomic "
                    "relation under this relation_type — when in doubt, "
                    "prefer neither."
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


_CLASSIFIER_SYSTEM = """You decide how a (child, parent) entity pair relates under a specified taxonomy relation_type.

The relation_type is given to you. It is is_a (categorical subsumption — class membership) OR part_of (constitutive parthood — composition). You are NOT picking the relation_type; the caller decided. Your job is to label whether the (child, parent) pair fits that relation, and in which direction.

You return ONE of four labels.

# Labels

**child_subsumed_by_parent** — the entity in the `child` column is subsumed by the entity in the `parent` column under the given relation_type. This is the NATURAL direction. The child is the more specific one; the parent is the more general one. `(golden retriever, dog, is_a)` — golden retriever is a kind of dog. `(Williamstown, Massachusetts, part_of)` — Williamstown is part of Massachusetts.

**parent_subsumed_by_child** — caller passed the arguments in INVERTED order. The entity in the `parent` column is actually the more specific one; the entity in the `child` column is the more general. `(mammal, golden retriever, is_a)` — the position labeled child here is "mammal" (the broader class), so the natural reading is reversed. The relation IS true, just framed inside-out.

**equivalent** — the two entities denote the same level under the relation. They are not surface aliases (that's entity_equivalence's job); they are taxonomically same-level. Holland and the Netherlands are the same country; Burma and Myanmar are the same country (politically renamed). `(Holland, Netherlands, is_a)` and `(Holland, Netherlands, part_of)` both pick equivalent — the entities are the same, so neither is_a-subsumed nor part_of-subsumed.

**neither** — no taxonomic relation between the entities under this relation_type. The entities may be related by something else (location, function, ownership, parent corporation, similarity-of-name) but they do not stand in is_a or part_of relation under the column ordering given. `(Apple, fruit, is_a)` — capital Apple is the company, not a fruit. `(doctor, hospital, is_a)` — doctors work at hospitals but are not a kind of hospital. `(key, lock, is_a)` — functional relation, not categorical.

# is_a vs part_of — the relation_type matters

is_a walks CATEGORICAL chains. Membership in a class. `(cheetah, animal, is_a)` because a cheetah is a kind of animal. `(sedan, car, is_a)` because a sedan is a kind of car. `(sonnet, poem, is_a)` because a sonnet is a kind of poem.

part_of walks COMPOSITIONAL chains. Parthood between distinct entities, not class membership. `(Williamstown, Massachusetts, part_of)` because Williamstown is a part of Massachusetts (it's not a kind of Massachusetts). `(cell, tissue, part_of)`. `(chapter, book, part_of)`.

The two relations are NOT interchangeable. `(Williamstown, Massachusetts, is_a)` is `neither` (Williamstown is not a kind of Massachusetts; it's part of one). `(cheetah, animal, part_of)` is `neither` (a cheetah is not part of "animal" the category; it's an instance of it).

When the (child, parent) pair fits ONE relation_type but not the other, and the caller picked the WRONG relation_type, label as `neither`. Do not "rescue" the pair by picking a label that would be right under a different relation_type — the relation_type is fixed by the caller.

# Conservative bias

When uncertain, prefer **neither**. Wrong-subsumption calls let the derivation walker (Phase 7) propagate facts up or down chains they shouldn't propagate through, contaminating derived verdicts. Wrong-`neither` calls just fail to derive — fall through to fresh verification. The asymmetry favors `neither` on uncertainty.

Case carries entity-disambiguation signal. `(apple, fruit, is_a)` is child_subsumed_by_parent. `(Apple, fruit, is_a)` is `neither` — the company is not a fruit. Respect the case.

# Worked examples

The order is deliberate. Edge cases come first — over-subsumption tempting and reverse-direction are the ones the oracle exists to handle.

## Edge: case carries disambiguation

(child=Apple, parent=fruit, relation_type=is_a) → label: neither, reason: "Capital 'Apple' is the company; the company is not a kind of fruit. Lowercase 'apple' would be child_subsumed_by_parent."

(child=Tesla, parent=battery, relation_type=is_a) → label: neither, reason: "Tesla is a car company; the company is not a battery. Cars use batteries but cars-the-category is not a battery-category."

(child=Mercury, parent=metal, relation_type=is_a) → label: neither, reason: "Capital 'Mercury' is the planet (or god, or band), none of which is a metal. Lowercase 'mercury' the element would be child_subsumed_by_parent."

## Edge: caller swapped the arguments — parent_subsumed_by_child

(child=mammal, parent=golden retriever, relation_type=is_a) → label: parent_subsumed_by_child, reason: "Inversion: the natural direction is golden retriever is_a mammal, so under the given column ordering the parent (golden retriever) is the more specific."

(child=Massachusetts, parent=Williamstown, relation_type=part_of) → label: parent_subsumed_by_child, reason: "Inversion: Williamstown is part of Massachusetts, so the parent column holds the more specific entity."

(child=animal, parent=cheetah, relation_type=is_a) → label: parent_subsumed_by_child, reason: "Cheetah is a kind of animal; the natural direction is reversed under this column ordering."

## Edge: relation_type mismatch — neither

(child=Williamstown, parent=Massachusetts, relation_type=is_a) → label: neither, reason: "Williamstown is part of Massachusetts, not a kind of Massachusetts; under is_a this is neither."

(child=cheetah, parent=animal, relation_type=part_of) → label: neither, reason: "A cheetah is an instance of animal (categorical), not a piece of 'animal' (compositional); under part_of this is neither."

(child=chapter, parent=book, relation_type=is_a) → label: neither, reason: "A chapter is part of a book, not a kind of book; under is_a this is neither."

## Edge: equivalent — same entity, two surface forms

(child=Holland, parent=Netherlands, relation_type=is_a) → label: equivalent, reason: "Same country under two surface forms; neither subsumes the other taxonomically."

(child=Burma, parent=Myanmar, relation_type=part_of) → label: equivalent, reason: "Same country (politically renamed in 1989); same level under part_of."

## child_subsumed_by_parent (clean is_a chains)

(child=golden retriever, parent=dog, relation_type=is_a) → label: child_subsumed_by_parent, reason: "Golden retriever is a breed of dog."

(child=dog, parent=mammal, relation_type=is_a) → label: child_subsumed_by_parent, reason: "Dogs are mammals."

(child=sedan, parent=car, relation_type=is_a) → label: child_subsumed_by_parent, reason: "A sedan is a body style of car."

(child=sonnet, parent=poem, relation_type=is_a) → label: child_subsumed_by_parent, reason: "A sonnet is a fixed verse form of poem."

## child_subsumed_by_parent (clean part_of chains)

(child=Williamstown, parent=Berkshire County, relation_type=part_of) → label: child_subsumed_by_parent, reason: "Williamstown is one of the towns in Berkshire County."

(child=Berkshire County, parent=Massachusetts, relation_type=part_of) → label: child_subsumed_by_parent, reason: "Berkshire County is one of Massachusetts's counties."

(child=cell, parent=tissue, relation_type=part_of) → label: child_subsumed_by_parent, reason: "Cells compose tissues."

(child=chapter, parent=book, relation_type=part_of) → label: child_subsumed_by_parent, reason: "Chapters are constituent parts of a book."

## neither (cross-relation distractors)

(child=doctor, parent=hospital, relation_type=is_a) → label: neither, reason: "Doctors work at hospitals; neither categorical subsumption nor parthood."

(child=doctor, parent=hospital, relation_type=part_of) → label: neither, reason: "Doctors are employees of hospitals, not constitutive parts; functional/employment relation, not parthood."

(child=key, parent=lock, relation_type=is_a) → label: neither, reason: "Functional pairing; a key is not a kind of lock."

(child=planet, parent=orbit, relation_type=is_a) → label: neither, reason: "Planets travel along orbits; an orbit is a path, not a category that planets belong to."

# Output

Always call the `record_entity_taxonomy` tool exactly once with `label` and `reason`."""


def _build_user_message(
    child: str, parent: str, relation_type: str,
) -> str:
    """Render the per-call user message. The caller's column
    positions and relation_type are presented verbatim; the oracle
    does not swap or canonicalize."""
    return (
        "Classify this taxonomic relationship.\n\n"
        f"  child: {child!r}\n"
        f"  parent: {parent!r}\n"
        f"  relation_type: {relation_type!r}\n\n"
        "Decide which of the four labels applies under THIS column "
        "ordering and THIS relation_type, and call "
        "record_entity_taxonomy."
    )


# ----- the oracle ----------------------------------------------------------


class EntityTaxonomy:
    """Memoized LLM classifier for (child, parent, relation_type)
    taxonomic triples.

    One instance per FactStore. The constructor takes only the store;
    an LLM is supplied per ``consult`` call.

    Phase 5 ships the oracle DORMANT — no Tier U / W / derivation
    consumer wires it. The inspector endpoints and tests populate
    the table. Phase 7's derivation walker is the consumer.
    """

    ORACLE_NAME = "entity_taxonomy"

    def __init__(self, store: FactStore):
        self._store = store

    # ---- public API --------------------------------------------------------

    def consult(
        self,
        child: str,
        parent: str,
        relation_type: str,
        *,
        llm: Optional[LLMClient] = None,
        source_turn_id: Optional[int] = None,
    ) -> EntityTaxonomyVerdict:
        """Classify the (child, parent, relation_type) triple.

        Lookup-then-write-through. On SQL hit, returns the cached
        verdict (no LLM call) and bumps last_consulted_at. On miss,
        calls the LLM, UPSERTs the row with counts at (0, 0), and
        returns the fresh verdict. On malformed LLM output, emits
        ``entity_taxonomy_classification_failed`` and returns a
        verdict with ``classification_failed=True`` and
        ``label=None`` — the caller treats this as no-signal.

        DIRECTIONAL — passing ``(child=Williamstown,
        parent=Massachusetts, part_of)`` and ``(child=Massachusetts,
        parent=Williamstown, part_of)`` are TWO DISTINCT lookups.
        Each produces (or hits) a separate row.
        """
        c, p, rt = _normalize_inputs(child, parent, relation_type)
        existing = self._lookup_normalized(c, p, rt)
        if existing is not None:
            return self._serve_hit(existing, source_turn_id)

        if llm is None:
            raise RuntimeError(
                f"entity_taxonomy consult miss for "
                f"({child!r}, {parent!r}, {relation_type!r}) with no "
                f"LLM provided; supply llm= or warm the cache via "
                f"record() first"
            )

        try:
            label, reason = self._classify_via_llm(c, p, rt, llm)
        except _ClassificationFailed as failure:
            _safe_emit_event(
                self._store, source_turn_id,
                "entity_taxonomy_classification_failed",
                {
                    "child": c,
                    "parent": p,
                    "relation_type": rt,
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
                    "child": c,
                    "parent": p,
                    "relation_type": rt,
                },
            )
            return EntityTaxonomyVerdict(
                label=None,
                reason=failure.reason,
                row_id=None,
                served_from_cache=False,
                confidence=confidence_from_counts(0, 0),
                classification_failed=True,
            )

        row = self._record_normalized(c, p, rt, label, reason)
        _safe_emit_event(
            self._store, source_turn_id,
            "entity_taxonomy_write",
            {
                "id": row.id,
                "child": c,
                "parent": p,
                "relation_type": rt,
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
                "child": c,
                "parent": p,
                "relation_type": rt,
                "label": label,
                "row_id": row.id,
            },
        )
        return EntityTaxonomyVerdict(
            label=row.label,
            reason=row.reason,
            row_id=row.id,
            served_from_cache=False,
            confidence=row.confidence(),
            classification_failed=False,
        )

    def lookup(
        self,
        child: str,
        parent: str,
        relation_type: str,
    ) -> Optional[EntityTaxonomyRow]:
        """Pure read. Returns the row at this exact column ordering,
        or None on miss. No swap-canonicalization — directional.

        Raises ValueError on empty inputs, self-pair, or unknown
        relation_type.
        """
        c, p, rt = _normalize_inputs(child, parent, relation_type)
        return self._lookup_normalized(c, p, rt)

    def list_rows(
        self,
        relation_type: Optional[str] = None,
    ) -> list[EntityTaxonomyRow]:
        """List rows, optionally filtered by relation_type. Used by
        the inspector endpoint at /v2/api/substrate/entity-taxonomy.
        Order is (relation_type, child, parent).
        """
        if relation_type is None:
            rows = self._store._conn.execute(
                "SELECT id, child, parent, relation_type, label, "
                "reason, affirmed_count, contradicted_count, "
                "created_at, last_consulted_at "
                "FROM entity_taxonomy "
                "ORDER BY relation_type, child, parent"
            ).fetchall()
        else:
            if relation_type not in RELATION_TYPES:
                raise ValueError(
                    f"relation_type {relation_type!r} not in "
                    f"{RELATION_TYPES}"
                )
            rows = self._store._conn.execute(
                "SELECT id, child, parent, relation_type, label, "
                "reason, affirmed_count, contradicted_count, "
                "created_at, last_consulted_at "
                "FROM entity_taxonomy "
                "WHERE relation_type = ? "
                "ORDER BY child, parent",
                (relation_type,),
            ).fetchall()
        return [_row_to_dataclass(r) for r in rows]

    def record(
        self,
        child: str,
        parent: str,
        relation_type: str,
        label: str,
        reason: Optional[str],
    ) -> EntityTaxonomyRow:
        """Direct UPSERT — bypasses the LLM. For tests and calibration
        warmup. Counts are PRESERVED across overwrites; only label,
        reason, and last_consulted_at update on conflict.

        Validates label and relation_type at the Python layer so
        callers get a clearer error than the SQL CHECK constraint.
        """
        if label not in LABELS:
            raise ValueError(f"label {label!r} not in {LABELS}")
        c, p, rt = _normalize_inputs(child, parent, relation_type)
        return self._record_normalized(c, p, rt, label, reason)

    # ---- internals ---------------------------------------------------------

    def _lookup_normalized(
        self, child: str, parent: str, relation_type: str,
    ) -> Optional[EntityTaxonomyRow]:
        row = self._store._conn.execute(
            "SELECT id, child, parent, relation_type, label, reason, "
            "affirmed_count, contradicted_count, created_at, "
            "last_consulted_at "
            "FROM entity_taxonomy "
            "WHERE child = ? AND parent = ? AND relation_type = ?",
            (child, parent, relation_type),
        ).fetchone()
        if row is None:
            return None
        return _row_to_dataclass(row)

    def _serve_hit(
        self,
        row: EntityTaxonomyRow,
        source_turn_id: Optional[int],
    ) -> EntityTaxonomyVerdict:
        """Bump last_consulted_at, emit hit events, return verdict.

        last_consulted_at IS updated (observability metadata, not a
        reinforcement signal). affirmed_count and contradicted_count
        are NOT touched — principle 3.
        """
        self._touch_consulted(
            row.child, row.parent, row.relation_type,
        )
        _safe_emit_event(
            self._store, source_turn_id,
            "entity_taxonomy_hit",
            {
                "id": row.id,
                "child": row.child,
                "parent": row.parent,
                "relation_type": row.relation_type,
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
                "child": row.child,
                "parent": row.parent,
                "relation_type": row.relation_type,
                "label": row.label,
                "row_id": row.id,
            },
        )
        return EntityTaxonomyVerdict(
            label=row.label,
            reason=row.reason,
            row_id=row.id,
            served_from_cache=True,
            confidence=row.confidence(),
            classification_failed=False,
        )

    def _record_normalized(
        self,
        child: str,
        parent: str,
        relation_type: str,
        label: str,
        reason: Optional[str],
    ) -> EntityTaxonomyRow:
        """UPSERT a row at (child, parent, relation_type). Counts
        preserved on conflict; only label, reason, and
        last_consulted_at update.
        """
        now = _now_iso()
        self._store._conn.execute(
            """
            INSERT INTO entity_taxonomy (
                child, parent, relation_type, label, reason,
                affirmed_count, contradicted_count,
                created_at, last_consulted_at
            ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)
            ON CONFLICT (child, parent, relation_type) DO UPDATE SET
                label = excluded.label,
                reason = excluded.reason,
                last_consulted_at = excluded.last_consulted_at
            """,
            (child, parent, relation_type, label, reason, now, now),
        )
        self._store._conn.commit()
        row = self._lookup_normalized(child, parent, relation_type)
        assert row is not None  # we just wrote it
        return row

    def _touch_consulted(
        self, child: str, parent: str, relation_type: str,
    ) -> None:
        """Bump last_consulted_at; observability metadata only. NOT
        a reinforcement signal."""
        self._store._conn.execute(
            "UPDATE entity_taxonomy SET last_consulted_at = ? "
            "WHERE child = ? AND parent = ? AND relation_type = ?",
            (_now_iso(), child, parent, relation_type),
        )
        self._store._conn.commit()

    def _classify_via_llm(
        self, child: str, parent: str, relation_type: str,
        llm: LLMClient,
    ) -> tuple[str, str]:
        """Call the LLM, parse the tool response, validate fields.

        Raises ``_ClassificationFailed`` on any malformed output —
        unknown label, missing reason, or an SDK-level failure that
        yields a non-dict response.
        """
        try:
            raw = llm.extract_with_tool(
                system=_CLASSIFIER_SYSTEM,
                user_message=_build_user_message(
                    child, parent, relation_type,
                ),
                tool=_CLASSIFY_TOOL,
                purpose="entity_taxonomy",
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


def _row_to_dataclass(row: Any) -> EntityTaxonomyRow:
    return EntityTaxonomyRow(
        id=int(row["id"]),
        child=row["child"],
        parent=row["parent"],
        relation_type=row["relation_type"],
        label=row["label"],
        reason=row["reason"],
        affirmed_count=int(row["affirmed_count"] or 0),
        contradicted_count=int(row["contradicted_count"] or 0),
        created_at=row["created_at"],
        last_consulted_at=row["last_consulted_at"],
    )
