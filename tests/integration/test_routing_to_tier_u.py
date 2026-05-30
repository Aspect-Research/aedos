"""Integration tests: claim → router → Tier U write → lookup roundtrip."""

from __future__ import annotations

import pytest

from aedos.database import open_memory_db
from aedos.layer1_extraction.extractor import Claim, ExtractionContext
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer2_routing.router import Router
from aedos.layer2_routing.validator import Validator
from aedos.layer3_substrate.predicate_translation import PredicateTranslation
from aedos.layer4_sources.tier_u import TierU
from aedos.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class MockTransport:
    def __init__(self, routing_hint: str = "user_authoritative"):
        self._hint = routing_hint

    def extract_with_tool(self, *a, **kw):
        return {
            "object_type": "entity",
            "user_subject_required": 0,
            "distinct_slots": None,
            "routing_hint": self._hint,
            "kb_namespace": "wikidata" if self._hint == "kb_resolvable" else None,
            "kb_property": "P39" if self._hint == "kb_resolvable" else None,
            "slot_to_qualifier": None,
            "reason": "test",
        }

    def chat(self, *a, **kw):
        return ""


def _make_system(routing_hint: str = "user_authoritative"):
    db = open_memory_db()
    transport = MockTransport(routing_hint)
    client = LLMClient(_transport=transport)
    oracle = PredicateTranslation(db=db, llm_client=client)
    validator = Validator()
    router = Router(predicate_translation=oracle, validator=validator)
    tier_u = TierU(db=db, predicate_translation=oracle)
    return router, tier_u, db


def _claim(
    subject="Asa", predicate="prefers", object_val="Python",
    asserting_party="user_test", polarity=1,
):
    return Claim(
        claim_id="c1",
        subject=subject,
        predicate=predicate,
        object=object_val,
        polarity=polarity,
        source_text="test",
        asserting_party=asserting_party,
        triage_decision=TriageDecision.VERIFY,
    )


# ---------------------------------------------------------------------------
# TestUserAuthoritativeRoundtrip
# ---------------------------------------------------------------------------

class TestUserAuthoritativeRoundtrip:
    def test_claim_routes_to_user_authoritative(self):
        router, tier_u, _ = _make_system("user_authoritative")
        claim = _claim()
        decision = router.route(claim)
        assert decision.route == "user_authoritative"

    def test_user_authoritative_claim_written_to_tier_u(self):
        router, tier_u, db = _make_system("user_authoritative")
        claim = _claim()
        decision = router.route(claim)
        assert decision.route == "user_authoritative"
        tier_u.write(claim)
        count = db.execute("SELECT count(*) FROM tier_u").fetchone()[0]
        assert count == 1

    def test_lookup_finds_written_claim(self):
        router, tier_u, _ = _make_system("user_authoritative")
        claim = _claim()
        tier_u.write(claim)
        result = tier_u.lookup(claim)
        assert result.found is True

    def test_second_write_idempotent(self):
        router, tier_u, db = _make_system("user_authoritative")
        claim = _claim()
        tier_u.write(claim)
        tier_u.write(claim)
        count = db.execute("SELECT count(*) FROM tier_u").fetchone()[0]
        assert count == 1

    def test_asserting_party_preserved_in_tier_u(self):
        router, tier_u, db = _make_system("user_authoritative")
        claim = _claim(asserting_party="user_alice")
        tier_u.write(claim)
        row = db.execute("SELECT asserting_party FROM tier_u LIMIT 1").fetchone()
        assert row["asserting_party"] == "user_alice"


# ---------------------------------------------------------------------------
# TestKBResolvableRoute
# ---------------------------------------------------------------------------

class TestKBResolvableRoute:
    def test_kb_resolvable_returns_stub(self):
        router, _, _ = _make_system("kb_resolvable")
        claim = _claim(predicate="holds_role")
        decision = router.route(claim)
        assert decision.route == "kb_resolvable"
        assert decision.stub is True

    def test_kb_resolvable_no_tier_u_write(self):
        router, tier_u, db = _make_system("kb_resolvable")
        claim = _claim(predicate="holds_role")
        decision = router.route(claim)
        # KB-resolvable claims are NOT written to Tier U in Phase 3
        count = db.execute("SELECT count(*) FROM tier_u").fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# TestContradictionFlow
# ---------------------------------------------------------------------------

class TestContradictionFlow:
    # B3/D16: a *different object* on the multi-valued `prefers` predicate is a
    # parallel assertion, not a contradiction (a user may prefer several
    # things). The genuine contradiction this flow exercises is a polarity flip
    # — the same object asserted, then negated — which closes the prior row
    # regardless of predicate cardinality. The functional-vs-multi-valued
    # object-difference closure rule is covered in test_tier_u.py.
    def test_contradicting_claim_closes_prior(self):
        router, tier_u, db = _make_system("user_authoritative")
        tier_u.write(_claim(object_val="Coffee", polarity=1))
        result2 = tier_u.write(_claim(object_val="Coffee", polarity=0))
        assert result2.contradiction_closed is True

    def test_after_contradiction_lookup_finds_new(self):
        router, tier_u, _ = _make_system("user_authoritative")
        tier_u.write(_claim(object_val="Coffee", polarity=1))
        tier_u.write(_claim(object_val="Coffee", polarity=0))
        result = tier_u.lookup(_claim(object_val="Coffee", polarity=0))
        assert result.found is True

    def test_after_contradiction_old_not_found(self):
        router, tier_u, _ = _make_system("user_authoritative")
        tier_u.write(_claim(object_val="Coffee", polarity=1))
        tier_u.write(_claim(object_val="Coffee", polarity=0))
        result = tier_u.lookup(_claim(object_val="Coffee", polarity=1))
        assert result.found is False
