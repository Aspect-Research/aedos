"""Integration: POST /chat and GET /verification/{id} endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# App factory with mocked dependencies
# ---------------------------------------------------------------------------

def _make_test_app():
    """Return a FastAPI TestClient with all dependencies mocked."""
    from src.aedos_v0_15.app import app
    from src.aedos_v0_15.database import open_memory_db
    from src.aedos_v0_15.deployment.chat_wrapper import ChatWrapper
    from src.aedos_v0_15.layer3_substrate import Substrate
    from src.aedos_v0_15.layer3_substrate.predicate_distribution import PredicateDistributionOracle
    from src.aedos_v0_15.layer3_substrate.predicate_translation import PredicateTranslation
    from src.aedos_v0_15.layer3_substrate.resolver import EntityResolver
    from src.aedos_v0_15.layer3_substrate.subsumption import SubsumptionOracle
    from src.aedos_v0_15.layer4_sources.kb_protocol import ResolutionCandidate, SubsumptionResult
    from src.aedos_v0_15.layer4_sources.kb_verifier import KBVerifier
    from src.aedos_v0_15.layer4_sources.python_verifier import PythonVerifier
    from src.aedos_v0_15.layer4_sources.tier_u import TierU
    from src.aedos_v0_15.layer4_sources.walker import Walker
    from src.aedos_v0_15.layer5_result.aggregator import Aggregator
    from src.aedos_v0_15.llm.client import LLMClient

    class MockTransport:
        def chat(self, *a, **kw):
            return "The sky is blue."

        def extract_with_tool(self, *a, purpose=None, **kw):
            if purpose in ("distribution_generation", "subsumption_generation"):
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

    import src.aedos_v0_15.app as _app_module
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
        client = _make_test_app()
        resp = client.post("/chat", json={"message": "Tell me about the sky."})
        body = resp.json()
        assert "intervention_type" in body
        assert body["intervention_type"] in ("pass_through", "abstain", "correct", "decline")

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

    def test_get_verification_unknown_id_returns_404(self):
        client = _make_test_app()
        resp = client.get("/verification/does-not-exist")
        assert resp.status_code == 404

    def test_no_claims_extracted_pass_through(self):
        # With extractor=None, no claims are extracted → pass_through
        client = _make_test_app()
        resp = client.post("/chat", json={"message": "Hello."})
        assert resp.json()["intervention_type"] == "pass_through"
