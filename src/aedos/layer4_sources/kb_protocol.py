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
    # Wikidata Q-ids of acceptable entity types for
    # the slot being resolved. When non-empty, _live_resolve post-filters
    # candidates whose P31 intersects this list. Empty/absent means no filter.
    expected_entity_types: list["KBEntityID"] = field(default_factory=list)
    # Context for the Wikipedia normalizer's Stage 2
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


# v0.16 WS2 §1: result of the first-class transitive-path primitive
# `verify_transitive_path`. Unlike `SubsumptionResult` (symmetric,
# four-verdict, both-direction), this is a SINGLE-direction path-existence
# answer the walker's discover/verify can drive for ANY transitive KB
# property. `holds` is the ASK boolean; `error` non-None means the lookup
# failed and `holds` was forced False (fail-open per architecture §3.1 —
# a transitive-path miss must abstain, never false-verify).
@dataclass
class TransitivePathResult:
    holds: bool  # ASK boolean (path source -> target exists)
    establishing_property: Optional[str] = None  # depth-1 anchor (observability)
    error: Optional[str] = None  # non-None => fail-open (holds=False)


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

    # A fourth operation. The first three check or
    # fetch for a *known* entity/pair; `enumerate_neighbors` discovers an
    # entity's KB neighbors along a constrained property set, so the walker
    # can ground a derivation in KB-sourced premises it didn't already have.
    # The property set is bounded (a 5-property geographic/taxonomic core),
    # depth is one-hop-per-call (the walker recurses via existing
    # `max_depth`), and failures fail-open (return empty dict).
    #
    # The `direction` parameter selects which
    # SPARQL direction the enumeration traverses.
    #   - "outgoing" (default): wd:E ?prop ?value —
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

    # v0.16 WS2 §1: the first-class transitive-path primitive. Where
    # `subsumption` is hardwired to the `relation_type -> _SUBSUMPTION_PROPERTIES`
    # alternation and always runs BOTH directions + an establishing-property
    # SELECT, `verify_transitive_path` is a SINGLE-direction, SINGLE-property
    # (or relation-alternation) path-existence check the walker's
    # discover/verify can drive for ANY transitive KB property (not just
    # is_a/part_of) — e.g. P171 (parent taxon), P127 (owned by).
    #
    #   - `relation_type` supplied: reuse the curated `_SUBSUMPTION_PROPERTIES`
    #     alternation (+ the type-guarded P361 bridge for part_of), so the
    #     walker's is_a/part_of hops share the verifier's entailment-correct
    #     query. `kb_property` is ignored in this branch.
    #   - `relation_type` None: build a single-property `(wdt:{kb_property})+`
    #     path — the generic transitive case.
    #
    # FAIL-OPEN: any error (timeout/network/malformed) returns
    # `TransitivePathResult(holds=False, error=...)`; the primitive NEVER
    # raises on a lookup failure (a transitive-path miss abstains, per §3.1).
    def verify_transitive_path(
        self,
        source: KBEntityID,
        target: KBEntityID,
        kb_property: KBPropertyID,
        relation_type: Optional[str] = None,
        *,
        exception_cache=None,
    ) -> "TransitivePathResult": ...

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
