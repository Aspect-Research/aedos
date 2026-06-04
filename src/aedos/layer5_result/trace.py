from __future__ import annotations

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
# user-authoritative pre-seed).
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
    # v0.16 WS3 (§3A): lazy AND/OR provenance term. The walker populates it as
    # edges are appended (via Walker._record_premise). It is the source of
    # truth for assertion-conditionality; chain_includes_assertion is now a
    # DERIVED read-only property over it. Lazy/discard-per-session — only the
    # flattened (table,row_id) list is persisted (via verdict_recorded).
    provenance: ProvenanceTerm = field(default_factory=ProvenanceTerm)

    # True (monotonically) when any premise on the
    # derivation chain is assertion-conditional (an asserted_unverified Tier U
    # row, or the user-authoritative pre-seed). The aggregator reads this and converts
    # a base verdict to its `*_given_assertion` variant. Individual edges carry
    # `metadata['premise_status']` for fine-grained audit; this is the
    # aggregated signal used at verdict-designation time.
    #
    # v0.16 WS3 (§3A): now a DERIVED read-only property over `provenance`
    # (monotone-OR over its literals), reproducing the legacy boolean exactly.
    @property
    def chain_includes_assertion(self) -> bool:
        return self.provenance.includes_assertion()


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


def trace_to_json_lossless(
    trace: JustificationTrace, walk_result: Any = None
) -> dict:
    """`trace_to_json` PLUS the per-claim facts the trace itself does not carry.

    `trace_to_json` already serializes the JustificationTrace losslessly — root,
    edges with their full open `metadata` dicts (every key round-trips verbatim),
    `polarity_trace`, `source_breakdown`, `walk_metadata`, `chain_includes_assertion`,
    and the AND/OR `provenance` term. The two things it CANNOT carry live on the
    `WalkResult`, not the trace: `budget_consumption` (wall_clock_ms / llm_calls) and
    the walk-layer `abstention_reason`. Pass `walk_result` to fold them in for the
    durable audit record (GET /verification/{id}). §3.2-neutral — serialization only,
    no verdict path reads this."""
    out = trace_to_json(trace)
    if walk_result is not None:
        bc = getattr(walk_result, "budget_consumption", None)
        if bc is not None:
            out["budget_consumption"] = {
                "wall_clock_ms": getattr(bc, "wall_clock_ms", None),
                "llm_calls": getattr(bc, "llm_calls", None),
            }
        ar = getattr(walk_result, "abstention_reason", None)
        if ar is not None:
            out["abstention_reason"] = ar
    return out


def trace_to_human(
    trace: JustificationTrace, *, claim: Any = None, verdict: Any = None
) -> str:
    """WS5 observability: render a justification trace as inspectable
    plain text. Lists the claim, the final verdict, each edge with its
    source + key metadata (verdict, contradicting_value, kb_property,
    premise_status, bindings/paths tried, discovery_source), the
    provenance term (WS3), and the source_breakdown. Pure/deterministic;
    no I/O. Tolerates partial traces (hand-built test traces, missing
    optional keys)."""
    lines: list[str] = []
    root = trace.root.content if trace.root else {}
    subj = root.get("subject")
    pred = root.get("predicate")
    obj = root.get("object")
    lines.append(f"Claim: {subj} {pred} {obj} (polarity={root.get('polarity')})")
    if verdict is not None:
        lines.append(f"Verdict: {verdict}")
    if getattr(trace, "chain_includes_assertion", False):
        lines.append("Note: chain includes an unverified user assertion (conditional).")
    for i, e in enumerate(trace.edges, 1):
        md = e.metadata
        parts = [f"[{i}] {e.edge_type} via {md.get('source', '?')}"]
        if md.get("verdict"):
            parts.append(f"verdict={md['verdict']}")
        if md.get("kb_property"):
            parts.append(f"property={md['kb_property']}")
        if md.get("contradicting_value") is not None:
            parts.append(f"source_value={md['contradicting_value']}")
        if md.get("premise_status"):
            parts.append(f"premise={md['premise_status']}")
        if md.get("belief_revision"):
            parts.append(f"belief_revision={md['belief_revision']}")
        if md.get("relation_type"):
            parts.append(f"{md['relation_type']}/{md.get('direction', '')}")
        if md.get("discovery_source"):
            parts.append(f"discovery_source={md['discovery_source']}")
        if md.get("bindings_tried"):
            parts.append(f"bindings_tried={md['bindings_tried']}")
        if md.get("paths_tried"):
            parts.append(f"paths_tried={md['paths_tried']}")
        lines.append("  " + " ".join(parts))
    prov = getattr(trace, "provenance", None)
    if prov is not None and prov.literals():
        # Round-1 observability follow-up: the human string is rendered for the
        # PUBLIC /chat body too, so it must stay free of internal substrate
        # identifiers. Render each literal as source + status + assertion-marker
        # only — the (table,row_id) pair is NOT human-meaningful and lives only
        # on the verbose audit surface (trace_to_json's provenance term, via
        # GET /verification/{id}).
        prov_parts = []
        for lit in prov.literals():
            seg = lit.source
            if lit.status:
                seg += f"[{lit.status}]"
            if lit.assertion:
                seg += "[assertion]"
            prov_parts.append(seg)
        lines.append(f"Provenance ({prov.op}): " + ", ".join(prov_parts))
    if trace.source_breakdown:
        lines.append(f"Sources: {trace.source_breakdown}")
    return "\n".join(lines)
