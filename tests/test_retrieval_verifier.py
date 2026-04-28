"""Tests for src.verifiers.retrieval_verifier (v0.3 — slots-aware, multi-attempt)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx
import pytest

from src.fact_store import FactStore
from src.pattern_registry import load_default_registry, reset_cache
from src.verifiers.types import VerificationOutcome
from src.verifiers.retrieval_verifier import (
    JudgeVerdict,
    QueryAttempt,
    RetrievalResult,
    RetrievalVerifier,
    Snippet,
    build_queries,
    default_search,
    parse_judge_response,
    search_duckduckgo,
    search_serpapi,
    search_tavily,
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
    rewrite_responses: list[str] = field(default_factory=list)
    rewrite_calls: list[dict] = field(default_factory=list)

    def rewrite(self, system, user_message, max_tokens=2048):
        self.rewrite_calls.append({"system": system, "user_message": user_message})
        return self.rewrite_responses.pop(0)


_DEFAULT_SLOTS = {"agent": "Donald Trump", "role": "47th President", "org": "United States"}


def _claim(
    pattern="role_assignment",
    predicate="holds_role",
    slots=None,
    polarity=1,
):
    # Use `is None` so callers can pass {} explicitly to test empty-slot cases.
    return {
        "pattern": pattern,
        "predicate": predicate,
        "slots": _DEFAULT_SLOTS if slots is None else slots,
        "polarity": polarity,
        "source_text": "<src>",
    }


def _verifier(store, llm, fake_search):
    return RetrievalVerifier(
        store=store,
        llm=llm,
        registry=load_default_registry(),
        search_fn=fake_search,
        ttl_hours=1,
    )


# ---------- judge response parsing ----------


def test_parse_supported():
    v = parse_judge_response("SUPPORTED\nJustification: ok")
    assert v.verdict == "SUPPORTED"


def test_parse_contradicted():
    v = parse_judge_response("CONTRADICTED\nJustification: snippet 2 disagrees")
    assert v.verdict == "CONTRADICTED"


def test_parse_insufficient():
    v = parse_judge_response("INSUFFICIENT_EVIDENCE\nJustification: silent")
    assert v.verdict == "INSUFFICIENT_EVIDENCE"


def test_parse_malformed_returns_none():
    assert parse_judge_response("MAYBE") is None
    assert parse_judge_response("") is None


def test_default_search_prefers_tavily(monkeypatch):
    """When Wikipedia is empty and TAVILY_API_KEY is set, default_search
    calls Tavily — not SerpAPI or DDG. (Wikipedia is now the *first*
    provider; this test exercises the next-tier fall-through.)"""
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.setenv("SERPAPI_KEY", "serp-key")
    monkeypatch.setattr(
        "src.verifiers.scrapers.search_wikipedia",
        lambda q, **kw: [],
    )

    called: list[str] = []
    def fake_tavily(query, key, *, top_n=3):
        called.append(f"tavily({query!r}, {key!r})")
        return [Snippet(title="t", snippet="s", url="u")]
    monkeypatch.setattr(
        "src.verifiers.retrieval_verifier.search_tavily", fake_tavily,
    )

    result = default_search("test query")
    assert result == [Snippet(title="t", snippet="s", url="u")]
    assert called == ["tavily('test query', 'tavily-key')"]


def test_default_search_falls_to_serpapi_when_no_tavily(monkeypatch):
    """Wikipedia empty + no Tavily key + SerpAPI key set → SerpAPI."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("SERPAPI_KEY", "serp-key")
    monkeypatch.setattr(
        "src.verifiers.scrapers.search_wikipedia",
        lambda q, **kw: [],
    )

    called: list[str] = []
    def fake_serp(query, key, *, top_n=3):
        called.append(f"serp({query!r}, {key!r})")
        return [Snippet(title="s", snippet="x", url="y")]
    monkeypatch.setattr(
        "src.verifiers.retrieval_verifier.search_serpapi", fake_serp,
    )

    result = default_search("test")
    assert called == ["serp('test', 'serp-key')"]


