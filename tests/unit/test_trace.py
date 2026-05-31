"""Tests for JustificationTrace and trace_to_json serialization."""

from __future__ import annotations

import json
import pytest

from aedos.layer5_result.trace import (
    JustificationTrace,
    ProvenanceLiteral,
    ProvenanceTerm,
    TraceEdge,
    TraceNode,
    trace_to_human,
    trace_to_json,
)


class TestTraceNode:
    def test_fields_present(self):
        n = TraceNode(node_type="claim", content={"subject": "Asa"})
        assert n.node_type == "claim"
        assert n.content["subject"] == "Asa"

    def test_default_content_empty(self):
        n = TraceNode(node_type="kb_statement")
        assert n.content == {}


class TestTraceEdge:
    def test_fields_present(self):
        src = TraceNode("claim")
        tgt = TraceNode("tier_u_row")
        e = TraceEdge(edge_type="premise_lookup", source=src, target=tgt)
        assert e.edge_type == "premise_lookup"
        assert e.source is src
        assert e.target is tgt
        assert e.metadata == {}

    def test_metadata_stored(self):
        src = TraceNode("claim")
        tgt = TraceNode("kb_statement")
        e = TraceEdge("predicate_equivalence", src, tgt, metadata={"kb_property": "P39"})
        assert e.metadata["kb_property"] == "P39"


class TestJustificationTrace:
    def test_fields_present(self):
        root = TraceNode("claim", {"subject": "Obama"})
        t = JustificationTrace(root=root)
        assert t.root is root
        assert t.edges == []
        assert t.polarity_trace == []
        assert t.source_breakdown == {}
        assert t.walk_metadata == {}

    def test_provenance_defaults_to_empty_term(self):
        # v0.16 WS3: JustificationTrace gains a lazy AND/OR `provenance` term.
        # In this foundation phase it defaults to an empty (no-literal) term so
        # the schema is stable across the workstream.
        root = TraceNode("claim", {"subject": "Obama"})
        t = JustificationTrace(root=root)
        assert isinstance(t.provenance, ProvenanceTerm)
        assert t.provenance.literals() == []
        # An empty term carries no assertion-conditional premise, so the
        # derived chain_includes_assertion signal is False.
        assert t.provenance.includes_assertion() is False

    def test_edges_appended(self):
        root = TraceNode("claim")
        t = JustificationTrace(root=root)
        edge = TraceEdge("premise_lookup", root, TraceNode("tier_u_row"))
        t.edges.append(edge)
        assert len(t.edges) == 1


class TestTraceToJson:
    def test_serializable(self):
        root = TraceNode("claim", {"subject": "Obama"})
        t = JustificationTrace(root=root)
        d = trace_to_json(t)
        json.dumps(d)  # must not raise

    def test_root_in_output(self):
        root = TraceNode("claim", {"subject": "Obama"})
        t = JustificationTrace(root=root)
        d = trace_to_json(t)
        assert d["root"]["node_type"] == "claim"
        assert d["root"]["content"]["subject"] == "Obama"

    def test_edges_in_output(self):
        root = TraceNode("claim")
        tgt = TraceNode("tier_u_row")
        t = JustificationTrace(root=root)
        t.edges.append(TraceEdge("premise_lookup", root, tgt))
        d = trace_to_json(t)
        assert len(d["edges"]) == 1
        assert d["edges"][0]["edge_type"] == "premise_lookup"

    def test_walk_metadata_in_output(self):
        root = TraceNode("claim")
        t = JustificationTrace(root=root)
        t.walk_metadata = {"depth_reached": 2, "llm_calls": 3}
        d = trace_to_json(t)
        assert d["walk_metadata"]["depth_reached"] == 2

    def test_source_breakdown_in_output(self):
        root = TraceNode("claim")
        t = JustificationTrace(root=root, source_breakdown={"tier_u": 1, "kb": 0})
        d = trace_to_json(t)
        assert d["source_breakdown"]["tier_u"] == 1

    def test_provenance_key_present_and_serializable(self):
        # v0.16 WS3: trace_to_json gains an additive `provenance` key. The
        # default empty term serializes (and dumps) without raising.
        root = TraceNode("claim")
        t = JustificationTrace(root=root)
        d = trace_to_json(t)
        assert "provenance" in d
        json.dumps(d)  # must not raise

    def test_provenance_literal_serialized(self):
        # A populated provenance term (one OR alternative wrapping a literal)
        # serializes its literal under the `provenance` key.
        root = TraceNode("claim")
        t = JustificationTrace(root=root)
        lit = ProvenanceLiteral(
            source="tier_u", table="tier_u", row_id=7,
            status="asserted_unverified", assertion=True,
        )
        t.provenance.add_alternative(ProvenanceTerm.lit(lit))
        d = trace_to_json(t)
        json.dumps(d)  # must not raise
        prov = d["provenance"]
        assert prov["op"] == "or"
        assert prov["children"][0]["op"] == "lit"
        assert prov["children"][0]["literal"]["row_id"] == 7
        assert prov["children"][0]["literal"]["assertion"] is True


