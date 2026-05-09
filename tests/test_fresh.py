"""Fresh dispatcher tests (v0.14 Phase 7e).

Pins the routing-method → verifier mapping and the v1 outcome →
8-state verification_status mapping. Verifies that the dispatcher:

  * Routes python and python_with_canonical_constants to the
    code-generation pipeline (single-shot vs cross-check).
  * Routes retrieval to the RetrievalVerifier.
  * Maps each v1 verifier outcome onto the right 8-state value.
  * Writes verified/contradicted verdicts to Tier W with the right
    stability class.
  * Skips Tier W writes for unverifiable / routing_anomaly /
    retrieval_failed / pending statuses (the right write/no-write
    boundary per Ambiguity #6).
  * Handles user_authoritative as routing_anomaly.
  * Handles unverifiable as terminal unverifiable_in_principle.
  * Returns the right LookupOutcome (MATCH / CONTRADICTION / MISS)
    for each status.

Verifier internals are mocked — the test exercises the dispatcher's
mapping logic, not the verifiers' correctness (those are tested in
their own test files in tests/).

A single integration smoke test runs a real
``verify_via_code_generation`` call against an obviously-true
claim to confirm v1 verifiers are invocable from v2's stack
(duck-typing across the layer boundary). Gated behind
``RUN_API_TESTS=1`` because it makes a real LLM call.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.fact_store import FactStore
from src.layer1_extraction.pattern_registry import (
    PatternRegistry,
)
from src.layer4_lookup import fresh, tier_w
from src.layer4_lookup.types import LookupOutcome


_PATTERNS_PATH = (
    Path(__file__).parent.parent
    / "src" / "layer1_extraction" / "patterns.yaml"
)


@pytest.fixture(scope="module")
def registry() -> PatternRegistry:
    return PatternRegistry.from_yaml(_PATTERNS_PATH)


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "fresh.db")
    yield s
    s.close()


class _StubLLM:
    """Minimal LLM stub. The dispatcher uses LLM only via the
    underlying verifiers, which we mock at the verify_* call site.
    The stub exists so the LLM-presence check passes."""

    pass


# ============================================================================
# Stability classifier canned responses (Phase 8f)
# ============================================================================
#
# Phase 8f wires classify_for_cache into the retrieval cache-write
# path. Tests that exercise Tier W writes need to mock this call
# unless they want the real classifier to fire.


def _canned_world_fact_decade_stable():
    """Canned CombinedDecision: world_fact + decade_stable, 1y TTL.
    Used by tests that just want a 'cacheable' classification result."""
    from src.cache.classify_combined import CombinedDecision
    from src.cache.scoping_classifier import ScopingDecision
    from src.cache.stability_classifier import (
        STABILITY_TTL_SECONDS, StabilityDecision,
    )
    return CombinedDecision(
        scoping=ScopingDecision(
            scope="world_fact", reason="test fixture",
        ),
        stability=StabilityDecision(
            stability_class="decade_stable",
            reason="test fixture",
            ttl_seconds=STABILITY_TTL_SECONDS["decade_stable"],
        ),
    )


def _canned_user_specific():
    """Canned CombinedDecision: user_specific scope (not cacheable)."""
    from src.cache.classify_combined import CombinedDecision
    from src.cache.scoping_classifier import ScopingDecision
    return CombinedDecision(
        scoping=ScopingDecision(
            scope="user_specific", reason="test fixture",
        ),
        stability=None,
    )


def _canned_session_specific():
    """Canned CombinedDecision: session_specific scope (not cacheable)."""
    from src.cache.classify_combined import CombinedDecision
    from src.cache.scoping_classifier import ScopingDecision
    return CombinedDecision(
        scoping=ScopingDecision(
            scope="session_specific", reason="test fixture",
        ),
        stability=None,
    )


def _canned_world_fact_volatile():
    """Canned CombinedDecision: world_fact but volatile (don't cache)."""
    from src.cache.classify_combined import CombinedDecision
    from src.cache.scoping_classifier import ScopingDecision
    from src.cache.stability_classifier import (
        STABILITY_TTL_SECONDS, StabilityDecision,
    )
    return CombinedDecision(
        scoping=ScopingDecision(
            scope="world_fact", reason="test fixture",
        ),
        stability=StabilityDecision(
            stability_class="volatile",
            reason="test fixture",
            ttl_seconds=STABILITY_TTL_SECONDS["volatile"],  # 0
        ),
    )


def _canned_world_fact_with_class(stability_class: str):
    """Canned CombinedDecision: world_fact + arbitrary stability_class."""
    from src.cache.classify_combined import CombinedDecision
    from src.cache.scoping_classifier import ScopingDecision
    from src.cache.stability_classifier import (
        STABILITY_TTL_SECONDS, StabilityDecision,
    )
    return CombinedDecision(
        scoping=ScopingDecision(
            scope="world_fact", reason="test fixture",
        ),
        stability=StabilityDecision(
            stability_class=stability_class,
            reason="test fixture",
            ttl_seconds=STABILITY_TTL_SECONDS[stability_class],
        ),
    )


# ============================================================================
# Method routing
# ============================================================================


class TestMethodRouting:
    """Each routing_method should reach its dedicated dispatch path."""

    def test_unverifiable_routing_terminal(self, store, registry):
        claim = {
            "pattern": "preference", "predicate": "loves",
            "polarity": 1,
            "slots": {"agent": "user", "object": "olives"},
            "source_text": "user loves olives",
        }
        decision = fresh.dispatch(
            claim,
            routing_method="unverifiable",
            store=store, registry=registry,
            llm=_StubLLM(), source_turn_id=None,
        )
        assert decision.served_from_tier == "fresh"
        assert decision.verification_status == "unverifiable_in_principle"
        assert decision.outcome is LookupOutcome.MISS
        assert decision.routing_method == "unverifiable"

    def test_user_authoritative_routing_flagged_as_anomaly(
        self, store, registry,
    ):
        claim = {
            "pattern": "preference", "predicate": "likes",
            "polarity": 1,
            "slots": {"agent": "user", "object": "olives"},
            "source_text": "user likes olives",
        }
        decision = fresh.dispatch(
            claim,
            routing_method="user_authoritative",
            store=store, registry=registry,
            llm=_StubLLM(), source_turn_id=None,
        )
        # Reaching fresh with user_authoritative is a routing anomaly:
        # Tier U should have resolved it.
        assert decision.verification_status == "routing_anomaly"
        assert any(
            "user_authoritative reached fresh" in note
            for note in decision.notes
        )

    def test_unknown_method_returns_pending(self, store, registry):
        claim = {
            "pattern": "preference", "predicate": "likes",
            "polarity": 1,
            "slots": {"agent": "user", "object": "olives"},
            "source_text": "user likes olives",
        }
        decision = fresh.dispatch(
            claim,
            routing_method="made_up_method",
            store=store, registry=registry,
            llm=_StubLLM(), source_turn_id=None,
        )
        assert decision.verification_status == "unverifiable_pending_implementation"

    def test_no_llm_returns_pending_for_python(self, store, registry):
        claim = {
            "pattern": "quantitative", "predicate": "has_count",
            "polarity": 1,
            "slots": {"subject": "alphabet", "value": 26},
            "source_text": "the English alphabet has 26 letters",
        }
        decision = fresh.dispatch(
            claim,
            routing_method="python",
            store=store, registry=registry,
            llm=None, source_turn_id=None,
        )
        assert decision.verification_status == "unverifiable_pending_implementation"

    def test_no_llm_returns_pending_for_retrieval(self, store, registry):
        claim = {
            "pattern": "spatial_temporal", "predicate": "located_in",
            "polarity": 1,
            "slots": {"entity": "Tokyo", "location": "Japan"},
            "source_text": "Tokyo is in Japan",
        }
        decision = fresh.dispatch(
            claim,
            routing_method="retrieval",
            store=store, registry=registry,
            llm=None, source_turn_id=None,
        )
        assert decision.verification_status == "unverifiable_pending_implementation"


# ============================================================================
# Python status mapping
# ============================================================================


class TestPythonStatusMapping:
    """v1 CodeGenVerificationResult.status → 8-state mapping."""

    @pytest.mark.parametrize("v1_status,v2_status", [
        ("verified", "verified"),
        ("contradicted", "contradicted"),
        ("code_execution_failed", "unverifiable_pending_implementation"),
        ("comparison_error", "unverifiable_pending_implementation"),
        ("canonical_constants_disagreement", "unverifiable_pending_implementation"),
    ])
    def test_python_status_mapped(
        self, store, registry, v1_status, v2_status,
    ):
        claim = {
            "pattern": "quantitative", "predicate": "has_count",
            "polarity": 1,
            "slots": {"subject": "test", "value": 7},
            "source_text": "test has 7",
        }
        from src.verifiers.code_generation.pipeline import (
            CodeGenVerificationResult,
        )
        canned = CodeGenVerificationResult(
            status=v1_status,
            actual_value=7 if v1_status == "verified" else 8,
            explanation="mocked",
            trace={"mock": True},
        )
        with patch(
            "src.layer4_lookup.fresh.verify_via_code_generation",
            return_value=canned,
            create=True,
        ) as mocked:
            # The import lives inside _dispatch_python; we patch it
            # by re-binding at the module path the import lands on.
            # Use a direct monkey-patch via the v1 module instead.
            pass
        # Simpler: patch the v1 module's function directly.
        with patch(
            "src.verifiers.code_generation.pipeline.verify_via_code_generation",
            return_value=canned,
        ):
            decision = fresh.dispatch(
                claim,
                routing_method="python",
                store=store, registry=registry,
                llm=_StubLLM(), source_turn_id=None,
            )
        assert decision.verification_status == v2_status
        if v2_status == "verified":
            assert decision.outcome is LookupOutcome.MATCH
        elif v2_status == "contradicted":
            assert decision.outcome is LookupOutcome.CONTRADICTION
        else:
            assert decision.outcome is LookupOutcome.MISS

    def test_python_with_canonical_constants_uses_cross_check(
        self, store, registry,
    ):
        """Verifies that the cross-check method actually invokes
        verify_with_cross_check (not just the single-shot path)."""
        claim = {
            "pattern": "quantitative", "predicate": "has_count",
            "polarity": 1,
            "slots": {"subject": "US states", "value": 50},
            "source_text": "there are 50 US states",
        }
        from src.verifiers.code_generation.pipeline import (
            CodeGenVerificationResult,
        )
        canned = CodeGenVerificationResult(
            status="verified",
            actual_value=50,
            explanation="mocked cross-check",
            trace={"cross_check": True},
        )
        with patch(
            "src.verifiers.code_generation.pipeline."
            "CodeGenerationVerifier.verify_with_cross_check",
            return_value=canned,
        ) as mocked:
            decision = fresh.dispatch(
                claim,
                routing_method="python_with_canonical_constants",
                store=store, registry=registry,
                llm=_StubLLM(), source_turn_id=None,
            )
        assert mocked.called
        assert decision.verification_status == "verified"
        assert decision.routing_method == "python_with_canonical_constants"


# ============================================================================
# Retrieval status mapping
# ============================================================================


class TestRetrievalStatusMapping:
    """v1 RetrievalResult outcome+error_flag → 8-state mapping.

    The retrieval_inconclusive vs retrieval_failed split is the
    load-bearing case.
    """

    def _canned_retrieval(
        self, *, outcome_value: str, error_flag=None,
    ):
        from src.verifiers.retrieval_verifier import RetrievalResult
        from src.verifiers.types import VerificationOutcome
        v1_outcome = VerificationOutcome(outcome_value)
        return RetrievalResult(
            outcome=v1_outcome,
            error_flag=error_flag,
            explanation="mocked",
        )

    @pytest.mark.parametrize("v1_outcome,error_flag,v2_status", [
        ("verified", None, "verified"),
        ("contradicted", None, "contradicted"),
        ("inconclusive", None, "retrieval_inconclusive"),
        ("inconclusive", "retrieval_error", "retrieval_failed"),
        ("inconclusive", "no_results", "retrieval_failed"),
        ("inconclusive", "judge_parse_error", "retrieval_failed"),
        ("inconclusive", "judge_error", "retrieval_failed"),
        ("inconclusive", "no_query_constructible", "retrieval_failed"),
    ])
    def test_retrieval_status_mapped(
        self, store, registry, v1_outcome, error_flag, v2_status,
    ):
        claim = {
            "pattern": "spatial_temporal", "predicate": "located_in",
            "polarity": 1,
            "slots": {"entity": "Tokyo", "location": "Japan"},
            "source_text": "Tokyo is in Japan",
        }
        canned = self._canned_retrieval(
            outcome_value=v1_outcome, error_flag=error_flag,
        )
        with patch(
            "src.verifiers.retrieval_verifier.RetrievalVerifier.verify",
            return_value=canned,
        ):
            decision = fresh.dispatch(
                claim,
                routing_method="retrieval",
                store=store, registry=registry,
                llm=_StubLLM(), source_turn_id=None,
            )
        assert decision.verification_status == v2_status


# ============================================================================
# Tier W write boundary
# ============================================================================


class TestTierWWriteBoundary:
    """The dispatcher writes verifier output to Tier W for verdicts
    that constitute usable evidence. retrieval_failed / unverifiable
    / pending statuses do NOT write to Tier W."""

    def _retrieval_with(
        self, store, registry, *, outcome_value: str, error_flag=None,
    ):
        claim = {
            "pattern": "spatial_temporal", "predicate": "located_in",
            "polarity": 1,
            "slots": {"entity": "Berlin", "location": "Germany"},
            "source_text": "Berlin is in Germany",
        }
        from src.verifiers.retrieval_verifier import RetrievalResult
        from src.verifiers.types import VerificationOutcome
        canned = RetrievalResult(
            outcome=VerificationOutcome(outcome_value),
            error_flag=error_flag,
            explanation="mocked",
        )
        before = store._conn.execute(
            "SELECT COUNT(*) FROM verification_cache").fetchone()[0]
        with patch(
            "src.verifiers.retrieval_verifier.RetrievalVerifier.verify",
            return_value=canned,
        ), patch(
            # Phase 8f: mock classify_for_cache so the test doesn't
            # need a live LLM. Returns world_fact + decade_stable
            # (a non-volatile cacheable verdict).
            "src.layer4_lookup.fresh.classify_for_cache",
            return_value=_canned_world_fact_decade_stable(),
        ):
            decision = fresh.dispatch(
                claim,
                routing_method="retrieval",
                store=store, registry=registry,
                llm=_StubLLM(), source_turn_id=None,
            )
        after = store._conn.execute(
            "SELECT COUNT(*) FROM verification_cache").fetchone()[0]
        return decision, after - before

    def test_verified_writes_to_tier_w(self, store, registry):
        decision, delta = self._retrieval_with(
            store, registry, outcome_value="verified",
        )
        assert decision.verification_status == "verified"
        assert delta == 1, "verified verdict should be cached"

    def test_contradicted_writes_to_tier_w(self, store, registry):
        decision, delta = self._retrieval_with(
            store, registry, outcome_value="contradicted",
        )
        assert decision.verification_status == "contradicted"
        assert delta == 1

    def test_retrieval_inconclusive_does_not_write_to_tier_w(
        self, store, registry,
    ):
        """v0.14.1: only verified/contradicted carry actionable
        knowledge worth caching. ``retrieval_inconclusive`` is a
        non-verdict — caching it suppresses retry without contributing
        information. The dispatch still returns the inconclusive
        decision; the cache table just doesn't grow."""
        decision, delta = self._retrieval_with(
            store, registry, outcome_value="inconclusive",
        )
        assert decision.verification_status == "retrieval_inconclusive"
        assert delta == 0

    def test_retrieval_failed_does_not_write_to_tier_w(
        self, store, registry,
    ):
        """retrieval_failed means the verifier broke — no signal
        to cache."""
        decision, delta = self._retrieval_with(
            store, registry, outcome_value="inconclusive",
            error_flag="retrieval_error",
        )
        assert decision.verification_status == "retrieval_failed"
        assert delta == 0

    def test_unverifiable_does_not_write_to_tier_w(self, store, registry):
        before = store._conn.execute(
            "SELECT COUNT(*) FROM verification_cache").fetchone()[0]
        claim = {
            "pattern": "preference", "predicate": "loves",
            "polarity": 1,
            "slots": {"agent": "user", "object": "olives"},
            "source_text": "user loves olives",
        }
        fresh.dispatch(
            claim, routing_method="unverifiable",
            store=store, registry=registry,
            llm=_StubLLM(), source_turn_id=None,
        )
        after = store._conn.execute(
            "SELECT COUNT(*) FROM verification_cache").fetchone()[0]
        assert after == before


# ============================================================================
# Pipeline event
# ============================================================================


def test_fresh_dispatch_event_fires(store, registry):
    store.insert_turn("user", "anything")
    turn_id = 1
    fresh.dispatch(
        {
            "pattern": "preference", "predicate": "loves",
            "polarity": 1,
            "slots": {"agent": "user", "object": "olives"},
            "source_text": "user loves olives",
        },
        routing_method="unverifiable",
        store=store, registry=registry,
        llm=_StubLLM(), source_turn_id=turn_id,
    )
    events = store.get_pipeline_events(turn_id)
    stages = [e["stage"] for e in events]
    assert "fresh_dispatch" in stages


# ============================================================================
# Stability behavior — Phase 8f: classifier-driven for retrieval
# ============================================================================


class TestStabilityClassifier:
    """Python verdicts → immutable (no classifier consulted).
    Retrieval verdicts → classify_for_cache decides scope + stability.
    """

    def test_python_verdict_immutable_no_expiry(self, store, registry):
        """Python path doesn't consult the classifier — math/structural
        facts are immutable by definition."""
        claim = {
            "pattern": "quantitative", "predicate": "has_count",
            "polarity": 1,
            "slots": {"subject": "alphabet", "value": 26},
            "source_text": "26 letters",
        }
        from src.verifiers.code_generation.pipeline import (
            CodeGenVerificationResult,
        )
        canned = CodeGenVerificationResult(
            status="verified", actual_value=26,
            explanation="ok", trace={},
        )
        with patch(
            "src.verifiers.code_generation.pipeline.verify_via_code_generation",
            return_value=canned,
        ):
            fresh.dispatch(
                claim, routing_method="python",
                store=store, registry=registry,
                llm=_StubLLM(), source_turn_id=None,
            )
        row = store._conn.execute(
            "SELECT stability_class, expires_at "
            "FROM verification_cache LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["stability_class"] == "immutable"
        assert row["expires_at"] is None

    @pytest.mark.parametrize("stability_class", [
        "immutable", "decade_stable", "years_stable",
        "months_stable", "days_stable",
    ])
    def test_retrieval_uses_classifier_stability_class(
        self, store, registry, stability_class,
    ):
        """Each non-volatile stability class flows through to the
        cached row's stability_class column and TTL."""
        claim = {
            "pattern": "spatial_temporal", "predicate": "located_in",
            "polarity": 1,
            "slots": {"entity": "Tokyo", "location": "Japan"},
            "source_text": "Tokyo is in Japan",
        }
        from src.verifiers.retrieval_verifier import RetrievalResult
        from src.verifiers.types import VerificationOutcome
        canned_retrieval = RetrievalResult(
            outcome=VerificationOutcome.VERIFIED,
            explanation="ok",
        )
        canned_class = _canned_world_fact_with_class(stability_class)
        with patch(
            "src.verifiers.retrieval_verifier.RetrievalVerifier.verify",
            return_value=canned_retrieval,
        ), patch(
            "src.layer4_lookup.fresh.classify_for_cache",
            return_value=canned_class,
        ):
            fresh.dispatch(
                claim, routing_method="retrieval",
                store=store, registry=registry,
                llm=_StubLLM(), source_turn_id=None,
            )
        row = store._conn.execute(
            "SELECT stability_class, expires_at "
            "FROM verification_cache LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["stability_class"] == stability_class
        if stability_class == "immutable":
            assert row["expires_at"] is None
        else:
            assert row["expires_at"] is not None

    def test_retrieval_volatile_skips_cache(self, store, registry):
        """volatile stability_class (ttl_seconds=0) skips the Tier W write."""
        claim = {
            "pattern": "spatial_temporal", "predicate": "located_in",
            "polarity": 1,
            "slots": {"entity": "Apple", "location": "stock_price"},
            "source_text": "AAPL closed at X today",
        }
        from src.verifiers.retrieval_verifier import RetrievalResult
        from src.verifiers.types import VerificationOutcome
        canned_retrieval = RetrievalResult(
            outcome=VerificationOutcome.VERIFIED,
            explanation="ok",
        )
        with patch(
            "src.verifiers.retrieval_verifier.RetrievalVerifier.verify",
            return_value=canned_retrieval,
        ), patch(
            "src.layer4_lookup.fresh.classify_for_cache",
            return_value=_canned_world_fact_volatile(),
        ):
            fresh.dispatch(
                claim, routing_method="retrieval",
                store=store, registry=registry,
                llm=_StubLLM(), source_turn_id=None,
            )
        rows = store._conn.execute(
            "SELECT COUNT(*) AS c FROM verification_cache"
        ).fetchone()
        assert rows["c"] == 0

    def test_retrieval_user_specific_skips_cache(self, store, registry):
        """user_specific scope skips the Tier W write — the answer
        depends on the user, so caching across users is unsafe."""
        claim = {
            "pattern": "preference", "predicate": "loves",
            "polarity": 1,
            "slots": {"agent": "user", "object": "olives"},
            "source_text": "user loves olives",
        }
        from src.verifiers.retrieval_verifier import RetrievalResult
        from src.verifiers.types import VerificationOutcome
        canned_retrieval = RetrievalResult(
            outcome=VerificationOutcome.VERIFIED,
            explanation="ok",
        )
        with patch(
            "src.verifiers.retrieval_verifier.RetrievalVerifier.verify",
            return_value=canned_retrieval,
        ), patch(
            "src.layer4_lookup.fresh.classify_for_cache",
            return_value=_canned_user_specific(),
        ):
            fresh.dispatch(
                claim, routing_method="retrieval",
                store=store, registry=registry,
                llm=_StubLLM(), source_turn_id=None,
            )
        rows = store._conn.execute(
            "SELECT COUNT(*) AS c FROM verification_cache"
        ).fetchone()
        assert rows["c"] == 0

    def test_retrieval_session_specific_skips_cache(self, store, registry):
        """session_specific scope skips the Tier W write."""
        claim = {
            "pattern": "quantitative", "predicate": "has_count",
            "polarity": 1,
            "slots": {"subject": "this sentence", "value": 5},
            "source_text": "this sentence has 5 words",
        }
        from src.verifiers.retrieval_verifier import RetrievalResult
        from src.verifiers.types import VerificationOutcome
        canned_retrieval = RetrievalResult(
            outcome=VerificationOutcome.VERIFIED,
            explanation="ok",
        )
        with patch(
            "src.verifiers.retrieval_verifier.RetrievalVerifier.verify",
            return_value=canned_retrieval,
        ), patch(
            "src.layer4_lookup.fresh.classify_for_cache",
            return_value=_canned_session_specific(),
        ):
            fresh.dispatch(
                claim, routing_method="retrieval",
                store=store, registry=registry,
                llm=_StubLLM(), source_turn_id=None,
            )
        rows = store._conn.execute(
            "SELECT COUNT(*) AS c FROM verification_cache"
        ).fetchone()
        assert rows["c"] == 0

    def test_retrieval_classifier_failure_skips_cache_no_crash(
        self, store, registry,
    ):
        """Classifier raising → no Tier W write, no crash. The dispatcher
        continues; the verdict still flows back to the caller via
        WalkerDecision, just without a cache row."""
        claim = {
            "pattern": "spatial_temporal", "predicate": "located_in",
            "polarity": 1,
            "slots": {"entity": "Tokyo", "location": "Japan"},
            "source_text": "Tokyo is in Japan",
        }
        from src.verifiers.retrieval_verifier import RetrievalResult
        from src.verifiers.types import VerificationOutcome
        canned_retrieval = RetrievalResult(
            outcome=VerificationOutcome.VERIFIED,
            explanation="ok",
        )
        with patch(
            "src.verifiers.retrieval_verifier.RetrievalVerifier.verify",
            return_value=canned_retrieval,
        ), patch(
            "src.layer4_lookup.fresh.classify_for_cache",
            side_effect=RuntimeError("simulated classifier failure"),
        ):
            decision = fresh.dispatch(
                claim, routing_method="retrieval",
                store=store, registry=registry,
                llm=_StubLLM(), source_turn_id=None,
            )
        assert decision.verification_status == "verified"
        rows = store._conn.execute(
            "SELECT COUNT(*) AS c FROM verification_cache"
        ).fetchone()
        assert rows["c"] == 0

    def test_classifier_decision_emits_event(self, store, registry):
        """A successful classification emits a cache_stability_decision
        event so the trace UI can show the scope+stability rationale."""
        store.insert_turn("user", "test")
        turn_id = 1
        claim = {
            "pattern": "spatial_temporal", "predicate": "located_in",
            "polarity": 1,
            "slots": {"entity": "Tokyo", "location": "Japan"},
            "source_text": "Tokyo is in Japan",
        }
        from src.verifiers.retrieval_verifier import RetrievalResult
        from src.verifiers.types import VerificationOutcome
        canned_retrieval = RetrievalResult(
            outcome=VerificationOutcome.VERIFIED,
            explanation="ok",
        )
        with patch(
            "src.verifiers.retrieval_verifier.RetrievalVerifier.verify",
            return_value=canned_retrieval,
        ), patch(
            "src.layer4_lookup.fresh.classify_for_cache",
            return_value=_canned_world_fact_decade_stable(),
        ):
            fresh.dispatch(
                claim, routing_method="retrieval",
                store=store, registry=registry,
                llm=_StubLLM(), source_turn_id=turn_id,
            )
        events = store.get_pipeline_events(turn_id)
        stages = [e["stage"] for e in events]
        assert "cache_stability_decision" in stages

    def test_skip_cache_emits_scoping_event(self, store, registry):
        """A skip-cache decision emits a cache_scoping_decision event
        explaining why the write was skipped."""
        store.insert_turn("user", "test")
        turn_id = 1
        claim = {
            "pattern": "preference", "predicate": "loves",
            "polarity": 1,
            "slots": {"agent": "user", "object": "olives"},
            "source_text": "user loves olives",
        }
        from src.verifiers.retrieval_verifier import RetrievalResult
        from src.verifiers.types import VerificationOutcome
        canned_retrieval = RetrievalResult(
            outcome=VerificationOutcome.VERIFIED,
            explanation="ok",
        )
        with patch(
            "src.verifiers.retrieval_verifier.RetrievalVerifier.verify",
            return_value=canned_retrieval,
        ), patch(
            "src.layer4_lookup.fresh.classify_for_cache",
            return_value=_canned_user_specific(),
        ):
            fresh.dispatch(
                claim, routing_method="retrieval",
                store=store, registry=registry,
                llm=_StubLLM(), source_turn_id=turn_id,
            )
        events = store.get_pipeline_events(turn_id)
        skip_events = [
            e for e in events
            if e["stage"] == "cache_scoping_decision"
        ]
        assert len(skip_events) >= 1
        assert any(
            e["data"].get("decision") == "skip_cache" for e in skip_events
        )


# ============================================================================
# Live integration smoke (gated; one LLM call)
# ============================================================================


@pytest.mark.skipif(
    os.environ.get("RUN_API_TESTS") != "1",
    reason="live LLM integration smoke gated behind RUN_API_TESTS=1",
)
def test_live_python_verifier_integration_smoke(
    store, registry, tmp_path,
):
    """Confirm v1 verifiers are invocable from v2's stack with a real
    LLM. One LLM call; runs an obvious-true python claim."""
    from src.llm_client import LLMClient
    llm = LLMClient()
    claim = {
        "pattern": "quantitative", "predicate": "has_count",
        "polarity": 1,
        "slots": {
            "subject": "the word strawberry",
            "value": 3,
            "property": "letter r",
        },
        "source_text": "the word strawberry has 3 r's",
    }
    decision = fresh.dispatch(
        claim, routing_method="python",
        store=store, registry=registry,
        llm=llm, source_turn_id=None,
    )
    # The verifier should produce verified or contradicted — a
    # well-defined arithmetic claim shouldn't trip code_execution
    # _failed under normal LLM behavior.
    assert decision.verification_status in ("verified", "contradicted"), (
        f"got {decision.verification_status!r}; live verifier should "
        f"produce a definitive verdict for an obvious claim"
    )
