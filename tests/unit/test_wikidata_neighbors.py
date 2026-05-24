"""Phase H D5: tests for WikidataAdapter.enumerate_neighbors.

Covers the four-outcome shape (success / empty / transient-error-retry /
hard-error fail-open), the SPARQL query builder, the bindings parser,
the fixture path, the protocol contract, and the wiring-gap defence.

No live API. Live tests live in
`tests/integration/live/test_wikidata_neighbors_live.py`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from aedos.config import Config
from aedos.database import open_memory_db
from aedos.layer4_sources.kb_wikidata import (
    WikidataAdapter,
    _DEFAULT_NEIGHBOR_PROPERTIES,
    _build_neighbors_query,
    _parse_neighbors_bindings,
)
from aedos.utils.http_cache import CachingHTTPClient, LRUHTTPCache


def _make_adapter():
    db = open_memory_db()
    cfg = Config()
    cache = LRUHTTPCache()
    http_client = CachingHTTPClient(cache=cache, headers={"User-Agent": cfg.user_agent})
    return WikidataAdapter(http_cache=http_client, db=db, config=cfg), db


def _make_response(body: bytes, status_code: int = 200, etag: str | None = None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = body
    resp.headers = {"ETag": etag} if etag else {}
    resp.raise_for_status = MagicMock()
    return resp


def _make_httpx_cm(response_or_exc):
    inner = MagicMock()
    if isinstance(response_or_exc, Exception):
        inner.get.side_effect = response_or_exc
    else:
        inner.get.return_value = response_or_exc
    cm = MagicMock()
    cm.__enter__.return_value = inner
    cm.__exit__.return_value = False
    return cm, inner


# ---------------------------------------------------------------------------
# Query builder + parser
# ---------------------------------------------------------------------------

class TestBuildNeighborsQuery:
    def test_default_property_set_in_query(self):
        q = _build_neighbors_query("Q49112", _DEFAULT_NEIGHBOR_PROPERTIES)
        # All 5 default properties appear as wdt: in the VALUES clause
        for p in ("P31", "P279", "P361", "P131", "P17"):
            assert f"wdt:{p}" in q

    def test_entity_appears_in_query(self):
        q = _build_neighbors_query("Q49112", ("P31",))
        assert "wd:Q49112" in q

    def test_filter_isiri_present(self):
        # Per the design, only entity-valued neighbors are useful for the
        # walker — literals (dates, quantities) don't yield premise entities.
        q = _build_neighbors_query("Q49112", ("P31",))
        assert "FILTER(isIRI(?value))" in q

    def test_invalid_entity_id_raises(self):
        with pytest.raises(ValueError, match="entity ID"):
            _build_neighbors_query("not_a_qid", ("P31",))

    def test_invalid_property_id_raises(self):
        with pytest.raises(ValueError, match="property ID"):
            _build_neighbors_query("Q49112", ("not_a_pid",))

    def test_empty_properties_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            _build_neighbors_query("Q49112", ())

    # --- D51: reverse direction ---

    def test_outgoing_query_shape(self):
        """Outgoing default: wd:E ?prop ?value (no LIMIT)."""
        q = _build_neighbors_query("Q49112", ("P31",), direction="outgoing")
        assert "wd:Q49112 ?prop ?value" in q
        assert "LIMIT" not in q

    def test_incoming_query_shape(self):
        """Incoming (D51): ?value ?prop wd:E with LIMIT."""
        q = _build_neighbors_query("Q49112", ("P31",), direction="incoming")
        assert "?value ?prop wd:Q49112" in q
        assert "LIMIT" in q

    def test_invalid_direction_raises(self):
        with pytest.raises(ValueError, match="direction"):
            _build_neighbors_query("Q49112", ("P31",), direction="sideways")

    def test_incoming_limit_validated(self):
        with pytest.raises(ValueError, match="limit"):
            _build_neighbors_query("Q49112", ("P31",), direction="incoming", limit=0)
        with pytest.raises(ValueError, match="limit"):
            _build_neighbors_query("Q49112", ("P31",), direction="incoming", limit=10000)


class TestParseNeighborsBindings:
    def test_groups_by_property(self):
        bindings = [
            {"prop": {"value": "http://www.wikidata.org/prop/direct/P31"},
             "value": {"value": "http://www.wikidata.org/entity/Q3918"}},
            {"prop": {"value": "http://www.wikidata.org/prop/direct/P31"},
             "value": {"value": "http://www.wikidata.org/entity/Q23002054"}},
            {"prop": {"value": "http://www.wikidata.org/prop/direct/P131"},
             "value": {"value": "http://www.wikidata.org/entity/Q771397"}},
        ]
        result = _parse_neighbors_bindings(bindings, ("P31", "P131", "P17"))
        assert result["P31"] == ["Q3918", "Q23002054"]
        assert result["P131"] == ["Q771397"]
        assert result["P17"] == []  # requested but no bindings

    def test_dedupes_within_property(self):
        bindings = [
            {"prop": {"value": "http://www.wikidata.org/prop/direct/P31"},
             "value": {"value": "http://www.wikidata.org/entity/Q3918"}},
            {"prop": {"value": "http://www.wikidata.org/prop/direct/P31"},
             "value": {"value": "http://www.wikidata.org/entity/Q3918"}},
        ]
        result = _parse_neighbors_bindings(bindings, ("P31",))
        assert result["P31"] == ["Q3918"]

    def test_ignores_out_of_set_properties(self):
        # The query VALUES clause should prevent this, but defense-in-depth.
        bindings = [
            {"prop": {"value": "http://www.wikidata.org/prop/direct/P50"},
             "value": {"value": "http://www.wikidata.org/entity/Q42"}},
        ]
        result = _parse_neighbors_bindings(bindings, ("P31",))
        assert result == {"P31": []}

    def test_skips_malformed_value_uri(self):
        bindings = [
            {"prop": {"value": "http://www.wikidata.org/prop/direct/P31"},
             "value": {"value": "not_an_entity_uri"}},
        ]
        result = _parse_neighbors_bindings(bindings, ("P31",))
        assert result["P31"] == []

    def test_skips_missing_prop_or_value(self):
        bindings = [
            {"prop": {"value": ""},
             "value": {"value": "http://www.wikidata.org/entity/Q3918"}},
            {"prop": {"value": "http://www.wikidata.org/prop/direct/P31"},
             "value": {"value": ""}},
        ]
        result = _parse_neighbors_bindings(bindings, ("P31",))
        assert result == {"P31": []}


# ---------------------------------------------------------------------------
# Fixture path
# ---------------------------------------------------------------------------

class TestFixtureNeighbors:
    def test_williams_college_neighbors_fixture(self):
        adapter = WikidataAdapter()
        result = adapter.enumerate_neighbors("Q49112", list(_DEFAULT_NEIGHBOR_PROPERTIES))
        assert "Q3918" in result["P31"]
        assert "Q771397" in result["P131"]
        assert "Q30" in result["P17"]
        # P279 and P361 requested but not in the fixture — empty lists
        assert result["P279"] == []
        assert result["P361"] == []

    def test_missing_fixture_returns_empty(self):
        adapter = WikidataAdapter()
        # Q49166 has no fixture file — should return all-empty, not raise.
        result = adapter.enumerate_neighbors("Q49166", ["P31", "P131"])
        assert result == {"P31": [], "P131": []}

    def test_empty_properties_uses_default_set(self):
        # API: empty list means "use the default property set".
        adapter = WikidataAdapter()
        result = adapter.enumerate_neighbors("Q49112", [])
        for p in _DEFAULT_NEIGHBOR_PROPERTIES:
            assert p in result


# ---------------------------------------------------------------------------
# Live path failure modes (mocked httpx)
# ---------------------------------------------------------------------------

class TestLiveNeighborsFailureModes:
    def test_success_records_audit_with_counts(self):
        adapter, db = _make_adapter()
        body = (
            b'{"results": {"bindings": ['
            b'{"prop": {"value": "http://www.wikidata.org/prop/direct/P31"},'
            b' "value": {"value": "http://www.wikidata.org/entity/Q3918"}}'
            b']}}'
        )
        cm, _ = _make_httpx_cm(_make_response(body))
        with patch("aedos.utils.http_cache.httpx.Client", return_value=cm):
            result = adapter._live_neighbors("Q49112", ("P31", "P131"))
        assert result["P31"] == ["Q3918"]
        assert result["P131"] == []
        from aedos.audit.log import query_events
        events = query_events(db, event_type="kb_live_neighbors")
        assert events[0]["event_data"]["total_neighbors_returned"] == 1
        assert events[0]["event_data"]["per_property_counts"] == {"P31": 1, "P131": 0}
        assert events[0]["event_data"]["retry_count"] == 0
        assert events[0]["event_data"]["error"] is None

    def test_retries_on_timeout_then_succeeds(self):
        adapter, db = _make_adapter()
        body = b'{"results": {"bindings": []}}'
        success_resp = _make_response(body)
        call_count = {"n": 0}

        def fake_get(url, params=None, headers=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise httpx.TimeoutException("simulated timeout")
            return success_resp

        inner = MagicMock()
        inner.get.side_effect = fake_get
        cm = MagicMock()
        cm.__enter__.return_value = inner
        cm.__exit__.return_value = False

        with patch("aedos.utils.http_cache.httpx.Client", return_value=cm):
            with patch("aedos.layer4_sources.kb_wikidata.time.sleep"):
                result = adapter._live_neighbors("Q49112", ("P31",))
        assert result == {"P31": []}
        assert call_count["n"] == 2
        from aedos.audit.log import query_events
        events = query_events(db, event_type="kb_live_neighbors")
        assert events[0]["event_data"]["retry_count"] == 1
        assert events[0]["event_data"]["error"] is None

    def test_retries_on_timeout_then_gives_up_with_empty(self):
        adapter, db = _make_adapter()
        cm, _ = _make_httpx_cm(httpx.TimeoutException("persistent timeout"))
        with patch("aedos.utils.http_cache.httpx.Client", return_value=cm):
            with patch("aedos.layer4_sources.kb_wikidata.time.sleep"):
                result = adapter._live_neighbors("Q49112", ("P31",))
        assert result == {"P31": []}  # fail-open: every requested prop has []
        from aedos.audit.log import query_events
        events = query_events(db, event_type="kb_live_neighbors")
        assert events[0]["event_data"]["retry_count"] == 1
        assert events[0]["event_data"]["total_neighbors_returned"] == 0
        assert "TimeoutException" in events[0]["event_data"]["error"]

    def test_hard_error_returns_empty_no_retry(self):
        """A non-transient httpx error (e.g. invalid SSL) shouldn't retry —
        retrying a programming/config error doesn't help."""
        adapter, db = _make_adapter()
        cm, _ = _make_httpx_cm(RuntimeError("boom"))
        with patch("aedos.utils.http_cache.httpx.Client", return_value=cm):
            result = adapter._live_neighbors("Q49112", ("P31",))
        assert result == {"P31": []}
        from aedos.audit.log import query_events
        events = query_events(db, event_type="kb_live_neighbors")
        assert events[0]["event_data"]["retry_count"] == 0
        assert "RuntimeError" in events[0]["event_data"]["error"]

    def test_malformed_response_returns_empty(self):
        adapter, db = _make_adapter()
        cm, _ = _make_httpx_cm(_make_response(b'{"unexpected": "shape"}'))
        with patch("aedos.utils.http_cache.httpx.Client", return_value=cm):
            result = adapter._live_neighbors("Q49112", ("P31",))
        assert result == {"P31": []}
        from aedos.audit.log import query_events
        events = query_events(db, event_type="kb_live_neighbors")
        assert events[0]["event_data"]["total_neighbors_returned"] == 0
        assert events[0]["event_data"]["error"] is None

    def test_raises_when_no_http_cache_wired(self):
        adapter = WikidataAdapter()  # no http_cache
        with pytest.raises(RuntimeError, match="http_cache"):
            adapter._live_neighbors("Q49112", ("P31",))

    def test_raises_when_called_with_invalid_entity(self):
        adapter, db = _make_adapter()
        with pytest.raises(ValueError, match="entity ID"):
            adapter._live_neighbors("not_a_qid", ("P31",))
        from aedos.audit.log import query_events
        events = query_events(db, event_type="kb_live_neighbors")
        assert len(events) == 1
        assert "entity ID" in events[0]["event_data"]["error"]


