"""Derivation walk (v0.14 Phase 7c).

Bounded BFS over (pattern, predicate, polarity, slots) states. The
walker takes a claim that direct Tier U and Tier W lookups missed and
explores the substrate to find a multi-step chain that supports it.

What the walk produces
======================

A ``DerivationResult`` (Phase 7b's types module). On MATCH, the
result carries the full chain of ``ChainEdge`` rows, the chain's
min-link reliability, and the matching fact in U or W. On MISS, the
result names the abort reason (depth bound hit, reliability floor
hit, exhausted, or classification failed mid-walk).

Architectural commitments
=========================

  * **Bounded depth.** ``MAX_DEPTH = 4`` logical steps. A logical step
    is a state transition; an entity_taxonomy + predicate_distribution
    composite (gate + state change) counts as ONE logical step but
    contributes TWO edges to the chain (both consulted, both feed
    into chain_reliability via min-link).

  * **Min-link chain reliability.** ``chain_reliability =
    min(edge.confidence for edge in chain)``. Every consulted oracle
    row contributes; one weak row drags the whole chain. Cold-start
    rows have confidence 0.5 (Beta(1,1) over zero counts) and pass
    the floor at 0.4 — they produce advisory chains.

  * **Cycle detection.** A visited-set on the state's canonical key
    (pattern, predicate-normalized, polarity, slots-canonical-string).
    Substrate-induced loops (e.g. constructed entity_equivalence
    cycles X≡Y, Y≡Z, Z≡X) terminate cleanly.

  * **Polarity tracked through chains.** ``predicate_equivalence``'s
    ``contradictory`` label flips polarity. The walker advances the
    state's polarity correctly; ``predicate_distribution`` is
    consulted on the CURRENT polarity (post-flip), not the original.

  * **Substrate may write during walk.** Cold-start oracle rows
    trigger LLM calls and UPSERT new substrate rows. This is expected
    architecturally — substrate rows are memoized classifications.
    But derived FACTS are never persisted: the walker only READS U
    and W, never writes. Tests verify via the three-snapshot gate
    (pre / mid / post) that ``facts`` and ``verification_cache`` row
    counts don't grow during a derivation MATCH.

  * **Composite et+pd step.** Walking an entity_taxonomy chain
    requires predicate_distribution to ratify the propagation
    direction (distributes_up for child→parent, distributes_down for
    parent→child). The composite step produces TWO ChainEdges:
    edge 1 is the et consultation with the substituted state; edge
    2 is the pd consultation with from_state == to_state (a gating
    edge that contributes its confidence to min-link without
    further state change).

  * **Subject_object_swap on relational pattern only.** The
    predicate_equivalence verdict ``equivalent +
    slot_reversal=subject_object_swap`` (e.g. wrote / authored_by)
    triggers a slot swap on the relational pattern. Other patterns
    don't have a meaningful subject/object pair; the walker skips
    swap-based expansion on non-relational states.

  * **participant_reorder deferred.** No Phase 7 calibration entry
    exercises this; the walker logs and skips.

The two-edge composite step in detail
=====================================

For an entity_taxonomy + predicate_distribution composite expansion:

  Step starts at state S = (P, p, pol, slots with V).
  Walker queries entity_taxonomy rows involving V.
  For each row whose label / direction yields a candidate V':
    Walker queries predicate_distribution(P, p_normalized, pol, R).
    If pd label matches the required propagation direction:
      new_state = (P, p, pol, slots with V→V')
      edges = [
        ChainEdge(oracle=entity_taxonomy, ..., to_state=new_state),
        ChainEdge(oracle=predicate_distribution, ...,
                  from_state=new_state, to_state=new_state),
      ]
      yield (new_state, edges)

The min-link reliability over a 2-step chain (et + pd + et + pd) is
min over 4 oracle rows. With the floor at 0.4, every row needs at
least 0.4 confidence. Cold-start rows at 0.5 pass.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from src.fact_store import DEFAULT_USER_ID, FactStore
from src.layer1_extraction.pattern_registry import PatternRegistry
from src.layer2_routing.constants import (
    KEY_SLOTS_BY_PATTERN,
    confidence_from_counts,
)
from src.layer3_substrate.classifier_base import _safe_emit_event
from src.layer3_substrate.entity_equivalence import (
    EntityEquivalence,
)
from src.layer3_substrate.entity_taxonomy import (
    EntityTaxonomy,
)
from src.layer3_substrate.predicate_distribution import (
    PredicateDistribution,
)
from src.layer3_substrate.predicate_equivalence import (
    PredicateEquivalence,
)
from src.layer4_lookup import tier_w as _tier_w
from src.layer4_lookup.types import (
    ChainEdge,
    DerivationResult,
    LookupOutcome,
)
from src.llm_client import LLMClient


# ============================================================================
# Architectural bounds
# ============================================================================

MAX_DEPTH = 4
"""Maximum logical-step depth the walker will explore.

