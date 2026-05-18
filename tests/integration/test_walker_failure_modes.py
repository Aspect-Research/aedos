"""Walker integration coverage for the six failure modes (architecture 8.1).

One integration test per failure mode, walking an actual derivation chain.
Cases are sourced from tests/calibration/derivation_corpus.jsonl — the
corpus is the spec; each test docstring cites its case id. This file is the
consolidated answer to the Phase 6 acceptance criterion "the walker correctly
handles each of the six failure modes" (audit findings M6 / C2), plus the C1
polarity case walked through the full verification path.

Substrate rows the walker traverses are seeded directly, exactly as the audit's
recommended C2 fix specifies ("against fixtures + seeded substrate rows").
"""

from __future__ import annotations

import pytest

from aedos.database import open_memory_db
from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer3_substrate import Substrate
from aedos.layer3_substrate.predicate_distribution import PredicateDistributionOracle
from aedos.layer3_substrate.predicate_translation import PredicateTranslation
from aedos.layer3_substrate.resolver import EntityResolver
from aedos.layer3_substrate.subsumption import SubsumptionOracle
from aedos.layer4_sources.kb_protocol import ResolutionCandidate, Statement, SubsumptionResult
from aedos.layer4_sources.kb_verifier import KBVerifier
from aedos.layer4_sources.python_verifier import PythonVerifier
from aedos.layer4_sources.tier_u import TierU
from aedos.layer4_sources.walker import VerificationContext, Walker
from aedos.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class _Transport:
    """Default LLM transport. Returns safe defaults so an un-seeded predicate
    routes to abstain and an un-seeded distribution gates closed — every test
    seeds exactly the substrate rows its derivation needs."""

    def extract_with_tool(self, *a, purpose=None, **kw):
        if purpose == "distribution_generation":
            return {"verdict": "neither", "reason": "test default"}
        if purpose == "subsumption_generation":
            return {"verdict": "unrelated", "reason": "test default"}
        return {
            "object_type": "entity", "user_subject_required": 0, "distinct_slots": None,
            "routing_hint": "abstain", "kb_namespace": None, "kb_property": None,
            "slot_to_qualifier": None, "single_valued": 0, "reason": "test default",
        }

    def chat(self, *a, **kw):
        return ""


class MockKB:
    """KB with a reference->candidate(s) resolution map and a
    (entity, property)->statements map."""

    def __init__(self, resolutions=None, statements=None):
        self._resolutions = resolutions or {}
        self._statements = statements or {}

    def resolve_entity(self, reference, local_context):
        value = self._resolutions.get((reference, local_context.predicate))
        if value is None:
            value = self._resolutions.get(reference)
        if value is None:
            return []
        if isinstance(value, list):
            return [ResolutionCandidate(q, score=s) for q, s in value]
        return [ResolutionCandidate(value, score=0.9)]

    def lookup_statements(self, entity, predicate):
        return list(self._statements.get((entity, predicate), []))

    def subsumption(self, entity_a, entity_b, relation_type):
        return SubsumptionResult(verdict="unrelated")


def _make_walker(kb=None):
    db = open_memory_db()
    client = LLMClient(_transport=_Transport())
    kb = kb or MockKB()
    pt = PredicateTranslation(db=db, llm_client=client)
    resolver = EntityResolver(kb_protocol=kb, db=db, llm_client=client)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(resolver=resolver, predicate_translation=pt, subsumption=sub, predicate_distribution=pd)
    tier_u = TierU(db=db, predicate_translation=pt)
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    walker = Walker(tier_u=tier_u, kb_verifier=kb_verifier, python_verifier=PythonVerifier(), substrate=substrate)
    return walker, tier_u, db


def _claim(subject, predicate, object_val, polarity=1):
    return Claim(
        claim_id="c1", subject=subject, predicate=predicate, object=object_val,
        polarity=polarity, source_text="test", asserting_party="user_test",
        triage_decision=TriageDecision.VERIFY,
    )


