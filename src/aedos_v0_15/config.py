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
        default_factory=lambda: os.getenv("AEDOS_DB_PATH", "aedos_v0_15.db")
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

    # Seeds
    seed_file: Optional[str] = None

    @classmethod
    def from_env(cls) -> "Config":
        return cls()
