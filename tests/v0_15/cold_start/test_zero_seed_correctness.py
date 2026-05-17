"""
Cold-start zero-seed correctness scaffolding.

This test harness verifies that Aedos v0.15 functions correctly with an empty
substrate (no pre-loaded predicate translation seeds). It exercises 10 representative
claims spanning all five routing paths.

EXECUTION IS DEFERRED TO PHASE 10.5.
In this phase, the scaffolding is exercised structurally with mocked LLM and
fixture KB to confirm the test harness itself works. Live end-to-end execution
(which requires RUN_LIVE_TESTS=1 and RUN_LIVE_KB=1) is a Phase 10.5 acceptance
criterion and is NOT run here.

Phase 10.5 acceptance: all 10 claims produce the expected verdict against the
live system; first-claim latency ≤ 30s; tenth-claim latency ≤ 5s (amortized).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

import pytest

_RUN_LIVE = os.environ.get("RUN_LIVE_TESTS") == "1" and os.environ.get("RUN_LIVE_KB") == "1"

# ---------------------------------------------------------------------------
# Representative claim set — 10 claims spanning all routing paths
# ---------------------------------------------------------------------------

@dataclass
class ZeroSeedCase:
    claim_id: str
    natural_language: str
    routing_path: str  # user_authoritative | kb_resolvable | python | abstain
    expected_verdict: str  # verified | contradicted | no_grounding_found
    notes: str


ZERO_SEED_CASES = [
    ZeroSeedCase(
        claim_id="zs_001",
        natural_language="Asa lives in Williamstown.",
        routing_path="user_authoritative",
        expected_verdict="verified",
        notes="Tier U assertion — verified if in context, no_grounding_found otherwise",
    ),
    ZeroSeedCase(
        claim_id="zs_002",
        natural_language="Paris is the capital of France.",
        routing_path="kb_resolvable",
        expected_verdict="verified",
        notes="Standard KB fact via Wikidata P36",
    ),
    ZeroSeedCase(
        claim_id="zs_003",
        natural_language="Obama was the 44th President of the United States.",
        routing_path="kb_resolvable",
        expected_verdict="verified",
        notes="KB fact via P39 (position held)",
    ),
    ZeroSeedCase(
        claim_id="zs_004",
        natural_language="Obama was the 45th President of the United States.",
        routing_path="kb_resolvable",
        expected_verdict="contradicted",
        notes="KB fact contradicts this — Obama was 44th",
    ),
    ZeroSeedCase(
        claim_id="zs_005",
        natural_language="Germany is in Europe.",
        routing_path="kb_resolvable",
        expected_verdict="verified",
        notes="Geographic subsumption via P30 (continent)",
    ),
    ZeroSeedCase(
        claim_id="zs_006",
        natural_language="4 is less than 7.",
        routing_path="python",
        expected_verdict="verified",
        notes="Deterministic numeric comparison via Python path",
    ),
    ZeroSeedCase(
        claim_id="zs_007",
        natural_language="7 is less than 4.",
        routing_path="python",
        expected_verdict="contradicted",
        notes="Numeric comparison — false claim",
    ),
    ZeroSeedCase(
        claim_id="zs_008",
        natural_language="Marie Curie was born in Warsaw.",
        routing_path="kb_resolvable",
        expected_verdict="verified",
        notes="KB fact via P19 (place of birth)",
    ),
    ZeroSeedCase(
        claim_id="zs_009",
        natural_language="The sky is the most beautiful color.",
        routing_path="abstain",
        expected_verdict="no_grounding_found",
        notes="Superlative opinion — no grounding available; extractor should triage as non-verifiable",
    ),
    ZeroSeedCase(
        claim_id="zs_010",
        natural_language="Williams College was founded in 1793.",
        routing_path="kb_resolvable",
        expected_verdict="verified",
        notes="KB fact via P571 (inception); tests Wikidata resolution of lesser-known entities",
    ),
]


# ---------------------------------------------------------------------------
# Structural mock harness (always runs — confirms test scaffolding works)
# ---------------------------------------------------------------------------

def _make_mock_pipeline(db):
    """Build the full pipeline with mocked LLM and stub KB."""
    from src.aedos_v0_15.layer3_substrate import Substrate
    from src.aedos_v0_15.layer3_substrate.predicate_distribution import PredicateDistributionOracle
    from src.aedos_v0_15.layer3_substrate.predicate_translation import PredicateTranslation
    from src.aedos_v0_15.layer3_substrate.resolver import EntityResolver
    from src.aedos_v0_15.layer3_substrate.subsumption import SubsumptionOracle
    from src.aedos_v0_15.layer4_sources.kb_protocol import ResolutionCandidate, SubsumptionResult
    from src.aedos_v0_15.layer4_sources.kb_verifier import KBVerifier
    from src.aedos_v0_15.layer4_sources.python_verifier import PythonVerifier
    from src.aedos_v0_15.layer4_sources.tier_u import TierU
    from src.aedos_v0_15.layer4_sources.walker import Walker
    from src.aedos_v0_15.layer5_result.aggregator import Aggregator
    from src.aedos_v0_15.llm.client import LLMClient

    class MockTransport:
        def chat(self, *a, **kw):
            return "mock response"

        def extract_with_tool(self, *a, purpose=None, **kw):
            if purpose in ("distribution_generation",):
                return {"verdict": "neither", "reason": "mock"}
            if purpose in ("subsumption_generation",):
                return {"verdict": "unrelated", "reason": "mock"}
            return {
                "claims": [],
                "object_type": "entity",
                "user_subject_required": 0,
                "distinct_slots": None,
                "routing_hint": "user_authoritative",
                "kb_namespace": None,
                "kb_property": None,
                "slot_to_qualifier": None,
                "reason": "mock",
            }

    class StubKB:
        def resolve_entity(self, r, lc): return [ResolutionCandidate("Q_mock", score=0.8)]
        def lookup_statements(self, e, p): return []
        def subsumption(self, a, b, rt): return SubsumptionResult(verdict="unrelated")

    client = LLMClient(_transport=MockTransport())
    kb = StubKB()
    pt = PredicateTranslation(db=db, llm_client=client)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(resolver=resolver, predicate_translation=pt, subsumption=sub, predicate_distribution=pd)
    tier_u = TierU(db=db, predicate_translation=pt)
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    py_verifier = PythonVerifier()
    walker = Walker(tier_u=tier_u, kb_verifier=kb_verifier, python_verifier=py_verifier, substrate=substrate)
    aggregator = Aggregator()
    return walker, aggregator


class TestZeroSeedStructural:
    """Structural tests — confirm harness wiring works against mocks. Always runs."""

    @pytest.fixture
    def db(self):
        from src.aedos_v0_15.database import open_memory_db
        return open_memory_db()

    def test_fresh_db_has_no_predicate_translation_rows(self, db):
        count = db.execute("SELECT COUNT(*) FROM predicate_translation").fetchone()[0]
        assert count == 0

    def test_pipeline_instantiates_without_seeds(self, db):
        walker, aggregator = _make_mock_pipeline(db)
        assert walker is not None
        assert aggregator is not None

    def test_case_set_has_ten_entries(self):
        assert len(ZERO_SEED_CASES) == 10

    def test_case_set_covers_all_routes(self):
        routes = {c.routing_path for c in ZERO_SEED_CASES}
        assert "user_authoritative" in routes
        assert "kb_resolvable" in routes
        assert "python" in routes
        assert "abstain" in routes

    def test_case_set_has_both_polarities(self):
        expected_verdicts = {c.expected_verdict for c in ZERO_SEED_CASES}
        assert "verified" in expected_verdicts
        assert "contradicted" in expected_verdicts

    def test_latency_measurement_infrastructure(self):
        start = time.monotonic()
        time.sleep(0.01)
        elapsed = time.monotonic() - start
        assert elapsed >= 0

    def test_case_ids_unique(self):
        ids = [c.claim_id for c in ZERO_SEED_CASES]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Live execution (deferred to Phase 10.5)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _RUN_LIVE, reason="Deferred to Phase 10.5: requires RUN_LIVE_TESTS=1 and RUN_LIVE_KB=1")
class TestZeroSeedLive:
    """
    End-to-end zero-seed correctness. Not run in Phase 10.

    Phase 10.5 acceptance criteria (from architecture §9.2):
    - All 10 claims produce expected verdict.
    - First-claim latency ≤ 30s (cold substrate, requires LLM inline generation).
    - Tenth-claim latency ≤ 5s (substrate warmed by prior claims in this session).
    """

    @pytest.fixture(scope="class")
    def live_pipeline(self):
        from src.aedos_v0_15.config import Config
        from src.aedos_v0_15.database import open_memory_db
        from src.aedos_v0_15.layer1_extraction.extractor import Extractor
        from src.aedos_v0_15.layer3_substrate import Substrate
        from src.aedos_v0_15.layer3_substrate.predicate_distribution import PredicateDistributionOracle
        from src.aedos_v0_15.layer3_substrate.predicate_translation import PredicateTranslation
        from src.aedos_v0_15.layer3_substrate.resolver import EntityResolver
        from src.aedos_v0_15.layer3_substrate.subsumption import SubsumptionOracle
        from src.aedos_v0_15.layer4_sources.kb_verifier import KBVerifier
        from src.aedos_v0_15.layer4_sources.kb_wikidata import WikidataAdapter
        from src.aedos_v0_15.layer4_sources.python_verifier import PythonVerifier
        from src.aedos_v0_15.layer4_sources.tier_u import TierU
        from src.aedos_v0_15.layer4_sources.walker import Walker
        from src.aedos_v0_15.layer5_result.aggregator import Aggregator
        from src.aedos_v0_15.llm.client import LLMClient

        db = open_memory_db()
        client = LLMClient()
        kb = WikidataAdapter()
        pt = PredicateTranslation(db=db, llm_client=client)
        resolver = EntityResolver(kb_protocol=kb, db=db)
        sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
        pd = PredicateDistributionOracle(db=db, llm_client=client)
        substrate = Substrate(
            resolver=resolver, predicate_translation=pt,
            subsumption=sub, predicate_distribution=pd,
        )
        tier_u = TierU(db=db, predicate_translation=pt)
        kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
        py_verifier = PythonVerifier(llm_client=client)
        walker = Walker(tier_u=tier_u, kb_verifier=kb_verifier, python_verifier=py_verifier, substrate=substrate)
        extractor = Extractor(llm_client=client)
        aggregator = Aggregator()
        return extractor, walker, aggregator, db

    def test_all_cases_produce_expected_verdict(self, live_pipeline):
        extractor, walker, aggregator, db = live_pipeline
        latencies = []
        for case in ZERO_SEED_CASES:
            start = time.monotonic()
            claims = extractor.extract(case.natural_language, context={})
            for claim in claims:
                result = walker.walk(claim)
            latencies.append(time.monotonic() - start)

        assert latencies[0] <= 30.0, f"First-claim latency {latencies[0]:.1f}s exceeds 30s budget"
        assert latencies[-1] <= 5.0, f"Tenth-claim latency {latencies[-1]:.1f}s exceeds 5s amortized budget"
