"""Tests for the v0.6 cache TTL stability classifier.

Same structure as the scoping classifier tests — mock the LLM,
exercise the parsing/wiring; real-API calibration is gated.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import pytest

from src.cache.stability_classifier import (
    STABILITY_CLASSES,
    STABILITY_TTL_SECONDS,
    StabilityDecision,
    classify_stability,
)


@dataclass
class _MockLLM:
    canned: dict = field(default_factory=dict)

    def extract_with_tool(self, system, user_message, tool, max_tokens=2048):
        return self.canned


def _claim(**kwargs):
    base = {
        "pattern": "spatial_temporal",
        "predicate": "located_in",
        "slots": {"entity": "Tokyo", "location": "Japan"},
        "polarity": 1,
        "source_text": "Tokyo is in Japan",
    }
    base.update(kwargs)
    return base


def test_returns_decade_stable_with_correct_ttl():
    llm = _MockLLM(canned={
        "stability_class": "decade_stable",
        "reason": "geographic", "confidence": 0.95,
    })
    d = classify_stability(_claim(), llm)
    assert d.stability_class == "decade_stable"
    assert d.ttl_seconds == STABILITY_TTL_SECONDS["decade_stable"]
    assert d.ttl_seconds == 10 * 365 * 24 * 3600


def test_returns_immutable_with_none_ttl():
    llm = _MockLLM(canned={
        "stability_class": "immutable",
        "reason": "math", "confidence": 0.99,
    })
    d = classify_stability(_claim(), llm)
    assert d.stability_class == "immutable"
    assert d.ttl_seconds is None  # never expires


def test_returns_volatile_with_zero_ttl():
    llm = _MockLLM(canned={
        "stability_class": "volatile",
        "reason": "stock price", "confidence": 0.99,
    })
    d = classify_stability(_claim(), llm)
    assert d.stability_class == "volatile"
    assert d.ttl_seconds == 0  # don't cache


def test_invalid_stability_class_raises():
    llm = _MockLLM(canned={
        "stability_class": "made_up", "reason": "junk", "confidence": 0.5,
    })
    with pytest.raises(RuntimeError, match="invalid class"):
        classify_stability(_claim(), llm)


def test_decision_to_dict_shape():
    d = StabilityDecision(
        stability_class="years_stable", reason="r", confidence=0.9,
        ttl_seconds=STABILITY_TTL_SECONDS["years_stable"],
    )
    assert d.to_dict() == {
        "stability_class": "years_stable",
        "reason": "r",
        "confidence": 0.9,
        "ttl_seconds": 365 * 24 * 3600,
    }


def test_all_classes_have_ttl_mapping():
    for cls in STABILITY_CLASSES:
        assert cls in STABILITY_TTL_SECONDS, f"missing TTL for {cls}"


# ---- pipeline integration: stability runs only on world_fact ----------


def _build_pipeline_with_classifiers(tmp_path, scoping_fn, stability_fn,
                                     facts):
    from src.cache.scoping_classifier import ScopingDecision
    from src.corrector import Corrector
    from src.extractor import ClaimExtractor
    from src.fact_store import FactStore
    from src.llm_router import RoutingDecision
    from src.pattern_registry import load_default_registry, reset_cache
    from src.pipeline import Pipeline
    from src.router import Router

    reset_cache()

    @dataclass
    class _MockLLM:
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

    mock = _MockLLM(
        chats=["assistant draft"],
        extracts=[{"facts": []}, {"facts": facts}],
        rewrites=["softened draft"],  # routes to unverifiable → SOFTEN
    )
    store = FactStore(tmp_path / "p.db")
    registry = load_default_registry()
    extractor = ClaimExtractor(mock, registry)
    router = Router(store, registry, routing_fn=lambda c: RoutingDecision(
        method="unverifiable", reason="x", confidence=0.9))
    p = Pipeline(store, registry, mock, extractor, router, Corrector(mock),
                 scoping_classifier=scoping_fn,
                 stability_classifier=stability_fn)
    return p, store


def test_stability_runs_only_on_world_fact_claims(tmp_path):
    """The stability classifier MUST NOT run for user_specific or
    session_specific claims — they're not cache-eligible regardless
    of TTL, so calling stability would waste LLM budget."""
    from src.cache.scoping_classifier import ScopingDecision

    facts = [
        {"pattern": "preference", "predicate": "likes",
         "slots": {"agent": "user", "object": "tea"},
         "polarity": 1, "source_text": "I like tea"},
        {"pattern": "spatial_temporal", "predicate": "located_in",
         "slots": {"entity": "Tokyo", "location": "Japan"},
         "polarity": 1, "source_text": "Tokyo is in Japan"},
    ]

    # Scoping returns user_specific for the first, world_fact for the second.
    scope_results = iter([
        ScopingDecision(scope="user_specific", reason="user pref", confidence=0.99),
        ScopingDecision(scope="world_fact", reason="geo", confidence=0.95),
    ])
    scoping_fn = lambda claim: next(scope_results)

    stability_calls: list[dict] = []
    def stability_fn(claim):
        stability_calls.append(claim)
        return StabilityDecision(
            stability_class="decade_stable", reason="geo", confidence=0.95,
            ttl_seconds=STABILITY_TTL_SECONDS["decade_stable"],
        )

    p, store = _build_pipeline_with_classifiers(
        tmp_path, scoping_fn, stability_fn, facts,
    )
    trace = p.run_turn("test")

    # Stability was called once — only for the world_fact (Tokyo) claim.
    assert len(stability_calls) == 1
    assert stability_calls[0]["slots"]["entity"] == "Tokyo"

    # Two scoping events, one stability event.
    events = store.get_pipeline_events(trace.assistant_turn_id)
    scope_events = [e for e in events if e["stage"] == "cache_scoping_decision"]
    stab_events = [e for e in events if e["stage"] == "cache_stability_decision"]
    assert len(scope_events) == 2
    assert len(stab_events) == 1
    assert stab_events[0]["data"]["decision"]["stability_class"] == "decade_stable"
    assert stab_events[0]["data"]["decision"]["ttl_seconds"] == 10 * 365 * 24 * 3600


def test_stability_classifier_failure_does_not_break_pipeline(tmp_path):
    from src.cache.scoping_classifier import ScopingDecision

    facts = [
        {"pattern": "spatial_temporal", "predicate": "located_in",
         "slots": {"entity": "Tokyo", "location": "Japan"},
         "polarity": 1, "source_text": "Tokyo is in Japan"},
    ]

    def scoping_fn(claim):
        return ScopingDecision(scope="world_fact", reason="r", confidence=0.95)

    def boom(claim):
        raise RuntimeError("stability blew up")

    p, store = _build_pipeline_with_classifiers(
        tmp_path, scoping_fn, boom, facts,
    )
    trace = p.run_turn("test")
    assert trace.final_content  # didn't crash

    events = store.get_pipeline_events(trace.assistant_turn_id)
    stab_events = [e for e in events if e["stage"] == "cache_stability_decision"]
    assert len(stab_events) == 1
    assert "error" in stab_events[0]["data"]
    assert "stability blew up" in stab_events[0]["data"]["error"]


def test_stability_skipped_when_scoping_failed(tmp_path):
    """If scoping itself raised, we don't have a scope to gate stability
    on, so stability MUST NOT run."""
    facts = [
        {"pattern": "spatial_temporal", "predicate": "located_in",
         "slots": {"entity": "Tokyo", "location": "Japan"},
         "polarity": 1, "source_text": "Tokyo is in Japan"},
    ]

    def scoping_boom(claim):
        raise RuntimeError("scoping failed")

    stability_calls: list[dict] = []
    def stability_fn(claim):
        stability_calls.append(claim)
        return StabilityDecision(
            stability_class="immutable", reason="r", confidence=0.99,
        )

    p, store = _build_pipeline_with_classifiers(
        tmp_path, scoping_boom, stability_fn, facts,
    )
    p.run_turn("test")
    assert stability_calls == []  # never called


# ---- always-on construction --------------------------------------------


def test_build_pipeline_always_wires_stability_and_scoping(tmp_path, monkeypatch):
    """Both classifiers always build now — no env-var gate. The cache
    accumulates across turns by design."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    for var in ("AEDOS_CACHE_TIER2", "AEDOS_CACHE_SCOPING",
                "AEDOS_CACHE_STABILITY"):
        monkeypatch.delenv(var, raising=False)

    from src.pipeline import build_pipeline
    p = build_pipeline(str(tmp_path / "x.db"))
    assert callable(p._scoping_classifier)
    assert callable(p._stability_classifier)
    p.store.close()