def _ctx():
    from datetime import datetime, timezone
    return VerificationContext(
        current_time=datetime.now(timezone.utc).isoformat(), asserting_party="user_test",
    )


def _seed_pt(db, predicate, kb_property, routing_hint="kb_resolvable", kb_namespace="wikidata"):
    db.execute(
        """INSERT INTO predicate_translation
           (aedos_predicate, object_type, routing_hint, kb_namespace, kb_property, reason, created_at)
           VALUES (?, 'entity', ?, ?, ?, 'seeded test row', '2026-01-01T00:00:00')""",
        (predicate, routing_hint, kb_namespace, kb_property),
    )
    db.commit()


def _seed_sub(db, a, b, relation_type, verdict="a_subsumed_by_b"):
    db.execute(
        """INSERT INTO subsumption
           (entity_a_namespace, entity_a_identifier, entity_b_namespace, entity_b_identifier,
            relation_type, verdict, source, reason, created_at)
           VALUES ('aedos', ?, 'aedos', ?, ?, ?, 'substrate', 'seeded test row', '2026-01-01T00:00:00')""",
        (a, b, relation_type, verdict),
    )
    db.commit()


def _seed_dist(db, predicate, relation_type, verdict, polarity=1):
    db.execute(
        """INSERT INTO predicate_distribution
           (aedos_predicate, polarity, relation_type, verdict, reason, created_at)
           VALUES (?, ?, ?, ?, 'seeded test row', '2026-01-01T00:00:00')""",
        (predicate, polarity, relation_type, verdict),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Failure mode 1 — multi-hop reasoning with predicate distribution
# ---------------------------------------------------------------------------

class TestFailureModeMultiHopDistribution:
    def test_multi_hop_distribution(self):
        """der_multihop_001: Asa lives_in Williamstown + Williamstown part_of
        Massachusetts => Asa lives_in Massachusetts (lives_in distributes_up)."""
        walker, tier_u, db = _make_walker()
        tier_u.write(_claim("Asa", "lives_in", "Williamstown"))
        _seed_sub(db, "Williamstown", "Massachusetts", "part_of")
        _seed_dist(db, "lives_in", "part_of", "distributes_up")
        result = walker.walk(_claim("Asa", "lives_in", "Massachusetts"), _ctx())
        assert result.verdict == "verified"


# ---------------------------------------------------------------------------
# Failure mode 2 — cross-source: independent single-source walks (see N2)
# ---------------------------------------------------------------------------

class TestFailureModeCrossSource:
    def test_cross_source_independent_walks(self):
        """This test does NOT exercise cross-source unification (architecture
        §8.1 failure mode 2 in its full form). It verifies that walks against
        Tier U and against the KB each produce correct verdicts independently.
        Genuine cross-source unification, where a single derivation chain
        composes a Tier U premise with a KB-sourced taxonomy step, requires
        KB-sourced neighbor enumeration in the walker — see v0.16 delta D5. The
        medium-bar evaluation's `cross_source_unification` cases will fail in
        Phase 10.5 unless they pre-seed substrate `subsumption` rows for every
        taxonomy step. This is a known capability gap, not a calibration issue.
        """
        kb = MockKB(
            resolutions={"Williams College": "Q49112", "Massachusetts": "Q771"},
            statements={("Q49112", "P131"): [Statement(value="Q771", value_type="entity")]},
        )
        walker, tier_u, db = _make_walker(kb)
        _seed_pt(db, "located_in", "P131")
        tier_u.write(_claim("Asa", "lives_in", "Williamstown"))

        tier_u_result = walker.walk(_claim("Asa", "lives_in", "Williamstown"), _ctx())
        kb_result = walker.walk(_claim("Williams College", "located_in", "Massachusetts"), _ctx())

        assert tier_u_result.verdict == "verified"
        assert kb_result.verdict == "verified"
        # The two verdicts are grounded in different sources.
        assert tier_u_result.trace.source_breakdown.get("tier_u", 0) >= 1
        assert kb_result.trace.source_breakdown.get("kb", 0) >= 1


# ---------------------------------------------------------------------------
# Failure mode 3 — contextual entity disambiguation
# ---------------------------------------------------------------------------

class TestFailureModeEntityDisambiguation:
    def test_entity_disambiguation_picks_correct_candidate(self):
        """der_disambiguation_007: 'Mercury' resolves among ranked candidates;
        only the planet (Q308) has the KB statement, so a verified verdict
        proves the resolver disambiguated correctly."""
        kb = MockKB(
            resolutions={
                # planet ranked above the chemical element
                "Mercury": [("Q308", 0.92), ("Q925", 0.40)],
                "Sun": "Q525",
            },
            statements={("Q308", "P-near"): [Statement(value="Q525", value_type="entity")]},
        )
        walker, _, db = _make_walker(kb)
        _seed_pt(db, "closer_to", "P-near")
        result = walker.walk(_claim("Mercury", "closer_to", "Sun"), _ctx())
        assert result.verdict == "verified"


# ---------------------------------------------------------------------------
# Failure mode 4 — structural predicate translation
# ---------------------------------------------------------------------------

class TestFailureModePredicateTranslation:
    def test_predicate_equivalence_substitution(self):
        """der_predicate_translation_003: 'works_at' and 'employed_by' map to
        the same KB property (P108); the predicate-translation oracle links
        them, so a claim under one predicate reaches a Tier U row stored under
        the equivalent predicate."""
        walker, tier_u, db = _make_walker()
        _seed_pt(db, "works_at", "P108")
        _seed_pt(db, "employed_by", "P108")
        tier_u.write(_claim("Asa", "employed_by", "Google"))
        result = walker.walk(_claim("Asa", "works_at", "Google"), _ctx())
        assert result.verdict == "verified"


# ---------------------------------------------------------------------------
# Failure mode 5 — cross-context belief revision
# ---------------------------------------------------------------------------

class TestFailureModeBeliefRevision:
    def test_claim_contradicting_tier_u_prior(self):
        """der_revision_003: 'Asa is not a student' contradicts the Tier U
        prior 'Asa is a student' — detected via lookup (architecture 8.1)."""
        walker, tier_u, _ = _make_walker()
        tier_u.write(_claim("Asa", "holds_role", "student", polarity=1))
        result = walker.walk(_claim("Asa", "holds_role", "student", polarity=0), _ctx())
        assert result.verdict == "contradicted"


# ---------------------------------------------------------------------------
# Failure mode 6 — principled abstention
# ---------------------------------------------------------------------------

class TestFailureModePrincipledAbstention:
    def test_no_source_abstains(self):
        """der_abstain_002: a claim no source can ground abstains rather than
        manufacturing a verdict (architecture 3.1)."""
        walker, _, _ = _make_walker()
        result = walker.walk(_claim("the meaning of life", "equals", "42"), _ctx())
        assert result.verdict == "no_grounding_found"


# ---------------------------------------------------------------------------
# C1 — claim polarity through the verification path
# ---------------------------------------------------------------------------

class TestNegatedClaimThroughWalker:
    def test_negated_claim_kb_supports_positive_is_contradicted(self):
        """A negated claim whose positive form the KB supports must walk to a
        `contradicted` verdict (C1). Exercised through the walker, not by
        calling KBVerifier.verify directly. The object "POTUS" does not resolve,
        so the positive content matches the literal statement value with or
        without object resolution — isolating the polarity defect."""
        kb = MockKB(
            resolutions={"Obama": "Q76"},
            statements={("Q76", "P39"): [Statement(value="potus", value_type="literal")]},
        )
        walker, _, db = _make_walker(kb)
        _seed_pt(db, "holds_role", "P39")
        result = walker.walk(_claim("Obama", "holds_role", "POTUS", polarity=0), _ctx())
        assert result.verdict == "contradicted"
