"""Layer 3 substrate oracle: entity equivalence.

Decides, for a pair of entity strings (e_a, e_b), whether they refer
to the SAME entity or to DIFFERENT entities. Two labels:

  * ``same``      — different surface forms of the same entity.
                    NYC and New York City; UN and United Nations;
                    Beyoncé and Beyonce; peanut butter and PB.
  * ``different`` — distinct entities, even if related or
                    similarly-named. apple (fruit) and Apple
                    (company); Tokyo and Japan (containment, not
                    equivalence — Phase 5/7's entity_taxonomy
                    handles containment); NSA and NASA.

Pattern-independence — the architectural divergence from
predicate_equivalence
================================================================

Entities are pattern-independent. "NYC" denotes the same entity
whether it appears as a ``spatial_temporal.location``, a
``preference.object``, or anywhere else. The table has NO pattern
column; one row per unordered (entity_a, entity_b) pair classifies
the relationship across all patterns. Pattern-keying would force
re-classification of every alias for every pattern and miss the
cross-pattern equivalences Phase 7's derivation walker relies on.

Case-sensitivity — the architectural contract
=============================================

The canonical-pair helper does ``strip()`` only. It does NOT
lowercase. Case is semantic for entities: ``apple`` (the fruit) and
``Apple`` (the company) are distinct entities. Lowercasing would
fold them into one canonical pair and force the oracle to learn the
disambiguation as a pseudo-self-pair, which the SQL CHECK rejects.

This diverges from predicate_equivalence, which DOES lowercase —
predicates are case-insensitive in practice (``Likes`` and ``likes``
are the same predicate from the extractor's perspective; case is
presentational). The divergence is documented in classifier_base.py.

Symmetry — the canonical-helper trick generalizes
=================================================

The (e_a, e_b) → label relation is symmetric: ``(NYC, New York City)
→ same`` reads the same forwards and backwards. The canonical-pair
helper hides ordering from callers exactly as predicate_equivalence's
does: callers pass ``(entity_query, entity_stored)`` in their own
frame and the oracle handles the lex-swap internally.

(Phase 5's predicate_distribution will NOT be symmetric — direction
matters for distribution. See classifier_base.py for the warning.)

Cost characteristics
===================

Tier U's Phase 4 alias-identity broadening calls
``EntityEquivalence.consult`` once per non-literal slot value pair
per candidate. On a cold-start tier_u lookup with N candidates
under (pattern), up to N × identity_slots LLM calls fire across
this oracle in the worst case. Warm-cache cost approaches zero
(SQL hits, no LLM). Phase 6's session-locality scoping reduces N
for typical queries; until then, a user with 100 facts under
``preference`` could see 100+ entity_equivalence calls on a single
first-time lookup. This is the bounded-by-memoization trade the
architecture commits to: oracle row writes are independent of
caller success — the same (e_a, e_b) pair is classified at most
once per pair, regardless of which lookup triggered the
classification.

Pipeline events
===============

  * ``entity_equivalence_hit``                  — SQL cache hit, no
                                                  LLM call.
  * ``entity_equivalence_write``                — LLM ran, row was
                                                  UPSERTed.
  * ``entity_equivalence_classification_failed``
                                                — LLM returned a
                                                  malformed tool
                                                  response. No row
                                                  written. Verdict
                                                  carries
                                                  ``classification_
                                                  failed=True``.
  * ``oracle_consulted``                        — emitted on every
                                                  call with
                                                  ``oracle="entity_
                                                  equivalence"``.
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


# Public label set. Mirrors the SQL CHECK constraint in the
# ``entity_equivalence`` table.
LABELS: tuple[str, ...] = ("same", "different")


@dataclass(frozen=True)
class EntityEquivalenceRow:
    """A row from the ``entity_equivalence`` table.

    ``entity_a < entity_b`` always (case-sensitive lex), enforced by
    the SQL CHECK.
    """

    entity_a: str
    entity_b: str
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
            "entity_a": self.entity_a,
            "entity_b": self.entity_b,
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
class EntityEquivalenceVerdict:
    """The result a caller receives from ``consult``.

    Symmetric: the verdict is unchanged regardless of whether the
    caller passed (e_a, e_b) or (e_b, e_a). The canonical swap is
    hidden inside ``consult``.

    ``classification_failed`` is True only when the LLM produced
    malformed tool output. ``label`` is None and ``row_id`` is None
    in that case — no row was written. The caller (Tier U) treats
    this as no-signal and continues without crashing.
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


