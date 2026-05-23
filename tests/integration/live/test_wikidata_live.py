"""Live tests for WikidataAdapter — exercises the real Wikidata API.

Gated by `RUN_LIVE_KB=1` (the existing convention; see
docs/phase_10_5_runbook.md and tests/cold_start/test_zero_seed_correctness.py).

Tests cover Phase F2's `_live_resolve` (commit 1). `_live_lookup` and
`_live_subsumption` tests land with their respective implementation
commits.
"""

from __future__ import annotations

import os

import pytest

from aedos.config import Config
from aedos.database import open_memory_db
from aedos.layer4_sources.kb_protocol import LocalContext
from aedos.layer4_sources.kb_wikidata import WikidataAdapter
from aedos.utils.http_cache import CachingHTTPClient, LRUHTTPCache


_RUN_LIVE = os.environ.get("RUN_LIVE_KB") == "1"
pytestmark = pytest.mark.skipif(
    not _RUN_LIVE,
    reason="Live Wikidata tests require RUN_LIVE_KB=1",
)


@pytest.fixture
def live_adapter():
    """A WikidataAdapter wired against the real Wikidata API.

    Uses an in-memory DB so audit events are captured but the test does
    not leave state on disk. The User-Agent comes from `Config.user_agent`
    — the same configuration the deployed pipeline uses, so live tests
    exercise the same headers Wikimedia sees in production."""
    db = open_memory_db()
    config = Config()
    cache = LRUHTTPCache(
        max_size=config.http_cache_lru_size,
        default_ttl_seconds=config.http_cache_entity_ttl_seconds,
    )
    http_client = CachingHTTPClient(
        cache=cache,
        default_ttl_seconds=config.http_cache_entity_ttl_seconds,
        headers={"User-Agent": config.user_agent},
    )
    adapter = WikidataAdapter(
        http_cache=http_client,
        db=db,
        config=config,
    )
    yield adapter
    db.close()


class TestLiveResolve:
    def test_resolve_returns_ranked_candidates(self, live_adapter):
        """Protocol shape: a query that has matches returns a non-empty
        list of `ResolutionCandidate`, each with kb_identifier, score,
        and provenance, scores monotone-decreasing (rank-based)."""
        lc = LocalContext(predicate="holds_role", slot_position="subject")
        candidates = live_adapter.resolve_entity("Obama", lc)
        assert len(candidates) > 0
        # Score formula is 1/(rank+1) → monotone decreasing
        scores = [c.kb_identifier and c.score for c in candidates]
        assert scores == sorted(scores, reverse=True)
        # Each candidate has the expected provenance keys
        for c in candidates:
            assert c.kb_identifier.startswith("Q")
            assert "search_rank" in c.provenance
            assert "label" in c.provenance

    def test_obama_disambiguation_returns_multiple(self, live_adapter):
        """`wbsearchentities` returns multiple candidates for "Obama" —
        downstream `EntityResolver.select` is responsible for picking
        between them. F2's _live_resolve just ranks; this test confirms
        ranking returns >1 candidate when the query is ambiguous."""
        lc = LocalContext(predicate="holds_role", slot_position="subject")
        candidates = live_adapter.resolve_entity("Obama", lc)
        assert len(candidates) >= 2

    def test_unknown_entity_returns_empty(self, live_adapter):
        """Sentinel reference unlikely to exist in Wikidata — confirms
        the empty-candidates abstention path (architecture §9.4)."""
        lc = LocalContext(predicate="holds_role", slot_position="subject")
        # Same sentinel used by the fixture-mode test (search_no_match.json).
        candidates = live_adapter.resolve_entity(
            "xyzzy_nonexistent_entity_42_aedos_test", lc
        )
        assert candidates == []

    def test_resolve_emits_audit_event(self, live_adapter):
        """Wiring verification (F1 acceptance criterion): a live resolve
        produces a `kb_live_resolve` audit event so F4's end-to-end
        trace can confirm live calls happened."""
        from aedos.audit.log import query_events
        lc = LocalContext(predicate="located_in", slot_position="subject")
        live_adapter.resolve_entity("Williams College", lc)
        events = query_events(
            live_adapter._db, event_type="kb_live_resolve", limit=10
        )
        assert len(events) >= 1
        assert events[0]["event_subject"] == "Williams College"
        assert "candidate_count" in events[0]["event_data"]
        assert "duration_ms" in events[0]["event_data"]


