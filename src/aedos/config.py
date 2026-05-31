"""Deployment configuration for Aedos.

Field validation lands on `__post_init__` to catch deployment-init
typos early rather than after a 30-minute calibration run. The
validation is honest about its scope: it checks
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

    # v0.16.1 WS4: SLING distant-supervision binding discovery. When True
    # (default), the predicate-metadata oracle is asked to emit a few example
    # subject Q-ids for long-tail edges the property ontology can't constrain,
    # and the SlingFallback consumes them to propose a co-occurrence candidate
    # binding. SLING bindings are verify-only (single_valued=False, never
    # CONTRADICTED), rank LAST, and are value-type-gated on the positive path
    # (fail-closed), so the flag is purely a coverage knob — turning it off can
    # only lose a verify, never change a sound verdict. AEDOS_ENABLE_SLING in
    # ('0','false','no','off', case-insensitive) disables it.
    enable_sling: bool = field(
        default_factory=lambda: os.getenv("AEDOS_ENABLE_SLING", "1").strip().lower()
        not in ("0", "false", "no", "off")
    )

    # Wikidata
    wikidata_sparql_endpoint: str = "https://query.wikidata.org/sparql"
    wikidata_search_endpoint: str = "https://www.wikidata.org/w/api.php"
    wikidata_subsumption_depth: int = 6
    # Pool raised from 10 to 30. The post-filter prunes
    # wrong-type candidates; a larger initial pool gives the filter more
    # to work with so the canonical entity is more likely to survive.
    wikidata_candidate_pool_size: int = 30
    # Rate limits. WDQS soft limit is ~5 req/s
    # SPARQL; wbsearchentities tolerates ~50 req/s. AEDOS_KB_REQUEST_DELAY_MS
    # (env, optional) overrides both with an explicit delay — the runbook's
    # existing knob now reaches the limiter.
    wikidata_sparql_rate_per_second: float = 5.0
    wikidata_search_rate_per_second: float = 50.0
    # Post-filter entity-resolution candidates by P31. The
    # candidate-pool size is raised to 30 (from 10) so the canonical
    # entity has a better chance of being in the pool before filtering.
    wikidata_type_filter_enabled: bool = True
    # wbgetentities accepts up to 50 entity ids per call; the batch size
    # caps how many candidates we fetch P31 for in a single roundtrip.
    wikidata_type_filter_p31_batch_size: int = 50

    # wbsearchentities `limit` parameter.
    # 20 is the design-doc default — gives Stage C's LLM a usefully
    # ranked candidate pool without bloating the prompt. The API caps
    # at 50 per call; values above 50 will be silently truncated by
    # Wikidata.
    wikidata_wbsearch_limit: int = 20

    # MediaWiki / Wikipedia normalizer.
    # Stage 1 resolves bare ambiguous references to canonical Wikipedia
    # article titles via the redirects API; Stage 2 falls back to an LLM
    # selection over disambiguation-page links when Stage 1 surfaces
    # ambiguity. The normalizer fires inside EntityResolver.resolve, so
    # every caller (pipeline, calibration runner, ad-hoc tests) benefits.
    wikipedia_api_url: str = "https://en.wikipedia.org/w/api.php"
    wikipedia_request_rate_per_second: float = 10.0
    wikipedia_normalizer_enabled: bool = True  # diagnostic kill switch
    # There is no `wikipedia_stage_2_max_candidates`: the Wikipedia
    # disambig-page candidate scraping it once bounded is gone;
    # wbsearchentities's `limit` parameter
    # (Config.wikidata_wbsearch_limit) bounds the flow instead.

    # HTTP User-Agent for external services (Wikimedia policy requires
    # contact info — URL or email). Privacy caveat: the contact info
    # appears in HTTP headers to Wikimedia and any network observers
    # between client and Wikidata; acceptable for research-scale traffic.
    # Commercial deployment should revisit.
    user_agent: str = field(
        default_factory=lambda: os.getenv(
            "AEDOS_USER_AGENT",
            "Aedos/0.15 (https://github.com/Aspect-Research/aedos; asa@aspectresearch.org)",
        )
    )

    # Seeds
    seed_file: Optional[str] = None

    def __post_init__(self) -> None:
        """Validate field values.

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
        if self.wikidata_type_filter_p31_batch_size <= 0:
            raise ValueError(
                f"wikidata_type_filter_p31_batch_size must be positive; got "
                f"{self.wikidata_type_filter_p31_batch_size!r}"
            )
        if self.wikidata_wbsearch_limit <= 0 or self.wikidata_wbsearch_limit > 50:
            raise ValueError(
                f"wikidata_wbsearch_limit must be in (0, 50]; got "
                f"{self.wikidata_wbsearch_limit!r}"
            )

        # Wikipedia normalizer fields.
        if not isinstance(self.wikipedia_api_url, str) or not self.wikipedia_api_url.startswith(("http://", "https://")):
            raise ValueError(
                f"wikipedia_api_url must be a http(s) URL; got "
                f"{self.wikipedia_api_url!r}"
            )
        if self.wikipedia_request_rate_per_second <= 0:
            raise ValueError(
                f"wikipedia_request_rate_per_second must be positive; got "
                f"{self.wikipedia_request_rate_per_second!r}"
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
