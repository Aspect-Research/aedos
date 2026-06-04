"""Integration: ChatWrapper end-to-end claim extraction (D18).

The load-bearing test here is `test_chat_extracts_claims`. Before the D18 fix,
`ChatWrapper.respond` called `extract(draft, asserting_party=...)` against an
`extract(text, context)` signature; the resulting `TypeError` was swallowed by
a broad `except Exception: claims = []`, so `/chat` extracted zero claims and
every response was pass-through. These tests wire a real `Extractor` through
the wrapper and assert that claims actually reach the verification machinery.
"""

from __future__ import annotations

from typing import Any

import pytest

from aedos.database import open_memory_db
from aedos.deployment.chat_wrapper import ChatResponse, ChatWrapper, InterventionType
from aedos.layer1_extraction.extractor import Extractor
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


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------

_DRAFT = "Obama was born in Honolulu."

_EXTRACTED_CLAIMS = {
    "claims": [
        {
            "subject": "Obama",
            "predicate": "born_in",
            "object": "Honolulu",
            "polarity": 1,
            "source_text": "Obama was born in Honolulu",
            "verb_tense": "past",
        }
    ]
}

# Substrate-oracle generation responses share one dict — each oracle reads only
# the keys it needs (translation fields, or verdict/reason).
_SUBSTRATE_GEN = {
    "object_type": "entity",
    "user_subject_required": 0,
    "distinct_slots": None,
    "routing_hint": "kb_resolvable",
    "kb_namespace": None,
    "kb_property": None,
    "slot_to_qualifier": None,
    "single_valued": 0,
    "verdict": "neither",
    "reason": "test",
}


