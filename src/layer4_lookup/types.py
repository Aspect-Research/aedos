"""Public types for Layer 4 lookup (v0.14 Phase 7).

Layer 4's tiers (Tier U, Tier W, derivation, fresh) all return shape-
specific result objects, but the walker that composes them produces
a single unified ``WalkerDecision`` that flows to Layer 5 in Phase 8.
This module owns those shapes.

Design notes
============

  * **LookupOutcome** is the orthogonal concept to ``verification_status``.
    LookupOutcome answers "what was the lookup result for the current
    claim?" — MATCH / CONTRADICTION / MISS. verification_status answers
    "what kind of evidence does the matching fact carry?" — verified,
    user_asserted, retrieval_inconclusive, etc. Layer 5 reads both:
    outcome decides whether intervention is needed; status drives
    soften/hedge/replace.

  * **WalkerDecision** is its own dataclass, NOT an extension of Layer
    2's ``Decision``. The two layers have different responsibilities:
    Layer 2 classifies; Layer 4 resolves. The ``routing_method`` field
    on WalkerDecision carries the Layer 2 method through to Layer 5,
    populated regardless of which tier resolved (Layer 2 ran first, so
    routing_method is always set).

  * **ChainEdge** records one step in a derivation chain. Each edge
    names the substrate oracle consulted, the row id, the label, and
    the row's confidence at consultation time. The walker accumulates
    edges as the BFS explores; chain_reliability is min-link across all
    edges in the chain.

  * **TierWResult** mirrors ``TierUResult`` in shape (literal/predicate-
    equivalence/alias-identity stages) but additionally carries the
    cache row's verification_status and evidence.

  * **DerivationResult** is what the derivation BFS returns to the
    walker. The walker wraps it into a WalkerDecision when used.

The migration-style note for ``verdict`` column reuse (Ambiguity #2 of
the Phase 7 plan): legacy v1 cache rows wrote one of three values to
``verdict`` (verified, contradicted, inconclusive). v2 widens the
column to carry the full 8-state ``verification_status`` enum. The v2
DB resets cleanly, so no actual migration is required; the legacy
values are a strict subset of the new domain. See ``tier_w.py``'s
docstring for the migration-helper-as-documentation pattern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class LookupOutcome(str, Enum):
    """The lookup outcome at any tier (U, W, derivation).

    Renamed from ``WalkerOutcome`` per the Phase 7 plan refinement: the
    enum describes the lookup result, not the walker's behavior. Future
    layer-4 modes (none planned, but keeping the namespace clean)
    won't conflict.
    """

    MATCH = "match"
    CONTRADICTION = "contradiction"
    MISS = "miss"


@dataclass(frozen=True)
class ChainEdge:
    """One step in a derivation chain.

    The chain reads from the model claim's starting state to whatever
    fact in U or W the walker eventually matched. Each edge names the
    substrate transition that took the walker from one state to the
    next.

    ``confidence`` is the row's Beta posterior at the moment the edge
    was traversed. The walker uses the minimum across all edges as
    the chain's reliability (min-link).
    """

    oracle: str          # 'entity_taxonomy' | 'predicate_distribution' | ...
    row_id: Optional[int]  # None when the edge is a virtual "literal step"
    label: str           # the row's label (e.g. 'child_subsumed_by_parent')
    confidence: float    # Beta posterior at consultation time
    from_state: dict[str, Any] = field(default_factory=dict)  # serialized state before
    to_state: dict[str, Any] = field(default_factory=dict)    # after
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "oracle": self.oracle,
            "row_id": self.row_id,
            "label": self.label,
            "confidence": self.confidence,
            "from_state": dict(self.from_state),
            "to_state": dict(self.to_state),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class TierWResult:
    """Result of a Tier W lookup.

    Mirrors ``TierUResult`` in shape: literal-match → predicate-
    equivalence broadening → alias-identity broadening, with the
    ``via`` list ordered by oracle consultation. Additionally carries
    the cache row's ``verification_status`` (one of the 8 enum values)
    and ``evidence`` (the verifier's snippets / generated code / etc.)
    for trace UI rendering.

    On MISS, every other field is None / empty.
    """

    outcome: LookupOutcome
    matching_row_id: Optional[int] = None
    contradicting_row_id: Optional[int] = None
    matching_canonical_key: Optional[str] = None
    contradicting_canonical_key: Optional[str] = None
    verification_status: Optional[str] = None  # 8-state value, when matched
    evidence: Optional[dict[str, Any]] = None
    expires_at: Optional[str] = None
    via: list[str] = field(default_factory=list)
    predicate_equivalence_row_id: Optional[int] = None
    entity_equivalence_row_ids: list[int] = field(default_factory=list)
    polarity_flipped: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "matching_row_id": self.matching_row_id,
            "contradicting_row_id": self.contradicting_row_id,
            "matching_canonical_key": self.matching_canonical_key,
            "contradicting_canonical_key": self.contradicting_canonical_key,
            "verification_status": self.verification_status,
            "evidence": self.evidence,
            "expires_at": self.expires_at,
            "via": list(self.via),
            "predicate_equivalence_row_id": self.predicate_equivalence_row_id,
            "entity_equivalence_row_ids": list(self.entity_equivalence_row_ids),
            "polarity_flipped": self.polarity_flipped,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class DerivationResult:
    """Result of a derivation BFS walk (Phase 7c).

    ``outcome`` is MATCH (chain found, supports the claim) or MISS
    (no chain ≥ floor; or every branch exhausted without a U/W match).

    ``chain`` is the ordered list of ``ChainEdge`` traversed. The
    chain's first edge starts from the claim's initial state and
    each subsequent edge transforms the state via one substrate
    transition. The final edge's to_state is what matched in U or W.

    ``chain_reliability`` is min over edges' confidences.

    ``matching_tier`` is 'u' or 'w' depending on where the chain
    landed. ``matching_fact_id`` (Tier U) or ``matching_w_row_id``
    (Tier W) name the specific row.

    ``abort_reason`` is None on MATCH; on MISS it explains why the
    walk gave up — 'depth' (every branch hit MAX_DEPTH), 'reliability'
    (every branch fell below floor), 'exhausted' (substrate offered
    no productive expansions), or 'classification_failed' (an oracle's
    LLM produced malformed output during the walk).
    """

    outcome: LookupOutcome
    chain: list[ChainEdge] = field(default_factory=list)
    chain_reliability: float = 0.0
    matching_fact_id: Optional[int] = None
    matching_w_row_id: Optional[int] = None
    matching_tier: Optional[str] = None  # 'u' | 'w' on MATCH; None on MISS
    explored_states: int = 0
    abort_reason: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "chain": [e.to_dict() for e in self.chain],
            "chain_reliability": self.chain_reliability,
            "matching_fact_id": self.matching_fact_id,
            "matching_w_row_id": self.matching_w_row_id,
            "matching_tier": self.matching_tier,
            "explored_states": self.explored_states,
            "abort_reason": self.abort_reason,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class WalkerDecision:
    """The unified Layer-4 output that Layer 5 consumes (Phase 8).

    The walker composes Tier U, Tier W, derivation, and fresh in that
    fall-through order. Whichever tier resolved the claim populates
    its tier-specific fields; the others stay None / empty.

    ``verification_status`` carries the 8-state architectural enum:

      * verified, contradicted (verifier produced a verdict)
      * user_asserted (Tier U match — fact came from the user)
      * unverifiable_in_principle (routing decided no method applies)
      * retrieval_inconclusive (verifier ran, evidence thin)
      * retrieval_failed (verifier broke; no evidence)
      * unverifiable_pending_implementation (verifier-side error)
      * routing_anomaly (Layer 2 validator rejected the claim)

    ``outcome`` is the orthogonal LookupOutcome — MATCH / CONTRADICTION
    / MISS. A Tier U row that contradicts the model claim has
    verification_status='user_asserted' AND outcome=CONTRADICTION;
    Layer 5 reads the contradiction signal and renders correction.

    ``routing_method`` is the Layer-2 routing classification (one of
    python, python_with_canonical_constants, retrieval,
    user_authoritative, unverifiable). Always populated by Layer 2,
    carried unchanged regardless of which tier resolved. Layer 5
    reads this to know what verifier *would* have run if fresh
    dispatched, even when an earlier tier resolved.
    """

    claim: dict[str, Any]
    served_from_tier: str  # 'u' | 'w' | 'derivation' | 'fresh' | 'routing_anomaly'
    outcome: LookupOutcome
    verification_status: str  # one of the 8 enum values
    routing_method: Optional[str] = None  # carried from Layer 2

    # Tier U fields
    matching_fact_id: Optional[int] = None
    contradicting_fact_id: Optional[int] = None

    # Tier W fields
    matching_w_row_id: Optional[int] = None
    contradicting_w_row_id: Optional[int] = None
    evidence: Optional[dict[str, Any]] = None

    # Derivation fields
    derivation_path: list[dict[str, Any]] = field(default_factory=list)
    chain_reliability: float = 1.0   # 1.0 for non-derivation tiers (no chain)

    # Substrate consultation trail (oracle names in consultation order)
    via: list[str] = field(default_factory=list)
    polarity_flipped: bool = False

    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": dict(self.claim),
            "served_from_tier": self.served_from_tier,
            "outcome": self.outcome.value,
            "verification_status": self.verification_status,
            "routing_method": self.routing_method,
            "matching_fact_id": self.matching_fact_id,
            "contradicting_fact_id": self.contradicting_fact_id,
            "matching_w_row_id": self.matching_w_row_id,
            "contradicting_w_row_id": self.contradicting_w_row_id,
            "evidence": self.evidence,
            "derivation_path": [dict(s) for s in self.derivation_path],
            "chain_reliability": self.chain_reliability,
            "via": list(self.via),
            "polarity_flipped": self.polarity_flipped,
            "notes": list(self.notes),
        }
