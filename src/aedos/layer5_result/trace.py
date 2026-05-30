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
    chain_includes_assertion: bool = False


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

    return {
        "root": _node(trace.root),
        "edges": [_edge(e) for e in trace.edges],
        "polarity_trace": trace.polarity_trace,
        "source_breakdown": trace.source_breakdown,
        "walk_metadata": trace.walk_metadata,
        "chain_includes_assertion": trace.chain_includes_assertion,
    }
