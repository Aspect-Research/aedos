"""Integration: POST /chat and GET /verification/{id} endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# App factory with mocked dependencies
# ---------------------------------------------------------------------------

def _make_test_app():
    """Return a FastAPI TestClient with all dependencies mocked."""
    from aedos.app import app
    from aedos.database import open_memory_db
    from aedos.deployment.chat_wrapper import ChatWrapper
    from aedos.layer3_substrate import Substrate
    from aedos.layer3_substrate.predicate_distribution import PredicateDistributionOracle
    from aedos.layer3_substrate.predicate_translation import PredicateTranslation
    from aedos.layer3_substrate.resolver import EntityResolver
    from aedos.layer3_substrate.subsumption import SubsumptionOracle
    from aedos.layer4_sources.kb_protocol import ResolutionCandidate, SubsumptionResult
    from aedos.layer4_sources.kb_verifier import KBVerifier
    from aedos.layer4_sources.python_verifier import PythonVerifier
    from aedos.layer4_sources.tier_u import TierU
    from aedos.layer4_sources.walker import Walker
    from aedos.layer5_result.aggregator import Aggregator
    from aedos.llm.client import LLMClient

    class MockTransport:
        def chat(self, *a, **kw):
            return "The sky is blue."

        def extract_with_tool(self, *a, purpose=None, **kw):
            if purpose in ("substrate:predicate_distribution", "substrate:subsumption"):
                return {"verdict": "neither", "reason": "test"}
            return {
                "claims": [],
                "object_type": "entity",
                "user_subject_required": 0,
                "distinct_slots": None,
                "routing_hint": "user_authoritative",
                "kb_namespace": None,
                "kb_property": None,
                "slot_to_qualifier": None,
                "reason": "test",
            }

    class StubKB:
        def resolve_entity(self, r, lc): return [ResolutionCandidate("Q76", score=0.9)]
        def lookup_statements(self, e, p): return []
        def subsumption(self, a, b, rt): return SubsumptionResult(verdict="unrelated")

    db = open_memory_db()
    client = LLMClient(_transport=MockTransport())
    kb = StubKB()
    pt = PredicateTranslation(db=db, llm_client=client)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(resolver=resolver, predicate_translation=pt, subsumption=sub, predicate_distribution=pd)
    tier_u = TierU(db=db, predicate_translation=pt)
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    py_verifier = PythonVerifier()
    walker = Walker(tier_u=tier_u, kb_verifier=kb_verifier, python_verifier=py_verifier, substrate=substrate)
    aggregator = Aggregator()
    wrapper = ChatWrapper(extractor=None, walker=walker, aggregator=aggregator, llm_client=client)

    import aedos.app as _app_module
    _app_module._db = db
    _app_module._chat_wrapper = wrapper
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestChatEndpoint:
    def test_post_chat_returns_200(self):
        client = _make_test_app()
        resp = client.post("/chat", json={"message": "Tell me about the sky."})
        assert resp.status_code == 200

    def test_post_chat_returns_final_message(self):
        client = _make_test_app()
        resp = client.post("/chat", json={"message": "Tell me about the sky."})
        body = resp.json()
        assert "final_message" in body
        assert body["final_message"]

    def test_post_chat_returns_intervention_type(self):
        # Phase 10.5 Session 2 Item 1: 3-value top-level
        # (pass_through / intervene / decline). The per-claim CORRECT
        # and ABSTAIN have moved into `per_claim_actions[].action_type`.
        client = _make_test_app()
        resp = client.post("/chat", json={"message": "Tell me about the sky."})
        body = resp.json()
        assert "intervention_type" in body
        assert body["intervention_type"] in ("pass_through", "intervene", "decline")

    def test_post_chat_returns_per_claim_actions(self):
        # Phase 10.5 Session 2 Item 1: the response now carries the
        # per-claim action list. Empty for pass_through / decline; one
        # entry per problematic claim for intervene.
        client = _make_test_app()
        resp = client.post("/chat", json={"message": "Tell me about the sky."})
        body = resp.json()
        assert "per_claim_actions" in body
        assert isinstance(body["per_claim_actions"], list)
        for action in body["per_claim_actions"]:
            assert "claim_id" in action
            assert "action_type" in action
            # WS5 (part d): the action_type set is widened to include
            # 'confirm_conditional' (surfaced for verified_given_assertion).
            assert action["action_type"] in (
                "correct", "abstain", "confirm_conditional",
            )
            assert "annotation" in action

    def test_post_chat_returns_observability(self):
        # WS5 (part e): /chat carries an additive `observability` list — one
        # structured, inspectable entry per verified claim.
        client = _make_test_app()
        resp = client.post("/chat", json={"message": "Tell me about the sky."})
        body = resp.json()
        assert "observability" in body
        assert isinstance(body["observability"], list)
        for entry in body["observability"]:
            assert "claim_id" in entry
            assert "verdict" in entry
            assert "base_verdict" in entry
            assert "conditional" in entry
            assert "contradicting_value" in entry
            # The human-readable trace rendering is the operator's inspection
            # surface and must be present for each claim with a trace.
            assert "trace_human" in entry

    def test_post_chat_observability_is_lightweight(self):
        # Round-1 observability follow-up: the PUBLIC /chat body carries the
        # LIGHTWEIGHT observability surface — verdict-level fields only. It must
        # NOT carry the full `trace` JSON nor the raw `provenance` term, because
        # both embed internal substrate row ids that are not part of the public
        # contract. A caller wanting the audit detail dereferences
        # `verification_id` against GET /verification/{id}.
        client = _make_test_app()
        resp = client.post("/chat", json={"message": "Tell me about the sky."})
        body = resp.json()
        for entry in body["observability"]:
            # The verdict-level fields a caller needs to render the turn.
            assert "verdict" in entry
            assert "trace_human" in entry
            assert "contradicting_value" in entry
            # The heavy / row-id-bearing keys are ABSENT from the public body.
            assert "trace" not in entry
            assert "provenance" not in entry

    def test_post_chat_body_leaks_no_internal_row_ids(self):
        # Soundness-of-contract: no internal substrate table/row_id identifier
        # may leak into the public /chat body. Scan the serialized body for the
        # row-id-bearing key names (trace edge metadata embeds these) and for a
        # raw integer under any `row_id` key.
        import json as _json

        client = _make_test_app()
        resp = client.post("/chat", json={"message": "Tell me about the sky."})
        raw = resp.text
        for forbidden in (
            "tier_u_row_id",
            "entity_resolution_cache_row_id",
            "subsumption_row_id",
        ):
            assert forbidden not in raw, f"internal id key {forbidden!r} leaked to /chat"
        # No `row_id` key bound to an integer anywhere in the public body.
        body = resp.json()

        def _no_row_id_int(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    assert not (k == "row_id" and isinstance(v, int)), (
                        "raw row_id integer leaked to /chat body"
                    )
                    _no_row_id_int(v)
            elif isinstance(node, list):
                for v in node:
                    _no_row_id_int(v)

        _no_row_id_int(body)
        # Guard the scan itself: the serialized body round-trips as JSON.
        assert _json.loads(raw) == body

    def test_post_chat_returns_verification_id(self):
        client = _make_test_app()
        resp = client.post("/chat", json={"message": "Tell me about the sky."})
        body = resp.json()
        assert "verification_id" in body
        assert body["verification_id"]

    def test_get_verification_returns_200(self):
        client = _make_test_app()
        post_resp = client.post("/chat", json={"message": "Tell me."})
        vid = post_resp.json()["verification_id"]
        get_resp = client.get(f"/verification/{vid}")
        assert get_resp.status_code == 200

    def test_get_verification_returns_metadata(self):
        client = _make_test_app()
        post_resp = client.post("/chat", json={"message": "Tell me."})
        vid = post_resp.json()["verification_id"]
        body = client.get(f"/verification/{vid}").json()
        assert "aggregate_metadata" in body
        assert "per_claim_verdicts" in body

    def test_get_verification_returns_observability_claims(self):
        # WS5 (part e): /verification/{id} is the deeper inspection surface —
        # an additive `claims` list, each entry carrying verdict + a
        # human-readable trace rendering. Existing keys stay (additive).
        client = _make_test_app()
        post_resp = client.post("/chat", json={"message": "Tell me."})
        vid = post_resp.json()["verification_id"]
        body = client.get(f"/verification/{vid}").json()
        assert "claims" in body
        assert isinstance(body["claims"], list)
        for entry in body["claims"]:
            assert "verdict" in entry
            assert "trace_human" in entry
            # Round-1 observability follow-up: the AUDIT endpoint stays FULLY
            # detailed — the verbose surface carries the row-id-bearing `trace`
            # and `provenance` keys that the public /chat body omits.
            assert "trace" in entry
            assert "provenance" in entry

    def test_get_verification_unknown_id_returns_404(self):
        client = _make_test_app()
        resp = client.get("/verification/does-not-exist")
        assert resp.status_code == 404

    def test_no_claims_extracted_pass_through(self):
        # With extractor=None, no claims are extracted → pass_through
        client = _make_test_app()
        resp = client.post("/chat", json={"message": "Hello."})
        assert resp.json()["intervention_type"] == "pass_through"