def test_default_search_falls_to_ddg_when_no_keys(monkeypatch):
    """When Wikipedia returns nothing and no paid keys are set, fall
    through to DDG. (Wikipedia is now the primary provider — see
    test_default_search_prefers_wikipedia for the happy path.)"""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_KEY", raising=False)

    # Stub Wikipedia as empty so we exercise the fall-through.
    monkeypatch.setattr(
        "src.verifiers.scrapers.search_wikipedia",
        lambda q, **kw: [],
    )

    called: list[str] = []
    def fake_ddg(query, *, top_n=3):
        called.append(f"ddg({query!r})")
        return [Snippet(title="d", snippet="x", url="y")]
    monkeypatch.setattr(
        "src.verifiers.retrieval_verifier.search_duckduckgo", fake_ddg,
    )

    result = default_search("test")
    assert called == ["ddg('test')"]


def test_default_search_prefers_wikipedia_when_results_exist(monkeypatch):
    """Wikipedia is the first provider in the chain. When it returns
    results, no paid API or DDG fallback fires — single network call,
    no key required, no rate-limit risk."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_KEY", raising=False)

    wiki_calls: list[str] = []
    def fake_wiki(query, **kw):
        wiki_calls.append(query)
        return [Snippet(title="W", snippet="from wikipedia", url="https://en.wikipedia.org/wiki/W")]
    monkeypatch.setattr(
        "src.verifiers.scrapers.search_wikipedia", fake_wiki,
    )

    ddg_calls: list = []
    monkeypatch.setattr(
        "src.verifiers.retrieval_verifier.search_duckduckgo",
        lambda q, **kw: ddg_calls.append(q) or [],
    )

    result = default_search("Marie Curie Nobel Prize year")
    assert len(result) == 1 and result[0].title == "W"
    assert wiki_calls == ["Marie Curie Nobel Prize year"]
    assert ddg_calls == []  # never reached


def test_default_search_falls_through_when_wikipedia_raises(monkeypatch):
    """If Wikipedia errors (network blip, schema change), the chain
    silently falls through to the next provider rather than failing
    the whole turn."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_KEY", raising=False)

    def boom_wiki(query, **kw):
        raise RuntimeError("wikipedia unreachable")
    monkeypatch.setattr(
        "src.verifiers.scrapers.search_wikipedia", boom_wiki,
    )

    def fake_ddg(query, *, top_n=3):
        return [Snippet(title="d", snippet="x", url="y")]
    monkeypatch.setattr(
        "src.verifiers.retrieval_verifier.search_duckduckgo", fake_ddg,
    )

    result = default_search("anything")
    assert len(result) == 1 and result[0].title == "d"


def test_search_tavily_parses_response(monkeypatch):
    """search_tavily extracts title/content/url from the Tavily response
    shape into Snippet objects."""
    fake_response = type("R", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: {
            "results": [
                {"title": "T1", "content": "C1", "url": "U1"},
                {"title": "T2", "content": "C2", "url": "U2"},
                {"title": "T3", "content": "C3", "url": "U3"},
                {"title": "T4", "content": "C4", "url": "U4"},  # truncated by top_n
            ]
        },
    })()
    monkeypatch.setattr(
        "src.verifiers.retrieval_verifier.httpx.post",
        lambda *a, **kw: fake_response,
    )
    snippets = search_tavily("q", "key", top_n=3)
    assert len(snippets) == 3
    assert snippets[0].title == "T1"
    assert snippets[0].snippet == "C1"
    assert snippets[0].url == "U1"


def test_search_duckduckgo_retries_with_alternate_ua_on_empty(monkeypatch):
    """DDG often returns 0 results due to bot fingerprinting. The new
    retry rotates User-Agent and tries again. Test: first attempt
    returns empty; second attempt with different UA returns results."""
    from src.verifiers import retrieval_verifier as rv

    calls: list[str] = []
    def fake_ddg_attempt(query, user_agent, top_n):
        calls.append(user_agent)
        if len(calls) == 1:
            return []  # first UA: 0 results
        return [Snippet(title="t", snippet="s", url="u")]

    monkeypatch.setattr(rv, "_ddg_attempt", fake_ddg_attempt)

    results = rv.search_duckduckgo("test query")
    assert len(results) == 1
    # Both UAs were tried.
    assert len(calls) == 2
    # And they were different — the retry actually rotates.
    assert calls[0] != calls[1]


def test_search_duckduckgo_returns_empty_when_all_uas_fail(monkeypatch):
    from src.verifiers import retrieval_verifier as rv

    calls: list[str] = []
    def fake_ddg_attempt(query, user_agent, top_n):
        calls.append(user_agent)
        return []

    monkeypatch.setattr(rv, "_ddg_attempt", fake_ddg_attempt)

    results = rv.search_duckduckgo("test query")
    assert results == []
    # All UAs were exhausted.
    assert len(calls) == len(rv._USER_AGENTS)


