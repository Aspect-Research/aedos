"""End-to-end integration test for the full v0.6 pipeline:

  chat → extract → scoping → stability → router (with cache lookup)
       → verify → cache write → corrector → cost telemetry → final

This is the 'one test to rule them all' for v0.6 — it confirms all
the new components compose. Each individual piece has unit tests
elsewhere; this verifies they don't break each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from src.cache import (
    ScopingDecision,
    StabilityDecision,
    VerificationCache,
    canonicalize_claim_key,
)
from src.cache.stability_classifier import STABILITY_TTL_SECONDS
from src.corrector import Corrector
from src.cost import CallCost
from src.extractor import ClaimExtractor
from src.fact_store import FactStore
from src.llm_router import RoutingDecision
from src.pattern_registry import load_default_registry, reset_cache
from src.pipeline import Pipeline
from src.router import Router
from src.verifiers.retrieval_verifier import RetrievalResult
from src.verifiers.types import VerificationOutcome


@pytest.fixture(autouse=True)
def _reset():
    reset_cache()
    yield
    reset_cache()


@dataclass
class _MockLLM:
    chats: list = field(default_factory=list)
    extracts: list = field(default_factory=list)
    rewrites: list = field(default_factory=list)
    corrector_model: str = "claude-mock"
    _recorded: list = field(default_factory=list)

    def chat(self, system, messages, max_tokens=4096):
        return self.chats.pop(0)

    def extract_with_tool(self, system, user_message, tool, max_tokens=2048):
        # Pretend each extract call costs some tokens.
        self._recorded.append(CallCost(
            model=self.corrector_model,
            input_tokens=200, output_tokens=80,
            input_usd=0.001, output_usd=0.002, total_usd=0.003,
            pricing_known=True,
        ))
        return self.extracts.pop(0)

    def rewrite(self, system, user_message, max_tokens=2048,
                temperature=None, model=None):
        self._recorded.append(CallCost(
            model=model or self.corrector_model,
            input_tokens=100, output_tokens=50,
            input_usd=0.0005, output_usd=0.001, total_usd=0.0015,
            pricing_known=True,
        ))
        return self.rewrites.pop(0)

    def pop_recorded_calls(self):
        out = self._recorded
        self._recorded = []
        return out


@dataclass
class _StubRetrieval:
    """Always returns verified."""
    calls: list = field(default_factory=list)

    def verify(self, claim, *, source_turn_id=None):
        self.calls.append(claim)
        return RetrievalResult(
            outcome=VerificationOutcome.VERIFIED,
            explanation="stubbed", snippets=[],
        )


def test_full_v06_turn_with_all_features_active(tmp_path):
    """Run a turn through the full pipeline with:
      - Chat backend (mock, the default LLMClient.chat path)
      - Extractor (mock, returns one world-fact claim)
      - Scoping classifier (returns world_fact)
      - Stability classifier (returns decade_stable)
      - Router → retrieval (stubbed → verified)
      - Cache (writes the verdict)
      - Cost telemetry (turn_cost event emitted)

    Then run a SECOND turn with the same claim and confirm cache hit
    short-circuits retrieval.
    """
    facts = [{
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"},
        "polarity": 1, "source_text": "Tokyo is in Japan",
    }]
    mock = _MockLLM(
        chats=["Tokyo is in Japan."],
        extracts=[{"facts": []}, {"facts": facts}],
        rewrites=[],
    )
    store = FactStore(tmp_path / "v06.db")
    registry = load_default_registry()
    extractor = ClaimExtractor(mock, registry)
    retrieval = _StubRetrieval()
    router = Router(
        store, registry,
        routing_fn=lambda c: RoutingDecision(
            method="retrieval", reason="r", confidence=0.95,
        ),
        retrieval_verifier=retrieval,
    )
    cache = VerificationCache(store)

    p = Pipeline(
        store, registry, mock, extractor, router, Corrector(mock),
        scoping_classifier=lambda claim: ScopingDecision(
            scope="world_fact", reason="geo", confidence=0.95,
        ),
        stability_classifier=lambda claim: StabilityDecision(
            stability_class="decade_stable", reason="geo",
            confidence=0.95,
            ttl_seconds=STABILITY_TTL_SECONDS["decade_stable"],
        ),
        verification_cache=cache,
    )

    # ---- Turn 1: should miss cache, retrieve, and write to cache ----
    trace1 = p.run_turn("where is tokyo")
    assert len(retrieval.calls) == 1, "retrieval should run on miss"

    events1 = store.get_pipeline_events(trace1.assistant_turn_id)
    stages1 = [e["stage"] for e in events1]
    # Confirm all the v0.6 events landed.
    assert "cache_scoping_decision" in stages1
    assert "cache_stability_decision" in stages1
    assert "cache_lookup" in stages1
    assert "cache_write" in stages1
    assert "turn_cost" in stages1
    assert "verification" in stages1

    # cache_lookup result was MISS.
    lookup_ev = next(e for e in events1 if e["stage"] == "cache_lookup")
    assert lookup_ev["data"]["result"] == "miss"

    # turn_cost has positive total.
    cost_ev = next(e for e in events1 if e["stage"] == "turn_cost")
    assert cost_ev["data"]["total_usd"] > 0
    assert cost_ev["data"]["total_calls"] >= 2  # at least extract + extract

    # The cache now has the entry.
    key = canonicalize_claim_key(facts[0])
    assert cache.lookup(key) is not None

    # ---- Turn 2: cache hit, retrieval skipped ----
    mock.chats = ["Tokyo is in Japan."]
    mock.extracts = [{"facts": []}, {"facts": facts}]
    mock.rewrites = []

    trace2 = p.run_turn("again, where is tokyo")
    # Retrieval was NOT called this turn — short-circuited by cache.
    assert len(retrieval.calls) == 1  # still just the one from turn 1

    events2 = store.get_pipeline_events(trace2.assistant_turn_id)
    lookup2 = next(e for e in events2 if e["stage"] == "cache_lookup")
    assert lookup2["data"]["result"] == "hit"
    assert lookup2["data"]["verdict"] == "verified"

    # Verification decision still landed (router built it from the
    # cached verdict).
    assert any(d.get("verification_status") == "verified"
               for d in trace2.verification_decisions)

    store.close()
