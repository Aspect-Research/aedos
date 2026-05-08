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
EXTRACT_CHAR_CAP = 600


def search_wikipedia(
    query: str,
    *,
    top_n: int = 3,
    client: Any | None = None,
) -> list[Any]:
    """Search Wikipedia for ``query`` and return up to ``top_n``
    Snippets. Returns an empty list when no results match (callers
    fall through to the next provider). Raises ``httpx.HTTPError``
    on network or 5xx — the caller in ``default_search`` swallows
    these to fall through.

    ``client`` lets tests inject a fake ``httpx.Client``-shaped object;
    when None we construct one with the required User-Agent header.
    """
    # Lazy import to keep the dependency graph one-way: retrieval_verifier
    # imports scrapers, scrapers don't import back.
    from src.legacy.verifiers.retrieval_verifier import Snippet

    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )

    try:
        # Phase 1 — search for matching pages.
        search_resp = client.get(WIKIPEDIA_API, params={
            "action": "query",
            "format": "json",
            "list": "search",
            "srsearch": query,
            "srlimit": top_n,
        })
        search_resp.raise_for_status()
        search_data = search_resp.json()
        results = (search_data.get("query") or {}).get("search") or []
        if not results:
            return []
        titles = [r["title"] for r in results[:top_n] if "title" in r]
        if not titles:
            return []

        # Phase 2 — fetch lead-section extracts.
        extract_resp = client.get(WIKIPEDIA_API, params={
            "action": "query",
            "format": "json",
            "prop": "extracts|info",
            "exintro": "1",
            "explaintext": "1",
            "inprop": "url",
            # "|" delimiter is the MediaWiki convention.
            "titles": "|".join(titles),
        })
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
            snippet = extract[:EXTRACT_CHAR_CAP]
            snippets.append(Snippet(title=title, snippet=snippet, url=url))
        return snippets
    finally:
        if owns_client:
            client.close()