def test_search_duckduckgo_first_ua_success_skips_rest(monkeypatch):
    from src.verifiers import retrieval_verifier as rv

    calls: list[str] = []
    def fake_ddg_attempt(query, user_agent, top_n):
        calls.append(user_agent)
        return [Snippet(title="t", snippet="s", url="u")]

    monkeypatch.setattr(rv, "_ddg_attempt", fake_ddg_attempt)

    results = rv.search_duckduckgo("test query")
    assert len(results) == 1
    # Only the first UA was tried.
    assert len(calls) == 1


# ---- direct tests for _ddg_attempt HTML parsing ----------------------


def test_ddg_attempt_parses_results_from_html(monkeypatch):
    """_ddg_attempt is the inner function that actually parses DDG's
    HTML. The wrapping search_duckduckgo() retries-on-empty layer is
    well-covered, but this is the actual parse logic. Mock httpx and
    check the BeautifulSoup selectors find title/snippet/url."""
    from src.verifiers import retrieval_verifier as rv

    captured: dict = {}

    class _StubResp:
        text = (
            "<html><body>"
            "<div class='result'>"
            "  <a class='result__a' href='https://example.com/1'>Title One</a>"
            "  <span class='result__snippet'>First snippet body.</span>"
            "</div>"
            "<div class='result'>"
            "  <a class='result__a' href='https://example.com/2'>Title Two</a>"
            "  <span class='result__snippet'>Second snippet body.</span>"
            "</div>"
            "</body></html>"
        )

        def raise_for_status(self):
            return None

    def fake_post(url, *, data, headers, timeout, follow_redirects):
        captured["url"] = url
        captured["q"] = data["q"]
        captured["ua"] = headers["User-Agent"]
        return _StubResp()

    monkeypatch.setattr(rv.httpx, "post", fake_post)

    results = rv._ddg_attempt(
        "marie curie nobel year",
        rv._USER_AGENTS[0],
        top_n=3,
    )
    assert len(results) == 2
    assert results[0].title == "Title One"
    assert results[0].snippet == "First snippet body."
    assert results[0].url == "https://example.com/1"
    # The request actually went to the DDG html endpoint with the query
    # as POST data, and our chosen UA header.
    assert captured["url"] == rv._DDG_URL
    assert captured["q"] == "marie curie nobel year"
    assert captured["ua"] == rv._USER_AGENTS[0]


def test_ddg_attempt_skips_results_with_missing_pieces(monkeypatch):
    """A real-world DDG hit list mixes well-formed result divs with
    ones that are missing the title link, the snippet, or both. The
    parser should silently skip those rather than producing partial
    Snippets with empty fields."""
    from src.verifiers import retrieval_verifier as rv

    class _StubResp:
        text = (
            "<html><body>"
            # Missing snippet (no .result__snippet child) — should be skipped.
            "<div class='result'>"
            "  <a class='result__a' href='https://x.com/a'>Title only</a>"
            "</div>"
            # Missing title link — should be skipped.
            "<div class='result'>"
            "  <span class='result__snippet'>Snippet only</span>"
            "</div>"
            # Empty strings even though the elements exist — should be skipped.
            "<div class='result'>"
            "  <a class='result__a' href='https://x.com/c'></a>"
            "  <span class='result__snippet'></span>"
            "</div>"
            # Well-formed — kept.
            "<div class='result'>"
            "  <a class='result__a' href='https://x.com/d'>Real Title</a>"
            "  <span class='result__snippet'>Real snippet</span>"
            "</div>"
            "</body></html>"
        )

        def raise_for_status(self):
            return None

    monkeypatch.setattr(rv.httpx, "post",
                        lambda *a, **kw: _StubResp())

    results = rv._ddg_attempt("q", rv._USER_AGENTS[0], top_n=3)
    assert len(results) == 1
    assert results[0].title == "Real Title"


def test_ddg_attempt_caps_at_top_n(monkeypatch):
    """_ddg_attempt scans up to top_n*3 result divs but stops adding
    after top_n usable Snippets. This guards against a rare DDG layout
    that pushes 50+ results into the page."""
    from src.verifiers import retrieval_verifier as rv

    rows = "".join(
        f"<div class='result'>"
        f"  <a class='result__a' href='https://x.com/{i}'>T{i}</a>"
        f"  <span class='result__snippet'>S{i}</span>"
        f"</div>"
        for i in range(20)
    )

    class _StubResp:
        text = f"<html><body>{rows}</body></html>"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(rv.httpx, "post",
                        lambda *a, **kw: _StubResp())

    results = rv._ddg_attempt("q", rv._USER_AGENTS[0], top_n=3)
    assert len(results) == 3
    assert [r.title for r in results] == ["T0", "T1", "T2"]


