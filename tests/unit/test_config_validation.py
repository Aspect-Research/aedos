"""F3 Q2 — Config field validation tests.

Confirms `Config.__post_init__` rejects invalid field values at
deployment-init time (Phase F3 §5.4-§5.5). Each test gives an
invalid value, expects `ValueError`, and asserts the field name
appears in the error message so the operator sees what to fix.

What this does NOT test: whether a value is *appropriate* for a
particular deployment. The validation is honest about its scope
(see `Config.__post_init__` docstring).
"""

from __future__ import annotations

import pytest

from aedos.config import Config


class TestConfigValidation:
    def test_default_config_is_valid(self):
        """The defaults must construct cleanly."""
        c = Config()
        assert c.walker_max_depth == 4
        assert c.circuit_breaker_threshold == 3

    def test_walker_wall_clock_seconds_must_be_positive(self):
        with pytest.raises(ValueError, match="walker_wall_clock_seconds"):
            Config(walker_wall_clock_seconds=0)
        with pytest.raises(ValueError, match="walker_wall_clock_seconds"):
            Config(walker_wall_clock_seconds=-5.0)

    def test_walker_max_llm_calls_must_be_positive(self):
        with pytest.raises(ValueError, match="walker_max_llm_calls"):
            Config(walker_max_llm_calls=0)
        with pytest.raises(ValueError, match="walker_max_llm_calls"):
            Config(walker_max_llm_calls=-1)

    def test_walker_max_depth_must_be_positive(self):
        with pytest.raises(ValueError, match="walker_max_depth"):
            Config(walker_max_depth=0)
        with pytest.raises(ValueError, match="walker_max_depth"):
            Config(walker_max_depth=-1)

    def test_circuit_breaker_threshold_must_be_positive(self):
        with pytest.raises(ValueError, match="circuit_breaker_threshold"):
            Config(circuit_breaker_threshold=0)
        with pytest.raises(ValueError, match="circuit_breaker_threshold"):
            Config(circuit_breaker_threshold=-3)

    def test_http_cache_lru_size_must_be_positive(self):
        with pytest.raises(ValueError, match="http_cache_lru_size"):
            Config(http_cache_lru_size=0)

    def test_http_cache_entity_ttl_must_be_positive(self):
        with pytest.raises(ValueError, match="http_cache_entity_ttl_seconds"):
            Config(http_cache_entity_ttl_seconds=0)

    def test_http_cache_statement_ttl_must_be_positive(self):
        with pytest.raises(ValueError, match="http_cache_statement_ttl_seconds"):
            Config(http_cache_statement_ttl_seconds=-100)

    def test_wikidata_sparql_endpoint_must_be_url(self):
        with pytest.raises(ValueError, match="wikidata_sparql_endpoint"):
            Config(wikidata_sparql_endpoint="not-a-url")
        with pytest.raises(ValueError, match="wikidata_sparql_endpoint"):
            Config(wikidata_sparql_endpoint="")

    def test_wikidata_search_endpoint_must_be_url(self):
        with pytest.raises(ValueError, match="wikidata_search_endpoint"):
            Config(wikidata_search_endpoint="ftp://example.com")

    def test_https_endpoint_accepted(self):
        c = Config(
            wikidata_sparql_endpoint="https://example.test/sparql",
            wikidata_search_endpoint="https://example.test/api",
        )
        assert c.wikidata_sparql_endpoint == "https://example.test/sparql"

    def test_http_endpoint_accepted(self):
        """Local proxies and dev setups use http://."""
        c = Config(wikidata_sparql_endpoint="http://localhost:8080/sparql")
        assert c.wikidata_sparql_endpoint == "http://localhost:8080/sparql"

    def test_wikidata_subsumption_depth_must_be_positive(self):
        with pytest.raises(ValueError, match="wikidata_subsumption_depth"):
            Config(wikidata_subsumption_depth=0)

    def test_wikidata_candidate_pool_size_must_be_positive(self):
        with pytest.raises(ValueError, match="wikidata_candidate_pool_size"):
            Config(wikidata_candidate_pool_size=0)

    def test_wikidata_rate_limits_must_be_positive(self):
        with pytest.raises(ValueError, match="wikidata_sparql_rate_per_second"):
            Config(wikidata_sparql_rate_per_second=0)
        with pytest.raises(ValueError, match="wikidata_search_rate_per_second"):
            Config(wikidata_search_rate_per_second=-1)

    def test_user_agent_must_be_non_empty(self):
        with pytest.raises(ValueError, match="user_agent"):
            Config(user_agent="")
        with pytest.raises(ValueError, match="user_agent"):
            Config(user_agent="   ")

    def test_db_path_must_be_non_empty(self):
        with pytest.raises(ValueError, match="db_path"):
            Config(db_path="")
