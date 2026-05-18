"""Tests for JustificationTrace and trace_to_json serialization."""

from __future__ import annotations

import json
import pytest

from aedos.layer5_result.trace import (
    JustificationTrace,
    TraceEdge,
    TraceNode,
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