class TestD33TypeFilterAndBareStringLimit:
    """Phase G D33 (2026-05-23): the type filter correctly drops wrong-type
    entities for queries where the canonical entity is type-distinguishable
    from the noise (test_type_filter_drops_obama_fukui_for_person_query).
    Live validation also surfaced the new finding D47: some canonical
    entities (Q76 for "Obama", Q49112 for "Williams College") are NOT
    reachable from their bare ambiguous string forms via Wikidata — the
    bare strings are not registered in Wikidata's label or altLabel fields
    for the canonical entities (verified by direct SPARQL on 2026-05-23,
    e.g. Q76's only English altLabel is "POTUS 44"). Contextual
    disambiguation upstream of KB queries is the v0.16 D47 work item.
    The two tests below pin this limit so a future Wikidata edit
    (Q76 gaining "Obama" as an altLabel, etc.) is detected via xpass.
    """

    def test_type_filter_drops_obama_fukui_for_person_query(self, live_adapter):
        # Q41773 (Obama, Fukui — the Japanese town) sits at the top of
        # the unfiltered ranking for query 'Obama'. With expected types
        # [Q5] (human), it must NOT survive the filter. This is the
        # load-bearing D33 correction: the filter prevents Q41773 from
        # masquerading as a person.
        lc = LocalContext(
            predicate="holds_role",
            slot_position="subject",
            expected_entity_types=["Q5"],
        )
        candidates = live_adapter.resolve_entity("Obama", lc)
        ids = [c.kb_identifier for c in candidates]
        assert "Q41773" not in ids, (
            f"Q41773 (Obama, Fukui — a town, not a human) should be "
            f"eliminated by the type filter; got {ids}"
        )

    def test_audit_event_records_filter_metrics(self, live_adapter):
        # The audit event must carry the D33 fields so Phase 10.5
        # measurement can compute filter-rescue rate post-hoc.
        from aedos.audit.log import query_events
        lc = LocalContext(
            predicate="holds_role",
            slot_position="subject",
            expected_entity_types=["Q5"],
        )
        live_adapter.resolve_entity("Obama", lc)
        events = query_events(
            live_adapter._db, event_type="kb_live_resolve", limit=10
        )
        assert len(events) >= 1
        data = events[0]["event_data"]
        assert "pre_filter_count" in data
        assert "filter_eliminated_count" in data
        assert "expected_entity_types" in data
        assert data["expected_entity_types"] == ["Q5"]
        assert "sparql_fallback_used" in data
        # The filter must actually be doing work here (eliminating non-humans).
        assert data["pre_filter_count"] > data["candidate_count"], (
            f"Filter should have eliminated at least one candidate; "
            f"pre={data['pre_filter_count']} post={data['candidate_count']}"
        )

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "D47 (2026-05-23): Q76's only English altLabel is 'POTUS 44'; "
            "the bare string 'Obama' is not registered as a label/altLabel "
            "for Q76 in Wikidata. Resolution of the bare string to the "
            "canonical Q76 requires contextual disambiguation upstream of "
            "the KB query (v0.16 D47 work item). The xfail pins this "
            "Wikidata data-model limit; an xpass means Wikidata edits have "
            "added 'Obama' as a Q76 altLabel and Aedos can now reach it."
        ),
    )
    def test_obama_short_query_does_not_yield_canonical_q76(self, live_adapter):
        # Direct empirical pin of the D47 finding. With type filter [Q5],
        # the bare 'Obama' query returns *some* humans (other people whose
        # primary label IS 'Obama'), but not Q76 — its label is 'Barack
        # Obama' and its only English altLabel is 'POTUS 44'.
        lc = LocalContext(
            predicate="holds_role",
            slot_position="subject",
            expected_entity_types=["Q5"],
        )
        candidates = live_adapter.resolve_entity("Obama", lc)
        ids = [c.kb_identifier for c in candidates]
        assert "Q76" in ids, (
            f"D47 finding: 'Obama' alone does not reach Q76; got {ids}. "
            f"If this xpasses, Wikidata may have added 'Obama' as a Q76 "
            f"altLabel — update the D47 documentation."
        )

    @pytest.mark.xfail(
        strict=False,
        reason=(
            "D47 (2026-05-23): Q49112 (Williams College, MA) is not "
            "reachable from the bare string 'Williams College' via either "
            "wbsearchentities (returns 13 unrelated entities, Q49112 not "
            "among them) or SPARQL label/altLabel match. Resolution "
            "requires contextual disambiguation upstream (v0.16 D47)."
        ),
    )
    def test_williams_college_short_query_does_not_yield_canonical_q49112(
        self, live_adapter
    ):
        lc = LocalContext(
            predicate="located_in",
            slot_position="subject",
            expected_entity_types=["Q3918", "Q38723", "Q1188663"],
        )
        candidates = live_adapter.resolve_entity("Williams College", lc)
        ids = [c.kb_identifier for c in candidates]
        assert "Q49112" in ids, (
            f"D47 finding: 'Williams College' alone does not reach Q49112; "
            f"got {ids}."
        )

    def test_barack_obama_full_name_reaches_q76(self, live_adapter):
        """Phase G D33 + D47 positive case: 'Barack Obama' (the canonical
        full label) DOES reach Q76 with the type filter. This is what
        Phase 10.5 corpora using unambiguous references will see — the
        filter works correctly when the canonical entity is in the pool.
        Documents the working path so future readers see both sides."""
        lc = LocalContext(
            predicate="holds_role",
            slot_position="subject",
            expected_entity_types=["Q5"],
        )
        candidates = live_adapter.resolve_entity("Barack Obama", lc)
        ids = [c.kb_identifier for c in candidates]
        assert "Q76" in ids, (
            f"With the canonical full name 'Barack Obama' and type filter "
            f"[Q5], Q76 must be in the candidates; got {ids}"
        )
        # Q76 should be the top candidate (highest score).
        assert candidates[0].kb_identifier == "Q76", (
            f"Q76 should be the top-ranked candidate; got "
            f"{candidates[0].kb_identifier}"
        )