def test_ddg_attempt_propagates_http_error(monkeypatch):
    """If the DDG response is non-200, raise_for_status raises and
    _ddg_attempt lets the exception propagate to its caller. The
    caller (search_duckduckgo) doesn't currently wrap this — it
    crashes the verifier turn — but it's worth pinning the contract
    so anyone changing the wrapper sees the expected propagation."""
    from src.verifiers import retrieval_verifier as rv

    class _StubResp:
        text = ""

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "503 Service Unavailable",
                request=httpx.Request("POST", rv._DDG_URL),
                response=httpx.Response(503),
            )

    monkeypatch.setattr(rv.httpx, "post",
                        lambda *a, **kw: _StubResp())

    with pytest.raises(httpx.HTTPStatusError):
        rv._ddg_attempt("q", rv._USER_AGENTS[0], top_n=3)


def test_search_serpapi_parses_response(monkeypatch):
    fake_response = type("R", (), {
        "raise_for_status": lambda self: None,
        "json": lambda self: {
            "organic_results": [
                {"title": "T1", "snippet": "S1", "link": "L1"},
                {"title": "T2", "snippet": "S2", "link": "L2"},
            ]
        },
    })()
    monkeypatch.setattr(
        "src.verifiers.retrieval_verifier.httpx.get",
        lambda *a, **kw: fake_response,
    )
    snippets = search_serpapi("q", "key", top_n=5)
    assert len(snippets) == 2
    assert snippets[0].title == "T1"
    assert snippets[1].url == "L2"


def test_parse_accepts_abbreviations():
    """Phase-2 dogfood (Tokyo→Edo) surfaced the judge LLM emitting
    'SUPPORT' instead of the prompted 'SUPPORTED'. Accept the common
    abbreviated forms as aliases for their canonical labels."""
    cases = [
        ("SUPPORT\nJustification: clear", "SUPPORTED"),
        ("Supports\nJustification: yep", "SUPPORTED"),
        ("CONTRADICT\nJustification: nope", "CONTRADICTED"),
        ("Contradicts\nJustification: explicitly", "CONTRADICTED"),
        ("INSUFFICIENT\nJustification: thin", "INSUFFICIENT_EVIDENCE"),
        ("INCONCLUSIVE\nJustification: ambiguous", "INSUFFICIENT_EVIDENCE"),
        ("UNCLEAR\nJustification: meh", "INSUFFICIENT_EVIDENCE"),
    ]
    for raw, expected in cases:
        v = parse_judge_response(raw)
        assert v is not None, f"parser refused {raw!r}"
        assert v.verdict == expected, (
            f"{raw!r} produced {v.verdict!r}, expected {expected!r}"
        )


# ---------- build_queries ----------


def test_build_queries_role_assignment_three_attempts():
    """Spec: queries try most-specific to least-specific, in order."""
    reg = load_default_registry()
    queries = build_queries(
        reg.get("role_assignment"),
        {"agent": "Donald Trump", "role": "47th President", "org": "United States"},
    )
    assert queries == [
        "Donald Trump 47th President",
        "Donald Trump United States 47th President",
        "Donald Trump",
    ]


def test_build_queries_skips_template_with_missing_slot():
    """If 'org' is missing, the {agent} {org} {role} template is skipped."""
    reg = load_default_registry()
    queries = build_queries(
        reg.get("role_assignment"),
        {"agent": "Tim Cook", "role": "CEO"},
    )
    # Skipped: "{agent} {org} {role}". Remaining: "{agent} {role}", "{agent}".
    assert queries == ["Tim Cook CEO", "Tim Cook"]


def test_build_queries_never_prepends_current():
    """Spec: 'do not prepend current' to any query."""
    reg = load_default_registry()
    queries = build_queries(
        reg.get("role_assignment"),
        {"agent": "Donald Trump", "role": "47th President", "org": "United States"},
    )
    for q in queries:
        assert "current" not in q.lower(), q


def test_build_queries_event_handles_participant_list():
    reg = load_default_registry()
    queries = build_queries(
        reg.get("event"),
        {"event_type": "inauguration", "participants": ["Donald Trump"]},
    )
    assert any("inauguration" in q and "Donald Trump" in q for q in queries)


