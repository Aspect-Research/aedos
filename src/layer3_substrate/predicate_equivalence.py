"""Layer 3 substrate oracle: predicate equivalence within a pattern.

Decides, for a pair of predicates (p_a, p_b) under the same pattern,
which of three labels applies:

  * ``equivalent``    — same proposition under the slot mapping in
                        ``slot_reversal``. (``wrote`` and
                        ``authored_by`` with subject/object swap.)
  * ``contradictory`` — opposite polarity of the same proposition.
                        Storing X with predicate=p_a, polarity=1 and
                        Y with predicate=p_b, polarity=0 describes
                        the same fact. (``likes`` and ``dislikes``.)
  * ``distinct``      — different propositions, even if related.
                        (``likes`` and ``loves`` — same direction,
                        different intensity.)

The oracle is consulted by Tier U after a literal-match miss. When
Tier U has a model claim with predicate=p_query and is comparing it
against a stored fact with predicate=p_stored (same pattern, same
identity slots), Tier U calls ``oracle.consult(pattern, p_query,
p_stored)``. The oracle handles canonical-pair ordering internally and
returns a verdict in the caller's frame: ``slot_reversal`` describes
the transformation Tier U should apply to the QUERY claim's slots,
not the stored fact's, regardless of which predicate happened to be
lex-smaller in the canonical pair.

Why canonical ordering at all: storing one row per unordered pair
keeps the oracle's working set small and the SQL CHECK constraint
``predicate_a < predicate_b`` enforces it as an invariant. Self-pairs
(``(p, p)``) are rejected by the same CHECK; the oracle is never
consulted for literal-match cases by construction.

**Symmetry note (Phase 5 generalization wart):** predicate_equivalence
is *symmetric* in label and slot_reversal under the canonical swap.
``(likes, dislikes) -> contradictory + 'none'`` reads the same
forwards and backwards. ``(defeated, defeated_by) -> equivalent +
'subject_object_swap'`` likewise. ``predicate_distribution`` (Phase 5)
is NOT symmetric — distribution is directional. See
``classifier_base.py`` for the full warning.

Counts (``affirmed_count`` / ``contradicted_count``) are independent-
external-evidence only per principle 3. ``consult`` never increments
them on hit; only the operator-action endpoint (Phase 8) and
contradiction propagation (Phase 7+) touch the counts. Confidence
flows from ``confidence_from_counts``.

Pipeline events emitted from ``consult``:

  * ``predicate_equivalence_hit``               — SQL cache hit, no
                                                  LLM call.
  * ``predicate_equivalence_write``             — LLM ran, row was
                                                  UPSERTed.
  * ``predicate_equivalence_classification_failed``
                                                — LLM returned a
                                                  malformed tool
                                                  response. No row
                                                  written. Verdict
                                                  carries
                                                  ``classification_
                                                  failed=True``.
  * ``oracle_consulted``                        — emitted on every
                                                  call (hit or write
                                                  or fail) with
                                                  ``oracle="predicate_
                                                  equivalence"`` so
                                                  the trace UI can
                                                  grep one stage for
                                                  all substrate
                                                  consultations.
"""

from __future__ import annotations

import json
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


# Public label sets. Mirror the SQL CHECK constraints in the
# ``predicate_equivalence`` table; kept here as Python tuples so
# consumers can validate without round-tripping through the DB.
LABELS: tuple[str, ...] = ("equivalent", "contradictory", "distinct")
SLOT_REVERSALS: tuple[str, ...] = (
    "none",
    "subject_object_swap",
    "participant_reorder",
)


@dataclass(frozen=True)
class PredicateEquivalenceRow:
    """A row from the ``predicate_equivalence`` table.

    Snapshot at read time. ``predicate_a < predicate_b`` always
    (enforced by the SQL CHECK).
    """

    pattern: str
    predicate_a: str
    predicate_b: str
    label: str
    slot_reversal: str
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
            "predicate_a": self.predicate_a,
            "predicate_b": self.predicate_b,
            "label": self.label,
            "slot_reversal": self.slot_reversal,
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
class PredicateEquivalenceVerdict:
    """The result a caller receives from ``consult``.

    ``slot_reversal`` is reported in the CALLER'S FRAME — i.e. the
    transformation the caller should apply to the query claim's
    slots when comparing against the stored fact. For
    ``predicate_equivalence`` this is the same as the canonical-pair
    slot_reversal (the relationship is symmetric); see the module
    docstring.

    ``classification_failed`` is True only when the LLM produced
    malformed tool output. In that case ``label`` is None and
    ``row_id`` is None — no row was written. The caller (Tier U)
    treats this as a MISS and falls through to fresh verification
    rather than crashing the turn.
    """

    label: Optional[str]
    slot_reversal: str
    reason: Optional[str]
    row_id: Optional[int]
    served_from_cache: bool
    confidence: float
    classification_failed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "slot_reversal": self.slot_reversal,
            "reason": self.reason,
            "row_id": self.row_id,
            "served_from_cache": self.served_from_cache,
            "confidence": self.confidence,
            "classification_failed": self.classification_failed,
        }