class TestLiveFetchP31:
    """Live tests for `_fetch_p31_for_candidates` (Phase G D33 step 2).

    Verifies the wbgetentities helper reaches the real API and returns
    P31 values for known canonical entities."""

    def test_fetch_p31_q76_includes_q5(self, live_adapter):
        """Q76 (Barack Obama) is an instance_of Q5 (human) on Wikidata.
        The P31 fetch should return a list containing Q5."""
        p31, err = live_adapter._fetch_p31_for_candidates(["Q76"])
        assert err is None
        assert "Q5" in p31["Q76"], (
            f"Q76's P31 should include Q5 (human); got {p31['Q76']}"
        )

    def test_fetch_p31_batch_multiple_entities(self, live_adapter):
        """Batched fetch with multiple Q-ids: each gets its own P31 list."""
        p31, err = live_adapter._fetch_p31_for_candidates(["Q76", "Q49112"])
        assert err is None
        assert "Q5" in p31["Q76"]
        # Q49112 (Williams College) carries Q1188663 (private not-for-profit
        # educational institution) among its P31 values on 2026-05-23. The
        # assertion is on the load-bearing relation (educational institution
        # of some flavor), not on a specific Q-id that Wikidata may revise.
        assert "Q1188663" in p31["Q49112"], (
            f"Q49112's P31 should include Q1188663 (private not-for-profit "
            f"educational institution); got {p31['Q49112']}"
        )


