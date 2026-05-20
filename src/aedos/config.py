"""Deployment configuration for Aedos v0.15.

Field validation lands on `__post_init__` (per F3 Q2 — catch
deployment-init typos early rather than after a 30-minute calibration
run). The validation is honest about its scope: it checks
*well-defined* invariants (positive numbers, URL-shaped endpoints,
non-empty strings) but cannot check whether a value is *appropriate*
for a particular deployment (e.g., whether a rate-limit of 5/s is the
right choice for the operator's network). That's the operator's
judgment call; Config catches what would obviously fail downstream.
"""

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

    def __post_init__(self) -> None:
        """Validate field values (F3 §5.4-§5.5).

        Checks invariants Aedos can verify: positive numbers, URL-shaped
        endpoints, non-empty strings, sane LRU sizes. Does NOT check
        whether values are *appropriate* for a particular deployment
        (e.g., is 5 SPARQL/s the right rate for the operator's
        infrastructure?) — that's an operator judgment, not a config
        invariant.
        """
        # Walker budgets — all positive.
        if self.walker_wall_clock_seconds <= 0:
            raise ValueError(
                f"walker_wall_clock_seconds must be positive; got "
                f"{self.walker_wall_clock_seconds!r}"
            )
        if self.walker_max_llm_calls <= 0:
            raise ValueError(
                f"walker_max_llm_calls must be positive; got "
                f"{self.walker_max_llm_calls!r}"
            )
        if self.walker_max_depth <= 0:
            raise ValueError(
                f"walker_max_depth must be positive; got {self.walker_max_depth!r}"
            )

        # Substrate consistency.
        if self.circuit_breaker_threshold <= 0:
            raise ValueError(
                f"circuit_breaker_threshold must be positive; got "
                f"{self.circuit_breaker_threshold!r}"
            )

        # HTTP cache.
        if self.http_cache_lru_size <= 0:
            raise ValueError(
                f"http_cache_lru_size must be positive; got "
                f"{self.http_cache_lru_size!r}"
            )
        if self.http_cache_entity_ttl_seconds <= 0:
            raise ValueError(
                f"http_cache_entity_ttl_seconds must be positive; got "
                f"{self.http_cache_entity_ttl_seconds!r}"
            )
        if self.http_cache_statement_ttl_seconds <= 0:
            raise ValueError(
                f"http_cache_statement_ttl_seconds must be positive; got "
                f"{self.http_cache_statement_ttl_seconds!r}"
            )

        # Wikidata endpoints — URL-shaped.
        for field_name in ("wikidata_sparql_endpoint", "wikidata_search_endpoint"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.startswith(("http://", "https://")):
                raise ValueError(
                    f"{field_name} must be a http(s) URL; got {value!r}"
                )
        if self.wikidata_subsumption_depth <= 0:
            raise ValueError(
                f"wikidata_subsumption_depth must be positive; got "
                f"{self.wikidata_subsumption_depth!r}"
            )
        if self.wikidata_candidate_pool_size <= 0:
            raise ValueError(
                f"wikidata_candidate_pool_size must be positive; got "
                f"{self.wikidata_candidate_pool_size!r}"
            )
        if self.wikidata_sparql_rate_per_second <= 0:
            raise ValueError(
                f"wikidata_sparql_rate_per_second must be positive; got "
                f"{self.wikidata_sparql_rate_per_second!r}"
            )
        if self.wikidata_search_rate_per_second <= 0:
            raise ValueError(
                f"wikidata_search_rate_per_second must be positive; got "
                f"{self.wikidata_search_rate_per_second!r}"
            )

        # User-Agent must be non-empty (Wikimedia policy requires
        # identifying info; the empty string would route through
        # Wikidata as a missing UA).
        if not self.user_agent or not self.user_agent.strip():
            raise ValueError("user_agent must be non-empty")

        # db_path is required (any non-empty string is fine; we don't
        # validate file existence — that's not Config's job).
        if not self.db_path or not self.db_path.strip():
            raise ValueError("db_path must be non-empty")

    @classmethod
    def from_env(cls) -> "Config":
        return cls()
