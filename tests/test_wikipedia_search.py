"""Tests for src.verifiers.scrapers.wikipedia — the
Wikipedia-direct retrieval provider that replaced the failing
DDG-only path as the primary search source.

Hermetic: every test stubs httpx.Client.get with canned responses.
A real-API test against Wikipedia is gated behind RUN_API_TESTS=1
at the bottom.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import pytest

from src.verifiers.retrieval_verifier import Snippet
from src.verifiers.scrapers.wikipedia import (
    EXTRACT_CHAR_CAP,
    USER_AGENT,
    WIKIPEDIA_API,
    search_wikipedia,
)


@dataclass
class _StubResponse:
    body: dict
    status_code: int = 200

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=httpx.Request("GET", WIKIPEDIA_API),
                response=httpx.Response(self.status_code),
            )


class _StubClient:
    """Minimal httpx.Client-shaped stub. Stores recorded requests so
    tests can assert on the params we sent (search ranking, extract
    titles)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, *, params=None, **_kw):
        self.calls.append({"url": url, "params": dict(params or {})})
        return self._responses.pop(0)

    def close(self):
        pass


def _search_response(titles):
    return _StubResponse({
        "query": {
            "search": [{"title": t, "snippet": ""} for t in titles],
        },
    })


def _extract_response(by_title):
    """``by_title`` maps title → extract text. URL fabricated."""
    return _StubResponse({
        "query": {
            "pages": {
                str(i): {
                    "title": title,
                    "extract": text,
                    "fullurl": f"https://en.wikipedia.org/wiki/"
                               f"{title.replace(' ', '_')}",
                }
                for i, (title, text) in enumerate(by_title.items())
            },
        },
    })


def test_search_wikipedia_two_phase_returns_snippets():
    """Happy path: search call returns titles, extract call returns
    intros. Output is a Snippet list in search-rank order."""
    client = _StubClient([
        _search_response(["Marie Curie", "Pierre Curie", "Curie family"]),
        _extract_response({
            "Marie Curie": "Marie Curie was a Polish-French physicist...",
            "Pierre Curie": "Pierre Curie was a French physicist...",
            "Curie family": "The Curie family is a French family...",
        }),
    ])
    snippets = search_wikipedia("Marie Curie", client=client)

    assert len(snippets) == 3
    assert all(isinstance(s, Snippet) for s in snippets)
    assert snippets[0].title == "Marie Curie"
    assert snippets[0].snippet.startswith("Marie Curie was a Polish")
    assert snippets[0].url == "https://en.wikipedia.org/wiki/Marie_Curie"
    # Search ranking preserved (titles in same order as the search response).
    assert [s.title for s in snippets] == [
        "Marie Curie", "Pierre Curie", "Curie family",
    ]


def test_search_wikipedia_passes_correct_params():
    """The search call uses list=search + srsearch; the extract call
    uses prop=extracts + exintro=1 + explaintext=1. Tests pin the
    MediaWiki API contract so a future change can't silently change
    semantics."""
    client = _StubClient([
        _search_response(["Donald Trump"]),
        _extract_response({"Donald Trump": "Donald John Trump..."}),
    ])
    search_wikipedia("Trump children", client=client, top_n=3)

    assert len(client.calls) == 2
    # Phase 1: search.
    p1 = client.calls[0]["params"]
    assert p1["action"] == "query"
    assert p1["list"] == "search"
    assert p1["srsearch"] == "Trump children"
    assert p1["srlimit"] == 3
    # Phase 2: extracts.
    p2 = client.calls[1]["params"]
    assert p2["action"] == "query"
    assert p2["prop"] == "extracts|info"
    assert p2["exintro"] == "1"
    assert p2["explaintext"] == "1"
    assert p2["titles"] == "Donald Trump"


def test_search_wikipedia_caps_extract_length():
    """Long page intros get truncated to keep the judge prompt small."""
    huge = "x" * 5000
    client = _StubClient([
        _search_response(["Very Long Page"]),
        _extract_response({"Very Long Page": huge}),
    ])
    snippets = search_wikipedia("anything", client=client)
    assert len(snippets) == 1
    assert len(snippets[0].snippet) == EXTRACT_CHAR_CAP


def test_search_wikipedia_returns_empty_on_no_results():
    """No matches → empty list. Caller falls through to next provider."""
    client = _StubClient([_search_response([])])
    snippets = search_wikipedia("hgkjhgkjhgkjhg-no-such-page", client=client)
    assert snippets == []
    # Phase 2 was not called — search returned 0 hits.
    assert len(client.calls) == 1


def test_search_wikipedia_skips_pages_with_empty_extract():
    """Some pages exist but have empty extracts (disambiguation /
    redirect / stubs). Skip them rather than emitting empty snippets."""
    client = _StubClient([
        _search_response(["Real Page", "Empty Page"]),
        _extract_response({
            "Real Page": "Real content here.",
            "Empty Page": "",
        }),
    ])
    snippets = search_wikipedia("query", client=client)
    assert len(snippets) == 1
    assert snippets[0].title == "Real Page"


def test_search_wikipedia_url_falls_back_when_fullurl_missing():
    """If the API response omits fullurl, build one from the title.
    The Wikipedia URL convention is /wiki/Title_With_Underscores."""
    client = _StubClient([
        _search_response(["No URL Page"]),
        _StubResponse({
            "query": {
                "pages": {
                    "1": {"title": "No URL Page", "extract": "content"}
                    # no fullurl
                },
            },
        }),
    ])
    snippets = search_wikipedia("query", client=client)
    assert snippets[0].url == "https://en.wikipedia.org/wiki/No_URL_Page"


def test_search_wikipedia_propagates_http_error():
    """A 5xx surfaces as httpx.HTTPStatusError so the default_search
    chain can swallow it and fall through to the next provider."""
    import httpx
    client = _StubClient([_StubResponse({}, status_code=503)])
    with pytest.raises(httpx.HTTPStatusError):
        search_wikipedia("query", client=client)


def test_search_wikipedia_constructs_default_client_with_user_agent():
    """When no client is injected, search_wikipedia builds an
    httpx.Client with the User-Agent that Wikipedia's bot policy
    requires. Verifies the policy compliance contract."""
    import httpx
    captured: dict = {}
    real_init = httpx.Client.__init__

    def spy_init(self, *args, **kwargs):
        captured.update(kwargs)
        # Make the client immediately raise so we don't actually hit
        # Wikipedia.
        self._closed = False  # placate Client._raise_if_closed
        # Use the real init then we'll close before any real call.
        real_init(self, *args, **kwargs)

    import unittest.mock as mock
    with mock.patch.object(httpx.Client, "__init__", spy_init):
        with mock.patch.object(
            httpx.Client, "get",
            side_effect=Exception("blocked outbound"),
        ):
            try:
                search_wikipedia("query")
            except Exception:
                pass

    assert "headers" in captured
    assert captured["headers"]["User-Agent"] == USER_AGENT


# ---- real-API smoke (gated) ----


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="real Wikipedia request gated behind RUN_API_TESTS=1",
)
def test_search_wikipedia_real_marie_curie():
    snippets = search_wikipedia("Marie Curie")
    assert len(snippets) >= 1
    assert any("Curie" in s.title for s in snippets)
    assert any("physicist" in s.snippet.lower() or "chemist" in s.snippet.lower()
               for s in snippets)
