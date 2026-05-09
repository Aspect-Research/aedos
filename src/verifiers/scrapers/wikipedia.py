"""Wikipedia retrieval — pure Python, no API key, no rate-limit
ceiling at our scale.

Two-phase against the MediaWiki API:

  1. ``action=query&list=search`` finds page titles for a free-text
     query. Returns up to ``top_n`` matching pages.
  2. ``action=query&prop=extracts`` fetches the lead-section plain-
     text for those pages.

The lead section is what an encyclopedia opens with — usually the
verbatim biographical / definitional facts AEDOS wants to verify
(birth years, occupations, founder names, member counts). We cap
each extract at 600 chars so the judge prompt stays tight.

Wikipedia is not a paid third-party API: it's hosted by Wikimedia,
explicitly designed for automated use, free, and only rate-limits
abusive bursts (we're nowhere near). The User-Agent header is
required by their bot policy — we identify the tool transparently.

Falls back to the legacy DDG / Tavily / SerpAPI chain in
``default_search`` if Wikipedia returns nothing or errors.
"""

from __future__ import annotations

from typing import Any

import httpx


WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = (
    "Aedos/0.6 (https://github.com/asashepard/aedos; research prototype) "
    "httpx"
)
REQUEST_TIMEOUT = 10.0

# v0.14.1 — two-tier extract caps. Lead-section calls stay tight (the
# judge usually only needs the first paragraph for biographical /
# definitional claims). Full-article calls get a generous cap so the
# fall-through retry can land facts that live in section 4 of the
# article (animal-behavior facts on the species page, etc.).
EXTRACT_CHAR_CAP_LEAD = 600
EXTRACT_CHAR_CAP_FULL = 3000

# v0.14.1 — bumped from 3 to 5. Two more candidate pages per query,
# negligible cost (snippets are ~100 bytes each); the judge already
# filters for relevance. Helps when a niche topic spreads across
# multiple articles.
DEFAULT_TOP_N = 5


def search_wikipedia(
    query: str,
    *,
    top_n: int = DEFAULT_TOP_N,
    client: Any | None = None,
    include_full_extract: bool = False,
) -> list[Any]:
    """Search Wikipedia for ``query`` and return up to ``top_n``
    Snippets. Returns an empty list when no results match (callers
    fall through to the next provider). Raises ``httpx.HTTPError``
    on network or 5xx — the caller in ``default_search`` swallows
    these to fall through.

    ``client`` lets tests inject a fake ``httpx.Client``-shaped object;
    when None we construct one with the required User-Agent header.

    ``include_full_extract`` (v0.14.1): when False (default), fetches
    only the lead section, capped at ``EXTRACT_CHAR_CAP_LEAD``. When
    True, drops the ``exintro`` flag and fetches the full article
    plaintext, capped at ``EXTRACT_CHAR_CAP_FULL``. The retrieval
    verifier uses the lead extract first (cheap, often sufficient for
    biographical / definitional claims) and falls through to the full
    extract only when the judge says the lead was insufficient. Avoids
    paying for full-article fetches on the common case.
    """
    # Lazy import to keep the dependency graph one-way: retrieval_verifier
    # imports scrapers, scrapers don't import back.
    from src.verifiers.retrieval_verifier import Snippet

    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )

    try:
        # Phase 1 — search for matching pages. ``srprop=snippet|sectiontitle``
        # lets the judge see WHICH section matched in the search hit
        # (important when the fact lives outside the lead).
        search_resp = client.get(WIKIPEDIA_API, params={
            "action": "query",
            "format": "json",
            "list": "search",
            "srsearch": query,
            "srlimit": top_n,
            "srprop": "snippet|sectiontitle",
        })
        search_resp.raise_for_status()
        search_data = search_resp.json()
        results = (search_data.get("query") or {}).get("search") or []
        if not results:
            return []
        titles = [r["title"] for r in results[:top_n] if "title" in r]
        if not titles:
            return []

        # Phase 2 — fetch extracts. Lead-only by default; full article
        # when the caller requested deeper retrieval.
        extract_params = {
            "action": "query",
            "format": "json",
            "prop": "extracts|info",
            "explaintext": "1",
            "inprop": "url",
            # "|" delimiter is the MediaWiki convention.
            "titles": "|".join(titles),
        }
        if not include_full_extract:
            extract_params["exintro"] = "1"
        char_cap = (EXTRACT_CHAR_CAP_FULL if include_full_extract
                   else EXTRACT_CHAR_CAP_LEAD)
        extract_resp = client.get(WIKIPEDIA_API, params=extract_params)
        extract_resp.raise_for_status()
        extract_data = extract_resp.json()
        pages = (extract_data.get("query") or {}).get("pages") or {}

        # Preserve the search ranking — the page-id keys aren't ordered.
        page_by_title = {
            p["title"]: p for p in pages.values() if "title" in p
        }
        snippets: list[Snippet] = []
        for title in titles:
            page = page_by_title.get(title)
            if page is None:
                continue
            extract = (page.get("extract") or "").strip()
            if not extract:
                continue
            url = page.get("fullurl") or (
                f"https://en.wikipedia.org/wiki/"
                f"{title.replace(' ', '_')}"
            )
            snippet = extract[:char_cap]
            snippets.append(Snippet(title=title, snippet=snippet, url=url))
        return snippets
    finally:
        if owns_client:
            client.close()