# ---------- multi-attempt strategy ----------


def test_first_attempt_with_two_results_is_used(store):
    """If attempt 1 returns >= 2 results, attempt 2 should not run."""
    call_log: list[str] = []

    def fake_search(q):
        call_log.append(q)
        # First query returns 2 results, second should never be called
        return [
            Snippet("t1", "sn1", "u1"),
            Snippet("t2", "sn2", "u2"),
        ]

    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJ: ok"])
    v = _verifier(store, llm, fake_search)
    r = v.verify(_claim())
    assert call_log == ["Donald Trump 47th President"]
    assert r.outcome is VerificationOutcome.VERIFIED
    used = [a for a in r.attempts if a.used]
    assert len(used) == 1
    assert used[0].query == "Donald Trump 47th President"


def test_falls_through_to_next_attempt_when_first_returns_zero(store):
    """Section 9 #5: attempt 1 returns 0; attempt 2 returns 3; attempt 2 wins."""
    call_log: list[str] = []
    results = {
        "Donald Trump 47th President": [],
        "Donald Trump United States 47th President": [
            Snippet("t1", "sn1", "u1"),
            Snippet("t2", "sn2", "u2"),
            Snippet("t3", "sn3", "u3"),
        ],
    }

    def fake_search(q):
        call_log.append(q)
        return results.get(q, [])

    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJ: snippet 1"])
    v = _verifier(store, llm, fake_search)
    r = v.verify(_claim())
    assert call_log == [
        "Donald Trump 47th President",
        "Donald Trump United States 47th President",
    ]
    assert r.outcome is VerificationOutcome.VERIFIED
    # Trace records BOTH attempts; second one is .used=True.
    assert len(r.attempts) == 2
    assert r.attempts[0].used is False
    assert r.attempts[0].result_count == 0
    assert r.attempts[1].used is True
    assert r.attempts[1].result_count == 3


def test_falls_through_when_first_returns_only_one_result(store):
    """A single result isn't enough — < 2 → continue."""
    call_log: list[str] = []
    results = {
        "Donald Trump 47th President": [Snippet("t", "s", "u")],
        "Donald Trump United States 47th President": [
            Snippet("t1", "s1", "u1"),
            Snippet("t2", "s2", "u2"),
        ],
    }

    def fake_search(q):
        call_log.append(q)
        return results.get(q, [])

    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJ: ok"])
    v = _verifier(store, llm, fake_search)
    r = v.verify(_claim())
    assert len(call_log) == 2
    assert r.attempts[0].result_count == 1
    assert r.attempts[0].used is False


def test_all_attempts_return_zero_yields_no_results_flag(store):
    def fake_search(q):
        return []

    llm = FakeLLM()
    v = _verifier(store, llm, fake_search)
    r = v.verify(_claim())
    assert r.outcome is VerificationOutcome.INCONCLUSIVE
    assert r.error_flag == "no_results"


def test_network_error_on_one_attempt_continues_to_next(store):
    """An error on attempt 1 should not abort the strategy."""
    call_log: list[str] = []

    def fake_search(q):
        call_log.append(q)
        if q == "Donald Trump 47th President":
            raise httpx.ConnectError("flake")
        return [Snippet("t1", "s1", "u1"), Snippet("t2", "s2", "u2")]

    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJ: ok"])
    v = _verifier(store, llm, fake_search)
    r = v.verify(_claim())
    assert r.outcome is VerificationOutcome.VERIFIED
    assert r.attempts[0].error is not None
    assert r.attempts[1].used is True


# ---------- pipeline_events logging ----------


def test_attempts_logged_to_pipeline_events(store):
    """Section 5 spec: each attempt gets a retrieval_query_attempt event."""
    def fake_search(q):
        if q == "Donald Trump 47th President":
            return []
        return [Snippet("t1", "s1", "u1"), Snippet("t2", "s2", "u2")]

    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJ: ok"])
    v = _verifier(store, llm, fake_search)
    tid = store.insert_turn("assistant", "draft")
    v.verify(_claim(), source_turn_id=tid)
    events = store.get_pipeline_events(tid)
    attempts_logged = [e for e in events if e["stage"] == "retrieval_query_attempt"]
    assert len(attempts_logged) == 2
    assert attempts_logged[0]["data"]["query"] == "Donald Trump 47th President"
    assert attempts_logged[0]["data"]["result_count"] == 0


# ---------- current vs historical judge prompts ----------


