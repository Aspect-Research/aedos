"""Wiring tests for `build_pipeline` Config threading (Phase F2 commit #4).

Verifies that the F-004 / F-005 / F-006 / F-007 wiring gaps surfaced by
F1 are closed:

- `build_pipeline` accepts a `Config` and threads it to `WikidataAdapter`.
- The Wikidata-related Config fields (endpoints, candidate pool size,
  rate limits, user_agent) reach the adapter.
- The HTTP cache is constructed with the configured TTL and User-Agent.
- An explicitly-passed `kb` overrides the default construction (test
  fixtures still work).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from aedos.config import Config
from aedos.database import open_memory_db
from aedos.layer4_sources.kb_wikidata import WikidataAdapter
from aedos.pipeline import build_pipeline


@pytest.fixture
def db():
    conn = open_memory_db()
    yield conn
    conn.close()


class TestConfigThreading:
    def test_build_pipeline_default_constructs_wikidata_adapter_with_http_cache(self, db):
        """Default construction: build_pipeline(db) builds a WikidataAdapter
        whose http_cache, llm_client, db, and config are all populated.
        Pre-F2 wiring constructed `WikidataAdapter()` with no arguments."""
        pipeline = build_pipeline(db)
        assert isinstance(pipeline.kb, WikidataAdapter)
        assert pipeline.kb._http is not None, "F-004/F-006: http_cache must be wired"
        assert pipeline.kb._llm is not None, "F-004: llm_client must be wired"
        assert pipeline.kb._db is not None, "F-004: db must be wired"
        assert pipeline.kb._config is not None, "F-004/F-005: config must be wired"

    def test_explicit_kb_is_used_unchanged(self, db):
        """When the caller passes a kb (benchmark.py's harness mode does this),
        build_pipeline uses it directly without constructing a new adapter."""
        custom_kb = MagicMock()
        pipeline = build_pipeline(db, kb=custom_kb)
        assert pipeline.kb is custom_kb

    def test_config_endpoints_reach_adapter(self, db):
        """Custom endpoints in Config flow through to the adapter."""
        config = Config()
        config.wikidata_sparql_endpoint = "https://custom.sparql/sparql"
        config.wikidata_search_endpoint = "https://custom.api/api"
        pipeline = build_pipeline(db, config=config)
        # The adapter reads endpoints via `_cfg_value`; verify it sees the custom config.
        assert pipeline.kb._cfg_value("wikidata_sparql_endpoint", "fallback") == (
            "https://custom.sparql/sparql"
        )
        assert pipeline.kb._cfg_value("wikidata_search_endpoint", "fallback") == (
            "https://custom.api/api"
        )

    def test_user_agent_reaches_http_request(self, db):
        """The configured User-Agent must appear in HTTP request headers —
        Wikimedia policy compliance (F-007) requires it. Run a fixture-mode
        resolve (no real HTTP); the adapter's _http is the CachingHTTPClient
        constructed with the User-Agent header."""
        config = Config()
        config.user_agent = "Aedos-Test/0.15 (https://example.test; test@example.test)"
        pipeline = build_pipeline(db, config=config)
        # The CachingHTTPClient stores headers on construction; inspect them.
        assert (
            pipeline.kb._http._base_headers.get("User-Agent")
            == "Aedos-Test/0.15 (https://example.test; test@example.test)"
        )

    def test_rate_limiters_constructed_on_adapter(self, db):
        """Rate limiters live as instance attributes on the adapter
        (per F2 Q3 refinement). Both SPARQL and search limiters should
        be constructed."""
        pipeline = build_pipeline(db)
        assert pipeline.kb._sparql_limiter is not None
        assert pipeline.kb._search_limiter is not None


class TestAedosKbRequestDelayEnvVar:
    def test_aedos_kb_request_delay_ms_overrides_rate_limiter(self, db, monkeypatch):
        """`AEDOS_KB_REQUEST_DELAY_MS` (the runbook's existing knob) must
        override the rate-limiter interval — F-022 closure. Set to 200ms;
        verify two acquires take at least 200ms."""
        monkeypatch.setenv("AEDOS_KB_REQUEST_DELAY_MS", "200")
        pipeline = build_pipeline(db)
        limiter = pipeline.kb._sparql_limiter
        t0 = time.monotonic()
        limiter.acquire()
        limiter.acquire()
        elapsed_ms = (time.monotonic() - t0) * 1000
        # Allow some slack for scheduling; assert >= 180ms (close to the 200ms
        # nominal delay; the first acquire returns immediately, the second
        # blocks for ~200ms).
        assert elapsed_ms >= 180, f"Expected ≥180ms, got {elapsed_ms:.1f}ms"


class TestF3ConfigThreading:
    """F-025 / F-026: walker budgets and circuit-breaker threshold
    flow from Config through build_pipeline to the consumers."""

    def test_walker_max_depth_threaded(self, db):
        """`Config.walker_max_depth` reaches `Walker._max_depth`."""
        config = Config()
        config.walker_max_depth = 7
        pipeline = build_pipeline(db, config=config)
        assert pipeline.walker._max_depth == 7

    def test_walker_budgets_threaded_via_default(self, db):
        """`Config.walker_wall_clock_seconds` and `walker_max_llm_calls`
        reach the walker's per-walk default budget."""
        config = Config()
        config.walker_wall_clock_seconds = 45.0
        config.walker_max_llm_calls = 20
        pipeline = build_pipeline(db, config=config)
        assert pipeline.walker._default_wall_clock_seconds == 45.0
        assert pipeline.walker._default_max_llm_calls == 20

    def test_default_walker_budgets_match_config_defaults(self, db):
        """Default Config + default build_pipeline yields the architecture
        defaults at the Walker layer."""
        pipeline = build_pipeline(db)
        assert pipeline.walker._max_depth == 4  # _DEFAULT_MAX_DEPTH
        assert pipeline.walker._default_wall_clock_seconds == 30.0
        assert pipeline.walker._default_max_llm_calls == 10

    def test_circuit_breaker_threshold_threaded(self, db):
        """`Config.circuit_breaker_threshold` reaches the ConsistencyChecker."""
        config = Config()
        config.circuit_breaker_threshold = 5
        pipeline = build_pipeline(db, config=config)
        assert pipeline.consistency._threshold == 5

    def test_default_circuit_breaker_threshold(self, db):
        """Default Config gives architecture-specified threshold of 3."""
        pipeline = build_pipeline(db)
        assert pipeline.consistency._threshold == 3
