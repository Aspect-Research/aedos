"""Search-engine-free retrieval providers.

Each scraper here returns a list of ``Snippet`` (defined in
``src.verifiers.retrieval_verifier``) and runs entirely against
public, no-cost, no-API-key endpoints. The retrieval verifier's
``default_search`` calls these in priority order before falling
back to the legacy Tavily / SerpAPI / DDG providers.

Lazy imports keep the dependency graph one-way:
``retrieval_verifier`` → ``scrapers``, never the other direction.
"""

from src.verifiers.scrapers.wikipedia import search_wikipedia

__all__ = ["search_wikipedia"]
