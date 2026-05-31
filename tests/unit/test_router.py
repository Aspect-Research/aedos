"""Live routing behavior (formerly the Layer 2 Router/Validator).

v0.16.1 WS5b: the standalone Layer-2 `Router`/`Validator` were deleted. Routing
is predicate-driven off the predicate translation oracle's `routing_hint`, and
the three former Validator anomaly checks are now covered in the live path:

  - `user_subject_required` — RELOCATED into the walker as a fail-closed
    walk-entry guard: a user_subject_required predicate asserted about a subject
    that is neither the asserting party nor a stipulated user persona
    short-circuits to an abstain (`no_grounding_found` / `user_subject_required`)
    BEFORE any Tier U / KB / Python lookup. It can only ever produce an abstain,
    never a verdict.
  - `distinct_slots` (subject == object) — SUPERSEDED by the extractor's
    `self_referential` abstention_reason (a subject==object triple is stamped at
    extraction and the walker short-circuits pre-lookup). Tested in
    test_extractor*.py.
  - `object_type` mismatch — SUPERSEDED by the kb_verifier value-type gate
    (`_object_satisfies_value_type`), which fails OPEN (abstains on a type
    mismatch, never false-contradicts). Tested in test_kb_verifier.py.

These tests assert the surviving routing BEHAVIOR via the oracle metadata and
the live walker, not the deleted Router/Validator objects.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aedos.database import open_memory_db
from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer3_substrate import Substrate
from aedos.layer3_substrate.predicate_distribution import PredicateDistributionOracle
from aedos.layer3_substrate.predicate_translation import PredicateTranslation
from aedos.layer3_substrate.resolver import EntityResolver
from aedos.layer3_substrate.subsumption import SubsumptionOracle
from aedos.layer4_sources.kb_protocol import ResolutionCandidate, SubsumptionResult
from aedos.layer4_sources.kb_verifier import KBVerdict, KBVerdictType
from aedos.layer4_sources.python_verifier import PythonVerifier
from aedos.layer4_sources.tier_u import TierU
from aedos.layer4_sources.walker import VerificationContext, Walker
from aedos.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockTransport:
    def __init__(self, routing_hint: str = "kb_resolvable",
                 user_subject_required: int = 0, distinct_slots=None,
                 object_type: str = "entity"):
        self._hint = routing_hint
        self._usr = user_subject_required
        self._distinct = distinct_slots
        self._obj_type = object_type
        self.call_count = 0

    def extract_with_tool(self, *a, purpose=None, **kw):
        self.call_count += 1
        if purpose == "substrate:predicate_distribution":
            return {"verdict": "neither", "reason": "test"}
        if purpose == "substrate:subsumption":
            return {"verdict": "unrelated", "reason": "test"}
        return {
            "object_type": self._obj_type,
            "user_subject_required": self._usr,
            "distinct_slots": self._distinct,
            "routing_hint": self._hint,
            "kb_namespace": "wikidata" if self._hint == "kb_resolvable" else None,
            "kb_property": "P39" if self._hint == "kb_resolvable" else None,
            "slot_to_qualifier": None,
            "reason": f"test reason for {self._hint}",
        }

    def chat(self, *a, **kw):
        return ""


class _CountingKBVerifier:
    """KB verifier that records whether it was reached and (optionally)
    returns a CONTRADICTED verdict, modelling the wrong-entity misresolution
    the persona / user_subject_required guards must prevent."""

    def __init__(self, verdict=KBVerdictType.CONTRADICTED):
        self.call_count = 0
        self._verdict = verdict

    def verify(self, claim, current_time=None, source_text=None):
        self.call_count += 1
        return KBVerdict(verdict=self._verdict, subject_kb_id="Q1")


def _claim(subject="Asa", predicate="holds_role", object_val="President",
           polarity=1, asserting_party="user_test"):
    return Claim(
        claim_id="c1", subject=subject, predicate=predicate, object=object_val,
        polarity=polarity, source_text="test", asserting_party=asserting_party,
        triage_decision=TriageDecision.VERIFY,
    )


def _ctx(asserting_party="user_test"):
    return VerificationContext(
        current_time=datetime.now(timezone.utc).isoformat(),
        asserting_party=asserting_party,
    )


def _make_oracle(transport):
    db = open_memory_db()
    client = LLMClient(_transport=transport)
    return PredicateTranslation(db=db, llm_client=client), db, client


def _make_walker(transport, kb_verifier=None):
    pt, db, client = _make_oracle(transport)

    class StubKB:
        def resolve_entity(self, r, lc): return [ResolutionCandidate("Q1", score=0.9)]
        def lookup_statements(self, e, p): return []
        def subsumption(self, a, b, rt): return SubsumptionResult(verdict="unrelated")

    resolver = EntityResolver(kb_protocol=StubKB(), db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=StubKB())
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(resolver=resolver, predicate_translation=pt,
                          subsumption=sub, predicate_distribution=pd)
    tier_u = TierU(db=db, predicate_translation=pt)
    return Walker(
        tier_u=tier_u,
        kb_verifier=kb_verifier or _CountingKBVerifier(KBVerdictType.NO_MATCH),
        python_verifier=PythonVerifier(),
        substrate=substrate,
    ), tier_u


# ---------------------------------------------------------------------------
# Routing hint is predicate-driven (oracle metadata)
# ---------------------------------------------------------------------------

class TestRoutingHintMetadata:
    @pytest.mark.parametrize("hint", [
        "user_authoritative", "kb_resolvable", "python", "abstain",
    ])
    def test_routing_hint_round_trips_from_oracle(self, hint):
        pt, _, _ = _make_oracle(MockTransport(routing_hint=hint))
        meta = pt.consult("some_predicate")
        assert meta.routing_hint == hint

    def test_cold_cache_triggers_one_oracle_call(self):
        transport = MockTransport(routing_hint="kb_resolvable")
        pt, _, _ = _make_oracle(transport)
        pt.consult("p")
        assert transport.call_count == 1

    def test_warm_cache_no_second_oracle_call(self):
        transport = MockTransport(routing_hint="kb_resolvable")
        pt, _, _ = _make_oracle(transport)
        pt.consult("p")
        pt.consult("p")
        assert transport.call_count == 1


# ---------------------------------------------------------------------------
# user_subject_required — RELOCATED fail-closed walk-entry guard
# ---------------------------------------------------------------------------

class TestUserSubjectRequiredGuard:
    def test_third_party_subject_abstains_never_contradicts(self):
        # A user_subject_required predicate asserted about a NON-user subject is
        # malformed (first-person predicate about a third party). The KB
        # verifier WOULD contradict; the fail-closed guard must short-circuit to
        # an abstain BEFORE KB is reached.
        kb = _CountingKBVerifier(KBVerdictType.CONTRADICTED)
        walker, _ = _make_walker(
            MockTransport(routing_hint="user_authoritative", user_subject_required=1),
            kb_verifier=kb,
        )
        result = walker.walk(_claim(subject="Obama", predicate="prefers"),
                             _ctx(asserting_party="user_test"))
        assert result.verdict == "no_grounding_found"
        assert result.abstention_reason == "user_subject_required"
        assert kb.call_count == 0, "KB must be unreachable for the anomaly"

    def test_subject_equals_asserting_party_passes_guard(self):
        # subject == asserting_party satisfies user_subject_required → routes
        # user_authoritative (no Tier U premise → abstained_given_assertion).
        walker, _ = _make_walker(
            MockTransport(routing_hint="user_authoritative", user_subject_required=1),
        )
        result = walker.walk(
            _claim(subject="user_test", predicate="prefers", asserting_party="user_test"),
            _ctx(asserting_party="user_test"),
        )
        assert result.verdict == "abstained_given_assertion"

    def test_persona_subject_passes_guard(self):
        # A stipulated user persona satisfies user_subject_required even though
        # the subject string differs from the asserting_party.
        walker, tier_u = _make_walker(
            MockTransport(routing_hint="user_authoritative", user_subject_required=1),
        )
        tier_u.write(
            Claim(claim_id="id", subject="user", predicate="identity",
                  object="Asa", polarity=1, source_text="seed",
                  asserting_party="user_test",
                  triage_decision=TriageDecision.VERIFY),
            bypass_normalizer=True,
        )
        result = walker.walk(
            _claim(subject="Asa", predicate="prefers", asserting_party="user_test"),
            _ctx(asserting_party="user_test"),
        )
        assert result.verdict == "abstained_given_assertion"

    def test_non_user_predicate_third_party_not_blocked(self):
        # A predicate that is NOT user_subject_required is unaffected by the
        # guard — a third-party subject proceeds to KB grounding.
        kb = _CountingKBVerifier(KBVerdictType.NO_MATCH)
        walker, _ = _make_walker(
            MockTransport(routing_hint="kb_resolvable", user_subject_required=0),
            kb_verifier=kb,
        )
        result = walker.walk(_claim(subject="Obama", predicate="holds_role"),
                             _ctx())
        assert result.verdict == "no_grounding_found"
        assert kb.call_count == 1, "KB must be reached for a non-anomalous claim"


# ---------------------------------------------------------------------------
# distinct_slots (subject == object) — SUPERSEDED by the extractor's
# self_referential abstention_reason; the walker short-circuits pre-lookup.
# ---------------------------------------------------------------------------

class TestDistinctSlotsSuperseded:
    def test_self_referential_claim_short_circuits_to_abstain(self):
        kb = _CountingKBVerifier(KBVerdictType.CONTRADICTED)
        walker, _ = _make_walker(
            MockTransport(routing_hint="kb_resolvable"), kb_verifier=kb,
        )
        # The extractor stamps abstention_reason='self_referential' on a
        # subject==object triple; simulate that input shape here.
        claim = _claim(subject="France", object_val="France")
        claim.abstention_reason = "self_referential"
        result = walker.walk(claim, _ctx())
        assert result.verdict == "no_grounding_found"
        assert result.abstention_reason == "self_referential"
        assert kb.call_count == 0
