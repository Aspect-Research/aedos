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
    #
    # Phase H D51 (2026-05-24): the `direction` parameter selects which
    # SPARQL direction the enumeration traverses.
    #   - "outgoing" (default; the v0.15 D5 shape): wd:E ?prop ?value —
    #     returns E's *parents* / containers (Williamstown's P361 → its
    #     containing entities). Serves the walker's `parent` direction.
    #   - "incoming": ?value ?prop wd:E — returns E's *children*
    #     (Williamstown is part_of Massachusetts; reverse of Massachusetts
    #     returns Williamstown, Boston, Cambridge, …). Serves the walker's
    #     `child` direction. Reverse enumeration is implementation-bounded
    #     by a `LIMIT` clause (default 100) to keep unbounded properties
    #     like P17=Q30 manageable.
    def enumerate_neighbors(
        self,
        entity: KBEntityID,
        properties: list[KBPropertyID],
        direction: str = "outgoing",
    ) -> dict[KBPropertyID, list[KBEntityID]]: ...

    # v0.16 WS1: two ontology/label operations supporting multi-property
    # binding discovery (PropertyRelations) and the WS5 correction surface.
    # Both are OPTIONAL on the protocol — consumers call them via `getattr`
    # so stub adapters that predate v0.16 keep satisfying KBProtocol — and
    # both FAIL OPEN by contract.
    #
    # `fetch_property_ontology(prop)` returns the property's Wikidata
    # constraint/relation ontology as a dict with keys subject_type_qids,
    # value_type_qids, inverse_pids, subproperty_pids, related_pids,
    # single_valued. On any error / non-P-id / no constraints it returns an
    # EMPTY ontology (all-empty lists, single_valued=False); it NEVER raises.
    # Discovery is additive enrichment — an empty ontology falls the caller
    # back to the oracle's primary binding (current behavior).
    def fetch_property_ontology(self, prop: KBPropertyID) -> dict: ...

    # `fetch_label(qid)` returns the entity's English label, or None on any
    # error / non-Q-id / missing label. NEVER raises.
    def fetch_label(self, qid: KBEntityID) -> Optional[str]: ...
