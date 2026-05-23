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