# ---------------------------------------------------------------------------
# trace_to_human — WS5 observability renderer
# ---------------------------------------------------------------------------

class TestTraceToHuman:
    def _trace(self):
        root = TraceNode("claim", {
            "subject": "Obama", "predicate": "birthplace",
            "object": "Chicago", "polarity": 1,
        })
        t = JustificationTrace(root=root)
        t.edges.append(TraceEdge(
            edge_type="premise_lookup",
            source=root,
            target=TraceNode("kb_statement", {"entity": "Q76"}),
            metadata={
                "source": "kb",
                "verdict": "contradicted",
                "kb_property": "P19",
                "contradicting_value": "Q18094",
                "contradicting_value_type": "entity",
            },
        ))
        t.source_breakdown = {"kb": 1}
        return t

    def test_renders_claim_and_verdict(self):
        text = trace_to_human(self._trace(), verdict="contradicted")
        assert "Claim: Obama birthplace Chicago (polarity=1)" in text
        assert "Verdict: contradicted" in text

    def test_renders_edge_with_source_and_metadata(self):
        text = trace_to_human(self._trace(), verdict="contradicted")
        # The edge line lists the source, the verdict, the kb property, and
        # the contradicting source value.
        assert "premise_lookup via kb" in text
        assert "verdict=contradicted" in text
        assert "property=P19" in text
        assert "source_value=Q18094" in text

    def test_renders_source_breakdown(self):
        text = trace_to_human(self._trace())
        assert "Sources:" in text
        assert "kb" in text

    def test_deterministic_same_input_same_output(self):
        # Pure/deterministic: identical traces render byte-identical text.
        a = trace_to_human(self._trace(), verdict="contradicted")
        b = trace_to_human(self._trace(), verdict="contradicted")
        assert a == b

    def test_renders_provenance_with_assertion_marker(self):
        root = TraceNode("claim", {"subject": "Asa", "predicate": "lives_in",
                                   "object": "Paris", "polarity": 1})
        t = JustificationTrace(root=root)
        t.provenance.add_alternative(ProvenanceTerm.lit(
            ProvenanceLiteral(source="tier_u", table="tier_u", row_id=7,
                              status="asserted_unverified", assertion=True)))
        text = trace_to_human(t, verdict="verified_given_assertion")
        # chain_includes_assertion is derived True → the conditional note,
        # and the provenance line marks the assertion literal.
        assert "chain includes an unverified user assertion" in text
        assert "Provenance" in text
        # Round-1 observability follow-up: trace_human is now rendered on the
        # PUBLIC /chat body, so the provenance render is ROW-ID-FREE — source +
        # [status] + [assertion] marker only, never the (table#row_id) pair.
        assert "tier_u[asserted_unverified][assertion]" in text
        # The internal row id must not leak into the human string.
        assert "tier_u#7" not in text
        assert "#7" not in text

    def test_tolerates_minimal_trace(self):
        # A bare trace (no edges, empty root content, no verdict) renders
        # without raising — the renderer must tolerate partial traces.
        t = JustificationTrace(root=TraceNode("claim"))
        text = trace_to_human(t)
        assert text.startswith("Claim:")
        assert isinstance(text, str)