class MockTransport:
    """Canned LLM transport: a fixed draft, a fixed extraction, generic
    substrate-generation responses."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(self, *a: Any, **kw: Any) -> str:
        self.calls.append({"type": "chat"})
        return _DRAFT

    def extract_with_tool(
        self, *a: Any, tool: dict[str, Any] | None = None, purpose: str | None = None, **kw: Any
    ) -> dict[str, Any]:
        # `tool` may arrive positionally (system, user_message, tool, ...).
        if tool is None:
            for arg in a:
                if isinstance(arg, dict) and "name" in arg:
                    tool = arg
                    break
        name = tool["name"] if tool else ""
        self.calls.append({"type": "extract_with_tool", "tool": name, "purpose": purpose})
        if name == "extract_claims":
            return _EXTRACTED_CLAIMS
        return dict(_SUBSTRATE_GEN)


class StubKB:
    def resolve_entity(self, reference: Any, local_context: Any) -> list[ResolutionCandidate]:
        return [ResolutionCandidate("Q76", score=0.9)]

    def lookup_statements(self, entity: Any, predicate: Any) -> list:
        return []

    def subsumption(self, a: Any, b: Any, relation_type: Any) -> SubsumptionResult:
        return SubsumptionResult(verdict="unrelated")


def _make_wrapper() -> ChatWrapper:
    """A ChatWrapper with a real Extractor wired through the full pipeline."""
    db = open_memory_db()
    client = LLMClient(_transport=MockTransport())
    kb = StubKB()
    pt = PredicateTranslation(db=db, llm_client=client)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(
        resolver=resolver, predicate_translation=pt, subsumption=sub, predicate_distribution=pd
    )
    tier_u = TierU(db=db, predicate_translation=pt)
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    py_verifier = PythonVerifier()
    walker = Walker(tier_u=tier_u, kb_verifier=kb_verifier, python_verifier=py_verifier, substrate=substrate)
    extractor = Extractor(llm_client=client)
    aggregator = Aggregator()
    return ChatWrapper(extractor=extractor, walker=walker, aggregator=aggregator, llm_client=client)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestChatWrapperExtraction:
    def test_chat_extracts_claims(self):
        """Load-bearing for D18: a real extractor wired through `/chat` must
        produce claims. Pre-fix, the stale `extract` signature raised a
        `TypeError` swallowed by a broad except, so this list was always empty."""
        wrapper = _make_wrapper()
        response = wrapper.respond("Tell me about Obama.")

        claims = response.verification_result.claims_extracted
        assert claims, "ChatWrapper extracted zero claims — D18 regression"
        assert any(
            c.subject == "Obama" and "Honolulu" in c.object for c in claims
        ), "expected a claim about Obama's birthplace"

    def test_chat_response_shape(self):
        """Post-fix, the verification machinery runs over the extracted claim:
        the response carries a per-claim verdict and a justification trace."""
        wrapper = _make_wrapper()
        response = wrapper.respond("Tell me about Obama.")

        assert isinstance(response, ChatResponse)
        assert response.intervention_type in [t.value for t in InterventionType]

        vr = response.verification_result
        claims = vr.claims_extracted
        assert claims
        claim_id = claims[0].claim_id
        # The claim was verified through the pipeline: it has a verdict and a trace.
        assert claim_id in vr.per_claim_verdicts
        assert claim_id in vr.per_claim_traces
        assert vr.aggregate_metadata.get("claim_count", 0) >= 1


# ---------------------------------------------------------------------------
# Phase H Cluster 2 step 2: user-message extraction + promotion
# (Q-ChatWrapperSource). When `tier_u` is wired into the ChatWrapper,
# `respond` extracts claims from the *user_message* and writes them to
# Tier U as `asserted_unverified` BEFORE generating the draft. This is
# how user-asserted knowledge accumulates as premises across a session.
# ---------------------------------------------------------------------------

def _make_wrapper_with_tier_u() -> tuple[ChatWrapper, TierU, "sqlite3.Connection"]:
    """ChatWrapper with the Cluster-2 wiring — Tier U threaded so the
    user-message extraction + promotion path fires."""
    import sqlite3  # noqa: F401  (used implicitly via open_memory_db's return type)
    db = open_memory_db()
    client = LLMClient(_transport=MockTransport())
    kb = StubKB()
    pt = PredicateTranslation(db=db, llm_client=client)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(
        resolver=resolver, predicate_translation=pt, subsumption=sub, predicate_distribution=pd
    )
    tier_u = TierU(db=db, predicate_translation=pt)
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    py_verifier = PythonVerifier()
    walker = Walker(tier_u=tier_u, kb_verifier=kb_verifier, python_verifier=py_verifier, substrate=substrate)
    extractor = Extractor(llm_client=client)
    aggregator = Aggregator()
    wrapper = ChatWrapper(
        extractor=extractor, walker=walker, aggregator=aggregator,
        llm_client=client, tier_u=tier_u,
    )
    return wrapper, tier_u, db


class TestChatWrapperUserMessagePromotion:
    def test_user_message_claims_land_in_tier_u(self):
        wrapper, _tier_u, db = _make_wrapper_with_tier_u()
        # The mock transport's `extract_with_tool` always returns the
        # canned _EXTRACTED_CLAIMS (Obama born_in Honolulu). With Tier U
        # wired, respond() extracts on user_message first and promotes.
        # v0.16.3: the user_message must be a genuine ASSERTION where BOTH
        # entities (Obama, Honolulu) appear in the source — the source-grounding
        # promotion gate now (correctly) blocks a request like "Tell me about
        # Obama." whose object the LLM would fabricate. The row lands with
        # status='asserted_unverified'.
        wrapper.respond("Obama was born in Honolulu.")
        rows = db.execute(
            "SELECT subject, predicate, object, status FROM tier_u"
        ).fetchall()
        assert len(rows) >= 1
        # At least one row has the asserted_unverified status (the
        # user-message promotion). The draft-extraction path does NOT
        # write to Tier U; it only walks against existing premises.
        statuses = {r["status"] for r in rows}
        assert "asserted_unverified" in statuses

    def test_user_message_promotion_emits_audit(self):
        from aedos.audit.log import query_events
        wrapper, _tier_u, db = _make_wrapper_with_tier_u()
        # v0.16.3: genuine assertion (both entities in source) so the
        # source-grounding gate admits it; the audit records the promotion.
        wrapper.respond("Obama was born in Honolulu.")
        # Promotion writes call TierU.write → log_event(row_created)
        # with the asserted_unverified status. Verify the audit trail
        # records the promotion explicitly.
        row_events = query_events(db, event_type="row_created")
        assert len(row_events) >= 1
        # At least one row_created event records status=asserted_unverified.
        assert any(
            e["event_data"].get("status") == "asserted_unverified"
            for e in row_events
        )

    def test_pre_cluster_2_back_compat_without_tier_u(self):
        # When tier_u is NOT wired (the legacy constructor shape used by
        # test_chat_wrapper.py's _make_wrapper), the user-message
        # extraction step is skipped. No Tier U writes happen on the
        # user-message side; behavior matches pre-Cluster-2. The
        # downstream walker still uses whatever Tier U state the
        # pipeline holds.
        wrapper = _make_wrapper()  # no tier_u kwarg
        response = wrapper.respond("Tell me about Obama.")
        # Wrapper still returns a normal response; only the new
        # promotion path is skipped. No exception, no test fixture
        # change required.
        assert isinstance(response, ChatResponse)

    def test_extraction_called_on_user_message_and_draft(self):
        # Both extractions happen — user_message first (for promotion),
        # then draft (for walker verification). The mock transport
        # records every call; we expect at least two extract_claims
        # invocations per respond() turn.
        wrapper, _tier_u, _db = _make_wrapper_with_tier_u()
        # Find the underlying mock transport so we can inspect calls.
        # The LLMClient stores _transport as an attribute.
        transport = wrapper._llm._transport
        wrapper.respond("Tell me about Obama.")
        extract_calls = [
            c for c in transport.calls
            if c.get("type") == "extract_with_tool" and c.get("tool") == "extract_claims"
        ]
        # Two extract_claims invocations: one on user_message, one on draft.
        assert len(extract_calls) >= 2
