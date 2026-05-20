"""Deployment configuration for Aedos v0.15."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    # LLM
    anthropic_api_key: Optional[str] = field(
        default_factory=lambda: os.getenv("ANTHROPIC_API_KEY")
    )
    openai_api_key: Optional[str] = field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY")
    )

    # Database
    db_path: str = field(
        default_factory=lambda: os.getenv("AEDOS_DB_PATH", "aedos.db")
    )

    # HTTP cache
    http_cache_lru_size: int = 256
    http_cache_entity_ttl_seconds: int = 3600
    http_cache_statement_ttl_seconds: int = 86400

    # Walker resource budgets
    walker_wall_clock_seconds: float = 30.0
    walker_max_llm_calls: int = 10
    walker_max_depth: int = 4

    # Substrate consistency check
    circuit_breaker_threshold: int = 3

    # Wikidata
    wikidata_sparql_endpoint: str = "https://query.wikidata.org/sparql"
    wikidata_search_endpoint: str = "https://www.wikidata.org/w/api.php"
    wikidata_subsumption_depth: int = 6
    wikidata_candidate_pool_size: int = 10
    # Rate limits (per Phase F2 design §7.4). WDQS soft limit is ~5 req/s
    # SPARQL; wbsearchentities tolerates ~50 req/s. AEDOS_KB_REQUEST_DELAY_MS
    # (env, optional) overrides both with an explicit delay — the runbook's
    # existing knob now reaches the limiter.
    wikidata_sparql_rate_per_second: float = 5.0
    wikidata_search_rate_per_second: float = 50.0

    # HTTP User-Agent for external services (Wikimedia policy requires
    # contact info — URL or email). Privacy caveat: the contact info
    # appears in HTTP headers to Wikimedia and any network observers
    # between client and Wikidata; acceptable for research-scale traffic.
    # Commercial deployment should revisit (Phase F2 design §7.3).
    user_agent: str = field(
        default_factory=lambda: os.getenv(
            "AEDOS_USER_AGENT",
            "Aedos/0.15 (https://github.com/Aspect-Research/aedos; asa@aspectresearch.org)",
        )
    )

    # Seeds
    seed_file: Optional[str] = None

    @classmethod
    def from_env(cls) -> "Config":
        return cls()