class TestLiveLookup:
    """Live tests for `_live_lookup` (F2 commit #2).

    Per D33/D34 discipline: tests verify the implementation reaches
    expected load-bearing data (specific entity values that anchor
    verifier-relevant semantics) without over-specifying fixture-frozen
    state (no exact statement counts, no exact qualifier values that
    aren't load-bearing, no preferred-vs-normal-rank assumptions).
    """

    def test_obama_p39_includes_president(self, live_adapter):
        """Q76 (Barack Obama) holds_role (P39) — the position-held
        predicate should return multiple positions including Q11696
        (President of the United States). The Phase E `der_disambiguation_*`
        cases exercise this lookup; this test confirms the live path
        returns the canonical role assertion."""
        statements = live_adapter.lookup_statements("Q76", "P39")
        assert len(statements) > 0
        values = [s.value for s in statements]
        assert "Q11696" in values, (
            f"Q11696 (US President) should be among Q76's P39 statements; "
            f"got {values[:10]}"
        )

    def test_obama_p39_president_has_temporal_qualifiers(self, live_adapter):
        """The President-of-US position (Q11696) has P580/P582 qualifiers
        (2009-01-20 / 2017-01-20). The walker's scope check (`KBVerifier._scope_compatible`)
        depends on these — this is a load-bearing protocol assertion."""
        statements = live_adapter.lookup_statements("Q76", "P39")
        president_stmts = [s for s in statements if s.value == "Q11696"]
        assert len(president_stmts) >= 1
        # At least one of the President statements has temporal qualifiers.
        # Do NOT assert exact dates — Wikidata is the source of truth and the
        # implementation just reflects it. The presence of P580/P582 is the
        # load-bearing signal.
        any_with_scope = any(
            "P580" in s.qualifiers and "P582" in s.qualifiers
            for s in president_stmts
        )
        assert any_with_scope, (
            "At least one Q11696 (US President) statement should carry "
            "P580/P582 temporal qualifiers"
        )

    def test_us_capital_inverse_direction_returns_dc(self, live_adapter):
        """Q30 (United States) capital (P36) → Q61 (Washington, D.C.).
        This is the inverse-predicate direction (D19, fixup-3): for
        `capital_of`, the KB statement is keyed on the *country*, not the
        city. KBVerifier swaps the lookup direction via slot_to_qualifier;
        this test exercises the swapped path's live read."""
        statements = live_adapter.lookup_statements("Q30", "P36")
        assert len(statements) > 0
        values = [s.value for s in statements]
        assert "Q61" in values, (
            f"Q61 (Washington, D.C.) should be Q30's P36 (capital); "
            f"got {values}"
        )

    def test_lookup_no_such_property_returns_empty(self, live_adapter):
        """Q76 (Obama) has no statements for an entirely-unrelated property —
        e.g., P9999 (which is not a real Wikidata property). Should return
        [] (architecture §9.4: no grounding found → empty, not error)."""
        statements = live_adapter.lookup_statements("Q76", "P99999999")
        assert statements == []

    def test_lookup_emits_audit_event(self, live_adapter):
        """Wiring verification: a live lookup produces a `kb_live_lookup`
        audit event with statement_count and duration_ms — F4's end-to-end
        trace inspection depends on this."""
        from aedos.audit.log import query_events
        live_adapter.lookup_statements("Q30", "P36")
        events = query_events(
            live_adapter._db, event_type="kb_live_lookup", limit=10
        )
        assert len(events) >= 1
        assert events[0]["event_subject"] == "Q30:P36"
        assert "statement_count" in events[0]["event_data"]
        assert events[0]["event_data"]["statement_count"] >= 1


class TestLiveSubsumption:
    """Live tests for `_live_subsumption` (F2 commit #3).

    Per D33/D34 discipline: tests verify the verdict shape and
    direction semantics, not specific establishing-property values
    (those are observability-only and may vary with Wikidata edits).
    """

    def test_obama_is_a_human_a_subsumed_by_b(self, live_adapter):
        """Q76 (Barack Obama) is_a Q5 (human) via P31. The directional
        result should be `a_subsumed_by_b` (Obama is a human, not the
        reverse). This is the most basic subsumption shape."""
        result = live_adapter.subsumption("Q76", "Q5", "is_a")
        assert result.verdict == "a_subsumed_by_b"
        # Establishing property should be P31 (instance of) or P279.
        # Don't assert which — observability-only signal.
        assert result.establishing_property in ("P31", "P279", None)
        # traversal_chain should be populated for non-unrelated verdicts
        assert result.traversal_chain == ["Q76", "Q5"]

    def test_nyc_part_of_ny_state_a_subsumed_by_b(self, live_adapter):
        """Q60 (New York City) is part_of Q1384 (New York state) via P131
        (located in administrative entity). Transitive part_of chains
        are the architecture §3.4 example case ("Asa lives_in
        Williamstown ... part_of Massachusetts")."""
        result = live_adapter.subsumption("Q60", "Q1384", "part_of")
        assert result.verdict == "a_subsumed_by_b"
        assert result.establishing_property in ("P131", "P361", None)

    def test_unrelated_pair_returns_unrelated(self, live_adapter):
        """Q76 (Obama) is_a Q42 (Douglas Adams)? Both are humans but
        neither is_a the other. The verdict should be `unrelated` (no
        path in either direction)."""
        result = live_adapter.subsumption("Q76", "Q42", "is_a")
        assert result.verdict == "unrelated"
        # No establishing property or chain when unrelated
        assert result.establishing_property is None
        assert result.traversal_chain == []

    def test_subsumption_emits_audit_event(self, live_adapter):
        """Wiring verification: live subsumption produces a
        `kb_live_subsumption` audit event with verdict and duration_ms."""
        from aedos.audit.log import query_events
        live_adapter.subsumption("Q76", "Q5", "is_a")
        events = query_events(
            live_adapter._db, event_type="kb_live_subsumption", limit=10
        )
        assert len(events) >= 1
        assert events[0]["event_subject"] == "Q76<>Q5:is_a"
        assert events[0]["event_data"]["verdict"] == "a_subsumed_by_b"
        assert "duration_ms" in events[0]["event_data"]
