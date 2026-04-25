"""Real retrieval-based claim verification.

Pipeline per claim:

    1. Build a search query from the claim using the predicate's
       ``retrieval_query_template`` (or a "{subject} {object}" fallback).
    2. Look up the query in the retrieval cache. If hit and within TTL,
       skip the network call.
    3. Otherwise, search via Tavily (preferred), SerpAPI, or DuckDuckGo
       (default, no API key required).
    4. Cache the result.
    5. Send the claim + top snippets to the LLM judge.
    6. Map the verdict to a VerificationOutcome and return a
       ``RetrievalResult`` carrying enough metadata for the trace UI.

Failure modes are explicit and never crash the pipeline:
    - Network/HTTP error  → INCONCLUSIVE, error_flag='retrieval_error'
    - Zero snippets       → INCONCLUSIVE, error_flag='no_results'
    - Malformed verdict   → INCONCLUSIVE, error_flag='judge_parse_error'
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote_plus, urlencode

import httpx
from bs4 import BeautifulSoup

from src.fact_store import FactStore
from src.llm_client import LLMClient
from src.predicate_registry import PredicateRegistry
from src.verifiers.python_verifiers import VerificationOutcome, VerificationResult


_USER_AGENT = (
    "Mozilla/5.0 (Aedos research prototype) "
    "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
)
_DDG_URL = "https://html.duckduckgo.com/html/"
_TAVILY_URL = "https://api.tavily.com/search"
_SERPAPI_URL = "https://serpapi.com/search.json"
_REQUEST_TIMEOUT = 10.0
_TOP_N = 3
_DEFAULT_TTL_HOURS = 24


@dataclass
class Snippet:
    title: str
    snippet: str
    url: str

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "snippet": self.snippet, "url": self.url}


@dataclass
class JudgeVerdict:
    verdict: str  # 'SUPPORTED' | 'CONTRADICTED' | 'INSUFFICIENT_EVIDENCE'
    justification: str

    def to_dict(self) -> dict[str, str]:
        return {"verdict": self.verdict, "justification": self.justification}


@dataclass
class RetrievalResult:
    """Returned by RetrievalVerifier.verify().

    Mirrors the shape of VerificationResult so the router can treat it
    uniformly, and adds query/snippets/verdict/error_flag for the trace UI.
    """

    outcome: VerificationOutcome
    query: str
    snippets: list[Snippet] = field(default_factory=list)
    verdict: JudgeVerdict | None = None
    error_flag: str | None = None
    explanation: str = ""
    actual_value: Any | None = None  # for CONTRADICTED — what the evidence said
    from_cache: bool = False

    @property
    def verified(self) -> bool:
        return self.outcome is VerificationOutcome.VERIFIED

    @property
    def contradicted(self) -> bool:
        return self.outcome is VerificationOutcome.CONTRADICTED

    @property
    def inconclusive(self) -> bool:
        return self.outcome is VerificationOutcome.INCONCLUSIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "query": self.query,
            "snippets": [s.to_dict() for s in self.snippets],
            "verdict": self.verdict.to_dict() if self.verdict else None,
            "error_flag": self.error_flag,
            "explanation": self.explanation,
            "actual_value": self.actual_value,
            "from_cache": self.from_cache,
        }


# ---- search providers ------------------------------------------------


def search_duckduckgo(query: str, *, top_n: int = _TOP_N) -> list[Snippet]:
    """Scrape DuckDuckGo's HTML endpoint. No API key, but flaky in practice."""
    resp = httpx.post(
        _DDG_URL,
        data={"q": query},
        headers={"User-Agent": _USER_AGENT},
        timeout=_REQUEST_TIMEOUT,
        follow_redirects=True,
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    out: list[Snippet] = []
    for result in soup.select("div.result, div.web-result")[: top_n * 3]:
        title_el = result.select_one("a.result__a, h2 a")
        snippet_el = result.select_one(".result__snippet, .result__body")
        if not title_el or not snippet_el:
            continue
        title = title_el.get_text(strip=True)
        snippet_text = snippet_el.get_text(" ", strip=True)
        url = title_el.get("href", "")
        if not (title and snippet_text):
            continue
        out.append(Snippet(title=title, snippet=snippet_text, url=url))
        if len(out) >= top_n:
            break
    return out


def search_tavily(query: str, api_key: str, *, top_n: int = _TOP_N) -> list[Snippet]:
    resp = httpx.post(
        _TAVILY_URL,
        json={
            "api_key": api_key,
            "query": query,
            "max_results": top_n,
            "search_depth": "basic",
        },
        timeout=_REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    return [
        Snippet(
            title=str(r.get("title", "")),
            snippet=str(r.get("content", "")),
            url=str(r.get("url", "")),
        )
        for r in payload.get("results", [])[:top_n]
    ]


def search_serpapi(query: str, api_key: str, *, top_n: int = _TOP_N) -> list[Snippet]:
    resp = httpx.get(
        _SERPAPI_URL,
        params={"q": query, "api_key": api_key, "num": top_n},
        timeout=_REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    return [
        Snippet(
            title=str(r.get("title", "")),
            snippet=str(r.get("snippet", "")),
            url=str(r.get("link", "")),
        )
        for r in payload.get("organic_results", [])[:top_n]
    ]


def default_search(query: str) -> list[Snippet]:
    """Provider dispatch. Tavily > SerpAPI > DuckDuckGo (free fallback)."""
    if (key := os.getenv("TAVILY_API_KEY")):
        return search_tavily(query, key)
    if (key := os.getenv("SERPAPI_KEY")):
        return search_serpapi(query, key)
    return search_duckduckgo(query)


# ---- judge ----------------------------------------------------------

_JUDGE_SYSTEM = """You are a strict, evidence-bounded judge.

You receive a structured claim and a small set of search-result snippets.
Decide whether the snippets SUPPORT, CONTRADICT, or are INSUFFICIENT_EVIDENCE
for the claim. Use only the snippets — never your prior knowledge.

A claim is SUPPORTED only if the snippets clearly state or directly imply
the claim. It is CONTRADICTED only if the snippets directly state the
opposite. Otherwise return INSUFFICIENT_EVIDENCE.

Output format (exactly two lines, no preamble):
VERDICT
Justification: <one sentence>"""


def parse_judge_response(text: str) -> JudgeVerdict | None:
    """Parse the judge LLM's response. Returns None on malformed output."""
    if not text:
        return None
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return None
    first = lines[0].split()[0].upper().rstrip(":.,")
    if first not in ("SUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE"):
        return None
    rest = " ".join(lines[1:]).strip()
    if rest.lower().startswith("justification:"):
        rest = rest.split(":", 1)[1].strip()
    return JudgeVerdict(verdict=first, justification=rest or "(no justification)")


def _format_judge_user_message(claim: dict[str, Any], snippets: list[Snippet]) -> str:
    polarity_word = "asserts" if int(claim.get("polarity", 1)) == 1 else "denies"
    snippets_block = "\n\n".join(
        f"[{i + 1}] {s.title}\n{s.snippet}\nSource: {s.url}"
        for i, s in enumerate(snippets)
    )
    return (
        f"Claim: subject={claim['subject']!r}, predicate={claim['predicate']!r}, "
        f"object={claim['object']!r}; the speaker {polarity_word} this relation.\n\n"
        f"Snippets:\n{snippets_block}\n\n"
        "Respond with the required two-line format."
    )


# ---- verifier -------------------------------------------------------


class RetrievalVerifier:
    """Composable retrieval verifier.

    Inject ``search_fn`` to mock search behavior in tests; inject ``llm`` to
    mock the judge call. The cache lives in the FactStore.
    """

    def __init__(
        self,
        store: FactStore,
        llm: LLMClient,
        registry: PredicateRegistry,
        search_fn: Callable[[str], list[Snippet]] | None = None,
        ttl_hours: int | None = None,
    ):
        self.store = store
        self.llm = llm
        self.registry = registry
        self._search = search_fn or default_search
        if ttl_hours is None:
            ttl_hours = int(
                os.getenv("AEDOS_RETRIEVAL_CACHE_TTL_HOURS", str(_DEFAULT_TTL_HOURS))
            )
        self.ttl_seconds = max(0, ttl_hours) * 3600

    def build_query(self, claim: dict[str, Any]) -> str:
        pred = self.registry.get(claim["predicate"])
        template = pred.retrieval_query_template or "{subject} {object}"
        return template.format(
            subject=str(claim["subject"]), object=str(claim["object"])
        )

    def verify(self, claim: dict[str, Any]) -> RetrievalResult:
        query = self.build_query(claim)

        # 1. Cache lookup
        cached = self.store.get_cached_retrieval(query, self.ttl_seconds)
        if cached is not None:
            snippets = [Snippet(**s) for s in cached]
            from_cache = True
        else:
            # 2. Network search, with explicit failure handling
            try:
                snippets = list(self._search(query))
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                return RetrievalResult(
                    outcome=VerificationOutcome.INCONCLUSIVE,
                    query=query,
                    error_flag="retrieval_error",
                    explanation=f"search failed: {type(e).__name__}: {e}",
                )
            except Exception as e:  # provider returned malformed JSON, etc.
                return RetrievalResult(
                    outcome=VerificationOutcome.INCONCLUSIVE,
                    query=query,
                    error_flag="retrieval_error",
                    explanation=f"search raised: {type(e).__name__}: {e}",
                )
            if not snippets:
                return RetrievalResult(
                    outcome=VerificationOutcome.INCONCLUSIVE,
                    query=query,
                    error_flag="no_results",
                    explanation=f"search returned 0 results for {query!r}",
                )
            self.store.cache_retrieval(query, [s.to_dict() for s in snippets])
            from_cache = False

        # 3. Judge
        try:
            judge_text = self.llm.rewrite(
                _JUDGE_SYSTEM, _format_judge_user_message(claim, snippets)
            )
        except Exception as e:
            return RetrievalResult(
                outcome=VerificationOutcome.INCONCLUSIVE,
                query=query,
                snippets=snippets,
                error_flag="judge_error",
                explanation=f"judge call failed: {type(e).__name__}: {e}",
                from_cache=from_cache,
            )

        verdict = parse_judge_response(judge_text)
        if verdict is None:
            return RetrievalResult(
                outcome=VerificationOutcome.INCONCLUSIVE,
                query=query,
                snippets=snippets,
                error_flag="judge_parse_error",
                explanation=f"judge returned malformed output: {judge_text!r}",
                from_cache=from_cache,
            )

        if verdict.verdict == "SUPPORTED":
            outcome = VerificationOutcome.VERIFIED
        elif verdict.verdict == "CONTRADICTED":
            outcome = VerificationOutcome.CONTRADICTED
        else:  # INSUFFICIENT_EVIDENCE
            outcome = VerificationOutcome.INCONCLUSIVE

        return RetrievalResult(
            outcome=outcome,
            query=query,
            snippets=snippets,
            verdict=verdict,
            explanation=verdict.justification,
            from_cache=from_cache,
        )


def to_verification_result(r: RetrievalResult) -> VerificationResult:
    """Adapter for code that consumes the python-verifier shape."""
    return VerificationResult(
        outcome=r.outcome,
        actual_value=r.actual_value,
        explanation=r.explanation or (r.verdict.justification if r.verdict else ""),
    )


# ---- back-compat surface --------------------------------------------


def retrieval_verify(claim: dict[str, Any]) -> RetrievalResult:
    """Stub-compatible function for callers that don't have a configured verifier.

    Returns INCONCLUSIVE with explanation=retrieval_not_configured. The router
    constructs a real verifier via build_pipeline; this only fires if someone
    imports the function directly without a verifier instance.
    """
    return RetrievalResult(
        outcome=VerificationOutcome.INCONCLUSIVE,
        query="",
        error_flag="retrieval_not_configured",
        explanation=(
            "RetrievalVerifier was not constructed; pass one to Router or call "
            "RetrievalVerifier(...).verify(claim) directly."
        ),
    )
