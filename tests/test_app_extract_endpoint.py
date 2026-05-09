"""Tests for the v0.14 /v2/api/extract HTTP endpoint.

The endpoint is a thin wrapper around ClaimExtractor.extract. These
tests verify the wrapper's request/response shape and lazy-init
behavior using a stub extractor — no live LLM call.

The endpoint exists so Phase 9's behavioral parity check can hit
the v2 stack as a system rather than constructing extractors
directly. It does NOT route, verify, store, or correct — Phase 2+
wires those layers in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.app import _set_extractor, app
from src.layer1_extraction.extractor import ExtractionResult
from src.layer1_extraction.pattern_registry import reset_cache


@dataclass
class StubExtractor:
    """Stand-in for ClaimExtractor that records calls and replays a
    canned ExtractionResult. Avoids LLMClient construction so the
    test doesn't need ANTHROPIC_API_KEY or network access."""

    canned: ExtractionResult
    calls: list[dict[str, Any]] = field(default_factory=list)

    def extract(self, text: str, role: str, *, context: str | None = None):
        self.calls.append({"text": text, "role": role, "context": context})
        return self.canned


@pytest.fixture(autouse=True)
def _isolate_extractor_singleton():
    """Each test gets its own stub extractor; clear registry cache so
    no v1/v2 cross-contamination if a test module ran first."""
    reset_cache()
    _set_extractor(None)
    yield
    _set_extractor(None)
    reset_cache()


@pytest.fixture
def client():
    return TestClient(app)


# ---- happy paths --------------------------------------------------------


def test_health_still_responds(client):
    """Sanity: /health still responds and reports the v0.14.1 version."""
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "0.14.2"
    assert body["ok"] is True


def test_extract_returns_mereological_for_williamstown_case(client):
    """The Phase 1 'Done when' bullet: /v2 returns mereological-typed
    claims for 'Williamstown is part of Massachusetts'."""
    canned = ExtractionResult(valid_facts=[
        {
            "pattern": "mereological",
            "predicate": "part_of",
            "slots": {"part": "Williamstown", "whole": "Massachusetts"},
            "polarity": 1,
            "source_text": "Williamstown is part of Massachusetts",
        }
    ])
    _set_extractor(StubExtractor(canned=canned))

    r = client.post(
        "/api/extract",
        json={"text": "Williamstown is part of Massachusetts.", "role": "user"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "valid_facts" in body
    assert len(body["valid_facts"]) == 1
    f = body["valid_facts"][0]
    assert f["pattern"] == "mereological"
    assert f["slots"]["part"] == "Williamstown"
    assert f["slots"]["whole"] == "Massachusetts"


def test_extract_returns_empty_for_abstention_input(client):
    canned = ExtractionResult(valid_facts=[])
    _set_extractor(StubExtractor(canned=canned))

    r = client.post(
        "/api/extract",
        json={"text": "The sunset was beautiful.", "role": "user"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid_facts"] == []
    assert body["rejected_facts"] == []
    assert body["warnings"] == []


def test_extract_passes_context_through_to_extractor(client):
    """The context field forwards to ClaimExtractor.extract for
    self-reference resolution."""
    stub = StubExtractor(canned=ExtractionResult())
    _set_extractor(stub)

    r = client.post(
        "/api/extract",
        json={
            "text": "Two words contain 'o'.",
            "role": "assistant",
            "context": "How many words in 'the quick brown fox' contain 'o'?",
        },
    )
    assert r.status_code == 200
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["context"] == "How many words in 'the quick brown fox' contain 'o'?"
    assert call["role"] == "assistant"


def test_extract_disambiguation_pair_returns_two_facts(client):
    """The canonical Phase 1 disambiguation pair: one mereological + one
    spatial_temporal claim from a single sentence."""
    canned = ExtractionResult(valid_facts=[
        {
            "pattern": "mereological",
            "predicate": "part_of",
            "slots": {"part": "Williamstown", "whole": "Massachusetts"},
            "polarity": 1,
            "source_text": "Williamstown is part of Massachusetts",
        },
        {
            "pattern": "spatial_temporal",
            "predicate": "lives_in",
            "slots": {
                "entity": "Asa",
                "location": "Williamstown",
                "relation_kind": "residence",
            },
            "polarity": 1,
            "source_text": "Asa lives in Williamstown",
        },
    ])
    _set_extractor(StubExtractor(canned=canned))

    r = client.post(
        "/api/extract",
        json={
            "text": "Williamstown is part of Massachusetts and Asa lives in Williamstown.",
            "role": "user",
        },
    )
    assert r.status_code == 200
    facts = r.json()["valid_facts"]
    patterns = sorted(f["pattern"] for f in facts)
    assert patterns == ["mereological", "spatial_temporal"]


# ---- validation paths ---------------------------------------------------


def test_extract_rejects_invalid_role(client):
    _set_extractor(StubExtractor(canned=ExtractionResult()))
    r = client.post(
        "/api/extract",
        json={"text": "x", "role": "system"},
    )
    assert r.status_code == 400
    body = r.json()
    assert "role" in body["detail"].lower()


def test_extract_rejects_missing_text(client):
    _set_extractor(StubExtractor(canned=ExtractionResult()))
    r = client.post("/api/extract", json={"role": "user"})
    # Pydantic validation kicks in before our handler — 422 is correct.
    assert r.status_code == 422


def test_extract_rejects_missing_role(client):
    _set_extractor(StubExtractor(canned=ExtractionResult()))
    r = client.post("/api/extract", json={"text": "hi"})
    assert r.status_code == 422


# ---- lazy-init contract -------------------------------------------------


def test_extractor_singleton_lazy_initialized_on_first_call(monkeypatch):
    """When no extractor has been injected, _get_extractor builds one.
    We monkeypatch ClaimExtractor + LLMClient to avoid a real API key
    and assert they were both invoked exactly once."""
    from src import app as v2_app_module

    _set_extractor(None)

    sentinel_extractor = StubExtractor(canned=ExtractionResult())
    construct_calls: list[dict[str, Any]] = []

    class _FakeClaimExtractor:
        def __init__(self, llm, registry):
            construct_calls.append({"llm": llm, "registry": registry})

        def extract(self, text, role, *, context=None):
            return sentinel_extractor.canned

    class _FakeLLMClient:
        def __init__(self, *a, **kw):
            pass

    # The lazy import path is inside _get_extractor; we patch the
    # symbols it imports at the moment of import.
    monkeypatch.setattr(
        "src.layer1_extraction.extractor.ClaimExtractor",
        _FakeClaimExtractor,
    )
    monkeypatch.setattr("src.llm_client.LLMClient", _FakeLLMClient)

    extractor = v2_app_module._get_extractor()
    assert isinstance(extractor, _FakeClaimExtractor)
    assert len(construct_calls) == 1

    # Second call returns the same instance — no re-construction.
    again = v2_app_module._get_extractor()
    assert again is extractor
    assert len(construct_calls) == 1
