"""Retrieval verifier (v0.3 — slots-aware multi-attempt query strategy).

Per the v0.2 dogfooding traces, query construction was the main retrieval
failure mode. v0.3 changes:

- Queries come from the PATTERN's ``query_strategy`` list, not from a
  per-predicate template. Slots fill in the placeholders.
- The verifier tries each attempt in order. The first attempt with ≥ 2
  results is used. Failed/empty attempts continue.
- Each attempt is cached independently so retries are cheap.
- We never inject "current" into a query — temporal scope comes from the
  slots, not from query string manipulation. The judge prompt asks
  current-vs-historical using the slot values.
- Each attempt is logged as a ``retrieval_query_attempt`` pipeline_event
  so the trace UI shows the strategy.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx
from bs4 import BeautifulSoup

from src.fact_store import FactStore
from src.llm_client import LLMClient
from src.pattern_registry import PatternRegistry, Pattern
from src.verifiers.types import VerificationOutcome, VerificationResult


# DDG's HTML endpoint is sensitive to User-Agent fingerprinting and
# often returns 0 results on the first request. Try a couple of UAs in
# sequence on empty result before giving up. Order: realistic-Chrome
# (current), realistic-Firefox (fallback). The "Aedos research
# prototype" label was getting filtered.
_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) "
    "Gecko/20100101 Firefox/121.0",
)
_DDG_URL = "https://html.duckduckgo.com/html/"
_TAVILY_URL = "https://api.tavily.com/search"
_SERPAPI_URL = "https://serpapi.com/search.json"
_REQUEST_TIMEOUT = 10.0
_TOP_N = 3
_MIN_RESULTS_TO_USE = 2  # spec: "If a query returns ≥ 2 results, use those"
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
    verdict: str
    justification: str

    def to_dict(self) -> dict[str, str]:
        return {"verdict": self.verdict, "justification": self.justification}


@dataclass
class QueryAttempt:
    query: str
    result_count: int
    used: bool
    from_cache: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "result_count": self.result_count,
            "used": self.used,
            "from_cache": self.from_cache,
            "error": self.error,
        }


@dataclass
class RetrievalResult:
    """Returned by RetrievalVerifier.verify().

    Carries enough metadata to render a full debugging view: every query
    attempt, the snippets used, the judge's verdict and justification,
    and the temporal scope used by the judge.
    """

    outcome: VerificationOutcome
    attempts: list[QueryAttempt] = field(default_factory=list)
    snippets: list[Snippet] = field(default_factory=list)
    verdict: Optional[JudgeVerdict] = None
    error_flag: Optional[str] = None
    explanation: str = ""
    actual_value: Any | None = None
    historical: bool = False  # True if judge used the historical-claim prompt

    @property
    def from_cache(self) -> bool:
        for a in self.attempts:
            if a.used:
                return a.from_cache
        return False

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
            "attempts": [a.to_dict() for a in self.attempts],
            "from_cache": self.from_cache,
            "snippets": [s.to_dict() for s in self.snippets],
            "verdict": self.verdict.to_dict() if self.verdict else None,
            "error_flag": self.error_flag,
            "explanation": self.explanation,
            "actual_value": self.actual_value,
            "historical": self.historical,
        }


# ---- search providers (unchanged from v0.2) -------------------------


def _ddg_attempt(query: str, user_agent: str, top_n: int) -> list[Snippet]:
    """Single DDG request with a specific User-Agent."""
    resp = httpx.post(
        _DDG_URL,
        data={"q": query},
        headers={"User-Agent": user_agent},
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


def search_duckduckgo(query: str, *, top_n: int = _TOP_N) -> list[Snippet]:
    """Try each User-Agent in turn until one returns results.

    DDG's HTML endpoint commonly returns 0 results due to bot
    fingerprinting (the dogfood corpus turn 9 'denver_elevation' hit
    this — all queries returned empty for an answer that's literally
    on every Denver Wikipedia page). Rotating UA usually unblocks it.

    Returns the first attempt's results that are non-empty. If every
    UA returns 0, returns []. Errors propagate from the first attempt
    (we don't retry on HTTP error — that's a different failure class)."""
    for ua in _USER_AGENTS:
        results = _ddg_attempt(query, ua, top_n)
        if results:
            return results
    return []


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
        Snippet(title=str(r.get("title", "")), snippet=str(r.get("content", "")),
                url=str(r.get("url", "")))
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
        Snippet(title=str(r.get("title", "")), snippet=str(r.get("snippet", "")),
                url=str(r.get("link", "")))
        for r in payload.get("organic_results", [])[:top_n]
    ]


def default_search(query: str) -> list[Snippet]:
    """Provider chain, in priority order:

      1. Wikipedia — pure-Python lookup against the MediaWiki API.
         Free, no key, no meaningful rate limit, and the highest-
         quality factual source for the bulk of AEDOS's queries
         (biographical / historical / definitional). Returns when
         it has results; falls through silently when it doesn't or
         when the request errors.
      2. Tavily — paid search API; used when TAVILY_API_KEY is set.
      3. SerpAPI — paid search API; used when SERPAPI_KEY is set.
      4. DuckDuckGo HTML scrape — free fallback; brittle (DDG
         frequently returns 0 results from bot fingerprinting).

    The Wikipedia-first ordering eliminates most retrieval failures
    on the corpus we actually care about while staying cost-free.
    Pure DDG (the prior default) was getting 0 results on most queries
    because the HTML endpoint blocks repeat scrapers.
    """
    # Wikipedia first — only providers below it pay $ or hit rate
    # walls.
    try:
        from src.verifiers.scrapers import search_wikipedia
        wiki = search_wikipedia(query)
        if wiki:
            return wiki
    except Exception:
        pass  # fall through to legacy providers

    if (key := os.getenv("TAVILY_API_KEY")):
        return search_tavily(query, key)
    if (key := os.getenv("SERPAPI_KEY")):
        return search_serpapi(query, key)
    return search_duckduckgo(query)


# ---- judge — current vs historical ----------------------------------

_JUDGE_SYSTEM_CURRENT = """You are a strict, evidence-bounded judge.