# ---- real-API calibration (gated) --------------------------------------


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="real API stability classifier calibration gated behind RUN_API_TESTS=1",
)
def test_stability_calibration_against_worked_examples():
    """Smoke-check that the stability classifier picks the expected
    bin on its own worked examples. Real API; one call per case."""
    from src.llm_client import LLMClient

    cases = [
        # (claim, expected_class)
        ({"pattern": "quantitative", "predicate": "has_count",
          "slots": {"subject": "strawberry", "property": "letter_r", "value": 3},
          "polarity": 1, "source_text": "3 r's in strawberry"},
         "immutable"),
        ({"pattern": "spatial_temporal", "predicate": "located_in",
          "slots": {"entity": "Tokyo", "location": "Japan"},
          "polarity": 1, "source_text": "Tokyo is in Japan"},
         "decade_stable"),
        ({"pattern": "quantitative", "predicate": "stock_price",
          "slots": {"subject": "Apple", "property": "closing_price",
                    "value": 175.50, "unit": "USD"},
          "polarity": 1, "source_text": "Apple closed at 175.50"},
         "volatile"),
        ({"pattern": "quantitative", "predicate": "birth_year",
          "slots": {"subject": "Marie Curie", "property": "birth_year",
                    "value": 1867},
          "polarity": 1, "source_text": "Marie Curie was born in 1867"},
         "immutable"),
    ]

    llm = LLMClient()
    correct = 0
    misses: list[str] = []
    for claim, expected in cases:
        d = classify_stability(claim, llm)
        if d.stability_class == expected:
            correct += 1
        else:
            misses.append(f"  claim={claim['source_text']!r} expected="
                          f"{expected} got={d.stability_class} reason={d.reason}")
    assert correct >= 3, (
        f"stability classifier calibration: only {correct}/{len(cases)} correct\n"
        + "\n".join(misses)
    )
