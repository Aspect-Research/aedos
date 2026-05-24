from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

KBEntityID = str
KBPropertyID = str


@dataclass
class LocalContext:
    predicate: str
    slot_position: str  # 'subject' or 'object'
    asserting_party: Optional[str] = None
    prior_resolutions: list["ResolutionCandidate"] = field(default_factory=list)
    # Phase G D33 (2026-05-23): Wikidata Q-ids of acceptable entity types for
    # the slot being resolved. When non-empty, _live_resolve post-filters
    # candidates whose P31 intersects this list. Empty/absent means no filter.
    expected_entity_types: list["KBEntityID"] = field(default_factory=list)
    # Phase H D47 (2026-05-23): context for the Wikipedia normalizer's Stage 2
    # LLM-mediated selection. `source_text` is the full input text the
    # extractor saw (request-scoped, not stored on Claim). The three claim_*
    # fields surface the immediate claim's slot values for the Stage 2 prompt.
    # All optional; absence falls through to Stage 2's abstention bias.
    source_text: Optional[str] = None
    claim_subject: Optional[str] = None
    claim_predicate: Optional[str] = None
    claim_object: Optional[str] = None
    claim_id: Optional[str] = None


@dataclass
class ResolutionCandidate:
    kb_identifier: KBEntityID
    provenance: dict = field(default_factory=dict)
    score: float = 0.0


@dataclass
class Statement:
    value: Any
    value_type: str  # entity | literal | date | quantity
    qualifiers: dict = field(default_factory=dict)
    rank: str = "normal"  # preferred | normal | deprecated
    provenance: dict = field(default_factory=dict)


@dataclass
class SubsumptionResult:
    verdict: str  # a_subsumed_by_b | b_subsumed_by_a | equivalent | unrelated
    establishing_property: Optional[str] = None
    traversal_chain: list[KBEntityID] = field(default_factory=list)


@runtime_checkable
class KBProtocol(Protocol):
    def resolve_entity(
        self, reference: str, local_context: LocalContext
    ) -> list[ResolutionCandidate]: ...

    def lookup_statements(
        self, entity: KBEntityID, predicate: KBPropertyID
    ) -> list[Statement]: ...

    def subsumption(
        self, entity_a: KBEntityID, entity_b: KBEntityID, relation_type: str
    ) -> SubsumptionResult: ...

    # Phase H D5 (2026-05-23): a fourth operation. The first three check or
    # fetch for a *known* entity/pair; `enumerate_neighbors` discovers an
    # entity's KB neighbors along a constrained property set, so the walker
    # can ground a derivation in KB-sourced premises it didn't already have.
    # Per `docs/phase_H/d5_design.md`: the property set is bounded (Decision
    # 1 — 5-property geographic/taxonomic core), depth is one-hop-per-call
    # (Decision 2 — walker recurses via existing `max_depth`), failures
    # fail-open (return empty dict).
    def enumerate_neighbors(
        self,
        entity: KBEntityID,
        properties: list[KBPropertyID],
    ) -> dict[KBPropertyID, list[KBEntityID]]: ...
