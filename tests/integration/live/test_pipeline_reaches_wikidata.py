"""End-to-end wiring verification for Phase F2 — confirms the assembled
deployed pipeline (built via `build_pipeline`) reaches live Wikidata.

This is the F1 acceptance criterion "the capability is reachable from
the deployed pipeline path, not just unit-test-callable" in test form.
F2 commit #4 (wiring) constructs `WikidataAdapter` with `http_cache`,
`llm_client`, `db`, `config`; this test confirms the construction
results in live Wikidata calls actually firing when the pipeline's
resolver and kb_verifier components are exercised.

Gated by `RUN_LIVE_KB=1` per the existing convention.
"""

from __future__ import annotations

import os

import pytest

from aedos.audit.log import query_events
from aedos.database import open_memory_db
from aedos.layer4_sources.kb_protocol import LocalContext
from aedos.pipeline import build_pipeline


_RUN_LIVE = os.environ.get("RUN_LIVE_KB") == "1"
pytestmark = pytest.mark.skipif(
    not _RUN_LIVE,
    reason="Pipeline-reaches-Wikidata tests require RUN_LIVE_KB=1",
)


@pytest.fixture
def live_pipeline():
    """An assembled pipeline against an in-memory DB. The LLM client is
    real (LLMClient default), but the pipeline's actual LLM calls are
    not exercised by this test — we drive only the KB layer."""
    db = open_memory_db()
    pipeline = build_pipeline(db)
    yield pipeline
    db.close()


class TestPipelineReachesWikidata:
    def test_assembled_pipeline_resolver_reaches_live_wikidata(self, live_pipeline):
        """The assembled pipeline's `resolver` (an `EntityResolver` wrapping
        the wired `WikidataAdapter`) makes live `wbsearchentities` calls.
        This is the F1 wiring-correctness check landed against the live
        path: build_pipeline → resolver.resolve → WikidataAdapter.resolve_entity
        → _live_resolve → live API."""
        lc = LocalContext(predicate="located_in", slot_position="subject")
        candidates = live_pipeline.resolver.resolve("United States", lc)
        assert len(candidates) > 0
        # Verify the audit event fired *through the assembled pipeline*,
        # not just through a directly-constructed adapter.
        events = query_events(live_pipeline.db, event_type="kb_live_resolve")
        assert len(events) >= 1, (
            "F1 acceptance: the assembled pipeline must emit kb_live_resolve "
            "audit events when live Wikidata is called"
        )
        assert events[0]["event_subject"] == "United States"

    def test_assembled_pipeline_kb_calls_use_configured_user_agent(self, live_pipeline):
        """The User-Agent threaded through `build_pipeline` (per F-007)
        must be the one in HTTP requests. We verify by inspecting the
        pipeline's adapter — direct HTTP inspection is impractical, but
        the adapter's `_http` carries the configured headers."""
        ua = live_pipeline.kb._http._base_headers.get("User-Agent", "")
        assert "Aedos/0.15" in ua
        # Wikimedia policy requires contact info — URL or email. Either
        # works; we assert at least one is present.
        assert ("github.com" in ua) or ("@" in ua), (
            f"User-Agent must include Wikimedia-compliant contact info; got: {ua}"
        )

    def test_assembled_pipeline_kb_lookup_emits_audit(self, live_pipeline):
        """Pipeline's WikidataAdapter.lookup_statements (called through
        kb_verifier on the verification path) emits `kb_live_lookup`."""
        # Direct adapter call here (rather than going through kb_verifier)
        # because kb_verifier requires a predicate-translation row, which
        # this test doesn't want to set up (it would also call the LLM).
        # The wiring assertion is the same: assembled adapter → live API.
        statements = live_pipeline.kb.lookup_statements("Q30", "P36")
        assert len(statements) > 0
        events = query_events(live_pipeline.db, event_type="kb_live_lookup")
        assert len(events) >= 1
        assert events[0]["event_subject"] == "Q30:P36"

    def test_assembled_pipeline_subsumption_emits_audit(self, live_pipeline):
        """Pipeline's WikidataAdapter.subsumption emits `kb_live_subsumption`."""
        result = live_pipeline.kb.subsumption("Q76", "Q5", "is_a")
        assert result.verdict == "a_subsumed_by_b"
        events = query_events(live_pipeline.db, event_type="kb_live_subsumption")
        assert len(events) >= 1


class TestD33FullResolutionPath:
    """Phase G D33 (2026-05-23): end-to-end resolution against live Wikidata
    with type filter engaged. Exercises the resolver path with a populated
    LocalContext — the same path the KBVerifier drives in production."""

    def test_obama_to_q76_through_assembled_resolver_with_type_filter(self, live_pipeline):
        """The assembled pipeline's resolver, given expected_entity_types=[Q5],
        picks Q76 (Barack Obama) over Q41773 (Obama, Fukui town) — the
        load-bearing D33 correction at the integration level."""
        lc = LocalContext(
            predicate="holds_role",
            slot_position="subject",
            expected_entity_types=["Q5"],
        )
        candidates = live_pipeline.resolver.resolve("Obama", lc)
        ids = [c.kb_identifier for c in candidates]
        # The resolver caches; the first candidate is the highest-scored
        # in the filtered list and should be Q76.
        assert "Q76" in ids
        # Q41773 must be filtered out
        assert "Q41773" not in ids

    def test_resolver_select_picks_q76_after_type_filter(self, live_pipeline):
        """`EntityResolver.select` returns the top-scored candidate after
        filtering. With type filter engaged, top-1 should be Q76, not
        Q41773 (the unfiltered top-1 historically)."""
        lc = LocalContext(
            predicate="holds_role",
            slot_position="subject",
            expected_entity_types=["Q5"],
        )
        candidates = live_pipeline.resolver.resolve("Obama", lc)
        selected = live_pipeline.resolver.select(candidates, lc)
        assert selected == "Q76", (
            f"Resolver should select Q76 with type filter engaged; got {selected!r}"
        )