def _canonical_pair(p_a: str, p_b: str) -> tuple[str, str, bool]:
    """Return ``(lex_smaller, lex_larger, was_swapped)``.

    **Architectural contract:** normalization is ``strip().lower()``
    only. No regex stem stripping (``is_likes`` -> ``likes``), no
    prefix removal, no Unicode collation beyond lowercase. Stem
    stripping was a v1 cache workaround for not having the oracle;
    with the oracle in play, ``is_likes`` and ``likes`` are two
    different predicates that the oracle resolves as equivalent (or
    not) on its own. Encoding morphology as a normalization rule
    short-circuits that learning.

    Case is presentational, not semantic — extractor calls vary in
    capitalization without intending different predicates, so
    case-folding is signal-preserving. Stem prefixes are different;
    the extractor is held to producing distinct labels for
    semantically distinct subtypes (CLAUDE.md), and stripping a stem
    can fuse what the extractor decided to keep distinct.

    Raises ValueError if the two predicates are equal post-
    normalization. Self-pairs are not part of the oracle's domain
    (literal match handles them) and the SQL CHECK rejects them.
    """
    a = (p_a or "").strip().lower()
    b = (p_b or "").strip().lower()
    if not a or not b:
        raise ValueError(
            f"both predicates must be non-empty after strip+lower; "
            f"got ({p_a!r}, {p_b!r}) -> ({a!r}, {b!r})"
        )
    if a == b:
        raise ValueError(
            f"predicate_equivalence does not classify self-pairs; "
            f"got {a!r} for both arguments"
        )
    if a < b:
        return a, b, False
    return b, a, True


# ----- LLM tool definition --------------------------------------------------

