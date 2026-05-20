"""Integration: Walker + PythonVerifier — Python-routed claims end to end."""

from __future__ import annotations

import pytest

from aedos.database import open_memory_db
from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer3_substrate import Substrate
from aedos.layer3_substrate.predicate_distribution import PredicateDistributionOracle
from aedos.layer3_substrate.predicate_translation import PredicateTranslation
from aedos.layer3_substrate.resolver import EntityResolver
from aedos.layer3_substrate.subsumption import SubsumptionOracle
from aedos.layer4_sources.kb_protocol import ResolutionCandidate, SubsumptionResult
from aedos.layer4_sources.kb_verifier import KBVerdictType, KBVerifier
from aedos.layer4_sources.python_verifier import PythonVerifier
from aedos.layer4_sources.tier_u import TierU
from aedos.layer4_sources.walker import VerificationContext, Walker, WalkerBudget
from aedos.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockTransport:
    """Routes by purpose= kwarg; python_verifier returns canned code."""
    def __init__(self, python_code: str = "def verify(s, p, o): return True"):
        self._py_code = python_code

    def extract_with_tool(self, *a, purpose=None, **kw):
        if purpose == "python_verifier":
            return {"code": self._py_code, "reasoning": "test"}
        if purpose == "substrate:predicate_distribution":
            return {"verdict": "neither", "reason": "test"}
        if purpose == "substrate:subsumption":
            return {"verdict": "unrelated", "reason": "test"}
        return {
            "object_type": "entity",
            "user_subject_required": 0,
            "distinct_slots": None,
            "routing_hint": "python",
            "kb_namespace": None,
            "kb_property": None,
            "slot_to_qualifier": None,
            "reason": "test",
        }

    def chat(self, *a, **kw):
        return ""


class MockKB:
    def resolve_entity(self, r, lc): return [ResolutionCandidate("Q_none", score=0.0)]
    def lookup_statements(self, e, p): return []
    def subsumption(self, a, b, rt): return SubsumptionResult(verdict="unrelated")


def _make_system(python_code: str = "def verify(s, p, o): return True"):
    db = open_memory_db()
    transport = MockTransport(python_code=python_code)
    client = LLMClient(_transport=transport)
    kb = MockKB()
    pt = PredicateTranslation(db=db, llm_client=client)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(resolver=resolver, predicate_translation=pt, subsumption=sub, predicate_distribution=pd)
    tier_u = TierU(db=db, predicate_translation=pt)
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    py_verifier = PythonVerifier(llm_client=client)
    walker = Walker(
        tier_u=tier_u,
        kb_verifier=kb_verifier,
        python_verifier=py_verifier,
        substrate=substrate,
    )
    return walker


def _claim(subject: str = "4", predicate: str = "less_than", object_val: str = "7") -> Claim:
    return Claim(
        claim_id="c1",
        subject=subject,
        predicate=predicate,
        object=object_val,
        polarity=1,
        source_text="test",
        asserting_party="user_test",
        triage_decision=TriageDecision.VERIFY,
    )


def _ctx() -> VerificationContext:
    from datetime import datetime, timezone
    return VerificationContext(
        current_time=datetime.now(timezone.utc).isoformat(),
        asserting_party="user_test",
    )


# ---------------------------------------------------------------------------
# Walker + PythonVerifier integration
# ---------------------------------------------------------------------------

class TestWalkerPythonPath:
    def test_python_code_true_yields_verified(self):
        walker = _make_system("def verify(s, p, o): return int(s) < int(o)")
        result = walker.walk(_claim("4", "less_than", "7"), _ctx())
        assert result.verdict == "verified"

    def test_python_code_false_yields_contradicted(self):
        walker = _make_system("def verify(s, p, o): return int(s) < int(o)")
        result = walker.walk(_claim("10", "less_than", "3"), _ctx())
        assert result.verdict == "contradicted"

    def test_python_exception_falls_through_to_no_grounding(self):
        walker = _make_system("def verify(s, p, o): raise ValueError('bad')")
        result = walker.walk(_claim(), _ctx())
        assert result.verdict == "no_grounding_found"

    def test_python_disallowed_import_falls_through(self):
        walker = _make_system("import os\ndef verify(s, p, o): return True")
        result = walker.walk(_claim(), _ctx())
        assert result.verdict == "no_grounding_found"


class TestWalkerPythonTrace:
    def test_python_source_in_breakdown_on_verified(self):
        walker = _make_system("def verify(s, p, o): return True")
        result = walker.walk(_claim(), _ctx())
        assert result.trace.source_breakdown.get("python", 0) >= 1

    def test_python_trace_edge_emitted(self):
        walker = _make_system("def verify(s, p, o): return True")
        result = walker.walk(_claim(), _ctx())
        edge_sources = [e.metadata.get("source") for e in result.trace.edges]
        assert "python" in edge_sources

    def test_trace_serializable_with_python_verdict(self):
        import json
        from aedos.layer5_result.trace import trace_to_json
        walker = _make_system("def verify(s, p, o): return True")
        result = walker.walk(_claim(), _ctx())
        d = trace_to_json(result.trace)
        json.dumps(d)


class TestWalkerPythonBudget:
    def test_python_verified_within_budget(self):
        walker = _make_system("def verify(s, p, o): return True")
        budget = WalkerBudget(wall_clock_seconds=30.0, max_llm_calls=20)
        result = walker.walk(_claim(), _ctx(), budget=budget)
        assert result.verdict == "verified"
