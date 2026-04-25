"""Tests for src.verifiers.retrieval_verifier."""

from __future__ import annotations

import pytest
pytestmark = pytest.mark.skip(
    reason="v0.3 migration: retrieval verifier rewritten in Section 5; tests there"
)

import os
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from src.fact_store import FactStore
from src.pattern_registry import load_default_registry, reset_cache
from src.verifiers.python_verifiers import VerificationOutcome
from src.verifiers.retrieval_verifier import (
    JudgeVerdict,
    RetrievalResult,
    RetrievalVerifier,
    Snippet,
    parse_judge_response,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_cache()
    yield
    reset_cache()


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "t.db")
    yield s
    s.close()


@dataclass
class FakeLLM:
    """Stand-in for LLMClient. The judge always uses .rewrite()."""

    rewrite_responses: list[str] = field(default_factory=list)
    rewrite_calls: list[dict] = field(default_factory=list)

    def rewrite(self, system, user_message, max_tokens=2048):
        self.rewrite_calls.append({"system": system, "user_message": user_message})
        return self.rewrite_responses.pop(0)


def _claim(predicate="capital_of", subject="Paris", object="France"):
    return {
        "subject": subject,
        "predicate": predicate,
        "object": object,
        "object_type": "entity",
        "polarity": 1,
        "source_text": f"{subject} ... {object}",
    }


# ---------- judge response parser ----------


def test_parse_supported():
    v = parse_judge_response("SUPPORTED\nJustification: it matches")
    assert v.verdict == "SUPPORTED"
    assert "matches" in v.justification


def test_parse_contradicted():
    v = parse_judge_response("CONTRADICTED\nJustification: snippets disagree")
    assert v.verdict == "CONTRADICTED"


def test_parse_insufficient():
    v = parse_judge_response("INSUFFICIENT_EVIDENCE\nJustification: snippets are silent")
    assert v.verdict == "INSUFFICIENT_EVIDENCE"


def test_parse_no_justification_token():
    v = parse_judge_response("SUPPORTED\nLooks right based on snippet 1")
    assert v.verdict == "SUPPORTED"
    assert "snippet 1" in v.justification


def test_parse_malformed_returns_none():
    assert parse_judge_response("totally not the right format") is None
    assert parse_judge_response("") is None
    assert parse_judge_response("MAYBE\nJustification: x") is None


# ---------- happy paths ----------


def _verifier(store, llm, search_results: list[Snippet]):
    return RetrievalVerifier(
        store=store,
        llm=llm,
        registry=load_default_registry(),
        search_fn=lambda q: search_results,
        ttl_hours=1,
    )


def test_supported_path_returns_verified(store):
    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJustification: snippet 1 confirms it."])
    snippets = [Snippet("Paris - Wikipedia", "Paris is the capital of France.", "https://en.wikipedia.org/Paris")]
    v = _verifier(store, llm, snippets)
    r = v.verify(_claim())
    assert r.outcome is VerificationOutcome.VERIFIED
    assert r.verdict.verdict == "SUPPORTED"
    assert r.snippets == snippets
    assert r.error_flag is None


def test_contradicted_path_returns_contradicted(store):
    llm = FakeLLM(rewrite_responses=["CONTRADICTED\nJustification: snippet says otherwise."])
    snippets = [Snippet("Paris", "Lyon is the capital of France (joke).", "https://x")]
    v = _verifier(store, llm, snippets)
    r = v.verify(_claim())
    assert r.outcome is VerificationOutcome.CONTRADICTED
    assert r.verdict.verdict == "CONTRADICTED"


def test_insufficient_returns_inconclusive(store):
    llm = FakeLLM(
        rewrite_responses=["INSUFFICIENT_EVIDENCE\nJustification: nothing relevant."]
    )
    snippets = [Snippet("Lyon", "Lyon is a city.", "https://x")]
    v = _verifier(store, llm, snippets)
    r = v.verify(_claim())
    assert r.outcome is VerificationOutcome.INCONCLUSIVE
    assert r.error_flag is None  # judge replied cleanly
    assert r.verdict.verdict == "INSUFFICIENT_EVIDENCE"


# ---------- failure modes ----------


def test_network_error_returns_retrieval_error(store):
    def raises(_q):
        raise httpx.ConnectError("network down")

    v = RetrievalVerifier(
        store=store,
        llm=FakeLLM(),
        registry=load_default_registry(),
        search_fn=raises,
        ttl_hours=1,
    )
    r = v.verify(_claim())
    assert r.outcome is VerificationOutcome.INCONCLUSIVE
    assert r.error_flag == "retrieval_error"
    assert "network down" in r.explanation


def test_no_results_returns_no_results_flag(store):
    v = _verifier(store, FakeLLM(), [])
    r = v.verify(_claim())
    assert r.outcome is VerificationOutcome.INCONCLUSIVE
    assert r.error_flag == "no_results"


def test_malformed_judge_output_returns_judge_parse_error(store):
    llm = FakeLLM(rewrite_responses=["uhhhh I dunno"])
    snippets = [Snippet("x", "y", "z")]
    v = _verifier(store, llm, snippets)
    r = v.verify(_claim())
    assert r.outcome is VerificationOutcome.INCONCLUSIVE
    assert r.error_flag == "judge_parse_error"
    assert r.snippets == snippets


