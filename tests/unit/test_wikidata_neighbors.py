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
from aedos.layer3_substrate.substrate_exceptions import SubstrateExceptionCache
from aedos.layer4_sources.kb_protocol import TransitivePathResult
from aedos.layer4_sources.kb_wikidata import (
    WikidataAdapter,
    _DEFAULT_NEIGHBOR_PROPERTIES,
    _NEIGHBOR_PROPERTIES_BY_RELATION,
    _SUBSUMPTION_PROPERTIES,
    _build_neighbors_query,
    _build_subsumption_ask_query,
    _build_transitive_ask_query,
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

    def test_relation_type_resolves_property_set(self):
        # v0.16.1 WS5c: CORE passes the OPAQUE relation_type and the adapter
        # resolves the P-id neighbor set internally (no P-id naming above the
        # seam). is_a -> (P31, P279); part_of -> (P131, P361, P17).
        adapter = WikidataAdapter()
        is_a = adapter.enumerate_neighbors("Q49112", relation_type="is_a")
        assert set(is_a.keys()) == set(_NEIGHBOR_PROPERTIES_BY_RELATION["is_a"])
        part_of = adapter.enumerate_neighbors("Q49112", relation_type="part_of")
        assert set(part_of.keys()) == set(_NEIGHBOR_PROPERTIES_BY_RELATION["part_of"])
        # Williams College fixture: P31 (is_a) carries the type neighbor,
        # P131/P17 (part_of) carry the containment neighbors.
        assert "Q3918" in is_a["P31"]
        assert "Q771397" in part_of["P131"]
        assert "Q30" in part_of["P17"]

    def test_explicit_properties_take_precedence_over_relation(self):
        # An explicit properties list wins over relation_type (SLING's
        # co-occurrence sampler passes an explicit list).
        adapter = WikidataAdapter()
        result = adapter.enumerate_neighbors(
            "Q49112", ["P31"], relation_type="part_of"
        )
        assert set(result.keys()) == {"P31"}

    def test_unknown_relation_type_falls_back_to_default(self):
        adapter = WikidataAdapter()
        result = adapter.enumerate_neighbors("Q49112", relation_type="bogus")
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


# ---------------------------------------------------------------------------
# v0.16 WS2 §1: verify_transitive_path — the first-class transitive-path
# primitive the walker's discover/verify drives. Covers query-builder parity
# (byte-identical to the old subsumption ASK), the single-property generic
# path, the type-guarded P361 part_of bridge (Warsaw⊄Germany leak guard
# preserved), the fixture path, and live fail-open.
# ---------------------------------------------------------------------------

class TestTransitiveAskQueryBuilder:
    def test_is_a_query_byte_identical_to_subsumption_ask(self):
        """The extracted `_build_transitive_ask_query` must produce output
        byte-identical to the old `_build_subsumption_ask_query` for is_a, so
        the verifier's subsumption ASK is unchanged by the generalization."""
        legacy = _build_subsumption_ask_query("Q5", "Q215627", "is_a")
        generalized = _build_transitive_ask_query(
            "Q5", "Q215627", _SUBSUMPTION_PROPERTIES["is_a"], use_part_of_bridge=False
        )
        assert generalized == legacy
        assert "wdt:P31" in generalized and "wdt:P279" in generalized

    def test_part_of_query_byte_identical_to_subsumption_ask(self):
        """part_of delegation must be byte-identical too — preserving the
        type-guarded P361 bridge (the Marie-Curie / Warsaw leak guard)."""
        legacy = _build_subsumption_ask_query("Q270", "Q183", "part_of")
        generalized = _build_transitive_ask_query(
            "Q270", "Q183", _SUBSUMPTION_PROPERTIES["part_of"],
            use_part_of_bridge=True,
        )
        assert generalized == legacy

    def test_single_property_generic_path_no_bridge(self):
        """relation_type=None → a plain single-property `(wdt:P)+` path for ANY
        transitive property (e.g. P171 parent-taxon). No is_a/part_of
        alternation, no P361 bridge, no UNION."""
        q = _build_transitive_ask_query("Q140", "Q729", ("P171",), use_part_of_bridge=False)
        assert q == "ASK { wd:Q140 (wdt:P171)+ wd:Q729 . }"
        assert "UNION" not in q
        assert "P361" not in q

    def test_part_of_bridge_is_type_guarded(self):
        """The type-guarded P361 part_of bridge that re-pins Warsaw⊄Germany:
        the bridge participates ONLY between two region-typed nodes (the
        VALUES guard). The plain safe alternation (P131/P30/P17) is the first
        UNION branch; the P361 hop is guarded by P31 ∈ _GEO_REGION_TYPES on
        BOTH endpoints — exactly the construction that closed the
        Warsaw-P206-Vistula-P17-Germany / Warsaw-P361-historical-Prussia leaks.
        A regression that drops the type guard (a bare `(...|wdt:P361)+`) would
        reopen Warsaw⊄Germany; this asserts the guard is present."""
        bridged = _build_transitive_ask_query(
            "Q270", "Q183", _SUBSUMPTION_PROPERTIES["part_of"],
            use_part_of_bridge=True,
        )
        # The bridge is present...
        assert "wdt:P361" in bridged
        # ...but P361 is NOT folded into the unbounded alternation path: the
        # alternation only contains the safe (P131/P30/P17) properties.
        for safe in ("P131", "P30", "P17"):
            assert f"wdt:{safe}" in bridged
        assert "(wdt:P131|wdt:P30|wdt:P17|wdt:P361)" not in bridged
        # ...and the P361 hop is type-guarded to region types on both nodes.
        assert "wdt:P31 ?gt1" in bridged
        assert "wdt:P31 ?gt2" in bridged
        assert bridged.count("VALUES") == 2

    def test_invalid_entity_id_raises(self):
        with pytest.raises(ValueError, match="entity ID"):
            _build_transitive_ask_query("not_a_qid", "Q1", ("P171",), use_part_of_bridge=False)

    def test_invalid_property_id_raises(self):
        with pytest.raises(ValueError, match="property ID"):
            _build_transitive_ask_query("Q1", "Q2", ("not_a_pid",), use_part_of_bridge=False)


class TestVerifyTransitivePathFixture:
    def test_holds_for_recorded_chain(self):
        """Fixture path: Q95's recorded subsumption chain is non-empty, so a
        transitive-path check from Q95 reports holds=True. The result is a
        single-direction `TransitivePathResult`, not the symmetric
        `SubsumptionResult`."""
        adapter = WikidataAdapter()  # _live=False → fixture path
        result = adapter.verify_transitive_path(
            "Q95", "Q43229", "P31", relation_type="is_a"
        )
        assert isinstance(result, TransitivePathResult)
        assert result.holds is True

    def test_missing_fixture_does_not_hold(self):
        """No fixture for the source → no recorded path → holds=False (a path
        miss abstains, never false-verifies)."""
        adapter = WikidataAdapter()
        result = adapter.verify_transitive_path(
            "Q49166", "Q183", "P131", relation_type="part_of"
        )
        assert result.holds is False

    def test_single_property_branch_uses_kb_property(self):
        """relation_type=None routes through the single-property branch; a
        missing fixture still yields a fail-open non-holding result."""
        adapter = WikidataAdapter()
        result = adapter.verify_transitive_path("Q49166", "Q729", "P171")
        assert isinstance(result, TransitivePathResult)
        assert result.holds is False

    def test_unsupported_relation_type_raises(self):
        adapter = WikidataAdapter()
        with pytest.raises(ValueError, match="relation_type"):
            adapter.verify_transitive_path("Q1", "Q2", "P31", relation_type="bogus")


class TestVerifyTransitivePathLiveFailOpen:
    """Exercise the live ASK path directly (mirrors TestLiveNeighborsFailureModes
    calling `_live_neighbors`): `verify_transitive_path` dispatches to the
    fixture path when RUN_LIVE_KB != 1, so the live behavior is tested by
    calling `_live_transitive_path` with the resolved property tuple."""

    def test_holds_true_on_positive_ask(self):
        adapter, db = _make_adapter()
        cm, _ = _make_httpx_cm(_make_response(b'{"boolean": true}'))
        with patch("aedos.utils.http_cache.httpx.Client", return_value=cm):
            result = adapter._live_transitive_path(
                "Q270", "Q36", _SUBSUMPTION_PROPERTIES["part_of"],
                use_part_of_bridge=True,
            )
        assert result.holds is True
        assert result.error is None
        from aedos.audit.log import query_events
        events = query_events(db, event_type="kb_verify_transitive_path")
        assert events[0]["event_data"]["holds"] is True
        assert events[0]["event_data"]["use_part_of_bridge"] is True

    def test_holds_false_on_negative_ask(self):
        """The Warsaw⊄Germany regression at the adapter level: a negative ASK
        (the leak guard keeps the path from existing live) yields holds=False."""
        adapter, db = _make_adapter()
        cm, _ = _make_httpx_cm(_make_response(b'{"boolean": false}'))
        with patch("aedos.utils.http_cache.httpx.Client", return_value=cm):
            result = adapter._live_transitive_path(
                "Q270", "Q183", _SUBSUMPTION_PROPERTIES["part_of"],  # Warsaw ⊄ Germany
                use_part_of_bridge=True,
            )
        assert result.holds is False
        assert result.error is None

    def test_timeout_fails_open_with_error(self):
        adapter, db = _make_adapter()
        cm, _ = _make_httpx_cm(httpx.TimeoutException("persistent timeout"))
        with patch("aedos.utils.http_cache.httpx.Client", return_value=cm):
            with patch("aedos.layer4_sources.kb_wikidata.time.sleep"):
                result = adapter._live_transitive_path(
                    "Q1", "Q2", ("P171",), use_part_of_bridge=False
                )
        assert result.holds is False
        assert "TimeoutException" in (result.error or "")

    def test_malformed_response_fails_open(self):
        adapter, db = _make_adapter()
        cm, _ = _make_httpx_cm(_make_response(b'not json'))
        with patch("aedos.utils.http_cache.httpx.Client", return_value=cm):
            result = adapter._live_transitive_path(
                "Q1", "Q2", ("P171",), use_part_of_bridge=False
            )
        assert result.holds is False


# ---------------------------------------------------------------------------
# v0.16 WS3 §3D: verify_transitive_path consults + records the bounded nogood
# cache. A confirmed non-holding path is EAGERLY recorded as `ask_false`; a
# later consult of the same (source, target, relation) short-circuits to
# holds=False WITHOUT a SPARQL ASK — the Marie-Curie / Warsaw⊄Germany leak
# guard. Because the recorded nogood is matched path-agnostically by relation,
# widening the property alternation later cannot resurrect the closed leak.
# ---------------------------------------------------------------------------

class TestVerifyTransitivePathNogoodCache:
    def test_negative_result_records_nogood(self):
        adapter, db = _make_adapter()  # fixture path: Warsaw⊄Germany → holds False
        cache = SubstrateExceptionCache(db)
        adapter._exception_cache = cache
        res = adapter.verify_transitive_path("Q270", "Q183", "P131", relation_type="part_of")
        assert res.holds is False
        # The negative ASK was eagerly recorded as a nogood.
        rows = db.execute(
            "SELECT relation_type, source_identifier, target_identifier, reason "
            "FROM substrate_exceptions"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["relation_type"] == "part_of"
        assert rows[0]["source_identifier"] == "Q270"
        assert rows[0]["target_identifier"] == "Q183"
        assert rows[0]["reason"] == "ask_false"

    def test_nogood_short_circuits_without_ask(self):
        adapter, db = _make_adapter()
        cache = SubstrateExceptionCache(db)
        adapter._exception_cache = cache
        # First call records the nogood.
        adapter.verify_transitive_path("Q270", "Q183", "P131", relation_type="part_of")

        # A leak-guard regression: even if the alternation were widened so the
        # underlying ASK now (wrongly) returns True, the recorded nogood must
        # short-circuit BEFORE any ASK fires. We assert the ASK is not reached
        # by making the fixture path raise if it is consulted.
        def _boom(*a, **k):
            raise AssertionError("verify_transitive_path must not ASK on a cached nogood")
        adapter._fixture_transitive_path = _boom  # type: ignore[method-assign]

        res2 = adapter.verify_transitive_path("Q270", "Q183", "P131", relation_type="part_of")
        assert res2.holds is False

    def test_retracted_nogood_no_longer_short_circuits(self):
        adapter, db = _make_adapter()
        cache = SubstrateExceptionCache(db)
        adapter._exception_cache = cache
        adapter.verify_transitive_path("Q270", "Q183", "P131", relation_type="part_of")
        row_id = db.execute(
            "SELECT id FROM substrate_exceptions ORDER BY id LIMIT 1"
        ).fetchone()["id"]
        cache.retract(row_id, reason="operator: path now holds")
        # After retraction the nogood no longer matches, so the consult does NOT
        # short-circuit — the ASK runs again. We prove the ASK is reached by
        # making the fixture path observable.
        ran = {"asked": False}
        orig = adapter._fixture_transitive_path

        def _spy(*a, **k):
            ran["asked"] = True
            return orig(*a, **k)
        adapter._fixture_transitive_path = _spy  # type: ignore[method-assign]
        res = adapter.verify_transitive_path("Q270", "Q183", "P131", relation_type="part_of")
        assert ran["asked"] is True
        assert res.holds is False
        # The retracted row stays soft-deleted (its UNIQUE key collides with the
        # eager re-record's INSERT OR IGNORE), so the operator's retraction is
        # durable — an `ask_false` re-record cannot silently resurrect it.
        assert cache.is_nogood(
            relation_type="part_of", source_identifier="Q270", target_identifier="Q183",
        ) is False

    def test_no_cache_wired_runs_as_before(self):
        # Without an exception cache the method behaves exactly as the WS2
        # fixture path — no consult, no record, no substrate_exceptions row.
        adapter, db = _make_adapter()
        res = adapter.verify_transitive_path("Q270", "Q183", "P131", relation_type="part_of")
        assert res.holds is False
        count = db.execute("SELECT COUNT(*) FROM substrate_exceptions").fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# v0.16.1 WS9 (item 8 lever A): verify_transitive_path positive-result memo.
# Process-scoped per-adapter; keyed by (cache_relation, source, target); stores
# ONLY definite (error-None) answers; bounded by an LRU cap AND a TTL on an
# injectable monotonic clock. Behavior-NEUTRAL: a hit returns exactly what a
# fresh definite ASK would (same holds, error=None), without the network — it
# changes ZERO verdicts. These tests drive the dispatch boundary
# (`_fixture_transitive_path`) with a call-counting spy so the memo's hit/miss,
# error-exclusion, TTL, and LRU behavior are isolated from any real fixture.
# ---------------------------------------------------------------------------


class _FakeClock:
    """A controllable monotonic clock for deterministic TTL tests."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _spy_dispatch(adapter, result_factory):
    """Replace the adapter's fixture-path dispatch with a call-counting spy
    that returns `result_factory()` each call. Returns the call-count dict so a
    test can assert how many times the (would-be-network) ASK actually ran."""
    calls = {"n": 0}

    def _spy(*a, **k):
        calls["n"] += 1
        return result_factory()
    adapter._fixture_transitive_path = _spy  # type: ignore[method-assign]
    return calls


class TestVerifyTransitivePathPositiveMemo:
    def test_definite_positive_is_memoized_no_second_ask(self):
        """A definite holds=True result is memoized; a repeat call for the same
        (relation, source, target) returns from the memo WITHOUT a second ASK."""
        adapter = WikidataAdapter()  # no cache, fixture path
        calls = _spy_dispatch(adapter, lambda: TransitivePathResult(holds=True))
        r1 = adapter.verify_transitive_path("Q95", "Q43229", "P31", relation_type="is_a")
        r2 = adapter.verify_transitive_path("Q95", "Q43229", "P31", relation_type="is_a")
        assert r1.holds is True and r2.holds is True
        assert r2.error is None
        assert calls["n"] == 1  # the second answer came from the memo

    def test_definite_negative_is_memoized_when_no_nogood_cache(self):
        """With no nogood cache wired (no retraction path), a definite holds=False
        is also memoized — the TTL bounds staleness. A repeat call hits the memo."""
        adapter = WikidataAdapter()
        calls = _spy_dispatch(adapter, lambda: TransitivePathResult(holds=False))
        r1 = adapter.verify_transitive_path("Q1", "Q2", "P171")
        r2 = adapter.verify_transitive_path("Q1", "Q2", "P171")
        assert r1.holds is False and r2.holds is False
        assert calls["n"] == 1

    def test_error_result_is_never_memoized(self):
        """3.2 paramount: a fail-open/error result (error non-None) must NEVER be
        memoized — every call must re-hit the network so a transient failure can't
        pin a wrong answer."""
        adapter = WikidataAdapter()
        calls = _spy_dispatch(
            adapter,
            lambda: TransitivePathResult(holds=False, error="TimeoutException: boom"),
        )
        adapter.verify_transitive_path("Q1", "Q2", "P171")
        adapter.verify_transitive_path("Q1", "Q2", "P171")
        adapter.verify_transitive_path("Q1", "Q2", "P171")
        assert calls["n"] == 3  # no memoization of an error/fail-open result
        assert len(adapter._transitive_memo) == 0

    def test_error_then_definite_recovers_and_memoizes(self):
        """A transient error does not pin the answer: once the ASK returns a
        definite result, that is memoized (the next call hits the memo). Proves
        the error-exclusion lets the correct answer take over."""
        adapter = WikidataAdapter()
        seq = [
            TransitivePathResult(holds=False, error="net"),  # transient failure
            TransitivePathResult(holds=True),                # recovered, definite
        ]
        calls = {"n": 0}

        def _spy(*a, **k):
            calls["n"] += 1
            return seq[min(calls["n"] - 1, len(seq) - 1)]
        adapter._fixture_transitive_path = _spy  # type: ignore[method-assign]

        r1 = adapter.verify_transitive_path("Q5", "Q6", "P171")
        assert r1.error == "net" and r1.holds is False  # surfaced live, not cached
        r2 = adapter.verify_transitive_path("Q5", "Q6", "P171")  # re-ASK → definite
        r3 = adapter.verify_transitive_path("Q5", "Q6", "P171")  # memo hit
        assert r2.holds is True and r3.holds is True
        assert calls["n"] == 2  # third answer came from the memo

    def test_memo_hit_returns_fresh_definite_result_object(self):
        """A hit returns a NEW TransitivePathResult with error=None — equivalent
        to a fresh definite ASK — not the stored object, so a caller can never
        mutate the cache and never observes a stale error field."""
        adapter = WikidataAdapter()
        _spy_dispatch(
            adapter,
            lambda: TransitivePathResult(holds=True, establishing_property="P31"),
        )
        r1 = adapter.verify_transitive_path("Q95", "Q43229", "P31", relation_type="is_a")
        r2 = adapter.verify_transitive_path("Q95", "Q43229", "P31", relation_type="is_a")
        assert r2 is not r1
        assert r2.holds is True
        assert r2.error is None
        assert r2.establishing_property == "P31"

    def test_distinct_keys_do_not_collide(self):
        """The key is (relation, source, target): different sources, targets, or
        relation types are independent entries (no cross-talk)."""
        adapter = WikidataAdapter()
        calls = _spy_dispatch(adapter, lambda: TransitivePathResult(holds=True))
        adapter.verify_transitive_path("Q1", "Q2", "P171")
        adapter.verify_transitive_path("Q1", "Q3", "P171")  # different target
        adapter.verify_transitive_path("Q9", "Q2", "P171")  # different source
        # different relation_type alternation for the same pair:
        adapter.verify_transitive_path("Q1", "Q2", "P31", relation_type="is_a")
        assert calls["n"] == 4  # four distinct keys, four ASKs
        # ...and each is independently a memo hit on repeat (no new ASK):
        adapter.verify_transitive_path("Q1", "Q2", "P171")
        assert calls["n"] == 4

    def test_ttl_expiry_drops_entry_and_re_asks(self):
        """A memo entry past its TTL is dropped on access and the ASK re-runs —
        a Wikidata edit cannot pin a stale answer for the process lifetime."""
        from aedos.layer4_sources.kb_wikidata import _TRANSITIVE_MEMO_TTL_SECONDS
        clock = _FakeClock()
        adapter = WikidataAdapter(clock=clock)
        calls = _spy_dispatch(adapter, lambda: TransitivePathResult(holds=True))
        adapter.verify_transitive_path("Q1", "Q2", "P171")
        assert calls["n"] == 1
        # within TTL: memo hit, no new ASK
        clock.advance(_TRANSITIVE_MEMO_TTL_SECONDS - 1.0)
        adapter.verify_transitive_path("Q1", "Q2", "P171")
        assert calls["n"] == 1
        # past TTL: entry dropped, ASK re-runs
        clock.advance(2.0)
        adapter.verify_transitive_path("Q1", "Q2", "P171")
        assert calls["n"] == 2

    def test_lru_cap_evicts_oldest(self):
        """The memo is bounded by an LRU cap: once over capacity the oldest
        (least-recently-used) entry is evicted; re-asking it re-runs the ASK."""
        import aedos.layer4_sources.kb_wikidata as mod
        adapter = WikidataAdapter()
        calls = _spy_dispatch(adapter, lambda: TransitivePathResult(holds=True))
        with patch.object(mod, "_TRANSITIVE_MEMO_MAX_ENTRIES", 2):
            adapter.verify_transitive_path("Q1", "Q2", "P171")  # key A
            adapter.verify_transitive_path("Q3", "Q4", "P171")  # key B
            assert calls["n"] == 2 and len(adapter._transitive_memo) == 2
            # key C exceeds the cap -> evict the LRU entry (A)
            adapter.verify_transitive_path("Q5", "Q6", "P171")  # key C
            assert calls["n"] == 3 and len(adapter._transitive_memo) == 2
            # B and C are still cached: repeats are memo hits, no new ASK
            adapter.verify_transitive_path("Q3", "Q4", "P171")  # B hit
            adapter.verify_transitive_path("Q5", "Q6", "P171")  # C hit
            assert calls["n"] == 3
            # A was evicted: re-asking it runs a fresh ASK
            adapter.verify_transitive_path("Q1", "Q2", "P171")  # A re-ASK
            assert calls["n"] == 4

    def test_definite_negative_not_memoized_when_nogood_cache_wired(self):
        """Negative-result ownership: when a nogood cache IS wired, the nogood
        cache (which honors operator retraction) owns definite negatives — the
        positive memo does NOT also store holds=False, so a retracted negative
        is never served stale from the memo. The memo stays empty for negatives."""
        adapter, db = _make_adapter()
        cache = SubstrateExceptionCache(db)
        adapter._exception_cache = cache
        calls = _spy_dispatch(adapter, lambda: TransitivePathResult(holds=False))
        adapter.verify_transitive_path("Q270", "Q183", "P131", relation_type="part_of")
        assert len(adapter._transitive_memo) == 0  # negative left to the nogood cache
        # The nogood cache short-circuits the repeat (its own mechanism), so this
        # is behavior-neutral; the memo simply abstains from negatives here.

    def test_positive_memoized_even_with_nogood_cache_wired(self):
        """A definite holds=True is always memoized (the nogood cache never holds
        positives); the second call is a memo hit with no new ASK."""
        adapter, db = _make_adapter()
        cache = SubstrateExceptionCache(db)
        adapter._exception_cache = cache
        calls = _spy_dispatch(adapter, lambda: TransitivePathResult(holds=True))
        adapter.verify_transitive_path("Q95", "Q43229", "P31", relation_type="is_a")
        adapter.verify_transitive_path("Q95", "Q43229", "P31", relation_type="is_a")
        assert calls["n"] == 1
        assert len(adapter._transitive_memo) == 1

    # --- (e) BEHAVIOR-NEUTRALITY: a warm memo changes no verdict ---
    #
    # The tests above drive a call-counting spy to isolate the memo's hit/miss
    # mechanics. These complementary tests run the REAL fixture dispatch (no
    # spy, no mock) and assert the public `verify_transitive_path` returns a
    # TransitivePathResult EQUIVALENT IN EVERY FIELD with vs without a warm
    # memo — for a representative is_a transitive case AND the part_of bridge.
    # That is the §3.2 guarantee made concrete: the memo returns exactly what a
    # fresh definite ASK would and cannot change a verdict.

    @staticmethod
    def _fields(r: TransitivePathResult) -> tuple:
        return (r.holds, r.establishing_property, r.error)

    def test_warm_memo_hit_equivalent_to_cold_is_a_path(self):
        """Representative is_a transitive case through the real fixture path
        (Q95 ⊂ Q43229 via the recorded chain → holds=True, error=None). A cold
        adapter and the second (warm-memo) call on the same adapter return
        field-identical results."""
        cold = WikidataAdapter()  # fresh adapter, empty memo → fixture ASK
        cold_result = cold.verify_transitive_path(
            "Q95", "Q43229", "P31", relation_type="is_a"
        )
        warm = WikidataAdapter()
        warm.verify_transitive_path("Q95", "Q43229", "P31", relation_type="is_a")  # populate
        warm_result = warm.verify_transitive_path(
            "Q95", "Q43229", "P31", relation_type="is_a"
        )  # memo hit
        # The cold (no-memo) and warm (memo-hit) results are field-identical:
        # same holds, same establishing_property, same (None) error.
        assert self._fields(cold_result) == self._fields(warm_result)
        assert cold_result.holds is True
        assert cold_result.error is None
        # And the warm hit truly skipped the dispatch (memo populated, 1 entry).
        assert len(warm._transitive_memo) == 1

    def test_warm_memo_hit_equivalent_to_cold_part_of_bridge(self):
        """The part_of bridge (type-guarded P361, the Warsaw⊄Germany leak guard)
        returns the SAME holds with vs without a warm memo. Driven by a
        deterministic ASK-shaped fixture-style result through `_live_transitive_path`
        so the bridge alternation actually runs, with the memo consulted at the
        public seam. Cold and warm calls are field-identical and the verdict is
        unchanged."""
        # Use the live ASK path with a mocked positive boolean so the part_of
        # bridge query is genuinely constructed and executed on the cold call;
        # the warm call must reproduce the identical verdict from the memo.
        def _run(adapter, *, warm: bool):
            cm, _ = _make_httpx_cm(_make_response(b'{"boolean": true}'))
            with patch("aedos.utils.http_cache.httpx.Client", return_value=cm):
                with patch.object(adapter, "_live", True):
                    if warm:
                        adapter.verify_transitive_path(
                            "Q270", "Q36", "P131", relation_type="part_of"
                        )  # populate memo
                    return adapter.verify_transitive_path(
                        "Q270", "Q36", "P131", relation_type="part_of"
                    )

        cold_adapter, _ = _make_adapter()
        warm_adapter, _ = _make_adapter()
        cold_result = _run(cold_adapter, warm=False)
        warm_result = _run(warm_adapter, warm=True)
        assert cold_result.holds is True
        assert cold_result.error is None
        assert self._fields(cold_result) == self._fields(warm_result)

    def test_warm_memo_hit_equivalent_to_cold_negative_path(self):
        """A definite negative (missing fixture → holds=False, error=None) is
        equivalent cold vs warm too (no nogood cache wired, so the negative is
        memoized and the TTL bounds staleness). The abstain verdict is unchanged."""
        cold = WikidataAdapter()
        cold_result = cold.verify_transitive_path(
            "Q49166", "Q183", "P131", relation_type="part_of"
        )
        warm = WikidataAdapter()
        warm.verify_transitive_path("Q49166", "Q183", "P131", relation_type="part_of")
        warm_result = warm.verify_transitive_path(
            "Q49166", "Q183", "P131", relation_type="part_of"
        )
        assert self._fields(cold_result) == self._fields(warm_result)
        assert cold_result.holds is False
        assert cold_result.error is None
