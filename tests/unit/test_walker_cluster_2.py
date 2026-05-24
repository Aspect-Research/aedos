"""Walker tests for Phase H Cluster 2 step 3 — chain-composition
tracking, dual-designation verdict emission, Q-Lookup-α upgrade path,
and Q-UserAuth route-aware short-circuiting.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aedos.audit.log import query_events
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
# Helpers — minimal walker fixtures parametrized by what each test needs
# ---------------------------------------------------------------------------

class _Transport:
    """LLM transport that returns the requested routing_hint for
    predicate metadata; defaults to neither/unrelated for substrate
    judgments so distribution gates close and no spurious expansion
    fires."""

    def __init__(self, routing_hint="kb_resolvable", kb_property="P39"):
        self._routing = routing_hint
        self._kb_property = kb_property

    def extract_with_tool(self, *a, purpose=None, **kw):
        if purpose == "substrate:predicate_distribution":
            return {"verdict": "neither", "reason": "test"}
        if purpose == "substrate:subsumption":
            return {"verdict": "unrelated", "reason": "test"}
        return {
            "object_type": "entity",
            "user_subject_required": 0,
            "distinct_slots": None,
            "routing_hint": self._routing,
            "kb_namespace": "wikidata" if self._routing == "kb_resolvable" else None,
            "kb_property": self._kb_property if self._routing == "kb_resolvable" else None,
            "slot_to_qualifier": None,
            "single_valued": 0,
            "reason": "test",
        }

    def chat(self, *a, **kw):
        return ""


class _KB:
    def __init__(self, statements=None, resolutions=None):
        self._stmts = statements or []
        self._res = resolutions or {"Asa": "Q_ASA", "Obama": "Q76"}

    def resolve_entity(self, ref, lc):
        qid = self._res.get(ref)
        return [ResolutionCandidate(qid, score=0.9)] if qid else []

    def lookup_statements(self, e, p):
        return list(self._stmts)

    def subsumption(self, a, b, rt):
        return SubsumptionResult(verdict="unrelated")


def _build(routing_hint="kb_resolvable", kb_statements=None):
    """Construct (walker, tier_u, db). Defaults: kb_resolvable route,
    no KB statements (Tier U / abstain paths dominate)."""
    db = open_memory_db()
    client = LLMClient(_transport=_Transport(routing_hint=routing_hint))
    kb = _KB(statements=kb_statements)
    pt = PredicateTranslation(db=db, llm_client=client)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(resolver=resolver, predicate_translation=pt, subsumption=sub, predicate_distribution=pd)
    tier_u = TierU(db=db, predicate_translation=pt)
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    py_verifier = PythonVerifier()
    walker = Walker(tier_u=tier_u, kb_verifier=kb_verifier, python_verifier=py_verifier, substrate=substrate)
    return walker, tier_u, db


def _claim(
    subject="Asa", predicate="lives_in", object_val="Williamstown", polarity=1,
):
    return Claim(
        claim_id="c1",
        subject=subject,
        predicate=predicate,
        object=object_val,
        polarity=polarity,
        source_text="test",
        asserting_party="user_test",
        triage_decision=TriageDecision.VERIFY,
    )


def _ctx():
    return VerificationContext(
        current_time=datetime.now(timezone.utc).isoformat(),
        asserting_party="user_test",
    )


# ---------------------------------------------------------------------------
# TestChainCompositionFlag — the basic flag-set rule
# ---------------------------------------------------------------------------

class TestChainCompositionFlag:
    def test_asserted_unverified_tier_u_hit_sets_flag(self):
        walker, tier_u, _ = _build()
        # Default-status write → asserted_unverified.
        tier_u.write(_claim())
        result = walker.walk(_claim(), _ctx())
        # KB returns no statements → upgrade attempt fails → flag stays
        # set → verdict converts to verified_given_assertion.
        assert result.verdict == "verified_given_assertion"
        assert result.trace.chain_includes_assertion is True

    def test_externally_verified_tier_u_hit_does_not_set_flag(self):
        walker, tier_u, _ = _build()
        tier_u.write(_claim(), status="externally_verified")
        result = walker.walk(_claim(), _ctx())
        assert result.verdict == "verified"
        assert result.trace.chain_includes_assertion is False

    def test_premise_status_metadata_on_edge(self):
        walker, tier_u, _ = _build()
        tier_u.write(_claim())  # asserted_unverified default
        result = walker.walk(_claim(), _ctx())
        tier_u_edges = [
            e for e in result.trace.edges
            if e.metadata.get("source") == "tier_u"
        ]
        assert any(
            e.metadata.get("premise_status") == "asserted_unverified"
            for e in tier_u_edges
        )


# ---------------------------------------------------------------------------
# TestBeliefRevisionStatusAwareness — polarity_conflict and object_conflict
# ---------------------------------------------------------------------------

class TestBeliefRevisionStatusAwareness:
    def test_polarity_conflict_asserted_yields_dual_designation(self):
        walker, tier_u, _ = _build()
        # Prior: negated assertion (default asserted_unverified).
        tier_u.write(_claim(polarity=0))
        # Walk the opposite polarity → polarity_conflict belief revision
        # against an asserted prior → contradicted_given_assertion.
        result = walker.walk(_claim(polarity=1), _ctx())
        assert result.verdict == "contradicted_given_assertion"
        assert result.trace.chain_includes_assertion is True

    def test_polarity_conflict_externally_verified_yields_plain_contradicted(self):
        walker, tier_u, _ = _build()
        tier_u.write(_claim(polarity=0), status="externally_verified")
        result = walker.walk(_claim(polarity=1), _ctx())
        assert result.verdict == "contradicted"
        assert result.trace.chain_includes_assertion is False


# ---------------------------------------------------------------------------
# TestQUserAuth — user_authoritative route always produces *_given_assertion
# ---------------------------------------------------------------------------

class TestQUserAuth:
    def test_user_authoritative_with_tier_u_hit_yields_dual(self):
        walker, tier_u, _ = _build(routing_hint="user_authoritative")
        tier_u.write(_claim(predicate="prefers", object_val="tea"))
        result = walker.walk(_claim(predicate="prefers", object_val="tea"), _ctx())
        assert result.verdict == "verified_given_assertion"

    def test_user_authoritative_without_tier_u_hit_abstains_dual(self):
        # No Tier U premise; user_authoritative route doesn't try KB
        # (structurally unreachable). Abstention is dual-designated
        # because external grounding can never apply.
        walker, _, _ = _build(routing_hint="user_authoritative")
        result = walker.walk(_claim(predicate="prefers", object_val="tea"), _ctx())
        assert result.verdict == "abstained_given_assertion"
        assert result.trace.chain_includes_assertion is True


# ---------------------------------------------------------------------------
# TestQLookupAlphaUpgrade — KB success after asserted_unverified Tier U hit
# upgrades the row and returns plain verified.
# ---------------------------------------------------------------------------

class TestQLookupAlphaUpgrade:
    def test_kb_grounding_after_asserted_tier_u_upgrades_row(self):
        # Seed an asserted_unverified Tier U row AND make KB return a
        # statement that grounds the same claim. The walker hits Tier U,
        # tries KB for upgrade, succeeds → upgrades the row to
        # externally_verified and returns plain verified (chain flag
        # NOT set).
        kb_stmts = [Statement(value="Q_WILLIAMSTOWN", value_type="entity")]
        walker, tier_u, db = _build(kb_statements=kb_stmts)
        wr = tier_u.write(
            _claim(subject="Asa", predicate="lives_in", object_val="Q_WILLIAMSTOWN"),
        )
        # Verify the row is asserted_unverified before the walk.
        pre = db.execute("SELECT status FROM tier_u WHERE id=?", (wr.row_id,)).fetchone()
        assert pre["status"] == "asserted_unverified"

        result = walker.walk(
            _claim(subject="Asa", predicate="lives_in", object_val="Q_WILLIAMSTOWN"),
            _ctx(),
        )
        # The KB stub resolves Asa to Q_ASA and returns Q_WILLIAMSTOWN
        # as the statement value — KBVerifier verifies. The walker
        # upgrades the row and returns plain verified.
        assert result.verdict == "verified"
        assert result.trace.chain_includes_assertion is False
        post = db.execute("SELECT status FROM tier_u WHERE id=?", (wr.row_id,)).fetchone()
        assert post["status"] == "externally_verified"

    def test_upgrade_emits_status_upgraded_audit_event(self):
        kb_stmts = [Statement(value="Q_WILLIAMSTOWN", value_type="entity")]
        walker, tier_u, db = _build(kb_statements=kb_stmts)
        wr = tier_u.write(
            _claim(subject="Asa", predicate="lives_in", object_val="Q_WILLIAMSTOWN"),
        )
        walker.walk(
            _claim(subject="Asa", predicate="lives_in", object_val="Q_WILLIAMSTOWN"),
            _ctx(),
        )
        events = query_events(db, event_type="tier_u_status_upgraded")
        assert len(events) == 1
        assert events[0]["event_subject"] == f"tier_u:{wr.row_id}"
        # verdict_produced is 'verified' per Q-Upgrade default; the
        # walker passes it explicitly so the event records it.
        assert events[0]["event_data"]["verdict_produced"] == "verified"
        # grounding_chain captures the KB source detail.
        gc = events[0]["event_data"]["grounding_chain"]
        assert gc["source"] == "kb"

    def test_no_kb_grounding_keeps_asserted_status_and_sets_flag(self):
        # Tier U hit but KB returns nothing → no upgrade; chain flag
        # set; verdict is verified_given_assertion.
        walker, tier_u, db = _build(kb_statements=[])  # KB silent
        wr = tier_u.write(_claim())
        result = walker.walk(_claim(), _ctx())
        assert result.verdict == "verified_given_assertion"
        post = db.execute("SELECT status FROM tier_u WHERE id=?", (wr.row_id,)).fetchone()
        # Row stays asserted_unverified.
        assert post["status"] == "asserted_unverified"


# ---------------------------------------------------------------------------
# TestAbstainedGivenAssertion — claim with no Tier U match and a
# user_authoritative route abstains as dual designation.
# ---------------------------------------------------------------------------

class TestAbstainedGivenAssertion:
    def test_no_premise_user_authoritative_abstain_dual(self):
        walker, _, _ = _build(routing_hint="user_authoritative")
        result = walker.walk(_claim(predicate="prefers"), _ctx())
        assert result.verdict == "abstained_given_assertion"

    def test_no_premise_kb_resolvable_abstain_plain(self):
        walker, _, _ = _build()  # kb_resolvable + no KB stmts
        result = walker.walk(_claim(), _ctx())
        assert result.verdict == "no_grounding_found"
        assert result.trace.chain_includes_assertion is False