_CLASSIFY_TOOL = {
    "name": "record_predicate_equivalence",
    "description": (
        "Record the semantic relationship between a pair of "
        "predicates that share a pattern. Pick the label whose "
        "definition fits, choose the slot transformation needed for "
        "the predicates to denote the same fact, and write a one-"
        "sentence reason."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "label": {
                "type": "string",
                "enum": list(LABELS),
                "description": (
                    "equivalent: same proposition under the slot "
                    "mapping in slot_reversal. contradictory: "
                    "opposite polarity of the same proposition (so "
                    "X(p_a, polarity=1) and Y(p_b, polarity=0) "
                    "describe the same fact). distinct: different "
                    "propositions, even if related — when in doubt, "
                    "prefer distinct."
                ),
            },
            "slot_reversal": {
                "type": "string",
                "enum": list(SLOT_REVERSALS),
                "description": (
                    "none: identity slot mapping (use this for the "
                    "preference, propositional_attitude, and "
                    "categorical patterns whose argument structure "
                    "doesn't invert; also for symmetric relations "
                    "where active/passive is a no-op). "
                    "subject_object_swap: active/passive inversion "
                    "of a relational predicate — the subject and "
                    "object slots trade roles (defeated/defeated_by, "
                    "wrote/authored_by). participant_reorder: event-"
                    "pattern participants reorder under the swap. "
                    "Use 'none' when the label is contradictory or "
                    "distinct."
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
        "required": ["label", "slot_reversal", "reason"],
    },
}


_CLASSIFIER_SYSTEM = """You decide how a pair of predicates within the same pattern relate to each other.

You return ONE of three labels and ONE slot_reversal value.

# Labels

**equivalent** — the two predicates name the same proposition under the slot mapping in `slot_reversal`. Storing X with predicate=p_a and Y with predicate=p_b describes the same fact. Active/passive of the same relation (`wrote` / `authored_by`), surface paraphrases (`born_in` / `was_born_in`), and symmetric-relation surface variants (`married_to` / `spouse_of`) are equivalent.

**contradictory** — the two predicates name OPPOSITE polarities of the same proposition. Storing X with predicate=p_a, polarity=1 and Y with predicate=p_b, polarity=0 describes the same fact. The classic case is antonyms used as predicate labels: `likes` / `dislikes`, `loves` / `hates`, `agrees_with` / `disagrees_with`. The system will flip the polarity at lookup time so the two propositions match.

**distinct** — the two predicates name DIFFERENT propositions, even if related. Same direction with different intensity (`likes` / `loves`), different temporal phase (`born_in` / `lived_in`), different commitment level (`believes` / `knows`), different specificity (`is_a` / `instance_of`). When in doubt, prefer distinct — wrong-equivalent calls let contradictions enter the store, while wrong-distinct calls just cost a cache miss.

# slot_reversal

The slot transformation that makes the two predicates denote the same fact:

- **none** — identity. The slot values map across without change. Use this for: any preference / propositional_attitude / categorical pair (their argument structure doesn't invert); contradictory pairs (the polarity flip is the only transformation); distinct pairs (no transformation makes them match); symmetric relations whose argument order is incidental (`married_to` / `spouse_of`).

- **subject_object_swap** — relational active/passive inversion. The subject slot of one predicate becomes the object of the other and vice versa. `wrote(Asa, paper)` ≡ `authored_by(paper, Asa)` under this swap. Use this only for relational pairs where the swap is genuinely needed.

- **participant_reorder** — event pattern: participants list order matters and the pair re-orders it. Rarely used; reserve it for event predicates where ordering is semantically meaningful.

# Worked examples

The order of these examples is deliberate. Edge cases come first — the cheetahs case (contradictory + polarity flip) is the canonical failure mode this oracle exists to fix.

## Edge: contradictory + polarity-flip semantics

(preference, dislikes, likes) → label: contradictory, slot_reversal: none, reason: "Antonym predicates over the same agent/object — storing X(dislikes, polarity=1) and Y(likes, polarity=0) describe the same fact about the user's attitude toward the object."

(preference, hates, loves) → label: contradictory, slot_reversal: none, reason: "Stark antonym predicates; same proposition under polarity flip."

(role_assignment, currently_holds, no_longer_holds) → label: contradictory, slot_reversal: none, reason: "Polar opposites of role-occupancy; the polarity flip makes them equivalent."

## Edge: equivalent + slot_reversal (active/passive)

(relational, defeated, defeated_by) → label: equivalent, slot_reversal: subject_object_swap, reason: "Active/passive of the same relation; subject and object swap roles."

(relational, authored_by, wrote) → label: equivalent, slot_reversal: subject_object_swap, reason: "Same authorship relation under active/passive inversion."

(relational, parent_of, child_of) → label: equivalent, slot_reversal: subject_object_swap, reason: "Same parent-child relation, argument roles swap."

## Edge: tempting over-merge → distinct

(preference, likes, loves) → label: distinct, slot_reversal: none, reason: "Same direction of attitude but different intensity — conflating them loses information the user gave."

(propositional_attitude, knows, believes) → label: distinct, slot_reversal: none, reason: "Different doxastic commitments; knowing implies truth, believing does not."

(spatial_temporal, born_in, lived_in) → label: distinct, slot_reversal: none, reason: "Different temporal phases of a person's relationship to a location."

## Equivalent + none (surface variations)

(spatial_temporal, born_in, was_born_in) → label: equivalent, slot_reversal: none, reason: "Surface tense variation; same proposition."

(role_assignment, holds_role, serves_in_role) → label: equivalent, slot_reversal: none, reason: "Surface paraphrase of role occupancy; identical argument structure."

(quantitative, has_count, count) → label: equivalent, slot_reversal: none, reason: "Same counting predicate; the prefix is incidental."

## Equivalent + none (symmetric relations — slot swap is a no-op)

(relational, married_to, spouse_of) → label: equivalent, slot_reversal: none, reason: "Symmetric relation; argument order is incidental, so swap and identity are the same mapping."

## Distinct (same domain, different proposition)

(spatial_temporal, lives_in, visited) → label: distinct, slot_reversal: none, reason: "Living somewhere and having visited it are different propositions about the same entity-location pair."

(categorical, is_a, instance_of) → label: distinct, slot_reversal: none, reason: "Class membership versus type instantiation — overlapping but logically distinct."

# Conservative bias

When uncertain, prefer **distinct**. The system recovers from a wrong-distinct call by falling through to fresh verification (a cache miss is cheap). It does NOT recover from a wrong-equivalent call: an equivalent verdict admits the stored fact's polarity flip into the model claim's verdict, so a wrong equivalent or contradictory claim contaminates the verified store. Bias toward distinct.

# Output

Always call the `record_predicate_equivalence` tool exactly once with `label`, `slot_reversal`, and `reason`."""


def _build_user_message(pattern: str, p_a: str, p_b: str) -> str:
    """Render the per-pair user message. The classifier is given the
    canonical-ordered pair so the prompt is deterministic — the oracle
    never sees the un-canonicalized argument order.
    """
    return (
        "Classify this predicate pair.\n\n"
        f"  pattern: {pattern!r}\n"
        f"  predicate_a: {p_a!r}\n"
        f"  predicate_b: {p_b!r}\n\n"
        "Decide which label applies, choose the slot_reversal value, "
        "and call record_predicate_equivalence."
    )


# ----- the oracle ----------------------------------------------------------


class PredicateEquivalence:
    """Memoized LLM classifier for predicate-pair equivalence.

    One instance per FactStore. The constructor takes only the store;
    an LLM is supplied per ``consult`` call so callers can pass in a
    test stub or share an LLMClient across multiple oracles.
    """

    ORACLE_NAME = "predicate_equivalence"

    def __init__(self, store: FactStore):
        self._store = store

    # ---- public API --------------------------------------------------------

    def consult(
        self,
        pattern: str,
        predicate_query: str,
        predicate_stored: str,
        *,
        llm: Optional[LLMClient] = None,
        source_turn_id: Optional[int] = None,
    ) -> PredicateEquivalenceVerdict:
        """Classify the (predicate_query, predicate_stored) pair.

        Lookup-then-write-through. On SQL hit, returns the cached
        verdict (no LLM call) and bumps last_consulted_at. On miss,
        calls the LLM, UPSERTs the row with counts at (0, 0), and
        returns the fresh verdict. On malformed LLM output, emits
        ``predicate_equivalence_classification_failed`` and returns
        a verdict with ``classification_failed=True`` and
        ``label=None`` — the caller treats this as a MISS.

        ``slot_reversal`` is returned in the caller's frame; the
        canonical-pair swap is hidden inside this method. (For
        ``predicate_equivalence`` the relationship is symmetric, so
        the canonical swap is a no-op for slot_reversal anyway, but
        the encapsulation matters for the inspector endpoint where
        operators see canonical rows directly.)
        """
        p_a, p_b, _swapped = _canonical_pair(predicate_query, predicate_stored)
        existing = self._lookup_canonical(pattern, p_a, p_b)
        if existing is not None:
            return self._serve_hit(existing, source_turn_id)

        if llm is None:
            raise RuntimeError(
                f"predicate_equivalence consult miss for "
                f"({pattern!r}, {predicate_query!r}, {predicate_stored!r}) "
                f"with no LLM provided; supply llm= or warm the cache "
                f"via record() first"
            )

        try:
            label, slot_reversal, reason = self._classify_via_llm(
                pattern, p_a, p_b, llm,
            )
        except _ClassificationFailed as failure:
            _safe_emit_event(
                self._store, source_turn_id,
                "predicate_equivalence_classification_failed",
                {
                    "pattern": pattern,
                    "predicate_a": p_a,
                    "predicate_b": p_b,
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
                    "pattern": pattern,
                    "predicate_a": p_a,
                    "predicate_b": p_b,
                },
            )
            return PredicateEquivalenceVerdict(
                label=None,
                slot_reversal="none",
                reason=failure.reason,
                row_id=None,
                served_from_cache=False,
                confidence=confidence_from_counts(0, 0),
                classification_failed=True,
            )

        row = self._record_canonical(
            pattern, p_a, p_b, label, slot_reversal, reason,
        )
        _safe_emit_event(
            self._store, source_turn_id,
            "predicate_equivalence_write",
            {
                "id": row.id,
                "pattern": pattern,
                "predicate_a": p_a,
                "predicate_b": p_b,
                "label": label,
                "slot_reversal": slot_reversal,
                "reason": reason,
            },
        )
        _safe_emit_event(
            self._store, source_turn_id,
            "oracle_consulted",
            {
                "oracle": self.ORACLE_NAME,
                "outcome": "write",
                "pattern": pattern,
                "predicate_a": p_a,
                "predicate_b": p_b,
                "label": label,
                "slot_reversal": slot_reversal,
                "row_id": row.id,
            },
        )
        return PredicateEquivalenceVerdict(
            label=row.label,
            slot_reversal=row.slot_reversal,
            reason=row.reason,
            row_id=row.id,
            served_from_cache=False,
            confidence=row.confidence(),
            classification_failed=False,
        )

    def lookup(
        self,
        pattern: str,
        predicate_a: str,
        predicate_b: str,
    ) -> Optional[PredicateEquivalenceRow]:
        """Pure read. Tries the canonical pair ordering — callers
        don't need to lex-sort their arguments. Returns None on miss.

        Raises ValueError on a self-pair (predicate_a equals
        predicate_b after normalization). The SQL layer rejects them
        too, but the Python check gives a clearer error.
        """
        p_a, p_b, _ = _canonical_pair(predicate_a, predicate_b)
        return self._lookup_canonical(pattern, p_a, p_b)

    def list_rows(
        self,
        pattern: Optional[str] = None,
    ) -> list[PredicateEquivalenceRow]:
        """List rows, optionally filtered by pattern. Used by the
        inspector endpoint at /v2/api/substrate/predicate-equivalence.
        Order is (pattern, predicate_a, predicate_b).
        """
        if pattern is None:
            rows = self._store._conn.execute(
                "SELECT id, pattern, predicate_a, predicate_b, label, "
                "slot_reversal, reason, affirmed_count, "
                "contradicted_count, created_at, last_consulted_at "
                "FROM predicate_equivalence "
                "ORDER BY pattern, predicate_a, predicate_b"
            ).fetchall()
        else:
            rows = self._store._conn.execute(
                "SELECT id, pattern, predicate_a, predicate_b, label, "
                "slot_reversal, reason, affirmed_count, "
                "contradicted_count, created_at, last_consulted_at "
                "FROM predicate_equivalence "
                "WHERE pattern = ? "
                "ORDER BY predicate_a, predicate_b",
                (pattern,),
            ).fetchall()
        return [_row_to_dataclass(r) for r in rows]

    def record(
        self,
        pattern: str,
        predicate_a: str,
        predicate_b: str,
        label: str,
        slot_reversal: str,
        reason: Optional[str],
    ) -> PredicateEquivalenceRow:
        """Direct UPSERT — bypasses the LLM. Useful for tests and for
        calibration-warmup scripts that want to seed gold pairs into
        the oracle table without paying for LLM classifications.

        Counts are PRESERVED across overwrites; only the label,
        slot_reversal, reason, and last_consulted_at fields are
        updated on conflict. The CHECK constraint enforces that
        ``predicate_a < predicate_b`` after normalization, so passing
        the pair in either order works — the helper canonicalizes.
        """
        if label not in LABELS:
            raise ValueError(f"label {label!r} not in {LABELS}")
        if slot_reversal not in SLOT_REVERSALS:
            raise ValueError(
                f"slot_reversal {slot_reversal!r} not in {SLOT_REVERSALS}"
            )
        p_a, p_b, _ = _canonical_pair(predicate_a, predicate_b)
        return self._record_canonical(
            pattern, p_a, p_b, label, slot_reversal, reason,
        )

    # ---- internals ---------------------------------------------------------

    def _lookup_canonical(
        self, pattern: str, p_a: str, p_b: str,
    ) -> Optional[PredicateEquivalenceRow]:
        row = self._store._conn.execute(
            "SELECT id, pattern, predicate_a, predicate_b, label, "
            "slot_reversal, reason, affirmed_count, "
            "contradicted_count, created_at, last_consulted_at "
            "FROM predicate_equivalence "
            "WHERE pattern = ? AND predicate_a = ? AND predicate_b = ?",
            (pattern, p_a, p_b),
        ).fetchone()
        if row is None:
            return None
        return _row_to_dataclass(row)

    def _serve_hit(
        self,
        row: PredicateEquivalenceRow,
        source_turn_id: Optional[int],
    ) -> PredicateEquivalenceVerdict:
        """Bump last_consulted_at, emit hit events, return verdict.

        last_consulted_at IS updated (observability metadata, not a
        reinforcement signal). affirmed_count and contradicted_count
        are NOT touched — principle 3.
        """
        self._touch_consulted(
            row.pattern, row.predicate_a, row.predicate_b,
        )
        _safe_emit_event(
            self._store, source_turn_id,
            "predicate_equivalence_hit",
            {
                "id": row.id,
                "pattern": row.pattern,
                "predicate_a": row.predicate_a,
                "predicate_b": row.predicate_b,
                "label": row.label,
                "slot_reversal": row.slot_reversal,
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
                "predicate_a": row.predicate_a,
                "predicate_b": row.predicate_b,
                "label": row.label,
                "slot_reversal": row.slot_reversal,
                "row_id": row.id,
            },
        )
        return PredicateEquivalenceVerdict(
            label=row.label,
            slot_reversal=row.slot_reversal,
            reason=row.reason,
            row_id=row.id,
            served_from_cache=True,
            confidence=row.confidence(),
            classification_failed=False,
        )

    def _record_canonical(
        self,
        pattern: str,
        p_a: str,
        p_b: str,
        label: str,
        slot_reversal: str,
        reason: Optional[str],
    ) -> PredicateEquivalenceRow:
        """UPSERT a row at canonical pair ordering. Counts preserved
        on conflict (only label/slot_reversal/reason/last_consulted_at
        are updated). created_at is also preserved across UPSERTs —
        a row was created when first written, repeated record() calls
        are observations of the same pair.
        """
        now = _now_iso()
        self._store._conn.execute(
            """
            INSERT INTO predicate_equivalence (
                pattern, predicate_a, predicate_b, label, slot_reversal,
                reason, affirmed_count, contradicted_count,
                created_at, last_consulted_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
            ON CONFLICT (pattern, predicate_a, predicate_b) DO UPDATE SET
                label = excluded.label,
                slot_reversal = excluded.slot_reversal,
                reason = excluded.reason,
                last_consulted_at = excluded.last_consulted_at
            """,
            (pattern, p_a, p_b, label, slot_reversal, reason, now, now),
        )
        self._store._conn.commit()
        row = self._lookup_canonical(pattern, p_a, p_b)
        assert row is not None  # we just wrote it
        return row

    def _touch_consulted(
        self, pattern: str, p_a: str, p_b: str,
    ) -> None:
        """Bump last_consulted_at to now. Same discipline as
        RoutingMemo.touch_consulted: NOT a reinforcement signal.
        """
        self._store._conn.execute(
            "UPDATE predicate_equivalence SET last_consulted_at = ? "
            "WHERE pattern = ? AND predicate_a = ? AND predicate_b = ?",
            (_now_iso(), pattern, p_a, p_b),
        )
        self._store._conn.commit()

    def _classify_via_llm(
        self, pattern: str, p_a: str, p_b: str, llm: LLMClient,
    ) -> tuple[str, str, str]:
        """Call the LLM, parse the tool response, validate fields.

        Raises ``_ClassificationFailed`` on any malformed output —
        unknown label, unknown slot_reversal, missing reason, or an
        SDK-level failure that yields a non-dict response. The caller
        catches and converts to a verdict-of-last-resort.
        """
        try:
            raw = llm.extract_with_tool(
                system=_CLASSIFIER_SYSTEM,
                user_message=_build_user_message(pattern, p_a, p_b),
                tool=_CLASSIFY_TOOL,
                purpose="predicate_equivalence",
            )
        except Exception as exc:
            raise _ClassificationFailed(
                reason=f"LLM call raised: {type(exc).__name__}: {exc}",
                raw={},
            )
        if not isinstance(raw, dict):
            raise _ClassificationFailed(
                reason=f"tool response was not a dict (got {type(raw).__name__})",
                raw={"received": str(raw)[:200]},
            )
        label = raw.get("label")
        if not isinstance(label, str) or label not in LABELS:
            raise _ClassificationFailed(
                reason=f"label {label!r} not in {LABELS}",
                raw=dict(raw),
            )
        slot_reversal = raw.get("slot_reversal")
        if (
            not isinstance(slot_reversal, str)
            or slot_reversal not in SLOT_REVERSALS
        ):
            raise _ClassificationFailed(
                reason=(
                    f"slot_reversal {slot_reversal!r} not in {SLOT_REVERSALS}"
                ),
                raw=dict(raw),
            )
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise _ClassificationFailed(
                reason="reason missing or empty",
                raw=dict(raw),
            )
        return label, slot_reversal, reason.strip()


def _row_to_dataclass(row: Any) -> PredicateEquivalenceRow:
    return PredicateEquivalenceRow(
        id=int(row["id"]),
        pattern=row["pattern"],
        predicate_a=row["predicate_a"],
        predicate_b=row["predicate_b"],
        label=row["label"],
        slot_reversal=row["slot_reversal"],
        reason=row["reason"],
        affirmed_count=int(row["affirmed_count"] or 0),
        contradicted_count=int(row["contradicted_count"] or 0),
        created_at=row["created_at"],
        last_consulted_at=row["last_consulted_at"],
    )