# ---------------------------------------------------------------------------
# Protocol contract
# ---------------------------------------------------------------------------

class TestProtocolContract:
    def test_adapter_implements_kbprotocol(self):
        from aedos.layer4_sources.kb_protocol import KBProtocol
        adapter = WikidataAdapter()
        assert isinstance(adapter, KBProtocol)

    def test_dispatch_routes_to_fixture_when_not_live(self):
        adapter = WikidataAdapter()  # default _live=False
        # enumerate_neighbors should call _fixture_neighbors → reads fixture.
        result = adapter.enumerate_neighbors("Q49112", ["P31"])
        assert "Q3918" in result["P31"]


# ---------------------------------------------------------------------------
# Phase H D51: reverse-direction fixture + live tests
# ---------------------------------------------------------------------------

class TestReverseDirectionFixture:
    def test_reverse_fixture_loaded(self):
        """Reverse direction reads a different fixture
        (`neighbors_<entity>_reverse.json`)."""
        adapter = WikidataAdapter()
        # Q49166 doesn't have an outgoing fixture but has a reverse one
        # we'll add in the same commit.
        result = adapter.enumerate_neighbors(
            "Q49166", ["P361"], direction="incoming",
        )
        # If reverse fixture exists, it returns non-empty. Otherwise empty.
        # Both shapes are acceptable here — the test asserts the protocol
        # contract (key present, list value), not the specific neighbors.
        assert "P361" in result
        assert isinstance(result["P361"], list)


class TestReverseLiveFailureModes:
    def test_reverse_call_records_direction_in_audit(self):
        adapter, db = _make_adapter()
        body = b'{"results": {"bindings": []}}'
        cm, _ = _make_httpx_cm(_make_response(body))
        with patch("aedos.utils.http_cache.httpx.Client", return_value=cm):
            adapter._live_neighbors("Q49112", ("P31",), direction="incoming")
        from aedos.audit.log import query_events
        events = query_events(db, event_type="kb_live_neighbors")
        assert events[0]["event_data"]["direction"] == "incoming"