A logical step is one state transition (one slot substitution OR one
predicate substitution OR one composite et+pd step). The chain may
contain more ChainEdges than there are logical steps (composite
steps emit two edges).

Set by the v0.14 architecture as a hard bound to keep walks O(1) in
walltime even on substrates with many cold-start cells.
"""

MIN_CHAIN_RELIABILITY = 0.4
"""Floor below which a chain is rejected.

Chosen to admit cold-start rows (Beta(1,1) over zero counts produces
confidence 0.5) and reject actively-contradicted rows (e.g. (0,2)
gives 0.25). The floor is intentionally low — derivation is cheap
and a low floor lets new oracle rows produce advisory verdicts that
strengthen with use.
"""


DEFAULT_ACTIVE_CLASSIFICATION_BUDGET = 20
"""Default per-walk budget for cold predicate_distribution
classifications (v0.14 Phase 8).

When ``walk()`` runs with ``llm`` provided, cold pd cells encountered
during expansion are classified up to this many times per walk; beyond
the budget, the walker treats subsequent cold cells as if llm were
None (graceful fall-through to fresh verification).

Set to 20 because: production deployments can dial this to 0 for
purely passive walks; development and exploration deployments run
with a high budget to populate the substrate as a side effect of use.
The cost contract is bounded by memoization — warm-cache walks pay no
LLM cost regardless of budget; cold-start walks pay at most budget-many
LLM calls. predicate_distribution is the only oracle the walker
actively classifies during expansion (entity_taxonomy and
entity_equivalence are read SQL-only inside _expand).
"""


# ============================================================================
# Walk state
# ============================================================================


@dataclass(frozen=True)
class _WalkState:
    """One state in the BFS frontier.

    Frozen so it's hashable + safe to use in visited-set keys.
    ``slots`` is stored as a sorted tuple of (key, value) pairs so
    the dataclass is hashable; helpers convert to/from dict.
    """

    pattern: str
    predicate: str          # normalized: strip().lower()
    polarity: int
    slots_tuple: tuple[tuple[str, Any], ...]  # sorted by key

    @classmethod
    def from_claim(cls, claim: dict) -> "_WalkState":
        slots = claim.get("slots") or {}
        slots_tuple = tuple(sorted(
            (k, _hashable(v)) for k, v in slots.items()
        ))
        return cls(
            pattern=claim.get("pattern", ""),
            predicate=(claim.get("predicate", "") or "").strip().lower(),
            polarity=int(claim.get("polarity", 1)),
            slots_tuple=slots_tuple,
        )

    @property
    def slots(self) -> dict[str, Any]:
        return dict(self.slots_tuple)

    def to_claim_shape(self) -> dict:
        """Shape suitable for re-querying tier_u / tier_w."""
        return {
            "pattern": self.pattern,
            "predicate": self.predicate,
            "polarity": self.polarity,
            "slots": self.slots,
            "source_text": "",  # tense detection in derivation defaults to present
        }

    def canonical_key(self) -> str:
        """Visited-set key. Stable string; hashable; collision-resistant."""
        slots_repr = "&".join(
            f"{k}={json.dumps(v, default=str, sort_keys=True)}"
            for k, v in self.slots_tuple
        )
        return f"{self.pattern}|{self.predicate}|p={self.polarity}|{slots_repr}"

    def with_slot(self, slot_name: str, new_value: Any) -> "_WalkState":
        """Return a new state with one slot substituted."""
        new_slots = dict(self.slots_tuple)
        new_slots[slot_name] = new_value
        new_tuple = tuple(sorted(
            (k, _hashable(v)) for k, v in new_slots.items()
        ))
        return _WalkState(
            pattern=self.pattern,
            predicate=self.predicate,
            polarity=self.polarity,
            slots_tuple=new_tuple,
        )

    def with_predicate(
        self, new_predicate: str, *, flip_polarity: bool = False,
    ) -> "_WalkState":
        return _WalkState(
            pattern=self.pattern,
            predicate=(new_predicate or "").strip().lower(),
            polarity=(1 - self.polarity) if flip_polarity else self.polarity,
            slots_tuple=self.slots_tuple,
        )

    def with_subject_object_swapped(
        self, new_predicate: str,
    ) -> Optional["_WalkState"]:
        """Apply a subject_object_swap. Only meaningful on relational
        patterns (or others with explicit subject/object slots)."""
        slots = self.slots
        if "subject" not in slots or "object" not in slots:
            return None
        slots["subject"], slots["object"] = slots["object"], slots["subject"]
        new_tuple = tuple(sorted(
            (k, _hashable(v)) for k, v in slots.items()
        ))
        return _WalkState(
            pattern=self.pattern,
            predicate=(new_predicate or "").strip().lower(),
            polarity=self.polarity,
            slots_tuple=new_tuple,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "predicate": self.predicate,
            "polarity": self.polarity,
            "slots": self.slots,
        }


def _hashable(v: Any) -> Any:
    """Make slot values hashable for the frozen dataclass."""
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, list):
        return tuple(_hashable(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, _hashable(val)) for k, val in v.items()))
    return str(v)


@dataclass(frozen=True)
class _Frontier:
    """One BFS frontier slot.

    ``depth`` counts logical steps from the initial state. ``chain``
    is the cumulative list of ChainEdges (may exceed ``depth`` due to
    composite et+pd steps emitting two edges per logical step).
    """

    state: _WalkState
    chain: tuple[ChainEdge, ...]
    depth: int

    @property
    def chain_reliability(self) -> float:
        if not self.chain:
            return 1.0
        return min(e.confidence for e in self.chain)


@dataclass
class _BudgetState:
    """Mutable per-walk active-classification budget (v0.14 Phase 8).

    Threaded through ``_expand`` → ``_expand_taxonomy`` → ``_resolve_pd``.
    ``remaining`` decrements once per cold predicate_distribution row
    classified; ``classified`` accumulates a count for the
    derivation_walk_completed event payload.

    When ``remaining`` hits zero, ``_resolve_pd`` treats subsequent cold
    rows as if ``llm`` were None — the lookup-first path returns None,
    the et+pd composite step is skipped, and the walker advances to
    other branches whose cells are warm. This is the "graceful
    fall-through" the architecture commits to: budget exhaustion is
    not a hard halt; only NEW cold cells are skipped.

    ``budget_exhausted_emitted`` ensures the
    derivation_walk_budget_exhausted event fires exactly once per walk
    (the first cold cell that hits the depleted-budget path) — without
    this guard, multi-cold-cell walks would emit the event N times.
    """

    remaining: int
    classified: int = 0
    budget_exhausted_emitted: bool = False

    def consume(self) -> bool:
        """Decrement the counter if budget remains.

        Returns True when consumption is allowed (budget had room and
        was decremented); False when the budget is depleted (caller
        treats this as a 'cold cell with no llm' miss).
        """
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        self.classified += 1
        return True


# ============================================================================
# Match check
# ============================================================================


@dataclass(frozen=True)
class _Match:
    tier: str               # 'u' | 'w'
    fact_id: Optional[int] = None
    w_row_id: Optional[int] = None


def _check_literal_match(
    state: _WalkState,
    store: FactStore,
    registry: PatternRegistry,
    *,
    user_id: str = DEFAULT_USER_ID,
    current_session: Optional[str] = None,
) -> Optional[_Match]:
    """Literal-match-only check against U and W.

    The walker explores via substrate; the substrate IS the semantic
    layer. At match-check time we only need raw SQL: same predicate,
    same slots, same polarity, non-expired. Calling tier_u's full
    oracle resolution chain here would double-count substrate
    consultations.
    """
    # Tier U
    pattern = state.pattern
    predicate_lower = state.predicate
    slots = state.slots
    key_slot_names = KEY_SLOTS_BY_PATTERN.get(pattern, [])
    if key_slot_names:
        key_slots = {k: slots[k] for k in key_slot_names if k in slots}
    else:
        key_slots = dict(slots)

    # Use find_currently_valid for an exact (pattern, predicate, slots,
    # polarity) match against user-asserted facts. We pass the
    # state's predicate at its normalized form and try both
    # normalized and verbatim against stored predicates because
    # stored predicates may be case-mixed.
    candidates = store.find_currently_valid(
        pattern,
        slot_match=key_slots,
        polarity=state.polarity,
        user_id=user_id,
        current_session=current_session,
    )
    for f in candidates:
        if (
            f.asserted_by == "user"
            and f.verification_status == "user_asserted"
            and (f.predicate or "").strip().lower() == predicate_lower
        ):
            return _Match(tier="u", fact_id=f.id)

    # Tier W: canonical key match, non-expired.
    try:
        canonical_key = _tier_w.canonicalize_claim_key(
            state.to_claim_shape(), registry,
        )
    except (KeyError, Exception):
        canonical_key = None
    if canonical_key:
        row = store._conn.execute(
            "SELECT id, expires_at, verdict FROM verification_cache "
            "WHERE canonical_key = ?",
            (canonical_key,),
        ).fetchone()
        if row is not None:
            from datetime import datetime, timezone
            expires_at = row["expires_at"]
            still_valid = True
            if expires_at is not None:
                try:
                    if datetime.fromisoformat(expires_at) < datetime.now(timezone.utc):
                        still_valid = False
                except ValueError:
                    still_valid = True
            verdict = row["verdict"]
            # Only "verified" Tier W rows MATCH the claim's polarity.
            # Other statuses (contradicted, retrieval_inconclusive, etc.)
            # don't constitute a positive derivation witness — the
            # walker is looking for a chain that SUPPORTS the claim.
            if still_valid and verdict == "verified":
                return _Match(tier="w", w_row_id=int(row["id"]))
    return None


# ============================================================================
# BFS expansion
# ============================================================================


def _expand(
    state: _WalkState,
    store: FactStore,
    *,
    predicate_oracle: PredicateEquivalence,
    entity_oracle: EntityEquivalence,
    taxonomy_oracle: EntityTaxonomy,
    distribution_oracle: PredicateDistribution,
    llm: Optional[LLMClient],
    source_turn_id: Optional[int],
    key_slot_names: list[str],
    budget_state: _BudgetState,
) -> Iterator[tuple[_WalkState, list[ChainEdge]]]:
    """Yield (new_state, edges) pairs for every productive expansion
    of ``state``. Each pair is one logical step.

    The walker reads existing entity_taxonomy / entity_equivalence /
    predicate_equivalence rows from SQL only — those are never cold-
    classified during expansion. Only ``predicate_distribution`` is a
    candidate for active classification (in ``_resolve_pd``), and that
    is bounded by ``budget_state``. With ``llm=None`` the walker is
    purely read-only; with ``llm`` provided AND budget remaining, cold
    pd cells are classified up to the budget. After exhaustion the
    walker degrades back to read-only behavior on cold cells without
    crashing.
    """
    pattern = state.pattern
    polarity = state.polarity

    # ------- Group 1: entity_taxonomy + predicate_distribution composite ----
    yield from _expand_taxonomy(
        state, store, key_slot_names,
        distribution_oracle, llm, source_turn_id,
        budget_state,
    )

    # ------- Group 2: entity_equivalence (alias) -----------------------------
    yield from _expand_entity_equivalence(
        state, store, key_slot_names,
    )

    # ------- Group 3: predicate_equivalence ---------------------------------
    yield from _expand_predicate_equivalence(
        state, store,
    )


def _expand_taxonomy(
    state: _WalkState,
    store: FactStore,
    key_slot_names: list[str],
    distribution_oracle: PredicateDistribution,
    llm: Optional[LLMClient],
    source_turn_id: Optional[int],
    budget_state: _BudgetState,
) -> Iterator[tuple[_WalkState, list[ChainEdge]]]:
    """Walk entity_taxonomy rows involving any identity slot value
    in the state. For each candidate substitution V→V', consult
    predicate_distribution to ratify the propagation direction.

    The composite step yields TWO ChainEdges per state transition:
    one for the et consultation, one for the pd ratification.
    """
    slots = state.slots
    pattern = state.pattern
    predicate_lower = state.predicate
    polarity = state.polarity

    for slot_name in key_slot_names:
        v = slots.get(slot_name)
        if not isinstance(v, str) or not v.strip():
            continue
        v = v.strip()

        # SELECT all entity_taxonomy rows mentioning v (as child OR parent).
        rows = store._conn.execute(
            "SELECT * FROM entity_taxonomy "
            "WHERE child = ? OR parent = ?",
            (v, v),
        ).fetchall()
        for r in rows:
            child = r["child"]
            parent = r["parent"]
            label = r["label"]
            relation_type = r["relation_type"]
            row_id = int(r["id"])
            row_confidence = confidence_from_counts(
                int(r["affirmed_count"] or 0),
                int(r["contradicted_count"] or 0),
            )

            # Determine V' and the required propagation direction.
            cases = _taxonomy_substitution_cases(v, child, parent, label)
            for v_prime, required_direction in cases:
                if v_prime == v:
                    continue  # no actual substitution
                if required_direction is None:
                    # Equivalent: no predicate_distribution gating.
                    new_state = state.with_slot(slot_name, v_prime)
                    et_edge = ChainEdge(
                        oracle="entity_taxonomy",
                        row_id=row_id,
                        label=label,
                        confidence=row_confidence,
                        from_state=state.to_dict(),
                        to_state=new_state.to_dict(),
                        notes=(
                            f"taxonomy: equivalent under {relation_type!r}; "
                            f"slot {slot_name!r}: {v!r} -> {v_prime!r}"
                        ),
                    )
                    yield new_state, [et_edge]
                    continue

                # Need pd ratification. Lookup-first: if the cached
                # row exists, use it; otherwise consult only when an
                # LLM is provided AND the per-walk budget hasn't been
                # exhausted (cold-start path under budget). Without an
                # LLM, or with budget exhausted, skip this expansion —
                # the walker is tolerant of sparse substrate and of
                # depleted budgets.
                pd_signal = _resolve_pd(
                    distribution_oracle,
                    pattern, predicate_lower, polarity, relation_type,
                    llm=llm, source_turn_id=source_turn_id,
                    budget_state=budget_state, store=store,
                )
                if pd_signal is None:
                    continue
                pd_label, pd_confidence, pd_row_id = pd_signal
                if not _pd_allows_direction(pd_label, required_direction):
                    continue

                new_state = state.with_slot(slot_name, v_prime)
                et_edge = ChainEdge(
                    oracle="entity_taxonomy",
                    row_id=row_id,
                    label=label,
                    confidence=row_confidence,
                    from_state=state.to_dict(),
                    to_state=new_state.to_dict(),
                    notes=(
                        f"taxonomy: {label!r} under {relation_type!r}; "
                        f"slot {slot_name!r}: {v!r} -> {v_prime!r}; "
                        f"required pd direction: {required_direction!r}"
                    ),
                )
                pd_edge = ChainEdge(
                    oracle="predicate_distribution",
                    row_id=pd_row_id,
                    label=pd_label,
                    confidence=pd_confidence,
                    from_state=new_state.to_dict(),
                    to_state=new_state.to_dict(),  # gating edge — no further state change
                    notes=(
                        f"distribution gate: ({pattern!r}, {predicate_lower!r}, "
                        f"p={polarity}, {relation_type!r}) -> {pd_label!r}"
                    ),
                )
                yield new_state, [et_edge, pd_edge]


def _resolve_pd(
    distribution_oracle: PredicateDistribution,
    pattern: str,
    predicate: str,
    polarity: int,
    relation_type: str,
    *,
    llm: Optional[LLMClient],
    source_turn_id: Optional[int],
    budget_state: _BudgetState,
    store: FactStore,
) -> Optional[tuple[str, float, Optional[int]]]:
    """Resolve a predicate_distribution row.

    Lookup-first; optionally cold-start via LLM, bounded by
    ``budget_state``. Returns ``(label, confidence, row_id)`` on
    success, or None when:
      * the row is missing AND no LLM is available, OR
      * the row is missing AND the budget is exhausted, OR
      * the LLM produced a malformed classification.

    Pipeline events:
      * ``derivation_walk_active_classification`` — once per cold
        cell that gets classified within the budget (with
        oracle/key/label/budget_remaining payload).
      * ``derivation_walk_budget_exhausted`` — once per walk, when
        the first cold cell encounters depleted budget.

    The lookup-first discipline keeps the walker tolerant of sparse
    substrate when no LLM is provided. When an LLM is provided AND
    budget remains, cold-start cells are classified and written,
    contributing to substrate accretion. When budget is exhausted,
    the walker degrades to lookup-first behavior on remaining cold
    cells (graceful fall-through; no crash, no hang).
    """
    existing = distribution_oracle.lookup(
        pattern, predicate, polarity, relation_type,
    )
    if existing is not None:
        return (existing.label, existing.confidence(), existing.id)
    if llm is None:
        return None
    if not budget_state.consume():
        if not budget_state.budget_exhausted_emitted:
            budget_state.budget_exhausted_emitted = True
            _safe_emit_event(
                store, source_turn_id,
                "derivation_walk_budget_exhausted",
                {
                    "oracle": "predicate_distribution",
                    "first_skipped_key": {
                        "pattern": pattern,
                        "predicate": predicate,
                        "polarity": polarity,
                        "taxonomy_relation_type": relation_type,
                    },
                    "classified_before_exhaustion": budget_state.classified,
                },
            )
        return None
    verdict = distribution_oracle.consult(
        pattern, predicate, polarity, relation_type,
        llm=llm, source_turn_id=source_turn_id,
    )
    if verdict.classification_failed or verdict.label is None:
        # Classification attempt counted toward budget even when it
        # failed — that's the LLM-cost contract; the walker paid for
        # the attempt regardless of outcome. Don't refund.
        return None
    _safe_emit_event(
        store, source_turn_id,
        "derivation_walk_active_classification",
        {
            "oracle": "predicate_distribution",
            "key": {
                "pattern": pattern,
                "predicate": predicate,
                "polarity": polarity,
                "taxonomy_relation_type": relation_type,
            },
            "label": verdict.label,
            "row_id": verdict.row_id,
            "budget_remaining": budget_state.remaining,
            "classified_so_far": budget_state.classified,
        },
    )
    return (verdict.label, verdict.confidence, verdict.row_id)


def _taxonomy_substitution_cases(
    v: str, child: str, parent: str, label: str,
) -> list[tuple[str, Optional[str]]]:
    """Given the entity_taxonomy row's columns and label, return the
    list of (V', required_propagation_direction) candidates.

    Required direction is None for ``equivalent`` (no pd gating);
    otherwise 'up' (V_prime is more specific) or 'down' (V_prime is
    more general).

    Direction logic recap:
      label=child_subsumed_by_parent:
        child is more specific, parent more general.
        If V == child: V' = parent (more general). To use a fact at
                       V' to support the current state, predicate
                       must distribute_DOWN (general → specific).
                       But the BFS substitutes IN PLACE — we're
                       moving the state from V (specific) to V'
                       (general), and a fact at V' supports the
                       state at V via distributes_DOWN. So
                       required_direction = 'down'.
        If V == parent: V' = child (more specific). Required:
                        distributes_UP (specific → general).
      label=parent_subsumed_by_child:
        parent is more specific (caller inverted args). child more
        general.
        If V == child: V' = parent (more specific). Required: 'up'.
        If V == parent: V' = child (more general). Required: 'down'.
      label=equivalent: V' = the other column. No pd gating.
      label=neither: no substitution.
    """
    out: list[tuple[str, Optional[str]]] = []
    if label == "neither":
        return out
    if label == "equivalent":
        if v == child:
            out.append((parent, None))
        if v == parent:
            out.append((child, None))
        return out
    if label == "child_subsumed_by_parent":
        if v == child:
            out.append((parent, "down"))
        if v == parent:
            out.append((child, "up"))
        return out
    if label == "parent_subsumed_by_child":
        if v == child:
            out.append((parent, "up"))
        if v == parent:
            out.append((child, "down"))
        return out
    return out


def _pd_allows_direction(pd_label: str, required: str) -> bool:
    """Map the required propagation direction to the pd label set
    that allows it.

    required='up'   ->   distributes_up or both
    required='down' ->   distributes_down or both
    """
    if required == "up":
        return pd_label in ("distributes_up", "both")
    if required == "down":
        return pd_label in ("distributes_down", "both")
    return False


def _expand_entity_equivalence(
    state: _WalkState,
    store: FactStore,
    key_slot_names: list[str],
) -> Iterator[tuple[_WalkState, list[ChainEdge]]]:
    """Walk entity_equivalence rows involving any identity slot value
    with label='same'. Substitute V → V'. One edge per substitution."""
    slots = state.slots
    for slot_name in key_slot_names:
        v = slots.get(slot_name)
        if not isinstance(v, str) or not v.strip():
            continue
        v = v.strip()
        rows = store._conn.execute(
            "SELECT * FROM entity_equivalence "
            "WHERE entity_a = ? OR entity_b = ?",
            (v, v),
        ).fetchall()
        for r in rows:
            label = r["label"]
            if label != "same":
                continue
            other = r["entity_a"] if r["entity_b"] == v else r["entity_b"]
            if other == v:
                continue
            row_confidence = confidence_from_counts(
                int(r["affirmed_count"] or 0),
                int(r["contradicted_count"] or 0),
            )
            new_state = state.with_slot(slot_name, other)
            edge = ChainEdge(
                oracle="entity_equivalence",
                row_id=int(r["id"]),
                label="same",
                confidence=row_confidence,
                from_state=state.to_dict(),
                to_state=new_state.to_dict(),
                notes=(
                    f"alias: slot {slot_name!r}: {v!r} -> {other!r}"
                ),
            )
            yield new_state, [edge]


def _expand_predicate_equivalence(
    state: _WalkState,
    store: FactStore,
) -> Iterator[tuple[_WalkState, list[ChainEdge]]]:
    """Walk predicate_equivalence rows under (pattern, current_predicate).

    label=equivalent + slot_reversal=none:        substitute predicate
    label=contradictory + slot_reversal=none:     substitute predicate AND flip polarity
    label=equivalent + subject_object_swap:       substitute predicate AND swap subj/obj (relational only)
    label=distinct:                                no expansion
    other slot_reversal:                           skipped (Phase 7 doesn't consume)
    """
    pattern = state.pattern
    pred_lower = state.predicate
    rows = store._conn.execute(
        "SELECT * FROM predicate_equivalence "
        "WHERE pattern = ? AND (predicate_a = ? OR predicate_b = ?)",
        (pattern, pred_lower, pred_lower),
    ).fetchall()
    for r in rows:
        label = r["label"]
        slot_reversal = r["slot_reversal"]
        other = r["predicate_a"] if r["predicate_b"] == pred_lower else r["predicate_b"]
        if other == pred_lower:
            continue
        row_confidence = confidence_from_counts(
            int(r["affirmed_count"] or 0),
            int(r["contradicted_count"] or 0),
        )
        if label == "distinct":
            continue
        if slot_reversal == "none":
            if label == "equivalent":
                new_state = state.with_predicate(other)
                edge = ChainEdge(
                    oracle="predicate_equivalence",
                    row_id=int(r["id"]),
                    label="equivalent",
                    confidence=row_confidence,
                    from_state=state.to_dict(),
                    to_state=new_state.to_dict(),
                    notes=(
                        f"predicate: equivalent paraphrase "
                        f"{pred_lower!r} -> {other!r}"
                    ),
                )
                yield new_state, [edge]
            elif label == "contradictory":
                new_state = state.with_predicate(other, flip_polarity=True)
                edge = ChainEdge(
                    oracle="predicate_equivalence",
                    row_id=int(r["id"]),
                    label="contradictory",
                    confidence=row_confidence,
                    from_state=state.to_dict(),
                    to_state=new_state.to_dict(),
                    notes=(
                        f"predicate: contradictory antonym "
                        f"{pred_lower!r} -> {other!r}; polarity flipped"
                    ),
                )
                yield new_state, [edge]
        elif slot_reversal == "subject_object_swap":
            if label != "equivalent":
                continue
            # Subject/object swap is meaningful only on the relational
            # pattern (other patterns don't have subject+object slots
            # in the architectural sense). Skip silently otherwise.
            if pattern != "relational":
                continue
            swapped = state.with_subject_object_swapped(other)
            if swapped is None:
                continue
            edge = ChainEdge(
                oracle="predicate_equivalence",
                row_id=int(r["id"]),
                label="equivalent",
                confidence=row_confidence,
                from_state=state.to_dict(),
                to_state=swapped.to_dict(),
                notes=(
                    f"predicate: equivalent active/passive swap "
                    f"{pred_lower!r} -> {other!r}; subject/object swapped"
                ),
            )
            yield swapped, [edge]
        # participant_reorder: deferred (no Phase 7 entries exercise)


# ============================================================================
# Public entry point
# ============================================================================


def walk(
    claim: dict,
    store: FactStore,
    *,
    key_slot_names: list[str],
    registry: PatternRegistry,
    predicate_oracle: PredicateEquivalence,
    entity_oracle: EntityEquivalence,
    taxonomy_oracle: EntityTaxonomy,
    distribution_oracle: PredicateDistribution,
    llm: Optional[LLMClient] = None,
    source_turn_id: Optional[int] = None,
    user_id: str = DEFAULT_USER_ID,
    current_session: Optional[str] = None,
    max_depth: int = MAX_DEPTH,
    min_chain_reliability: float = MIN_CHAIN_RELIABILITY,
    active_classification_budget: int = DEFAULT_ACTIVE_CLASSIFICATION_BUDGET,
) -> DerivationResult:
    """Walk the substrate looking for a chain that supports ``claim``.

    Returns a DerivationResult. On MATCH, the chain reads from the
    initial state through one or more substrate transitions to a
    fact in U or W.

    ``active_classification_budget`` (v0.14 Phase 8) bounds how many
    cold ``predicate_distribution`` rows the walker may classify
    during this walk. ``llm=None`` makes the walker purely passive
    regardless of budget. When ``llm`` is provided AND budget>0, cold
    pd cells are classified up to the budget; beyond the budget,
    cold cells are skipped (lookup-first behavior). Default 20 — set
    to 0 for purely passive walks (architectural Phase 7 parity).

    Emits ``derivation_walk_attempt`` on entry, ``derivation_walk_
    completed`` on MATCH or final MISS, per-branch
    ``derivation_walk_aborted_*`` events as the BFS prunes, plus
    ``derivation_walk_active_classification`` per cold-row write and
    ``derivation_walk_budget_exhausted`` once if the budget is
    depleted mid-walk.
    """
    initial_state = _WalkState.from_claim(claim)
    visited: set[str] = {initial_state.canonical_key()}
    explored = 0
    abort_reason: Optional[str] = None
    budget_state = _BudgetState(remaining=active_classification_budget)

    _safe_emit_event(
        store, source_turn_id, "derivation_walk_attempt",
        {
            "claim": {
                "pattern": initial_state.pattern,
                "predicate": initial_state.predicate,
                "polarity": initial_state.polarity,
                "slots": initial_state.slots,
            },
            "max_depth": max_depth,
            "min_chain_reliability": min_chain_reliability,
            "active_classification_budget": active_classification_budget,
        },
    )

    frontier: deque[_Frontier] = deque([
        _Frontier(state=initial_state, chain=(), depth=0),
    ])
    while frontier:
        f = frontier.popleft()
        if f.depth >= max_depth:
            continue
        for new_state, new_edges in _expand(
            f.state, store,
            predicate_oracle=predicate_oracle,
            entity_oracle=entity_oracle,
            taxonomy_oracle=taxonomy_oracle,
            distribution_oracle=distribution_oracle,
            llm=llm,
            source_turn_id=source_turn_id,
            key_slot_names=key_slot_names,
            budget_state=budget_state,
        ):
            new_chain = f.chain + tuple(new_edges)
            new_reliability = min(e.confidence for e in new_chain)
            if new_reliability < min_chain_reliability:
                _safe_emit_event(
                    store, source_turn_id,
                    "derivation_walk_aborted_reliability",
                    {
                        "depth": f.depth + 1,
                        "edge_oracles": [e.oracle for e in new_edges],
                        "min_reliability_seen": new_reliability,
                        "floor": min_chain_reliability,
                    },
                )
                continue
            new_key = new_state.canonical_key()
            if new_key in visited:
                continue
            visited.add(new_key)
            explored += 1

            match = _check_literal_match(
                new_state, store, registry,
                user_id=user_id,
                current_session=current_session,
            )
            if match is not None:
                _safe_emit_event(
                    store, source_turn_id, "derivation_walk_completed",
                    {
                        "outcome": "match",
                        "matching_tier": match.tier,
                        "matching_fact_id": match.fact_id,
                        "matching_w_row_id": match.w_row_id,
                        "depth": f.depth + 1,
                        "chain_reliability": new_reliability,
                        "explored_states": explored,
                        "edge_count": len(new_chain),
                        "active_classifications": budget_state.classified,
                        "budget_remaining": budget_state.remaining,
                    },
                )
                return DerivationResult(
                    outcome=LookupOutcome.MATCH,
                    chain=list(new_chain),
                    chain_reliability=new_reliability,
                    matching_fact_id=match.fact_id,
                    matching_w_row_id=match.w_row_id,
                    matching_tier=match.tier,
                    explored_states=explored,
                    abort_reason=None,
                    notes=[
                        f"derivation MATCH at depth {f.depth + 1} "
                        f"via {[e.oracle for e in new_chain]!r}"
                    ],
                )

            new_depth = f.depth + 1
            if new_depth < max_depth:
                frontier.append(_Frontier(new_state, new_chain, new_depth))
            else:
                _safe_emit_event(
                    store, source_turn_id,
                    "derivation_walk_aborted_depth",
                    {
                        "depth": new_depth,
                        "max_depth": max_depth,
                        "edge_oracles": [e.oracle for e in new_chain],
                    },
                )

    abort_reason = "exhausted"
    _safe_emit_event(
        store, source_turn_id, "derivation_walk_completed",
        {
            "outcome": "miss",
            "abort_reason": abort_reason,
            "explored_states": explored,
            "active_classifications": budget_state.classified,
            "budget_remaining": budget_state.remaining,
        },
    )
    return DerivationResult(
        outcome=LookupOutcome.MISS,
        chain=[],
        chain_reliability=0.0,
        explored_states=explored,
        abort_reason=abort_reason,
        notes=[
            f"derivation MISS: explored {explored} states; no chain "
            f"≥ floor {min_chain_reliability} reached a fact in U or W"
        ],
    )
