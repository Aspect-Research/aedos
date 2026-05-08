"""End-to-end tests for v0.6 cache writes.

The Pipeline runs scoping + stability per claim, stashes the
decisions, then after verification writes verdicts to the
VerificationCache for cache-eligible claims (scope=world_fact,
stability != volatile, verdict from retrieval not python).
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
from src.legacy.fact_store import FactStore


@dataclass
class _PipelineMockLLM:
    chats: list = field(default_factory=list)
    extracts: list = field(default_factory=list)
    rewrites: list = field(default_factory=list)
    corrector_model: str = "mock"

    def chat(self, system, messages, max_tokens=4096, **_kwargs):
        return self.chats.pop(0)

    def extract_with_tool(self, system, user_message, tool, max_tokens=2048, **_kwargs):
        return self.extracts.pop(0)

    def rewrite(self, system, user_message, max_tokens=2048, temperature=None, **_kwargs):
        return self.rewrites.pop(0)


def _build_pipeline_with_cache(
    tmp_path,
    *,
    facts: list,
    scoping_returns: ScopingDecision,
    stability_returns: StabilityDecision | None,
    routing_method: str = "retrieval",
    retrieval_outcome: str = "verified",
):
    """Build a Pipeline with cache writes wired and a stub retrieval
    verifier whose verdict can be controlled."""
    from src.legacy.corrector import Corrector
    from src.legacy.extractor import ClaimExtractor
    from src.legacy.llm_router import RoutingDecision
    from src.legacy.pattern_registry import load_default_registry, reset_cache
    from src.legacy.pipeline import Pipeline
    from src.legacy.router import Router
    from src.legacy.verifiers.retrieval_verifier import RetrievalResult
    from src.legacy.verifiers.types import VerificationOutcome

    reset_cache()

    mock = _PipelineMockLLM(
        chats=["assistant draft"],
        extracts=[{"facts": []}, {"facts": facts}],
        rewrites=["softened draft"] * 5,  # plenty for the corrector
    )
    store = FactStore(tmp_path / "p.db")
    registry = load_default_registry()
    extractor = ClaimExtractor(mock, registry)

    # Stub retrieval verifier so we don't actually call DDG / a real
    # judge. Returns the requested outcome.
    outcome_map = {
        "verified": VerificationOutcome.VERIFIED,
        "contradicted": VerificationOutcome.CONTRADICTED,
        "inconclusive": VerificationOutcome.INCONCLUSIVE,
    }

    @dataclass
    class _StubRetrieval:
        def verify(self, claim, *, source_turn_id=None):
            return RetrievalResult(
                outcome=outcome_map[retrieval_outcome],
                explanation="stubbed",
                snippets=[],
            )

    router = Router(
        store, registry,
        routing_fn=lambda c: RoutingDecision(
            method=routing_method, reason="x",
            retrieval_query_hint="x",
        ),
        retrieval_verifier=_StubRetrieval(),
    )

    cache = VerificationCache(store)
    p = Pipeline(
        store, registry, mock, extractor, router, Corrector(mock),
        scoping_classifier=lambda claim: scoping_returns,
        stability_classifier=(
            (lambda claim: stability_returns) if stability_returns else None
        ),
        verification_cache=cache,
    )
    return p, store, cache


def test_world_fact_with_decade_stable_writes_to_cache(tmp_path):
    fact = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"},
        "polarity": 1, "source_text": "Tokyo is in Japan",
    }
    p, store, cache = _build_pipeline_with_cache(
        tmp_path, facts=[fact],
        scoping_returns=ScopingDecision(
            scope="world_fact", reason="geo",
        ),
        stability_returns=StabilityDecision(
            stability_class="decade_stable", reason="geo",
            ttl_seconds=STABILITY_TTL_SECONDS["decade_stable"],
        ),
        retrieval_outcome="verified",
    )
    trace = p.run_turn("where is tokyo")

    key = canonicalize_claim_key(fact)
    hit = cache.lookup(key)
    assert hit is not None
    assert hit.verdict == "verified"
    assert hit.stability_class == "decade_stable"

    events = store.get_pipeline_events(trace.assistant_turn_id)
    write_events = [e for e in events if e["stage"] == "cache_write"]
    assert len(write_events) == 1
    assert write_events[0]["data"]["verdict"] == "verified"


def test_user_specific_does_not_write_to_cache(tmp_path):
    fact = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "tea"},
        "polarity": 1, "source_text": "I like tea",
    }
    p, store, cache = _build_pipeline_with_cache(
        tmp_path, facts=[fact],
        scoping_returns=ScopingDecision(
            scope="user_specific", reason="user pref",
        ),
        # Stability won't run (gated on world_fact); pass None.
        stability_returns=None,
        retrieval_outcome="verified",
    )
    p.run_turn("test")

    key = canonicalize_claim_key(fact)
    assert cache.lookup(key) is None
    s = cache.stats()
    assert s["total_entries"] == 0


def test_volatile_does_not_write_to_cache(tmp_path):
    fact = {
        "pattern": "quantitative", "predicate": "stock_price",
        "slots": {"subject": "Apple", "property": "closing_price",
                  "value": 175.50},
        "polarity": 1, "source_text": "Apple closed at 175.50",
    }
    p, store, cache = _build_pipeline_with_cache(
        tmp_path, facts=[fact],
        scoping_returns=ScopingDecision(
            scope="world_fact", reason="market data",
        ),
        stability_returns=StabilityDecision(
            stability_class="volatile", reason="prices change",
            ttl_seconds=0,  # the don't-cache marker
        ),
        retrieval_outcome="verified",
    )
    trace = p.run_turn("test")

    key = canonicalize_claim_key(fact)
    assert cache.lookup(key) is None
    # The cache_write event should NOT have been emitted (we never
    # called write — gated on ttl != 0).
    events = store.get_pipeline_events(trace.assistant_turn_id)
    write_events = [e for e in events if e["stage"] == "cache_write"]
    assert write_events == []


def test_immutable_writes_with_no_expiry(tmp_path):
    fact = {
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"subject": "strawberry", "property": "letter_r", "value": 3},
        "polarity": 1, "source_text": "strawberry has 3 r's",
    }
    p, store, cache = _build_pipeline_with_cache(
        tmp_path, facts=[fact],
        scoping_returns=ScopingDecision(
            scope="world_fact", reason="structural",
        ),
        stability_returns=StabilityDecision(
            stability_class="immutable", reason="fixed string",
            ttl_seconds=None,
        ),
        retrieval_outcome="verified",
    )
    p.run_turn("test")

    key = canonicalize_claim_key(fact)
    hit = cache.lookup(key)
    assert hit is not None
    assert hit.expires_at is None  # immutable


def test_contradicted_verdict_also_caches(tmp_path):
    """A contradiction is a useful cached verdict — next time someone
    claims X is true and we know X is false, we can serve immediately."""
    fact = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Paris", "location": "Germany"},
        "polarity": 1, "source_text": "Paris is in Germany",
    }
    p, store, cache = _build_pipeline_with_cache(
        tmp_path, facts=[fact],
        scoping_returns=ScopingDecision(
            scope="world_fact", reason="geo",
        ),
        stability_returns=StabilityDecision(
            stability_class="decade_stable", reason="geo",
            ttl_seconds=STABILITY_TTL_SECONDS["decade_stable"],
        ),
        retrieval_outcome="contradicted",
    )
    p.run_turn("test")

    key = canonicalize_claim_key(fact)
    hit = cache.lookup(key)
    assert hit is not None
    assert hit.verdict == "contradicted"


def test_python_verdict_does_not_cache_via_retrieval_path(tmp_path):
    """Python-routed verifications produce code_gen_result; we don't
    cache those (cheap to redo)."""
    from src.legacy.verifiers.code_generation.pipeline import CodeGenVerificationResult

    fact = {
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"subject": "strawberry", "property": "letter_r", "value": 3},
        "polarity": 1, "source_text": "3 r's",
    }

    @dataclass
    class _StubCodeGen:
        def verify(self, claim, *, source_turn_id=None):
            return CodeGenVerificationResult(
                status="verified", actual_value=3,
                explanation="stub",
            )
        def verify_with_cross_check(self, claim, *, source_turn_id=None):
            return self.verify(claim, source_turn_id=source_turn_id)

    from src.legacy.corrector import Corrector
    from src.legacy.extractor import ClaimExtractor
    from src.legacy.llm_router import RoutingDecision
    from src.legacy.pattern_registry import load_default_registry, reset_cache
    from src.legacy.pipeline import Pipeline
    from src.legacy.router import Router

    reset_cache()
    mock = _PipelineMockLLM(
        chats=["draft"],
        extracts=[{"facts": []}, {"facts": [fact]}],
        rewrites=["soft"] * 3,
    )
    store = FactStore(tmp_path / "p.db")
    registry = load_default_registry()
    router = Router(
        store, registry,
        routing_fn=lambda c: RoutingDecision(
            method="python", reason="counting",
            python_inputs_self_contained=True,
        ),
        code_gen_verifier=_StubCodeGen(),
    )
    cache = VerificationCache(store)
    p = Pipeline(
        store, registry, mock, ClaimExtractor(mock, registry),
        router, Corrector(mock),
        scoping_classifier=lambda claim: ScopingDecision(
            scope="world_fact", reason="r",
        ),
        stability_classifier=lambda claim: StabilityDecision(
            stability_class="immutable", reason="r",
            ttl_seconds=None,
        ),
        verification_cache=cache,
    )
    trace = p.run_turn("test")

    key = canonicalize_claim_key(fact)
    # Python verifications are NOT cached via the retrieval path.
    assert cache.lookup(key) is None
    events = store.get_pipeline_events(trace.assistant_turn_id)
    write_events = [e for e in events if e["stage"] == "cache_write"]
    assert write_events == []


def test_cache_write_failure_does_not_break_pipeline(tmp_path):
    fact = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"},
        "polarity": 1, "source_text": "Tokyo is in Japan",
    }

    class _BoomCache:
        def write(self, **kwargs):
            raise RuntimeError("disk full")
        def lookup(self, key):  # required by interface
            return None

    from src.legacy.corrector import Corrector
    from src.legacy.extractor import ClaimExtractor
    from src.legacy.llm_router import RoutingDecision
    from src.legacy.pattern_registry import load_default_registry, reset_cache
    from src.legacy.pipeline import Pipeline
    from src.legacy.router import Router
    from src.legacy.verifiers.retrieval_verifier import RetrievalResult
    from src.legacy.verifiers.types import VerificationOutcome

    reset_cache()
    mock = _PipelineMockLLM(
        chats=["draft"],
        extracts=[{"facts": []}, {"facts": [fact]}],
        rewrites=["soft"] * 3,
    )
    store = FactStore(tmp_path / "p.db")
    registry = load_default_registry()

    @dataclass
    class _StubRetrieval:
        def verify(self, claim, *, source_turn_id=None):
            return RetrievalResult(outcome=VerificationOutcome.VERIFIED,
                                   explanation="stub", snippets=[])

    router = Router(
        store, registry,
        routing_fn=lambda c: RoutingDecision(
            method="retrieval", reason="r",
            retrieval_query_hint="x",
        ),
        retrieval_verifier=_StubRetrieval(),
    )
    p = Pipeline(
        store, registry, mock, ClaimExtractor(mock, registry),
        router, Corrector(mock),
        scoping_classifier=lambda claim: ScopingDecision(
            scope="world_fact", reason="r",
        ),
        stability_classifier=lambda claim: StabilityDecision(
            stability_class="decade_stable", reason="r",
            ttl_seconds=STABILITY_TTL_SECONDS["decade_stable"],
        ),
        verification_cache=_BoomCache(),
    )
    # Pipeline runs to completion despite the cache write raising.
    trace = p.run_turn("test")
    assert trace.final_content

    events = store.get_pipeline_events(trace.assistant_turn_id)
    write_events = [e for e in events if e["stage"] == "cache_write"]
    assert len(write_events) == 1
    assert "error" in write_events[0]["data"]
    assert "disk full" in write_events[0]["data"]["error"]


def test_build_pipeline_cache_is_always_on(tmp_path, monkeypatch):
    """The Tier 2 cache always builds — no env opt-in. The previous
    AEDOS_CACHE_* flags were removed because the cache should always
    accumulate verdicts across turns; opting out would mean the
    pipeline never gets faster from real usage."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # Even with the old opt-in env vars unset (or set to 0), the
    # cache must be wired.
    for var in ("AEDOS_CACHE_TIER2", "AEDOS_CACHE_SCOPING",
                "AEDOS_CACHE_STABILITY", "AEDOS_CACHE_WRITES"):
        monkeypatch.delenv(var, raising=False)

    from src.cache import VerificationCache
    from src.legacy.pipeline import build_pipeline
    p = build_pipeline(str(tmp_path / "x.db"))
    assert p._scoping_classifier is not None
    assert p._stability_classifier is not None
    assert isinstance(p._verification_cache, VerificationCache)
    p.store.close()


def test_build_pipeline_cache_ignores_legacy_off_flags(tmp_path, monkeypatch):
    """Even if a stale .env still sets AEDOS_CACHE_TIER2=0 or any of
    the granular flags to 0, the cache builds. Removing the opt-in
    means there's no way to disable it from the env — by design."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AEDOS_CACHE_TIER2", "0")
    monkeypatch.setenv("AEDOS_CACHE_SCOPING", "0")
    monkeypatch.setenv("AEDOS_CACHE_STABILITY", "0")
    monkeypatch.setenv("AEDOS_CACHE_WRITES", "0")

    from src.cache import VerificationCache
    from src.legacy.pipeline import build_pipeline
    p = build_pipeline(str(tmp_path / "x.db"))
    assert isinstance(p._verification_cache, VerificationCache)
    p.store.close()
