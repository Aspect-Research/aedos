from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class TraceNode:
    node_type: str  # 'claim' | 'kb_statement' | 'tier_u_row' | 'python_result'
    content: dict = field(default_factory=dict)


@dataclass
class TraceEdge:
    edge_type: str  # premise_lookup | predicate_equivalence | entity_equivalence | subsumption_traversal
    source: TraceNode
    target: TraceNode
    metadata: dict = field(default_factory=dict)


# v0.16 WS3: Semiring-style provenance literal: one grounded premise the
# verdict rests on. `table`/`row_id` is the retractable substrate/Tier U row
# (None for transient sources e.g. live KB statements with no cached row);
# `source` in {tier_u, kb, python, subsumption, predicate_translation,
# entity_resolution}; `status` carries the Tier U premise status when
# source=='tier_u' (asserted_unverified | externally_verified | ...), else
# None. `assertion` is True iff this literal makes the verdict
# assertion-conditional (an asserted_unverified Tier U premise, or the
# Q-UserAuth pre-seed).
@dataclass(frozen=True)
class ProvenanceLiteral:
    source: str
    table: Optional[str] = None
    row_id: Optional[int] = None
    status: Optional[str] = None
    assertion: bool = False


# v0.16 WS3: AND/OR provenance term. `op` in {'lit','and','or'}. A 'lit' node
# wraps one ProvenanceLiteral; 'and'/'or' nodes combine children. The walker
# composes a term per claim: each grounded premise contributes one
# alternative (OR across independent grounding chains found in one walk), and
# a multi-hop chain ANDs its hops. Lazy: built only while the walk runs,
# discarded with the trace at session end.
@dataclass
class ProvenanceTerm:
    op: str = "or"                       # default: OR over alternatives
    literal: Optional[ProvenanceLiteral] = None
    children: list["ProvenanceTerm"] = field(default_factory=list)

    @classmethod
    def lit(cls, literal: ProvenanceLiteral) -> "ProvenanceTerm":
        return cls(op="lit", literal=literal)

    def add_alternative(self, term: "ProvenanceTerm") -> None:
        """OR a fresh grounding alternative into this (root) term."""
        self.children.append(term)

    def literals(self) -> list[ProvenanceLiteral]:
        if self.op == "lit" and self.literal is not None:
            return [self.literal]
        out: list[ProvenanceLiteral] = []
        for c in self.children:
            out.extend(c.literals())
        return out

    def includes_assertion(self) -> bool:
        """True iff ANY literal on the term is assertion-conditional.
        chain_includes_assertion derives from this (monotone-OR over
        literals, matching the legacy boolean's monotonic semantics)."""
        return any(l.assertion for l in self.literals())

    def source_rows(self) -> list[tuple[str, int]]:
        """Distinct (table,row_id) pairs across all literals — the
        retraction dependency footprint. Mirrors
        aggregator._extract_source_rows but sourced from the term rather
        than re-scanning edges."""
        seen: set[tuple[str, int]] = set()
        rows: list[tuple[str, int]] = []
        for l in self.literals():
            if l.table is not None and l.row_id is not None and (l.table, l.row_id) not in seen:
                seen.add((l.table, l.row_id))
                rows.append((l.table, l.row_id))
        return rows


@dataclass
class JustificationTrace:
    root: TraceNode
    edges: list[TraceEdge] = field(default_factory=list)
    polarity_trace: list[int] = field(default_factory=list)
    source_breakdown: dict = field(default_factory=dict)  # tier_u | kb | python counts
    walk_metadata: dict = field(default_factory=dict)  # depth, llm_calls, wall_clock_ms
    # Phase H Cluster 2 step 1: set True (monotonically) when any premise on
    # the derivation chain was an `asserted_unverified` Tier U row. The
    # aggregator reads this flag and converts a base verdict to its
    # `*_given_assertion` variant. Individual contributing edges carry
    # `metadata['premise_status']` for fine-grained audit; this flag is
    # the aggregated signal used at verdict-designation time. (Walker
    # step 3 sets this; the field exists in step 1 so the schema is
    # stable across the cluster.)
    #
    # v0.16 WS3 (Phase 1, additive): kept as a normal settable field for now.
    # A later phase makes it a derived read-only property over `provenance`;
    # in this foundation phase the walker still writes it directly.
    chain_includes_assertion: bool = False
    # v0.16 WS3: lazy AND/OR provenance term. A later phase has the walker
    # populate it as edges are appended and derives chain_includes_assertion
    # from it. In this foundation phase the field exists (default empty term)
    # so the schema is stable across the workstream.
    provenance: ProvenanceTerm = field(default_factory=ProvenanceTerm)


def trace_to_json(trace: JustificationTrace) -> dict:
    """Serialize a JustificationTrace to a JSON-compatible dict."""
    def _node(n: TraceNode) -> dict:
        return {"node_type": n.node_type, "content": n.content}

    def _edge(e: TraceEdge) -> dict:
        return {
            "edge_type": e.edge_type,
            "source": _node(e.source),
            "target": _node(e.target),
            "metadata": e.metadata,
        }

    def _prov(t: ProvenanceTerm) -> dict:
        if t.op == "lit" and t.literal is not None:
            return {"op": "lit", "literal": asdict(t.literal)}
        return {"op": t.op, "children": [_prov(c) for c in t.children]}

    return {
        "root": _node(trace.root),
        "edges": [_edge(e) for e in trace.edges],
        "polarity_trace": trace.polarity_trace,
        "source_breakdown": trace.source_breakdown,
        "walk_metadata": trace.walk_metadata,
        "chain_includes_assertion": trace.chain_includes_assertion,
        "provenance": _prov(trace.provenance),
    }
