"""C2 / D22 — the three substrate oracles log their events in the deployed pipeline.

`predicate_translation`, `subsumption` and `predicate_distribution` previously
gated their `log_event` calls on an `audit_log` constructor flag that
`build_pipeline` never set, so `row_created` was inert in the deployed
pipeline. These tests build the pipeline through `build_pipeline` — the single
production assembly the chat-wrapper and the benchmark both use — and assert
that each oracle's cold consultation reaches the audit log. Against
`v0.15.0-rc.3` they fail (the gated branch never fires); post-C2 they pass.
"""

from __future__ import annotations

from aedos.audit.log import query_events
from aedos.database import open_memory_db
from aedos.layer3_substrate.subsumption import EntityRef
from aedos.llm.client import LLMClient
from aedos.pipeline import build_pipeline


class _OracleTransport:
    """Mock LLM transport answering each oracle's row-generation tool."""

    def extract_with_tool(self, system, user_message, tool, model="", purpose=None):
        name = tool["name"]
        if name == "generate_predicate_metadata":
            return {
                "object_type": "entity",
                "user_subject_required": 0,
                "routing_hint": "abstain",
                "single_valued": 0,
                "reason": "C2 test metadata",
            }
        if name == "generate_subsumption_verdict":
            return {"verdict": "unrelated", "reason": "C2 test verdict"}
        if name == "generate_distribution_verdict":
            return {"verdict": "neither", "reason": "C2 test verdict"}
        raise AssertionError(f"unexpected generation tool: {name}")

    def chat(self, system, messages, model="", purpose=None):
        return ""


def _pipeline():
    return build_pipeline(
        db=open_memory_db(), llm_client=LLMClient(_transport=_OracleTransport())
    )


def _row_created_subjects(db):
    return [e["event_subject"] for e in query_events(db, event_type="row_created")]


class TestOracleAuditLoggingInDeployedPipeline:
    """A cold consultation of each oracle writes a `row_created` audit event in
    the pipeline `build_pipeline` assembles (D22)."""

    def test_predicate_translation_consult_logs_event(self):
        pipeline = _pipeline()
        pipeline.predicate_translation.consult("c2_probe_predicate")
        subjects = _row_created_subjects(pipeline.db)
        assert any(s.startswith("predicate_translation:") for s in subjects)

    def test_subsumption_consult_logs_event(self):
        pipeline = _pipeline()
        pipeline.subsumption.consult(
            EntityRef("aedos", "c2_probe_a"), EntityRef("aedos", "c2_probe_b"), "is_a"
        )
        subjects = _row_created_subjects(pipeline.db)
        assert any(s.startswith("subsumption:") for s in subjects)

    def test_predicate_distribution_consult_logs_event(self):
        pipeline = _pipeline()
        pipeline.predicate_distribution.consult("c2_probe_predicate", 1, "part_of")
        subjects = _row_created_subjects(pipeline.db)
        assert any(s.startswith("predicate_distribution:") for s in subjects)
