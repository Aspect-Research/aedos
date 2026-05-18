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