def test_current_claim_uses_current_judge_prompt(store):
    """A claim with no valid_until should use the CURRENT judge prompt."""
    def fake_search(q):
        return [Snippet("t1", "s1", "u1"), Snippet("t2", "s2", "u2")]

    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJ: ok"])
    v = _verifier(store, llm, fake_search)
    v.verify(_claim())  # no valid_until
    sys = llm.rewrite_calls[0]["system"]
    assert "CURRENT-TENSE" in sys
    assert "HISTORICAL" not in sys


def test_historical_claim_uses_historical_judge_prompt(store):
    """A claim with valid_until set should use the HISTORICAL prompt and pass dates."""
    def fake_search(q):
        return [Snippet("t1", "s1", "u1"), Snippet("t2", "s2", "u2")]

    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJ: ok"])
    v = _verifier(store, llm, fake_search)
    v.verify(_claim(slots={
        "agent": "Donald Trump", "role": "45th President", "org": "United States",
        "valid_from": "2017-01-20", "valid_until": "2021-01-20",
    }))
    sys = llm.rewrite_calls[0]["system"]
    user = llm.rewrite_calls[0]["user_message"]
    assert "HISTORICAL" in sys
    assert "2017-01-20" in user
    assert "2021-01-20" in user


# ---------- caching is per-query attempt ----------


def test_each_attempt_caches_independently(store):
    """Cache is keyed by the actual query string, so each attempt caches separately."""
    call_log: list[str] = []
    results = {
        "Donald Trump 47th President": [],
        "Donald Trump United States 47th President": [
            Snippet("t1", "s1", "u1"), Snippet("t2", "s2", "u2"),
        ],
    }

    def fake_search(q):
        call_log.append(q)
        return list(results.get(q, []))

    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJ: ok", "SUPPORTED\nJ: ok"])
    v = _verifier(store, llm, fake_search)
    v.verify(_claim())
    v.verify(_claim())
    # First call: 2 search calls. Second call: ZERO (both cached).
    assert call_log == [
        "Donald Trump 47th President",
        "Donald Trump United States 47th President",
    ]


# ---------- failure modes ----------


def test_judge_parse_error(store):
    def fake_search(q):
        return [Snippet("t1", "s1", "u1"), Snippet("t2", "s2", "u2")]

    llm = FakeLLM(rewrite_responses=["uhh"])
    v = _verifier(store, llm, fake_search)
    r = v.verify(_claim())
    assert r.outcome is VerificationOutcome.INCONCLUSIVE
    assert r.error_flag == "judge_parse_error"


def test_judge_call_failure_returns_judge_error(store):
    @dataclass
    class CrashLLM:
        def rewrite(self, *args, **kwargs):
            raise RuntimeError("boom")

    def fake_search(q):
        return [Snippet("t1", "s1", "u1"), Snippet("t2", "s2", "u2")]

    v = RetrievalVerifier(
        store=store, llm=CrashLLM(), registry=load_default_registry(),
        search_fn=fake_search, ttl_hours=1,
    )
    r = v.verify(_claim())
    assert r.outcome is VerificationOutcome.INCONCLUSIVE
    assert r.error_flag == "judge_error"


def test_no_query_constructible_for_pattern(store):
    """If no template can be filled, return early with a specific error_flag."""
    def fake_search(q):
        return []

    llm = FakeLLM()
    v = _verifier(store, llm, fake_search)
    # role_assignment requires agent + role; both missing.
    r = v.verify(_claim(slots={}))
    # This will actually fail at extractor's required-slot validation in the
    # real pipeline; but the verifier itself must also be defensive.
    assert r.outcome is VerificationOutcome.INCONCLUSIVE
    assert r.error_flag == "no_query_constructible"


# ---------- real API gated test ----------


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="real network + LLM gated behind RUN_API_TESTS=1",
)
def test_real_retrieval_marie_curie(tmp_path):
    from src.llm_client import LLMClient

    s = FactStore(tmp_path / "real.db")
    try:
        v = RetrievalVerifier(
            store=s, llm=LLMClient(), registry=load_default_registry(), ttl_hours=1
        )
        r = v.verify({
            "pattern": "categorical",
            "predicate": "is_a",
            "slots": {"entity": "Marie Curie", "category": "physicist"},
            "polarity": 1,
            "source_text": "Marie Curie was a physicist",
        })
        assert r.outcome is not VerificationOutcome.CONTRADICTED, r.to_dict()
    finally:
        s.close()
