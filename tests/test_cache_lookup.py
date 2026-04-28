"""End-to-end tests for v0.6 cache lookups.

When the cache contains a non-expired entry for a cache-eligible
claim, the router returns the cached verdict and skips the retrieval
verifier entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from src.cache import (
    ScopingDecision,
    StabilityDecision,
    VerificationCache,
    canonicalize_claim_key,
)
from src.cache.stability_classifier import STABILITY_TTL_SECONDS
from src.fact_store import FactStore


@dataclass
class _PipelineMockLLM:
    chats: list = field(default_factory=list)
    extracts: list = field(default_factory=list)
    rewrites: list = field(default_factory=list)
    corrector_model: str = "mock"

    def chat(self, system, messages, max_tokens=4096):
        return self.chats.pop(0)

    def extract_with_tool(self, system, user_message, tool, max_tokens=2048):
        return self.extracts.pop(0)

    def rewrite(self, system, user_message, max_tokens=2048, temperature=None):
        return self.rewrites.pop(0)


@dataclass
class _CountingRetrieval:
    """Counts how many times verify() was called."""
    calls: list = field(default_factory=list)

    def verify(self, claim, *, source_turn_id=None):
        from src.verifiers.retrieval_verifier import RetrievalResult
        from src.verifiers.types import VerificationOutcome

        self.calls.append(claim)
        return RetrievalResult(
            outcome=VerificationOutcome.VERIFIED,
            explanation="from real retrieval", snippets=[],
        )


def _build(tmp_path, fact, *, prepopulate_cache: bool = False):
    from src.corrector import Corrector
    from src.extractor import ClaimExtractor
    from src.llm_router import RoutingDecision
    from src.pattern_registry import load_default_registry, reset_cache
    from src.pipeline import Pipeline
    from src.router import Router

    reset_cache()
    mock = _PipelineMockLLM(
        chats=["draft"],
        extracts=[{"facts": []}, {"facts": [fact]}],
        rewrites=["soft"] * 5,
    )
    store = FactStore(tmp_path / "p.db")
    registry = load_default_registry()
    retrieval = _CountingRetrieval()
    router = Router(
        store, registry,
        routing_fn=lambda c: RoutingDecision(
            method="retrieval", reason="r", confidence=0.95,
            retrieval_query_hint="x",
        ),
        retrieval_verifier=retrieval,
    )
    cache = VerificationCache(store)

    if prepopulate_cache:
        cache.write(
            canonical_key=canonicalize_claim_key(fact),
            pattern=fact["pattern"], predicate=fact["predicate"],
            verdict="verified",
            stability_class="decade_stable",
            ttl_seconds=STABILITY_TTL_SECONDS["decade_stable"],
            evidence={"explanation": "prepopulated"},
        )

    p = Pipeline(
        store, registry, mock, ClaimExtractor(mock, registry),
        router, Corrector(mock),
        scoping_classifier=lambda claim: ScopingDecision(
            scope="world_fact", reason="r", confidence=0.95,
        ),
        stability_classifier=lambda claim: StabilityDecision(
            stability_class="decade_stable", reason="r", confidence=0.95,
            ttl_seconds=STABILITY_TTL_SECONDS["decade_stable"],
        ),
        verification_cache=cache,
    )
    return p, store, cache, retrieval


def test_cache_hit_skips_retrieval_call(tmp_path):
    fact = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"},
        "polarity": 1, "source_text": "Tokyo is in Japan",
    }
    p, store, cache, retrieval = _build(tmp_path, fact, prepopulate_cache=True)
    trace = p.run_turn("test")

    # Retrieval verifier was NOT called — the hit short-circuited it.
    assert retrieval.calls == []
    # The verdict is verified, served from cache.
    assert len(trace.verification_decisions) == 1
    d = trace.verification_decisions[0]
    assert d["verification_status"] == "verified"
    assert "served from cache" in d["notes"][0]

    # cache_lookup event with result=hit landed.
    events = store.get_pipeline_events(trace.assistant_turn_id)
    lookup_events = [e for e in events if e["stage"] == "cache_lookup"]
    assert len(lookup_events) == 1
    assert lookup_events[0]["data"]["result"] == "hit"


def test_cache_miss_falls_through_to_retrieval(tmp_path):
    fact = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"},
        "polarity": 1, "source_text": "Tokyo is in Japan",
    }
    p, store, cache, retrieval = _build(tmp_path, fact, prepopulate_cache=False)
    trace = p.run_turn("test")

    # Cache was empty → retrieval was called.
    assert len(retrieval.calls) == 1
    # The verdict is verified, came from real retrieval.
    d = trace.verification_decisions[0]
    assert d["verification_status"] == "verified"

    # cache_lookup event with result=miss landed.
    events = store.get_pipeline_events(trace.assistant_turn_id)
    lookup_events = [e for e in events if e["stage"] == "cache_lookup"]
    assert len(lookup_events) == 1
    assert lookup_events[0]["data"]["result"] == "miss"

    # And then the cache_write fired (filling the cache for next time).
    write_events = [e for e in events if e["stage"] == "cache_write"]
    assert len(write_events) == 1


def test_two_consecutive_calls_first_writes_second_hits(tmp_path):
    """First call: miss + retrieve + write. Second call (same key):
    hit, no retrieval."""
    fact = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"},
        "polarity": 1, "source_text": "Tokyo is in Japan",
    }
    # First pipeline run.
    p1, store, cache, retrieval = _build(
        tmp_path, fact, prepopulate_cache=False,
    )
    p1.run_turn("first call")
    assert len(retrieval.calls) == 1  # one real retrieval

    # Second run on same store + same fact — should hit cache.
    # Need to build a fresh pipeline because the first one consumed
    # its mock chat/extracts.
    from src.corrector import Corrector
    from src.extractor import ClaimExtractor
    from src.llm_router import RoutingDecision
    from src.pattern_registry import load_default_registry, reset_cache
    from src.pipeline import Pipeline
    from src.router import Router

    reset_cache()
    mock2 = _PipelineMockLLM(
        chats=["draft"],
        extracts=[{"facts": []}, {"facts": [fact]}],
        rewrites=["soft"] * 5,
    )
    registry = load_default_registry()
    router2 = Router(
        store, registry,
        routing_fn=lambda c: RoutingDecision(
            method="retrieval", reason="r", confidence=0.95,
            retrieval_query_hint="x",
        ),
        retrieval_verifier=retrieval,  # same counting verifier
    )
    p2 = Pipeline(
        store, registry, mock2, ClaimExtractor(mock2, registry),
        router2, Corrector(mock2),
        scoping_classifier=lambda c: ScopingDecision(
            scope="world_fact", reason="r", confidence=0.95,
        ),
        stability_classifier=lambda c: StabilityDecision(
            stability_class="decade_stable", reason="r", confidence=0.95,
            ttl_seconds=STABILITY_TTL_SECONDS["decade_stable"],
        ),
        verification_cache=cache,
    )
    trace2 = p2.run_turn("second call")
    # No new retrieval call — the cache hit short-circuited.
    assert len(retrieval.calls) == 1

    events = store.get_pipeline_events(trace2.assistant_turn_id)
    lookup_events = [e for e in events if e["stage"] == "cache_lookup"]
    assert lookup_events and lookup_events[0]["data"]["result"] == "hit"


def test_user_specific_claim_does_not_check_cache(tmp_path):
    """Cache lookup runs only for cache-eligible claims (scope=
    world_fact). A user_specific claim never even hits the cache."""
    fact = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "tea"},
        "polarity": 1, "source_text": "I like tea",
    }
    from src.corrector import Corrector
    from src.extractor import ClaimExtractor
    from src.llm_router import RoutingDecision
    from src.pattern_registry import load_default_registry, reset_cache
    from src.pipeline import Pipeline
    from src.router import Router

    reset_cache()
    mock = _PipelineMockLLM(
        chats=["draft"],
        extracts=[{"facts": []}, {"facts": [fact]}],
        rewrites=["soft"] * 5,
    )
    store = FactStore(tmp_path / "p.db")
    registry = load_default_registry()
    retrieval = _CountingRetrieval()
    router = Router(
        store, registry,
        routing_fn=lambda c: RoutingDecision(
            method="retrieval", reason="r", confidence=0.95,
        ),
        retrieval_verifier=retrieval,
    )
    cache = VerificationCache(store)
    # Pre-populate the cache with a fake key (would never match a
    # user_specific claim's canonical key).
    cache.write(canonical_key="some-other-key", pattern="x", predicate="y",
                verdict="verified", stability_class="immutable",
                ttl_seconds=None)

    p = Pipeline(
        store, registry, mock, ClaimExtractor(mock, registry),
        router, Corrector(mock),
        scoping_classifier=lambda c: ScopingDecision(
            scope="user_specific", reason="user pref", confidence=0.99,
        ),
        # No stability — gated on world_fact.
        verification_cache=cache,
    )
    trace = p.run_turn("test")

    # No cache_lookup event — the claim was not flagged eligible, so
    # the lookup was never even attempted.
    events = store.get_pipeline_events(trace.assistant_turn_id)
    lookup_events = [e for e in events if e["stage"] == "cache_lookup"]
    assert lookup_events == []
    # Real retrieval ran.
    assert len(retrieval.calls) == 1


def test_cache_lookup_failure_falls_through_to_retrieval(tmp_path):
    """If the cache.lookup() itself raises, log the error and proceed
    to real retrieval — caching is an optimization, not a hard dep."""
    fact = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"},
        "polarity": 1, "source_text": "Tokyo is in Japan",
    }
    p, store, cache, retrieval = _build(tmp_path, fact, prepopulate_cache=False)

    # Sabotage lookup.
    def boom(_key):
        raise RuntimeError("cache disk corrupt")
    cache.lookup = boom  # type: ignore[assignment]

    trace = p.run_turn("test")
    # Retrieval still ran.
    assert len(retrieval.calls) == 1
    events = store.get_pipeline_events(trace.assistant_turn_id)
    lookup_events = [e for e in events if e["stage"] == "cache_lookup"]
    assert len(lookup_events) == 1
    assert "error" in lookup_events[0]["data"]
    assert "cache disk corrupt" in lookup_events[0]["data"]["error"]


def test_cache_hit_contradicted_serves_with_correction(tmp_path):
    """Cache hits with verdict=contradicted return a Decision with the
    correction populated from cached evidence — not just verified."""
    fact = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Paris", "location": "Germany"},
        "polarity": 1, "source_text": "Paris is in Germany",
    }
    p, store, cache, retrieval = _build(tmp_path, fact, prepopulate_cache=False)

    # Pre-populate the cache with a contradicted verdict + evidence.
    cache.write(
        canonical_key=canonicalize_claim_key(fact),
        pattern=fact["pattern"], predicate=fact["predicate"],
        verdict="contradicted",
        stability_class="decade_stable",
        ttl_seconds=STABILITY_TTL_SECONDS["decade_stable"],
        evidence={"actual_value": "France",
                  "explanation": "Paris is in France, not Germany"},
    )

    trace = p.run_turn("test")
    # Retrieval skipped — cache hit served.
    assert retrieval.calls == []
    d = trace.verification_decisions[0]
    assert d["verification_status"] == "contradicted"
    # Correction populated from cached evidence.
    correction = d.get("correction") or {}
    assert correction.get("corrected_object") == "France"
    assert "Paris is in France" in (correction.get("explanation") or "")


def test_cache_hit_inconclusive_serves_without_redoing_retrieval(tmp_path):
    """Cache hits with retrieval_inconclusive verdict still skip
    re-retrieval — we don't redo expensive work on a known-tough claim."""
    fact = {
        "pattern": "quantitative", "predicate": "has_population",
        "slots": {"subject": "obscure-town", "property": "population",
                  "value": 1234},
        "polarity": 1, "source_text": "obscure-town has 1234 people",
    }
    p, store, cache, retrieval = _build(tmp_path, fact, prepopulate_cache=False)

    cache.write(
        canonical_key=canonicalize_claim_key(fact),
        pattern=fact["pattern"], predicate=fact["predicate"],
        verdict="retrieval_inconclusive",
        stability_class="decade_stable",
        ttl_seconds=STABILITY_TTL_SECONDS["decade_stable"],
        evidence={"explanation": "no signal from prior retrieval"},
    )

    trace = p.run_turn("test")
    assert retrieval.calls == []  # not re-attempted
    d = trace.verification_decisions[0]
    assert d["verification_status"] == "retrieval_inconclusive"
