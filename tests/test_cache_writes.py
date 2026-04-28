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
    from src.corrector import Corrector
    from src.extractor import ClaimExtractor
    from src.llm_router import RoutingDecision
    from src.pattern_registry import load_default_registry, reset_cache
    from src.pipeline import Pipeline
    from src.router import Router
    from src.verifiers.retrieval_verifier import RetrievalResult
    from src.verifiers.types import VerificationOutcome

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
            method=routing_method, reason="x", confidence=0.9,
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
            scope="world_fact", reason="geo", confidence=0.95,
        ),
        stability_returns=StabilityDecision(
            stability_class="decade_stable", reason="geo", confidence=0.95,
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
            scope="user_specific", reason="user pref", confidence=0.99,
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
            scope="world_fact", reason="market data", confidence=0.95,
        ),
        stability_returns=StabilityDecision(
            stability_class="volatile", reason="prices change", confidence=0.99,
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
            scope="world_fact", reason="structural", confidence=0.99,
        ),
        stability_returns=StabilityDecision(
            stability_class="immutable", reason="fixed string", confidence=0.99,
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
            scope="world_fact", reason="geo", confidence=0.95,
        ),
        stability_returns=StabilityDecision(
            stability_class="decade_stable", reason="geo", confidence=0.95,
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
    from src.verifiers.code_generation.pipeline import CodeGenVerificationResult

    fact = {
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"subject": "strawberry", "property": "letter_r", "value": 3},
        "polarity": 1, "source_text": "3 r's",
    }

    @dataclass
    class _StubCodeGen:
        def verify(self, claim, *, source_turn_id=None):
            return CodeGenVerificationResult(
                status="verified", confidence=0.99, actual_value=3,
                explanation="stub",
            )
        def verify_with_cross_check(self, claim, *, source_turn_id=None):
            return self.verify(claim, source_turn_id=source_turn_id)

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
        rewrites=["soft"] * 3,
    )
    store = FactStore(tmp_path / "p.db")
    registry = load_default_registry()
    router = Router(
        store, registry,
        routing_fn=lambda c: RoutingDecision(
            method="python", reason="counting", confidence=0.99,
            python_inputs_self_contained=True,
        ),
        code_gen_verifier=_StubCodeGen(),
    )
    cache = VerificationCache(store)
    p = Pipeline(
        store, registry, mock, ClaimExtractor(mock, registry),
        router, Corrector(mock),
        scoping_classifier=lambda claim: ScopingDecision(
            scope="world_fact", reason="r", confidence=0.95,
        ),
        stability_classifier=lambda claim: StabilityDecision(
            stability_class="immutable", reason="r", confidence=0.99,
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

    from src.corrector import Corrector
    from src.extractor import ClaimExtractor
    from src.llm_router import RoutingDecision
    from src.pattern_registry import load_default_registry, reset_cache
    from src.pipeline import Pipeline
    from src.router import Router
    from src.verifiers.retrieval_verifier import RetrievalResult
    from src.verifiers.types import VerificationOutcome

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
            method="retrieval", reason="r", confidence=0.95,
            retrieval_query_hint="x",
        ),
        retrieval_verifier=_StubRetrieval(),
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


def test_build_pipeline_cache_writes_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("AEDOS_CACHE_SCOPING", raising=False)
    monkeypatch.delenv("AEDOS_CACHE_STABILITY", raising=False)
    monkeypatch.delenv("AEDOS_CACHE_WRITES", raising=False)

    from src.pipeline import build_pipeline
    p = build_pipeline(str(tmp_path / "x.db"))
    assert p._verification_cache is None
    p.store.close()


def test_build_pipeline_cache_writes_on_when_all_three_env_vars_set(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AEDOS_CACHE_SCOPING", "1")
    monkeypatch.setenv("AEDOS_CACHE_STABILITY", "1")
    monkeypatch.setenv("AEDOS_CACHE_WRITES", "1")

    from src.cache import VerificationCache
    from src.pipeline import build_pipeline
    p = build_pipeline(str(tmp_path / "x.db"))
    assert isinstance(p._verification_cache, VerificationCache)
    p.store.close()


def test_build_pipeline_cache_writes_off_without_classifiers(tmp_path, monkeypatch):
    """Cache writes need scope + stability to know what to write.
    Setting only AEDOS_CACHE_WRITES=1 must NOT enable cache writes."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("AEDOS_CACHE_SCOPING", raising=False)
    monkeypatch.delenv("AEDOS_CACHE_STABILITY", raising=False)
    monkeypatch.setenv("AEDOS_CACHE_WRITES", "1")
    monkeypatch.delenv("AEDOS_CACHE_TIER2", raising=False)

    from src.pipeline import build_pipeline
    p = build_pipeline(str(tmp_path / "x.db"))
    assert p._verification_cache is None
    p.store.close()


def test_aedos_cache_tier2_shortcut_enables_all_three_layers(tmp_path, monkeypatch):
    """The single AEDOS_CACHE_TIER2=1 knob enables scoping, stability,
    and writes — equivalent to setting all 3 granular flags."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AEDOS_CACHE_TIER2", "1")
    monkeypatch.delenv("AEDOS_CACHE_SCOPING", raising=False)
    monkeypatch.delenv("AEDOS_CACHE_STABILITY", raising=False)
    monkeypatch.delenv("AEDOS_CACHE_WRITES", raising=False)

    from src.cache import VerificationCache
    from src.pipeline import build_pipeline
    p = build_pipeline(str(tmp_path / "x.db"))
    assert p._scoping_classifier is not None
    assert p._stability_classifier is not None
    assert isinstance(p._verification_cache, VerificationCache)
    p.store.close()


def test_aedos_cache_tier2_does_not_apply_when_zero(tmp_path, monkeypatch):
    """AEDOS_CACHE_TIER2=0 (or any non-1 value) leaves the cache off
    — symmetric with the granular-flag semantics."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AEDOS_CACHE_TIER2", "0")
    monkeypatch.delenv("AEDOS_CACHE_SCOPING", raising=False)
    monkeypatch.delenv("AEDOS_CACHE_STABILITY", raising=False)
    monkeypatch.delenv("AEDOS_CACHE_WRITES", raising=False)

    from src.pipeline import build_pipeline
    p = build_pipeline(str(tmp_path / "x.db"))
    assert p._scoping_classifier is None
    assert p._verification_cache is None
    p.store.close()


def test_granular_flag_overrides_tier2_default(tmp_path, monkeypatch):
    """AEDOS_CACHE_TIER2=1 + AEDOS_CACHE_WRITES=0 → observation mode:
    scoping + stability run, but no actual cache reads/writes. This
    is the documented power-user pattern."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AEDOS_CACHE_TIER2", "1")
    monkeypatch.setenv("AEDOS_CACHE_WRITES", "0")
    monkeypatch.delenv("AEDOS_CACHE_SCOPING", raising=False)
    monkeypatch.delenv("AEDOS_CACHE_STABILITY", raising=False)

    from src.pipeline import build_pipeline
    p = build_pipeline(str(tmp_path / "x.db"))
    assert p._scoping_classifier is not None
    assert p._stability_classifier is not None
    assert p._verification_cache is None
    p.store.close()


def test_granular_scoping_off_disables_all_downstream_under_tier2(
    tmp_path, monkeypatch,
):
    """AEDOS_CACHE_TIER2=1 + AEDOS_CACHE_SCOPING=0 disables scoping —
    and since stability/writes require scoping, the whole stack stays
    off. The override goes top-to-bottom."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("AEDOS_CACHE_TIER2", "1")
    monkeypatch.setenv("AEDOS_CACHE_SCOPING", "0")
    monkeypatch.delenv("AEDOS_CACHE_STABILITY", raising=False)
    monkeypatch.delenv("AEDOS_CACHE_WRITES", raising=False)

    from src.pipeline import build_pipeline
    p = build_pipeline(str(tmp_path / "x.db"))
    assert p._scoping_classifier is None
    assert p._stability_classifier is None
    assert p._verification_cache is None
    p.store.close()