def test_judge_call_failure_returns_judge_error(store):
    @dataclass
    class CrashLLM:
        def rewrite(self, *args, **kwargs):
            raise RuntimeError("anthropic exploded")

    snippets = [Snippet("x", "y", "z")]
    v = RetrievalVerifier(
        store=store,
        llm=CrashLLM(),
        registry=load_default_registry(),
        search_fn=lambda q: snippets,
        ttl_hours=1,
    )
    r = v.verify(_claim())
    assert r.outcome is VerificationOutcome.INCONCLUSIVE
    assert r.error_flag == "judge_error"


# ---------- caching ----------


def test_cache_hit_skips_network_on_repeat(store):
    call_count = {"n": 0}
    snippets = [Snippet("Paris", "Paris is the capital of France.", "https://x")]

    def search(_q):
        call_count["n"] += 1
        return snippets

    llm = FakeLLM(
        rewrite_responses=[
            "SUPPORTED\nJustification: yes.",
            "SUPPORTED\nJustification: yes again.",
        ]
    )
    v = RetrievalVerifier(
        store=store,
        llm=llm,
        registry=load_default_registry(),
        search_fn=search,
        ttl_hours=1,
    )
    r1 = v.verify(_claim())
    r2 = v.verify(_claim())
    assert call_count["n"] == 1, "second call must hit cache, not search"
    assert r1.from_cache is False
    assert r2.from_cache is True


def test_cache_expires_after_ttl(store):
    snippets = [Snippet("x", "y", "z")]
    call_count = {"n": 0}

    def search(_q):
        call_count["n"] += 1
        return snippets

    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJ: ok"] * 2)
    # ttl_hours=0 means every entry is expired immediately
    v = RetrievalVerifier(
        store=store,
        llm=llm,
        registry=load_default_registry(),
        search_fn=search,
        ttl_hours=0,
    )
    v.verify(_claim())
    v.verify(_claim())
    assert call_count["n"] == 2


# ---------- query template ----------


def test_query_uses_predicate_template():
    reg = load_default_registry()

    @dataclass
    class Recorder:
        queries: list[str] = field(default_factory=list)

        def __call__(self, q):
            self.queries.append(q)
            return [Snippet("t", "s", "u")]

    rec = Recorder()
    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJ: ok"])
    # Use a real store fixture-style by hand
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        s = FactStore(os.path.join(td, "x.db"))
        try:
            v = RetrievalVerifier(store=s, llm=llm, registry=reg, search_fn=rec, ttl_hours=1)
            v.verify(
                {
                    "subject": "Donald Trump",
                    "predicate": "holds_role",
                    "object": "US President",
                    "object_type": "string",
                    "polarity": 1,
                    "source_text": "...",
                }
            )
        finally:
            s.close()
    assert rec.queries == ["current US President"]


def test_query_falls_back_when_no_template(tmp_path):
    """A retrieval predicate without a template uses '{subject} {object}'."""
    import yaml

    yaml_path = tmp_path / "p.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "no_template_pred": {
                    "object_type": "entity",
                    "verification_method": "retrieval",
                    "description": "x",
                    "example": "x",
                }
            }
        ),
        encoding="utf-8",
    )
    from src.pattern_registry import PredicateRegistry

    reg = PredicateRegistry.from_yaml(yaml_path)

    captured: list[str] = []

    def search(q):
        captured.append(q)
        return [Snippet("t", "s", "u")]

    s = FactStore(tmp_path / "x.db")
    try:
        v = RetrievalVerifier(
            store=s,
            llm=FakeLLM(rewrite_responses=["SUPPORTED\nJ: ok"]),
            registry=reg,
            search_fn=search,
            ttl_hours=1,
        )
        v.verify(
            {
                "subject": "Foo",
                "predicate": "no_template_pred",
                "object": "Bar",
                "object_type": "entity",
                "polarity": 1,
                "source_text": "x",
            }
        )
    finally:
        s.close()
    assert captured == ["Foo Bar"]


# ---------- real API (gated) ----------


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="real network + LLM gated behind RUN_API_TESTS=1",
)
def test_real_retrieval_marie_curie_supported(tmp_path):
    """Live retrieval — DuckDuckGo + Anthropic judge. Flaky by design.

    Asserts that for a well-known fact, the verifier returns either
    VERIFIED or INCONCLUSIVE — both are acceptable here, since DDG can
    return thin results. CONTRADICTED would be a real bug.
    """
    from src.llm_client import LLMClient

    s = FactStore(tmp_path / "real.db")
    try:
        v = RetrievalVerifier(
            store=s, llm=LLMClient(), registry=load_default_registry(), ttl_hours=1
        )
        r = v.verify(
            {
                "subject": "Marie Curie",
                "predicate": "is_a",
                "object": "physicist",
                "object_type": "string",
                "polarity": 1,
                "source_text": "Marie Curie was a physicist",
            }
        )
        assert r.outcome is not VerificationOutcome.CONTRADICTED, r.to_dict()
    finally:
        s.close()