You receive a structured CURRENT-TENSE claim and a small set of search
result snippets. Decide whether the snippets SUPPORT, CONTRADICT, or are
INSUFFICIENT_EVIDENCE for the claim. Use only the snippets — never your
prior knowledge.

A claim is SUPPORTED only if the snippets clearly state or directly
imply it as currently true. CONTRADICTED only if they clearly state the
opposite. Otherwise INSUFFICIENT_EVIDENCE.

Output exactly two lines, no preamble:
VERDICT
Justification: <one sentence>"""

_JUDGE_SYSTEM_HISTORICAL = """You are a strict, evidence-bounded judge.

You receive a structured HISTORICAL claim with an explicit time period
(valid_from / valid_until) and a small set of search result snippets.
Decide whether the snippets SUPPORT, CONTRADICT, or are INSUFFICIENT_EVIDENCE
for the claim FOR THAT SPECIFIC PERIOD.

Pay attention to dates. A snippet describing a different time period is
NOT support — it's INSUFFICIENT_EVIDENCE. A snippet stating a different
time-bounded fact is CONTRADICTION only if it directly conflicts with
the claim's period.

Output exactly two lines, no preamble:
VERDICT
Justification: <one sentence>"""


# Map of accepted verdict tokens (after upper + strip) → canonical label.
# The judge prompt asks for SUPPORTED / CONTRADICTED / INSUFFICIENT_EVIDENCE,
# but real LLM output abbreviates ('SUPPORT', 'CONTRADICT', 'INCONCLUSIVE').
# Accepting the abbreviated forms turns the dogfood-observed
# 'judge_parse_error' on Tokyo→Edo into the SUPPORT verdict the judge
# clearly intended. Canonical labels stay unchanged downstream.
_JUDGE_VERDICT_ALIASES = {
    "SUPPORTED": "SUPPORTED",
    "SUPPORT": "SUPPORTED",
    "SUPPORTS": "SUPPORTED",
    "CONTRADICTED": "CONTRADICTED",
    "CONTRADICT": "CONTRADICTED",
    "CONTRADICTS": "CONTRADICTED",
    "INSUFFICIENT_EVIDENCE": "INSUFFICIENT_EVIDENCE",
    "INSUFFICIENT": "INSUFFICIENT_EVIDENCE",
    "INCONCLUSIVE": "INSUFFICIENT_EVIDENCE",
    "UNCLEAR": "INSUFFICIENT_EVIDENCE",
}


def parse_judge_response(text: str) -> JudgeVerdict | None:
    """Tolerant verdict parser.

    The judge prompt asks for "VERDICT \\n Justification: ..." but real
    Claude output is messy:

      * ``**SUPPORTED**`` — markdown bolds around the verdict
      * ``Verdict: SUPPORTED`` — labeled prefix instead of bare verdict
      * ``## SUPPORTED`` — markdown heading
      * ``Based on the snippets, the verdict is SUPPORTED.`` — preamble
      * ``The claim is NOT SUPPORTED.`` — negation flips the meaning

    Strategy:

      1. Search the first 600 chars of the response for any aliased
         verdict token, matched as a whole word (case-insensitive).
      2. If the verdict is preceded immediately by ``not`` / ``no``
         (within ~5 chars), flip the canonical label:
         not-SUPPORTED → CONTRADICTED, not-CONTRADICTED → SUPPORTED.
      3. The earliest-occurring (post-flip) verdict wins.
      4. Justification = everything from the verdict onward, with
         the verdict word removed and "Justification:" / markdown
         stripped.

    Returns None only when no aliased verdict word appears at all.
    """
    if not text:
        return None
    head = text[:600]

    candidates: list[tuple[int, str]] = []  # (position, canonical_label)
    for token, label in _JUDGE_VERDICT_ALIASES.items():
        for m in re.finditer(
            rf"\b{re.escape(token)}\b", head, flags=re.IGNORECASE,
        ):
            # Negation flip: "NOT SUPPORTED" / "no SUPPORTED" reads as
            # the opposite of the bare token. Look in the ~10 chars
            # before the match for a negation cue.
            preceding = head[max(0, m.start() - 10):m.start()].lower()
            negated = bool(re.search(r"\b(not|no)\s*$", preceding))
            actual = label
            if negated:
                if label == "SUPPORTED":
                    actual = "CONTRADICTED"
                elif label == "CONTRADICTED":
                    actual = "SUPPORTED"
                # INSUFFICIENT_EVIDENCE preceded by "not" stays
                # INSUFFICIENT — it's not a polar verdict.
            candidates.append((m.start(), actual))

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    pos, canonical = candidates[0]

    # Justification: text after the verdict word.
    # Find the end of the matched word; everything after is the
    # justification (sans "Justification:" prefix and markdown).
    rest = text[pos:]
    # Drop the verdict word and any immediately-following bold/header
    # markers.
    rest = re.sub(
        r"^\s*\*{0,2}_?[A-Z_]+_?\*{0,2}\s*[:.,]?\s*",
        "",
        rest,
        count=1,
    )
    # Strip a "Justification:" lead.
    rest = re.sub(
        r"^\s*\**\s*[Jj]ustification\s*:?\s*\**\s*",
        "",
        rest,
        count=1,
    )
    # Drop residual markdown markers and surrounding whitespace.
    rest = rest.strip().strip("*_#>`-").strip()
    return JudgeVerdict(
        verdict=canonical,
        justification=rest or "(no justification)",
    )


def _is_historical(claim: dict) -> bool:
    """A claim is historical if its slots specify a valid_until."""
    slots = claim.get("slots") or {}
    return bool(slots.get("valid_until"))


def _format_judge_user_message(claim: dict, snippets: list[Snippet], historical: bool) -> str:
    slots = claim.get("slots") or {}
    polarity_word = "asserts" if int(claim.get("polarity", 1)) == 1 else "denies"
    snippets_block = "\n\n".join(
        f"[{i + 1}] {s.title}\n{s.snippet}\nSource: {s.url}"
        for i, s in enumerate(snippets)
    )

    slot_lines = "\n".join(f"  {k}: {v!r}" for k, v in slots.items())
    if historical:
        period = f"{slots.get('valid_from') or 'unspecified'} to {slots.get('valid_until')}"
        framing = (
            f"Time period: {period}\n"
            f"The speaker {polarity_word} that this relation held during that period."
        )
    else:
        framing = (
            f"The speaker {polarity_word} this relation as currently true "
            "(no end date specified)."
        )

    return (
        f"Claim:\n"
        f"  pattern: {claim.get('pattern')!r}\n"
        f"  predicate: {claim.get('predicate')!r}\n"
        f"  slots:\n{slot_lines}\n\n"
        f"{framing}\n\n"
        f"Snippets:\n{snippets_block}\n\n"
        "Respond with the required two-line format."
    )


# ---- query construction --------------------------------------------


_SLOT_REF_RE = re.compile(r"\{(\w+)\}")


def _slot_refs(template: str) -> list[str]:
    return _SLOT_REF_RE.findall(template)


def _enrich_slots(slots: dict[str, Any]) -> dict[str, Any]:
    """Add derived keys + natural-language conversion for query templates.

    Two transformations:

    1. ``participants_joined`` — for the event pattern's list slot.
    2. snake_case → space-separated for slots whose values are AEDOS-
       internal predicate/category identifiers (``relation``,
       ``property``, ``relation_kind``, ``event_type``). Without this,
       query templates emit garbage like "Donald Trump parent_of
       Donald Jr." or "presidential_campaign 2024" — search engines
       (and Wikipedia in particular) rank pages much better against
       "Donald Trump parent of Donald Jr." or "presidential campaign
       2024". The judge always sees the original slot values; this
       enrichment is query-only.
    """
    out = dict(slots)
    parts = slots.get("participants")
    if isinstance(parts, list):
        out["participants_joined"] = " ".join(str(p) for p in parts)
    for key in ("relation", "property", "relation_kind", "event_type",
                "predicate", "role"):
        val = slots.get(key)
        if isinstance(val, str) and "_" in val:
            out[key] = val.replace("_", " ")
    return out


def build_queries(pattern: Pattern, slots: dict[str, Any]) -> list[str]:
    """Return the ordered list of query attempts for these slots.

    Templates that reference missing slots are skipped silently; we'd
    rather skip an over-specified template than emit a query with empty
    placeholders.
    """
    enriched = _enrich_slots(slots)
    queries: list[str] = []
    for template in pattern.query_strategy:
        refs = _slot_refs(template)
        if not all(refs and r in enriched and str(enriched[r]).strip() for r in refs):
            continue
        # Spec: never prepend "current" — the temporal context comes from
        # slots. Defensive guard:
        assert "current" not in template.lower(), (
            f"query_strategy template {template!r} contains 'current'; "
            "remove it — temporal scope is determined by slots"
        )
        formatted = template.format_map(enriched).strip()
        formatted = " ".join(formatted.split())  # collapse whitespace
        if formatted and formatted not in queries:
            queries.append(formatted)
    return queries


# ---- verifier -------------------------------------------------------


class RetrievalVerifier:
    def __init__(
        self,
        store: FactStore,
        llm: LLMClient,
        registry: PatternRegistry,
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

    def verify(
        self, claim: dict, *, source_turn_id: int | None = None
    ) -> RetrievalResult:
        pattern = self.registry.get(claim["pattern"])
        slots = claim.get("slots") or {}
        queries = build_queries(pattern, slots)
        historical = _is_historical(claim)

        if not queries:
            return RetrievalResult(
                outcome=VerificationOutcome.INCONCLUSIVE,
                error_flag="no_query_constructible",
                explanation=(
                    f"could not construct any query for pattern {pattern.name!r} "
                    f"from slots {slots!r}"
                ),
                historical=historical,
            )

        attempts: list[QueryAttempt] = []
        chosen_snippets: list[Snippet] = []

        for q in queries:
            cached = self.store.get_cached_retrieval(q, self.ttl_seconds)
            if cached is not None:
                sn = [Snippet(**s) for s in cached]
                attempt = QueryAttempt(
                    query=q, result_count=len(sn), used=False, from_cache=True
                )
            else:
                try:
                    sn = list(self._search(q))
                    attempt = QueryAttempt(
                        query=q, result_count=len(sn), used=False, from_cache=False
                    )
                    # Cache every attempt (including empty) so retries don't
                    # re-hit the same flaky endpoint. TTL handles staleness.
                    self.store.cache_retrieval(q, [s.to_dict() for s in sn])
                except (httpx.HTTPError, httpx.TimeoutException) as e:
                    attempt = QueryAttempt(
                        query=q, result_count=0, used=False, from_cache=False,
                        error=f"{type(e).__name__}: {e}",
                    )
                    sn = []
                except Exception as e:
                    attempt = QueryAttempt(
                        query=q, result_count=0, used=False, from_cache=False,
                        error=f"{type(e).__name__}: {e}",
                    )
                    sn = []

            attempts.append(attempt)
            self._log_attempt(source_turn_id, attempt)

            if attempt.result_count >= _MIN_RESULTS_TO_USE:
                attempt.used = True
                chosen_snippets = sn
                # re-log the attempt now that 'used' is True so the trace shows it
                self._log_attempt(source_turn_id, attempt, is_decision=True)
                break

        if not chosen_snippets:
            # All attempts returned 0 results or errored.
            any_error = any(a.error for a in attempts)
            flag = "retrieval_error" if any_error else "no_results"
            err_summary = next((a.error for a in attempts if a.error), None)
            return RetrievalResult(
                outcome=VerificationOutcome.INCONCLUSIVE,
                attempts=attempts,
                error_flag=flag,
                explanation=(
                    err_summary
                    or f"all {len(attempts)} query attempt(s) returned < "
                    f"{_MIN_RESULTS_TO_USE} results"
                ),
                historical=historical,
            )

        # Judge step
        try:
            system = _JUDGE_SYSTEM_HISTORICAL if historical else _JUDGE_SYSTEM_CURRENT
            judge_text = self.llm.rewrite(
                system, _format_judge_user_message(claim, chosen_snippets, historical)
            )
        except Exception as e:
            return RetrievalResult(
                outcome=VerificationOutcome.INCONCLUSIVE,
                attempts=attempts,
                snippets=chosen_snippets,
                error_flag="judge_error",
                explanation=f"judge call failed: {type(e).__name__}: {e}",
                historical=historical,
            )

        verdict = parse_judge_response(judge_text)
        if verdict is None:
            return RetrievalResult(
                outcome=VerificationOutcome.INCONCLUSIVE,
                attempts=attempts,
                snippets=chosen_snippets,
                error_flag="judge_parse_error",
                explanation=f"judge returned malformed output: {judge_text!r}",
                historical=historical,
            )

        if verdict.verdict == "SUPPORTED":
            outcome = VerificationOutcome.VERIFIED
        elif verdict.verdict == "CONTRADICTED":
            outcome = VerificationOutcome.CONTRADICTED
        else:
            outcome = VerificationOutcome.INCONCLUSIVE

        return RetrievalResult(
            outcome=outcome,
            attempts=attempts,
            snippets=chosen_snippets,
            verdict=verdict,
            explanation=verdict.justification,
            historical=historical,
        )

    def _log_attempt(
        self,
        source_turn_id: int | None,
        attempt: QueryAttempt,
        *,
        is_decision: bool = False,
    ) -> None:
        if source_turn_id is None:
            return
        # We log twice in the "used" case: once when discovered, once when
        # marked used. Keep it simple — only emit on the discovery side.
        if is_decision:
            return
        try:
            self.store.insert_pipeline_event(
                source_turn_id, "retrieval_query_attempt", attempt.to_dict()
            )
        except Exception:
            # Logging must never crash verification.
            pass


