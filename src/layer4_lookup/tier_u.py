"""Tier U lookup and storage — Phases 3-6 progressive integration.

Phase 3 wired ``predicate_equivalence`` for the cheetahs case
(antonym + polarity-flip resolution). Phase 4 added
``entity_equivalence`` for alias-identity resolution: a stored fact
under (entity=user, location="NYC") matches a model claim about
(entity=user, location="New York City") via the oracle. Phase 6
adds the session model — both on the lookup side (a new
``current_session`` parameter filters the candidate set against
``is_session_local`` and ``session_ids``) and on the storage side
(``store_user_fact`` decides between session-local and cross-session
storage based on a marker phrase, and handles cross-session
reaffirmation per principle 3).

Phase 6 storage is intentionally narrow: it handles new insert,
same-content reaffirmation (cross-session: append+increment;
session-local: noop), and same-session no-op. It does NOT close
opposite-polarity priors — that contradiction-handling lives in
Phase 7+'s orchestrator. A user who says "I love olives" then "I
don't love olives" through this storage path produces two
coexisting rows; the lookup will surface the conflict via
CONTRADICTION on the next model claim.

Phase 7 extends the walker further (Tier W, derivation). **Do not
extend this module** beyond Phase 6 — Phase 7's walker.py replaces
the lookup orchestration.

The walker resolves a model claim against Tier U in three stages,
each cheaper than the next:

  1. **Literal match**. SQL exact match on (pattern, identity slots,
     predicate, polarity) → MATCH; same predicate + slots, opposite
     polarity → CONTRADICTION. No oracle calls. The cheapest path;
     the cheetahs and most other common cases resolve here.

  2. **Predicate equivalence on exact-identity candidates**. Candidates
     share (pattern, identity slots) with the claim but have a
     different predicate. ``predicate_equivalence`` is consulted
     pairwise. ``contradictory + opposite polarity`` → MATCH (the
     cheetahs case). ``equivalent + same polarity`` → MATCH. And so
     on; see ``_resolve_via_verdict`` for the full table.

  3. **Alias-identity broadening (Phase 4)**. Only runs when
     ``entity_oracle`` is provided. Gathers all currently-valid
     user-asserted facts under (pattern) — without filtering by
     identity slots — and for each candidate consults
     ``entity_equivalence`` on each non-literal-matching identity
     slot. If every identity slot is either literally equal, the
     user (lexical canonicalization), or alias-equivalent per the
     oracle, the candidate qualifies. The qualifying candidate then
     runs through the literal + predicate-equivalence pipeline
     against the model claim's predicate.

The ordering matters for cost. Step 1 is free (SQL only); step 2
costs one ``predicate_equivalence`` call per same-identity
candidate (memoized after first encounter); step 3 costs up to N x
identity_slots ``entity_equivalence`` calls plus a
``predicate_equivalence`` call per qualifying candidate. Putting
step 1 first means the cheetahs and other common cases resolve
without any oracle calls. The cost-correctness invariant: if the
cheaper SQL path resolves the lookup, the oracle is NEVER consulted.

Cold-start cost characteristics for step 3
==========================================

A user with N facts under a single pattern, asking a novel claim
whose identity slots don't literally match any stored fact, can
trigger up to N x identity_slots calls to entity_equivalence in a
single tier_u lookup. Memoization makes warm-cache cost approach
zero. Phase 6's session-locality scoping has reduced N for typical
queries: the candidate set excludes session-local facts that
aren't in the current session, so users with many session-local
hypotheticals across many conversations don't pay cold-start cost
for irrelevant ones. Further reductions are possible if Phase 7+
adds slot-value indexing or pre-filtering, but the current
implementation is bounded-by-memoization rather than bounded-by-N.
Oracle row writes are independent of caller success — the same
(entity_a, entity_b) pair is classified at most once per pair,
regardless of which lookup triggered the classification. Deferring
writes until a candidate qualifies would let the same pair be
re-classified across different lookups in the same conversation,
defeating the amortization.

The ``via`` taxonomy
====================

``TierUResult.via`` is a list of oracle names in consultation order:

  * ``[]``                                    — pure literal match;
                                                 no oracles consulted.
  * ``["predicate_equivalence"]``             — predicate oracle
                                                 resolved the case.
  * ``["entity_equivalence"]``                — entity oracle alone
                                                 (alias identity +
                                                 literal predicate).
  * ``["entity_equivalence",
     "predicate_equivalence"]``               — both, in the order
                                                 they fired (entity
                                                 broadens the
                                                 candidate set,
                                                 predicate resolves
                                                 within it).

This shape extends cleanly through Phases 5-7; ``entity_taxonomy``
and ``predicate_distribution`` append to the list as the derivation
walker consults them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from src.fact_store import DEFAULT_USER_ID, Fact, FactStore
from src.layer2_routing.constants import (
    confidence_from_counts,
    is_user,
)
from src.layer3_substrate.classifier_base import _safe_emit_event
from src.layer3_substrate.entity_equivalence import (
    EntityEquivalence,
    EntityEquivalenceVerdict,
)
from src.layer3_substrate.entity_taxonomy import (
    EntityTaxonomy,
    EntityTaxonomyVerdict,
)
from src.layer3_substrate.predicate_equivalence import (
    PredicateEquivalence,
    PredicateEquivalenceVerdict,
)
from src.layer4_lookup.relevance import (
    candidate_tokens as _candidate_tokens,
    is_candidate_relevant as _is_candidate_relevant,
)
from src.session_markers import find_session_marker
from src.llm_client import LLMClient


# ============================================================================
# Lookup-first helpers (v0.14 Phase 8d)
# ============================================================================
#
# Phase 7 surfaced that tier_u step 2 / step 3 crash when ``llm=None`` and
# the substrate cell they need is cold — ``oracle.consult()`` raises
# RuntimeError on cold-cell-with-no-llm by design. The architecturally
# clean fix mirrors derivation._resolve_pd: lookup-first; consult only
# when llm is provided. With llm=None, return None on cold cells (the
# caller treats that as no-signal and falls through gracefully); with
# llm provided, behavior is unchanged from Phase 7 (consult's own
# lookup-first handles the warm path; cold cells get classified).


def _resolve_predicate_equivalence(
    oracle: PredicateEquivalence,
    pattern: str,
    predicate_query: str,
    predicate_stored: str,
    *,
    llm: Optional[LLMClient],
    source_turn_id: Optional[int],
) -> Optional[PredicateEquivalenceVerdict]:
    """Lookup-first predicate_equivalence resolution.

    With ``llm=None``, returns the cached verdict if a row exists, else
    None (no crash on cold cells). With ``llm`` provided, defers to
    ``oracle.consult`` whose own lookup-first path serves warm rows
    and classifies cold rows. The two modes produce identical verdicts
    on warm cells.
    """
    if llm is None:
        existing = oracle.lookup(pattern, predicate_query, predicate_stored)
        if existing is None:
            return None
        return PredicateEquivalenceVerdict(
            label=existing.label,
            slot_reversal=existing.slot_reversal,
            reason=existing.reason,
            row_id=existing.id,
            served_from_cache=True,
            confidence=existing.confidence(),
            classification_failed=False,
        )
    return oracle.consult(
        pattern, predicate_query, predicate_stored,
        llm=llm, source_turn_id=source_turn_id,
    )


def _resolve_entity_equivalence(
    oracle: EntityEquivalence,
    entity_query: str,
    entity_stored: str,
    *,
    llm: Optional[LLMClient],
    source_turn_id: Optional[int],
) -> Optional[EntityEquivalenceVerdict]:
    """Lookup-first entity_equivalence resolution.

    Same shape as ``_resolve_predicate_equivalence``. With ``llm=None``,
    cold cells return None instead of raising; with ``llm`` provided,
    consult's own lookup-first path handles both warm and cold cases.
    """
    if llm is None:
        existing = oracle.lookup(entity_query, entity_stored)
        if existing is None:
            return None
        return EntityEquivalenceVerdict(
            label=existing.label,
            reason=existing.reason,
            row_id=existing.id,
            served_from_cache=True,
            confidence=existing.confidence(),
            classification_failed=False,
        )
    return oracle.consult(
        entity_query, entity_stored,
        llm=llm, source_turn_id=source_turn_id,
    )


def _resolve_entity_taxonomy(
    oracle: EntityTaxonomy,
    child: str,
    parent: str,
    relation_type: str,
    *,
    llm: Optional[LLMClient],
    source_turn_id: Optional[int],
) -> Optional[EntityTaxonomyVerdict]:
    """Lookup-first entity_taxonomy resolution. v0.14.8 — used by the
    alias-broadening path on slots declared as ``taxonomy_relevant_slots``
    in the pattern's schema. Mirrors tier_w.py's helper.

    On cold cell with no LLM, returns None. The caller treats None
    as "no signal yet" and falls through to entity_equivalence.
    On consult ValueError (empty / self-pair / unknown relation_type)
    also returns None — the caller falls through rather than crashing.
    """
    if llm is None:
        existing = oracle.lookup(child, parent, relation_type)
        if existing is None:
            return None
        return EntityTaxonomyVerdict(
            label=existing.label,
            reason=existing.reason,
            row_id=existing.id,
            served_from_cache=True,
            confidence=existing.confidence(),
            classification_failed=False,
        )
    try:
        return oracle.consult(
            child, parent, relation_type,
            llm=llm, source_turn_id=source_turn_id,
        )
    except ValueError:
        return None


_TAXONOMY_CONTAINMENT_LABELS: frozenset[str] = frozenset({
    "child_subsumed_by_parent",
    "parent_subsumed_by_child",
})


class TierUOutcome(str, Enum):
    """Three outcomes from a Tier U lookup.

    Mirrors v1's ``StoreLookupOutcome``. Layer 4's walker (Phase 7)
    will wrap these in a richer Decision shape.
    """

    MATCH = "match"
    CONTRADICTION = "contradiction"
    MISS = "miss"


@dataclass(frozen=True)
class TierUResult:
    """The result of a Tier U lookup.

    ``via`` is a list of oracle names in consultation order; pure
    literal match yields ``[]``. ``predicate_equivalence_row_id``
    (renamed in Phase 4 from ``oracle_row_id``) names the row from
    the predicate-equivalence oracle when one was consulted.
    ``entity_equivalence_row_ids`` is the list of oracle rows the
    alias-identity stage consulted on the resolving path; multiple
    when more than one identity slot needed alias resolution.

    ``polarity_flipped`` and ``slot_reversal_applied`` describe the
    transformations applied. ``polarity_flipped`` only fires on a
    contradictory predicate verdict (Phase 3). ``slot_reversal_
    applied`` is False in Phases 3-4 — Phase 4's tier_u doesn't
    consume slot_reversal != 'none' verdicts.
    """

    outcome: TierUOutcome
    matching_fact: Optional[Fact] = None
    contradicting_fact: Optional[Fact] = None
    via: list[str] = field(default_factory=list)
    predicate_equivalence_row_id: Optional[int] = None
    entity_equivalence_row_ids: list[int] = field(default_factory=list)
    polarity_flipped: bool = False
    slot_reversal_applied: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "matching_fact_id": (
                self.matching_fact.id if self.matching_fact else None
            ),
            "contradicting_fact_id": (
                self.contradicting_fact.id
                if self.contradicting_fact else None
            ),
            "via": list(self.via),
            "predicate_equivalence_row_id": self.predicate_equivalence_row_id,
            "entity_equivalence_row_ids": list(self.entity_equivalence_row_ids),
            "polarity_flipped": self.polarity_flipped,
            "slot_reversal_applied": self.slot_reversal_applied,
            "notes": list(self.notes),
        }


def lookup(
    claim: dict,
    store: FactStore,
    predicate_oracle: PredicateEquivalence,
    *,
    key_slot_names: list[str],
    user_id: str = DEFAULT_USER_ID,
    current_session: Optional[str] = None,
    llm: Optional[LLMClient] = None,
    source_turn_id: Optional[int] = None,
    entity_oracle: Optional[EntityEquivalence] = None,
    taxonomy_oracle: Optional[EntityTaxonomy] = None,
    registry: Optional[Any] = None,
    active_context_tokens: Optional[frozenset] = None,
) -> TierUResult:
    """Look for a matching or contradicting prior user-asserted fact.

    Three resolution stages, in cost-ascending order:

      1. Literal match (SQL only).
      2. Predicate equivalence on exact-identity candidates.
      3. Alias-identity broadening via entity_equivalence (Phase 4),
         skipped when ``entity_oracle is None``.

    Phase 3 callers pass only ``predicate_oracle`` (positional); the
    Phase-3 behavior is preserved exactly because step 3 is gated on
    ``entity_oracle is not None``.

    Phase 6 adds ``current_session``. When None (the default — and
    Phase 3-5 callers' behavior), the candidate set is restricted to
    cross-session facts (``is_session_local=0``); session-local rows
    are invisible. When set to a session id, the candidate set
    includes cross-session facts plus session-locals whose
    ``session_ids`` includes ``current_session``. The Q3 tie-breaker
    is enforced at the SQL layer: ``ORDER BY is_session_local DESC``
    means session-locals appear before cross-session rows when both
    are visible, so the lookup prefers the more specific contextual
    signal.

    The ``llm`` argument is required for any oracle call that misses
    the cache. Warm-cache lookups need no LLM.
    """
    pattern = claim.get("pattern", "")
    predicate = claim.get("predicate", "")
    polarity = int(claim.get("polarity"))
    slots = claim.get("slots") or {}
    key_slots = {k: slots[k] for k in key_slot_names if k in slots}

    # ---- Step 1: literal match ------------------------------------------------
    literal = _literal_match(
        store, pattern, predicate, key_slots, polarity, user_id,
        current_session,
    )
    if literal is not None:
        outcome, fact = literal
        _safe_emit_event(
            store, source_turn_id, "tier_lookup",
            {
                "tier": "U",
                "outcome": outcome.value,
                "via": [],
                "fact_id": fact.id,
                "pattern": pattern,
                "predicate": predicate,
            },
        )
        if outcome is TierUOutcome.MATCH:
            return TierUResult(
                outcome=TierUOutcome.MATCH, matching_fact=fact,
                via=[],
                notes=["literal match: same predicate, same polarity"],
            )
        return TierUResult(
            outcome=TierUOutcome.CONTRADICTION,
            contradicting_fact=fact, via=[],
            notes=[
                "literal contradiction: same predicate, opposite polarity"
            ],
        )

    # ---- Step 2: predicate equivalence on exact-identity candidates --------
    exact_candidates = _gather_exact_identity_candidates(
        store, pattern, key_slots, predicate, user_id, current_session,
    )
    for candidate in exact_candidates:
        verdict = _resolve_predicate_equivalence(
            predicate_oracle, pattern, predicate, candidate.predicate,
            llm=llm, source_turn_id=source_turn_id,
        )
        if verdict is None:
            # Cold cell, no LLM → skip this candidate. Honors the
            # lookup-first contract.
            continue
        outcome = _resolve_via_predicate_verdict(
            verdict,
            claim_polarity=polarity,
            candidate_polarity=int(candidate.polarity),
        )
        if outcome is None:
            continue
        kind, polarity_flipped, slot_reversal_applied = outcome
        notes = [
            f"oracle resolved ({predicate!r}, {candidate.predicate!r}) "
            f"-> {verdict.label} + {verdict.slot_reversal}; "
            f"polarity_flipped={polarity_flipped}"
        ]
        if kind == "match":
            return TierUResult(
                outcome=TierUOutcome.MATCH,
                matching_fact=candidate,
                via=["predicate_equivalence"],
                predicate_equivalence_row_id=verdict.row_id,
                polarity_flipped=polarity_flipped,
                slot_reversal_applied=slot_reversal_applied,
                notes=notes,
            )
        return TierUResult(
            outcome=TierUOutcome.CONTRADICTION,
            contradicting_fact=candidate,
            via=["predicate_equivalence"],
            predicate_equivalence_row_id=verdict.row_id,
            polarity_flipped=polarity_flipped,
            slot_reversal_applied=slot_reversal_applied,
            notes=notes,
        )

    # ---- Step 3: alias-identity broadening (Phase 4) -----------------------
    if entity_oracle is not None and key_slots:
        alias_candidates = _gather_alias_identity_candidates(
            store, pattern, key_slots, key_slot_names, user_id,
            current_session,
            entity_oracle, llm, source_turn_id,
            taxonomy_oracle=taxonomy_oracle,
            registry=registry,
            active_context_tokens=active_context_tokens,
        )
        for candidate, entity_row_ids in alias_candidates:
            # First try literal-predicate match against the alias
            # candidate.
            if candidate.predicate == predicate:
                if int(candidate.polarity) == polarity:
                    return TierUResult(
                        outcome=TierUOutcome.MATCH,
                        matching_fact=candidate,
                        via=["entity_equivalence"],
                        entity_equivalence_row_ids=list(entity_row_ids),
                        notes=[
                            f"alias-identity match via "
                            f"entity_equivalence rows {entity_row_ids!r}; "
                            f"literal predicate match"
                        ],
                    )
                return TierUResult(
                    outcome=TierUOutcome.CONTRADICTION,
                    contradicting_fact=candidate,
                    via=["entity_equivalence"],
                    entity_equivalence_row_ids=list(entity_row_ids),
                    notes=[
                        f"alias-identity contradiction via "
                        f"entity_equivalence rows {entity_row_ids!r}; "
                        f"literal predicate, opposite polarity"
                    ],
                )

            # Different predicate: consult predicate_equivalence on
            # top of the alias-identity broadening (lookup-first).
            verdict = _resolve_predicate_equivalence(
                predicate_oracle, pattern, predicate, candidate.predicate,
                llm=llm, source_turn_id=source_turn_id,
            )
            if verdict is None:
                continue
            outcome = _resolve_via_predicate_verdict(
                verdict,
                claim_polarity=polarity,
                candidate_polarity=int(candidate.polarity),
            )
            if outcome is None:
                continue
            kind, polarity_flipped, slot_reversal_applied = outcome
            notes = [
                f"alias-identity + predicate equivalence: "
                f"entity rows={entity_row_ids!r}, "
                f"predicate verdict=({verdict.label}, "
                f"{verdict.slot_reversal}), "
                f"polarity_flipped={polarity_flipped}"
            ]
            if kind == "match":
                return TierUResult(
                    outcome=TierUOutcome.MATCH,
                    matching_fact=candidate,
                    via=["entity_equivalence", "predicate_equivalence"],
                    predicate_equivalence_row_id=verdict.row_id,
                    entity_equivalence_row_ids=list(entity_row_ids),
                    polarity_flipped=polarity_flipped,
                    slot_reversal_applied=slot_reversal_applied,
                    notes=notes,
                )
            return TierUResult(
                outcome=TierUOutcome.CONTRADICTION,
                contradicting_fact=candidate,
                via=["entity_equivalence", "predicate_equivalence"],
                predicate_equivalence_row_id=verdict.row_id,
                entity_equivalence_row_ids=list(entity_row_ids),
                polarity_flipped=polarity_flipped,
                slot_reversal_applied=slot_reversal_applied,
                notes=notes,
            )

    _safe_emit_event(
        store, source_turn_id, "tier_lookup",
        {
            "tier": "U",
            "outcome": "miss",
            "pattern": pattern,
            "predicate": predicate,
            "exact_candidates_considered": len(exact_candidates),
        },
    )
    return TierUResult(outcome=TierUOutcome.MISS)


# ---- step 1 helper --------------------------------------------------------


def _literal_match(
    store: FactStore,
    pattern: str,
    predicate: str,
    key_slots: dict[str, Any],
    polarity: int,
    user_id: str,
    current_session: Optional[str],
) -> Optional[tuple[TierUOutcome, Fact]]:
    """Literal SQL match. Returns (MATCH, fact) on same-polarity hit;
    (CONTRADICTION, fact) on opposite-polarity hit; None on miss.

    The session-locality filter is delegated to
    ``find_currently_valid`` / ``find_contradictions``; ``ORDER BY
    is_session_local DESC`` at the SQL layer means the session-local
    row (if any) appears before the cross-session row when both are
    visible — encoding the Q3 tie-breaker.
    """
    same = [
        f for f in store.find_currently_valid(
            pattern, predicate=predicate, slot_match=key_slots,
            polarity=polarity, user_id=user_id,
            current_session=current_session,
        )
        if f.asserted_by == "user"
        and f.verification_status == "user_asserted"
    ]
    if same:
        return (TierUOutcome.MATCH, same[0])

    opposite = [
        f for f in store.find_contradictions(
            pattern, predicate, key_slots, polarity, user_id=user_id,
            current_session=current_session,
        )
        if f.asserted_by == "user"
        and f.verification_status == "user_asserted"
    ]
    if opposite:
        return (TierUOutcome.CONTRADICTION, opposite[0])
    return None


# ---- step 2 helper --------------------------------------------------------


def _gather_exact_identity_candidates(
    store: FactStore,
    pattern: str,
    key_slots: dict[str, Any],
    claim_predicate: str,
    user_id: str,
    current_session: Optional[str],
) -> list[Fact]:
    """User-asserted facts under (pattern, identity slots) whose
    predicate differs from the claim's. The literal-match step
    already handled same-predicate cases, so they're excluded here.

    Session-locality filter delegated to ``find_currently_valid``.
    """
    return [
        f for f in store.find_currently_valid(
            pattern, slot_match=key_slots, user_id=user_id,
            current_session=current_session,
        )
        if f.asserted_by == "user"
        and f.verification_status == "user_asserted"
        and f.predicate != claim_predicate
    ]


# ---- step 3 helpers (Phase 4) ---------------------------------------------


def _gather_alias_identity_candidates(
    store: FactStore,
    pattern: str,
    claim_key_slots: dict[str, Any],
    key_slot_names: list[str],
    user_id: str,
    current_session: Optional[str],
    entity_oracle: EntityEquivalence,
    llm: Optional[LLMClient],
    source_turn_id: Optional[int],
    *,
    taxonomy_oracle: Optional[EntityTaxonomy] = None,
    registry: Optional[Any] = None,
    active_context_tokens: Optional[frozenset] = None,
) -> list[tuple[Fact, list[int]]]:
    """Gather user-asserted facts under (pattern) whose identity
    slots are alias-equivalent to the claim's. Returns a list of
    (candidate_fact, entity_oracle_row_ids) tuples.

    For each candidate, walk the identity slots in order. A slot
    qualifies if:
      * the values are literally equal, OR
      * both values are lexically the user (``is_user``), OR
      * ``entity_equivalence.consult`` returns ``label='same'``.

    A candidate qualifies only if EVERY identity slot qualifies AND
    at least one slot needed the oracle (otherwise the candidate
    would have appeared in the exact-identity set already and
    wouldn't reach step 3).

    ``entity_oracle_row_ids`` collects the rows consulted on the
    qualifying path. If the candidate is rejected (one slot resolves
    'different' or classification_failed), the entity_equivalence
    rows that DID get written along the way still cost their LLM
    calls — see the module docstring for why this is intentional.

    v0.14.8 — for slots declared as ``taxonomy_relevant_slots`` on
    the pattern's schema (requires both ``taxonomy_oracle`` and
    ``registry`` to be passed), the function consults
    ``entity_taxonomy(part_of)`` BEFORE ``entity_equivalence``. When
    the taxonomy verdict is ``child_subsumed_by_parent`` or
    ``parent_subsumed_by_child``, the slot values are in a
    containment relation (Williamstown ↔ Massachusetts, etc.) — NOT
    aliases — and the candidate doesn't qualify here; derivation
    will compose the containment chain via predicate_distribution.
    """
    out: list[tuple[Fact, list[int]]] = []

    # v0.14.8 — taxonomy-relevant slots from the pattern schema.
    # Requires both registry and taxonomy_oracle; otherwise the
    # taxonomy short-circuit is disabled (back-compat).
    taxonomy_relevant: frozenset[str] = frozenset()
    if registry is not None and taxonomy_oracle is not None:
        try:
            if registry.has(pattern):
                taxonomy_relevant = frozenset(
                    registry.get(pattern).taxonomy_relevant_slots
                )
        except Exception:
            taxonomy_relevant = frozenset()

    # All currently-valid user-asserted facts under (pattern), with
    # NO identity-slot filter. The exact-identity candidates from
    # step 2 will reappear here; we filter them out below by
    # detecting "all slots literally equal" and skipping (they were
    # already handled). Session-locality filter delegated to
    # ``find_currently_valid``: a session-local fact in a different
    # session never reaches this loop, so the entity oracle is never
    # consulted on it (Phase 6 cost reduction over Phase 4-5).
    all_candidates = [
        f for f in store.find_currently_valid(
            pattern, user_id=user_id,
            current_session=current_session,
        )
        if f.asserted_by == "user"
        and f.verification_status == "user_asserted"
    ]

    for candidate in all_candidates:
        candidate_slots = candidate.slots or {}

        # v0.14.4 — relevance gate. Skip the candidate entirely (no
        # entity_equivalence consultation, no row write, no warm-cache
        # accumulation) when its slot values + source_text share no
        # tokens with the active verification context. Fires only when
        # active_context_tokens is non-empty (back-compat: callers
        # without context get unchanged behavior).
        if active_context_tokens:
            cand_tokens = _candidate_tokens(
                list(candidate_slots.values()),
                source_text=candidate.source_text,
            )
            if not _is_candidate_relevant(active_context_tokens, cand_tokens):
                continue

        entity_row_ids: list[int] = []
        all_qualify = True
        any_via_oracle = False

        for slot_name in key_slot_names:
            cv = claim_key_slots.get(slot_name)
            sv = candidate_slots.get(slot_name)
            if cv is None or sv is None:
                # Missing identity slot — candidate doesn't share
                # the same identity shape. Skip.
                all_qualify = False
                break
            if cv == sv:
                continue  # literal slot match — no oracle call
            if isinstance(cv, str) and isinstance(sv, str) \
                    and is_user(cv) and is_user(sv):
                continue  # both lexical user — no oracle call
            if not isinstance(cv, str) or not isinstance(sv, str):
                # Non-string slot values aren't entity-comparable
                # under entity_equivalence. Conservative fall-
                # through: don't qualify.
                all_qualify = False
                break

            # v0.14.8 — taxonomy-aware short-circuit. For slots the
            # pattern declared as taxonomy_relevant, consult
            # entity_taxonomy(part_of) FIRST. Containment pairs
            # (Williamstown ↔ Massachusetts, the Berkshires ↔
            # Massachusetts) record taxonomy rows instead of polluting
            # entity_equivalence with "different" verdicts; derivation
            # composes the containment chain via predicate_distribution.
            if slot_name in taxonomy_relevant:
                tax_verdict = _resolve_entity_taxonomy(
                    taxonomy_oracle, cv, sv, "part_of",
                    llm=llm, source_turn_id=source_turn_id,
                )
                if (
                    tax_verdict is not None
                    and not tax_verdict.classification_failed
                    and tax_verdict.label in _TAXONOMY_CONTAINMENT_LABELS
                ):
                    # Containment, not alias. Don't qualify here;
                    # derivation will compose the chain.
                    all_qualify = False
                    break
                if (
                    tax_verdict is not None
                    and not tax_verdict.classification_failed
                    and tax_verdict.label == "equivalent"
                ):
                    # Rare taxonomy "equivalent" verdict — treat as
                    # alias match via the taxonomy row.
                    if tax_verdict.row_id is not None:
                        entity_row_ids.append(tax_verdict.row_id)
                    any_via_oracle = True
                    continue
                # tax_verdict is None / classification_failed /
                # label == "neither" → fall through to
                # entity_equivalence below.

            verdict = _resolve_entity_equivalence(
                entity_oracle, cv, sv,
                llm=llm, source_turn_id=source_turn_id,
            )
            if verdict is None:
                # Cold cell, no LLM → conservative: this candidate
                # doesn't qualify under lookup-first contract.
                all_qualify = False
                break
            if verdict.classification_failed or verdict.label != "same":
                all_qualify = False
                break
            if verdict.row_id is not None:
                entity_row_ids.append(verdict.row_id)
            any_via_oracle = True

        if all_qualify and any_via_oracle:
            out.append((candidate, entity_row_ids))

    return out


# ---- predicate-equivalence verdict resolution ----------------------------


def _resolve_via_predicate_verdict(
    verdict: PredicateEquivalenceVerdict,
    *,
    claim_polarity: int,
    candidate_polarity: int,
) -> Optional[tuple[str, bool, bool]]:
    """Map a predicate_equivalence verdict to a tier-U outcome.

    Returns ``(kind, polarity_flipped, slot_reversal_applied)`` or
    None on no-signal.

    Polarity logic for the consumed cases:
      * equivalent      → match iff polarities are equal; else
                          contradiction. No flip applied either way.
      * contradictory   → match iff polarities DIFFER (the implicit
                          polarity flip lines them up — cheetahs
                          case); contradiction iff polarities are
                          EQUAL (both predicates assert the same
                          polarity of contradictory propositions).

    No signal:
      * classification_failed
      * label == 'distinct'
      * slot_reversal != 'none' — Phase 4 doesn't consume slot
        transformations; defer to Phase 4/7.
    """
    if verdict.classification_failed or verdict.label is None:
        return None
    if verdict.label == "distinct":
        return None
    if verdict.slot_reversal != "none":
        return None

    if verdict.label == "equivalent":
        if claim_polarity == candidate_polarity:
            return ("match", False, False)
        return ("contradiction", False, False)

    if verdict.label == "contradictory":
        if claim_polarity != candidate_polarity:
            return ("match", True, False)
        return ("contradiction", True, False)

    return None  # belt-and-braces; LABELS is closed.


# ============================================================================
# Phase 6 — storage path
# ============================================================================
#
# ``store_user_fact`` is the entry point for user-authoritative claim
# storage under the v0.14 session model. It decides between session-
# local and cross-session storage based on the source-text marker
# (via ``find_session_marker``) plus the active session, then either
# inserts a new fact or reaffirms an existing one.
#
# Phase 6 deliberately does NOT close opposite-polarity prior facts
# — that contradiction handling lives in Phase 7+'s orchestrator.
# A user who says "I love olives" then "I don't love olives" through
# this storage path produces two coexisting rows of opposite polarity.
# The lookup-side CONTRADICTION outcome surfaces the conflict to the
# operator without forcing a resolution at storage time.


class StoreUserFactOutcome(str, Enum):
    """Three outcomes from a user-authoritative storage call.

    Mirrors the principle-3 discipline: ``INSERTED`` and
    ``REAFFIRMED`` are the only outcomes that change counts;
    ``NOOP`` covers same-session repetition (cross-session and
    session-local both) plus the no-active-session-but-existing-
    match case (where we cannot deduplicate).
    """

    INSERTED = "inserted"
    REAFFIRMED = "reaffirmed"
    NOOP = "noop"


@dataclass(frozen=True)
class StoreUserFactResult:
    """The result of a ``store_user_fact`` call.

    ``outcome`` says what happened. ``fact_id`` is the row's id
    (the new row on INSERTED, the existing row on REAFFIRMED /
    NOOP). The ``*_after`` fields reflect the row's state AFTER the
    call — the same value as the underlying ``Fact`` row would
    show.

    ``marker_detected_phrase`` is the matched session-marker text
    when one fired (None otherwise). The pipeline event also
    surfaces this; the trace UI uses it to explain why a fact was
    stored as session-local.
    """

    outcome: StoreUserFactOutcome
    fact_id: int
    is_session_local: int
    session_ids_after: list[str]
    affirmed_count_after: int
    confidence_after: float
    marker_detected_phrase: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "fact_id": self.fact_id,
            "is_session_local": self.is_session_local,
            "session_ids_after": list(self.session_ids_after),
            "affirmed_count_after": self.affirmed_count_after,
            "confidence_after": self.confidence_after,
            "marker_detected_phrase": self.marker_detected_phrase,
            "notes": list(self.notes),
        }


def store_user_fact(
    claim: dict,
    store: FactStore,
    *,
    current_session: Optional[str],
    key_slot_names: list[str],
    user_id: str = DEFAULT_USER_ID,
    source_turn_id: Optional[int] = None,
    raw_text: Optional[str] = None,
) -> StoreUserFactResult:
    """Store a user-authoritative claim under the session model.

    Decides between session-local and cross-session storage based on
    a session-marker check plus ``current_session``:

      * marker present + active session → session-local. Same-session
        repetition is NOOP; otherwise INSERTED with
        ``session_ids=[current_session]``, ``is_session_local=1``.
      * marker present + no active session → cross-session (the
        marker is recorded in the event payload but ignored for
        storage; we have nowhere to scope a session-local without
        a session).
      * no marker → cross-session. Same-session repetition (or no-
        active-session repetition) is NOOP; new-session reaffirmation
        appends to ``session_ids`` and increments ``affirmed_count``;
        first assertion is INSERTED with
        ``session_ids=[current_session]`` (or ``[]`` if no session).

    Counts:
      * INSERTED produces ``affirmed_count=1``. The first assertion
        IS the first independent external evidence event under
        principle 3.
      * REAFFIRMED increments by 1 (atomic single-UPDATE via
        ``FactStore.reaffirm_cross_session``).
      * NOOP leaves counts unchanged.

    Phase 6 scope: contradiction handling (closing opposite-polarity
    priors) is NOT done here. The lookup-side CONTRADICTION outcome
    surfaces those to the operator; Phase 7+'s orchestrator decides
    how to react.

    **v0.14 Phase 8.6 — raw_text parameter.** Marker detection runs on
    ``raw_text`` (the original turn utterance) when provided, falling
    back to ``claim['source_text']`` when not. Pre-Phase-8.6 the marker
    check ran only on ``source_text``, which the extractor often strips
    of the marker phrase ("Let's say for this conversation I live in
    Williamsburg" → source_text "I live in Williamsburg"); the storage
    path then defaulted to cross-session even when the user explicitly
    bounded the assertion. Phase 9's chat endpoint will pass raw_text
    from the user turn; current callers (tests, dispatch-one) can pass
    raw_text explicitly or rely on the fallback when ``source_text``
    still contains the marker.
    """
    pattern = claim.get("pattern", "")
    predicate = claim.get("predicate", "")
    polarity = int(claim.get("polarity"))
    slots = claim.get("slots") or {}
    source_text = claim.get("source_text") or ""
    marker_text = raw_text if raw_text is not None else source_text
    key_slots = {k: slots[k] for k in key_slot_names if k in slots}

    matched_phrase = find_session_marker(marker_text)
    marker_present = matched_phrase is not None
    use_session_local = marker_present and current_session is not None
    marker_ignored_no_session = marker_present and current_session is None

    if use_session_local:
        return _store_session_local(
            store, pattern, predicate, polarity, slots, key_slots,
            source_text, current_session, user_id, source_turn_id,
            matched_phrase,
        )
    return _store_cross_session(
        store, pattern, predicate, polarity, slots, key_slots,
        source_text, current_session, user_id, source_turn_id,
        matched_phrase, marker_ignored_no_session,
    )


# ---- session-local storage -----------------------------------------------


def _store_session_local(
    store: FactStore,
    pattern: str, predicate: str, polarity: int,
    slots: dict, key_slots: dict,
    source_text: str,
    current_session: str,
    user_id: str,
    source_turn_id: Optional[int],
    matched_phrase: str,
) -> StoreUserFactResult:
    """Marker present + active session. Look for a session-local
    match in ``current_session``; NOOP if found, INSERT otherwise.
    """
    candidates = store.find_currently_valid(
        pattern, predicate=predicate, slot_match=key_slots,
        polarity=polarity, user_id=user_id,
        current_session=current_session,
    )
    matching = [
        f for f in candidates
        if f.is_session_local == 1
        and f.asserted_by == "user"
        and f.verification_status == "user_asserted"
        and current_session in f.session_ids
    ]
    if matching:
        existing = matching[0]
        result = _make_noop_result(
            existing, matched_phrase,
            note="session-local same-session repetition; no count change",
        )
        _emit_storage_event(
            store, source_turn_id, result, current_session,
            marker_ignored_no_session=False,
        )
        return result

    initial_conf = confidence_from_counts(1, 0)
    fact_id = store.insert_fact(Fact(
        pattern=pattern, predicate=predicate, slots=dict(slots),
        polarity=polarity,
        asserted_by="user", verification_status="user_asserted",
        is_session_local=1,
        session_ids=[current_session],
        affirmed_count=1, contradicted_count=0,
        confidence=initial_conf,
        source_turn_id=source_turn_id,
        source_text=source_text or None,
        user_id=user_id,
    ))
    result = StoreUserFactResult(
        outcome=StoreUserFactOutcome.INSERTED,
        fact_id=fact_id,
        is_session_local=1,
        session_ids_after=[current_session],
        affirmed_count_after=1,
        confidence_after=initial_conf,
        marker_detected_phrase=matched_phrase,
        notes=[
            f"new session-local fact in session {current_session!r}; "
            f"marker={matched_phrase!r}"
        ],
    )
    _emit_storage_event(
        store, source_turn_id, result, current_session,
        marker_ignored_no_session=False,
    )
    return result


# ---- cross-session storage -----------------------------------------------


def _store_cross_session(
    store: FactStore,
    pattern: str, predicate: str, polarity: int,
    slots: dict, key_slots: dict,
    source_text: str,
    current_session: Optional[str],
    user_id: str,
    source_turn_id: Optional[int],
    matched_phrase: Optional[str],
    marker_ignored_no_session: bool,
) -> StoreUserFactResult:
    """Marker absent, OR marker present but no active session. Look
    for a cross-session match (current_session=None on the query
    restricts to is_session_local=0); NOOP / REAFFIRMED / INSERTED
    based on what's found.
    """
    candidates = store.find_currently_valid(
        pattern, predicate=predicate, slot_match=key_slots,
        polarity=polarity, user_id=user_id,
        current_session=None,  # cross-session candidates only
    )
    matching = [
        f for f in candidates
        if f.is_session_local == 0
        and f.asserted_by == "user"
        and f.verification_status == "user_asserted"
    ]
    if matching:
        existing = matching[0]
        if current_session is None:
            # Cannot tell same-session from new-session without a
            # session id. Conservative reading: treat as no new
            # evidence. Principle 3 wins ties.
            result = _make_noop_result(
                existing, matched_phrase,
                note=(
                    "cross-session match found but current_session=None; "
                    "cannot deduplicate, no count change"
                ),
            )
            _emit_storage_event(
                store, source_turn_id, result, current_session,
                marker_ignored_no_session,
            )
            return result
        if current_session in existing.session_ids:
            result = _make_noop_result(
                existing, matched_phrase,
                note=(
                    f"same-session repetition in {current_session!r}; "
                    f"no count change"
                ),
            )
            _emit_storage_event(
                store, source_turn_id, result, current_session,
                marker_ignored_no_session,
            )
            return result
        # New-session reaffirmation. Atomic append + increment.
        new_count, new_sessions, new_conf = store.reaffirm_cross_session(
            existing.id, current_session,
        )
        result = StoreUserFactResult(
            outcome=StoreUserFactOutcome.REAFFIRMED,
            fact_id=existing.id,
            is_session_local=0,
            session_ids_after=new_sessions,
            affirmed_count_after=new_count,
            confidence_after=new_conf,
            marker_detected_phrase=matched_phrase,
            notes=[
                f"cross-session reaffirmation in new session "
                f"{current_session!r}; affirmed_count "
                f"{existing.affirmed_count} -> {new_count}"
            ],
        )
        _emit_storage_event(
            store, source_turn_id, result, current_session,
            marker_ignored_no_session,
        )
        return result

    # Fresh insert.
    initial_sessions = (
        [current_session] if current_session is not None else []
    )
    initial_conf = confidence_from_counts(1, 0)
    fact_id = store.insert_fact(Fact(
        pattern=pattern, predicate=predicate, slots=dict(slots),
        polarity=polarity,
        asserted_by="user", verification_status="user_asserted",
        is_session_local=0,
        session_ids=list(initial_sessions),
        affirmed_count=1, contradicted_count=0,
        confidence=initial_conf,
        source_turn_id=source_turn_id,
        source_text=source_text or None,
        user_id=user_id,
    ))
    note = "new cross-session fact"
    if marker_ignored_no_session:
        note = (
            f"new cross-session fact (marker {matched_phrase!r} "
            f"detected but ignored: no active session)"
        )
    result = StoreUserFactResult(
        outcome=StoreUserFactOutcome.INSERTED,
        fact_id=fact_id,
        is_session_local=0,
        session_ids_after=list(initial_sessions),
        affirmed_count_after=1,
        confidence_after=initial_conf,
        marker_detected_phrase=matched_phrase,
        notes=[note],
    )
    _emit_storage_event(
        store, source_turn_id, result, current_session,
        marker_ignored_no_session,
    )
    return result


# ---- helpers --------------------------------------------------------------


def _make_noop_result(
    existing: Fact,
    matched_phrase: Optional[str],
    *,
    note: str,
) -> StoreUserFactResult:
    """Build a NOOP result from the existing row's current state."""
    assert existing.id is not None
    return StoreUserFactResult(
        outcome=StoreUserFactOutcome.NOOP,
        fact_id=existing.id,
        is_session_local=existing.is_session_local,
        session_ids_after=list(existing.session_ids),
        affirmed_count_after=existing.affirmed_count,
        confidence_after=existing.confidence,
        marker_detected_phrase=matched_phrase,
        notes=[note],
    )


def _emit_storage_event(
    store: FactStore,
    source_turn_id: Optional[int],
    result: StoreUserFactResult,
    current_session: Optional[str],
    marker_ignored_no_session: bool,
) -> None:
    """Emit the ``tier_u_storage`` pipeline event with the result
    payload plus storage-decision context (current_session, marker
    flags). Logging is best-effort — exceptions in event emission
    must not crash the storage path."""
    payload = result.to_dict()
    payload["current_session"] = current_session
    payload["marker_ignored_no_session"] = marker_ignored_no_session
    _safe_emit_event(store, source_turn_id, "tier_u_storage", payload)