def _canonical_pair(a: str, b: str) -> tuple[str, str, bool]:
    """Return ``(lex_smaller, lex_larger, was_swapped)``.

    **Architectural contract:** ``strip()`` only. NO lowercase.
    Case carries entity-disambiguation signal — ``apple`` (fruit)
    and ``Apple`` (company) are distinct entities. Lowercasing
    would fold them into a self-pair the SQL CHECK rejects, and
    even if it didn't, would force the oracle to handle the
    disambiguation as a pseudo-self-pair which is the wrong
    primitive.

    Diverges from predicate_equivalence's ``_canonical_pair`` which
    DOES lowercase. The divergence is documented in
    classifier_base.py.

    Raises ValueError if either string is empty after strip, or if
    the two strings are equal post-strip (self-pair). The SQL CHECK
    rejects self-pairs at the storage layer; the Python check gives
    a clearer error.
    """
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        raise ValueError(
            f"both entity strings must be non-empty after strip; "
            f"got ({a!r}, {b!r})"
        )
    if a == b:
        raise ValueError(
            f"entity_equivalence does not classify self-pairs; "
            f"got {a!r} for both arguments"
        )
    if a < b:
        return a, b, False
    return b, a, True


# ----- LLM tool definition --------------------------------------------------

_CLASSIFY_TOOL = {
    "name": "record_entity_equivalence",
    "description": (
        "Record whether two entity strings refer to the same entity "
        "or to different entities. Pick the label whose definition "
        "fits and write a one-sentence reason."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "label": {
                "type": "string",
                "enum": list(LABELS),
                "description": (
                    "same: different surface forms of the same "
                    "entity (NYC and New York City; UN and United "
                    "Nations; Beyoncé and Beyonce). different: "
                    "distinct entities, even if related, similar in "
                    "spelling, or in a containment relationship "
                    "(apple the fruit vs. Apple the company; Tokyo "
                    "vs. Japan; NSA vs. NASA). When in doubt, prefer "
                    "different."
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


_CLASSIFIER_SYSTEM = """You decide whether two entity strings refer to the SAME entity or to DIFFERENT entities.

You return ONE of two labels.

# Labels

**same** — different surface forms of the SAME entity. The strings denote one and the same thing in the world; storing X under entity_a and Y under entity_b describes the same entity. Aliases (NYC / New York City), abbreviations (UN / United Nations), alternate spellings (Beyoncé / Beyonce, color / colour), translingual variants (Mumbai / Bombay, Beijing / Peking) all count as same.

**different** — distinct entities, even if related, similar-sounding, or in a containment relationship. Cases that LOOK same but aren't:

  - **Case disambiguation.** apple (the fruit) vs. Apple (the company). turkey (the bird) vs. Turkey (the country). bass (the fish) vs. Bass (the beer). Case carries entity-disambiguation signal; respect it.

  - **Person vs. place.** Paris (the city) vs. "Paris Hilton" (the person). Madison (the city) vs. "James Madison" (the president). Same string, different referent.

  - **Containment is not equivalence.** Tokyo and Japan are different entities even though Tokyo is part of Japan. Manhattan and NYC are different entities even though Manhattan is part of NYC. The part_of relation is what Phase 5+ entity_taxonomy stores; this oracle is for equivalence ONLY.

  - **Parent/subsidiary.** Microsoft and Bing are different entities even though Microsoft owns Bing. Alphabet and Google. Meta and Instagram. Different corporate entities.

  - **Product vs. company.** Apple and iPhone are different entities. Google and Gmail. Tesla and Model S.

  - **Initialism collisions.** NSA (National Security Agency) and NASA (National Aeronautics and Space Administration) — visually similar abbreviations, completely different organizations.

# Conservative bias

When uncertain, prefer **different**. Wrong-same calls admit FALSE EQUIVALENCES into the system's verified store: the oracle's verdict propagates through Tier U's alias-resolution path and lets one entity's facts answer queries about a different entity. That's contamination. Wrong-different calls just cost a cache miss — the system falls through to fresh verification and the user re-states the alias if needed. The asymmetry favors different on uncertainty.

# Worked examples

The order is deliberate. Edge cases come first — the case-disambiguation case (apple vs Apple) is the canonical failure mode this oracle exists to prevent.

## Edge: case carries disambiguation

(Apple, apple) → label: different, reason: "Capitalized 'Apple' is the company; lowercase 'apple' is the fruit. Case is semantic for entity disambiguation."

(Mercury, mercury) → label: different, reason: "Capitalized 'Mercury' is the planet (or the Roman god, or the rock band); lowercase 'mercury' is the chemical element."

(Turkey, turkey) → label: different, reason: "Capitalized 'Turkey' is the country; lowercase 'turkey' is the bird."

## Edge: containment is not equivalence

(Japan, Tokyo) → label: different, reason: "Tokyo is contained within Japan but is not the same entity. The part-of relation is captured by entity_taxonomy, not entity_equivalence."

(NYC, Manhattan) → label: different, reason: "Manhattan is a borough of NYC but is not the same entity."

(Alphabet, Google) → label: different, reason: "Google is a subsidiary of Alphabet; they are distinct corporate entities."

## Edge: initialism collision

(NASA, NSA) → label: different, reason: "Different organizations whose initialisms differ by one letter; National Aeronautics and Space Administration vs. National Security Agency."

## Same: aliases

(NYC, New York City) → label: same, reason: "Common abbreviation; both surface forms refer to the same city."

(JFK, John F. Kennedy) → label: same, reason: "Initialism for the same person."

(PB, peanut butter) → label: same, reason: "Common abbreviation for the same food."

## Same: abbreviations

(MIT, Massachusetts Institute of Technology) → label: same, reason: "Initialism; same university."

(UN, United Nations) → label: same, reason: "Initialism; same international organization."

## Same: alternate spelling

(Beyoncé, Beyonce) → label: same, reason: "Same person; the diacritic is presentational."

(color, colour) → label: same, reason: "Spelling variant of the same word; American vs. British English."

(Mumbai, Bombay) → label: same, reason: "Translingual variants for the same Indian city; Mumbai is the contemporary name."

# Output

Always call the `record_entity_equivalence` tool exactly once with `label` and `reason`."""


def _build_user_message(e_a: str, e_b: str) -> str:
    """Render the per-pair user message. The canonical-ordered pair
    is presented to the LLM so the prompt is deterministic — the
    oracle never sees un-canonicalized argument order.
    """
    return (
        "Classify this entity pair.\n\n"
        f"  entity_a: {e_a!r}\n"
        f"  entity_b: {e_b!r}\n\n"
        "Decide whether the two strings refer to the SAME entity or "
        "to DIFFERENT entities, and call record_entity_equivalence."
    )


# ----- the oracle ----------------------------------------------------------


class EntityEquivalence:
    """Memoized LLM classifier for entity-pair equivalence.

    One instance per FactStore. The constructor takes only the store;
    an LLM is supplied per ``consult`` call.
    """

    ORACLE_NAME = "entity_equivalence"

    def __init__(self, store: FactStore):
        self._store = store

    # ---- public API --------------------------------------------------------

    def consult(
        self,
        entity_query: str,
        entity_stored: str,
        *,
        llm: Optional[LLMClient] = None,
        source_turn_id: Optional[int] = None,
    ) -> EntityEquivalenceVerdict:
        """Classify the (entity_query, entity_stored) pair.

        Lookup-then-write-through. On SQL hit, returns the cached
        verdict (no LLM call) and bumps last_consulted_at. On miss,
        calls the LLM, UPSERTs the row with counts at (0, 0), and
        returns the fresh verdict. On malformed LLM output, emits
        ``entity_equivalence_classification_failed`` and returns a
        verdict with ``classification_failed=True`` and ``label=
        None`` — the caller treats this as no-signal.

        The verdict is symmetric: passing (a, b) yields the same
        verdict as passing (b, a). The canonical swap is hidden.
        """
        e_a, e_b, _swapped = _canonical_pair(entity_query, entity_stored)
        existing = self._lookup_canonical(e_a, e_b)
        if existing is not None:
            return self._serve_hit(existing, source_turn_id)

        if llm is None:
            raise RuntimeError(
                f"entity_equivalence consult miss for "
                f"({entity_query!r}, {entity_stored!r}) with no LLM "
                f"provided; supply llm= or warm the cache via "
                f"record() first"
            )

        try:
            label, reason = self._classify_via_llm(e_a, e_b, llm)
        except _ClassificationFailed as failure:
            _safe_emit_event(
                self._store, source_turn_id,
                "entity_equivalence_classification_failed",
                {
                    "entity_a": e_a,
                    "entity_b": e_b,
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
                    "entity_a": e_a,
                    "entity_b": e_b,
                },
            )
            return EntityEquivalenceVerdict(
                label=None,
                reason=failure.reason,
                row_id=None,
                served_from_cache=False,
                confidence=confidence_from_counts(0, 0),
                classification_failed=True,
            )

        row = self._record_canonical(e_a, e_b, label, reason)
        _safe_emit_event(
            self._store, source_turn_id,
            "entity_equivalence_write",
            {
                "id": row.id,
                "entity_a": e_a,
                "entity_b": e_b,
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
                "entity_a": e_a,
                "entity_b": e_b,
                "label": label,
                "row_id": row.id,
            },
        )
        return EntityEquivalenceVerdict(
            label=row.label,
            reason=row.reason,
            row_id=row.id,
            served_from_cache=False,
            confidence=row.confidence(),
            classification_failed=False,
        )

    def lookup(
        self, entity_a: str, entity_b: str,
    ) -> Optional[EntityEquivalenceRow]:
        """Pure read. Tries the canonical pair ordering — callers
        don't need to lex-sort their arguments. Returns None on miss.

        Raises ValueError on a self-pair (entity_a equals entity_b
        after strip).
        """
        e_a, e_b, _ = _canonical_pair(entity_a, entity_b)
        return self._lookup_canonical(e_a, e_b)

    def list_rows(self) -> list[EntityEquivalenceRow]:
        """List rows, ordered by (entity_a, entity_b). Used by the
        inspector endpoint at /v2/api/substrate/entity-equivalence.
        """
        rows = self._store._conn.execute(
            "SELECT id, entity_a, entity_b, label, reason, "
            "affirmed_count, contradicted_count, created_at, "
            "last_consulted_at "
            "FROM entity_equivalence "
            "ORDER BY entity_a, entity_b"
        ).fetchall()
        return [_row_to_dataclass(r) for r in rows]

    def record(
        self,
        entity_a: str,
        entity_b: str,
        label: str,
        reason: Optional[str],
    ) -> EntityEquivalenceRow:
        """Direct UPSERT — bypasses the LLM. For tests and calibration
        warmup. Counts are PRESERVED across overwrites; only label,
        reason, and last_consulted_at update on conflict. The
        canonical helper handles either argument order.
        """
        if label not in LABELS:
            raise ValueError(f"label {label!r} not in {LABELS}")
        e_a, e_b, _ = _canonical_pair(entity_a, entity_b)
        return self._record_canonical(e_a, e_b, label, reason)

    # ---- internals ---------------------------------------------------------

    def _lookup_canonical(
        self, e_a: str, e_b: str,
    ) -> Optional[EntityEquivalenceRow]:
        row = self._store._conn.execute(
            "SELECT id, entity_a, entity_b, label, reason, "
            "affirmed_count, contradicted_count, created_at, "
            "last_consulted_at "
            "FROM entity_equivalence "
            "WHERE entity_a = ? AND entity_b = ?",
            (e_a, e_b),
        ).fetchone()
        if row is None:
            return None
        return _row_to_dataclass(row)

    def _serve_hit(
        self,
        row: EntityEquivalenceRow,
        source_turn_id: Optional[int],
    ) -> EntityEquivalenceVerdict:
        """Bump last_consulted_at, emit hit events, return verdict.

        last_consulted_at IS updated (observability metadata, not a
        reinforcement signal). affirmed_count and contradicted_count
        are NOT touched — principle 3.
        """
        self._touch_consulted(row.entity_a, row.entity_b)
        _safe_emit_event(
            self._store, source_turn_id,
            "entity_equivalence_hit",
            {
                "id": row.id,
                "entity_a": row.entity_a,
                "entity_b": row.entity_b,
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
                "entity_a": row.entity_a,
                "entity_b": row.entity_b,
                "label": row.label,
                "row_id": row.id,
            },
        )
        return EntityEquivalenceVerdict(
            label=row.label,
            reason=row.reason,
            row_id=row.id,
            served_from_cache=True,
            confidence=row.confidence(),
            classification_failed=False,
        )

    def _record_canonical(
        self,
        e_a: str,
        e_b: str,
        label: str,
        reason: Optional[str],
    ) -> EntityEquivalenceRow:
        """UPSERT a row at canonical pair ordering. Counts preserved
        on conflict.
        """
        now = _now_iso()
        self._store._conn.execute(
            """
            INSERT INTO entity_equivalence (
                entity_a, entity_b, label, reason,
                affirmed_count, contradicted_count,
                created_at, last_consulted_at
            ) VALUES (?, ?, ?, ?, 0, 0, ?, ?)
            ON CONFLICT (entity_a, entity_b) DO UPDATE SET
                label = excluded.label,
                reason = excluded.reason,
                last_consulted_at = excluded.last_consulted_at
            """,
            (e_a, e_b, label, reason, now, now),
        )
        self._store._conn.commit()
        row = self._lookup_canonical(e_a, e_b)
        assert row is not None  # we just wrote it
        return row

    def _touch_consulted(self, e_a: str, e_b: str) -> None:
        """Bump last_consulted_at; observability metadata only. Not
        a reinforcement signal."""
        self._store._conn.execute(
            "UPDATE entity_equivalence SET last_consulted_at = ? "
            "WHERE entity_a = ? AND entity_b = ?",
            (_now_iso(), e_a, e_b),
        )
        self._store._conn.commit()

    def _classify_via_llm(
        self, e_a: str, e_b: str, llm: LLMClient,
    ) -> tuple[str, str]:
        """Call the LLM, parse the tool response, validate fields.

        Raises ``_ClassificationFailed`` on any malformed output —
        unknown label, missing reason, or an SDK-level failure that
        yields a non-dict response. The caller catches and returns
        a verdict with ``classification_failed=True``.
        """
        try:
            raw = llm.extract_with_tool(
                system=_CLASSIFIER_SYSTEM,
                user_message=_build_user_message(e_a, e_b),
                tool=_CLASSIFY_TOOL,
                purpose="entity_equivalence",
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


def _row_to_dataclass(row: Any) -> EntityEquivalenceRow:
    return EntityEquivalenceRow(
        id=int(row["id"]),
        entity_a=row["entity_a"],
        entity_b=row["entity_b"],
        label=row["label"],
        reason=row["reason"],
        affirmed_count=int(row["affirmed_count"] or 0),
        contradicted_count=int(row["contradicted_count"] or 0),
        created_at=row["created_at"],
        last_consulted_at=row["last_consulted_at"],
    )
