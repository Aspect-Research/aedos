"""v0.16.5 — a draft must not self-ground on the question it is answering, and a
height comparison should verify against real heights.

Live bug (full trace, session 1021012a): "Is the Eiffel Tower taller than the
Statue of Liberty?" extracted `taller_than(Eiffel, Statue)` FROM THE QUESTION —
the extractor does not treat interrogatives specially (the "no speech-act
detection" rule), and both entities are in the question so the source-grounding
gate admitted it. It was promoted as `asserted_unverified`, and the draft (which
re-states the same proposition) ground on it → `verified_given_assertion` ("rests
on your own assertion") at depth 0, crediting the user with a claim the MODEL
made.

Fix (chat_wrapper): user-message premises are promoted AFTER the draft walk, and a
proposition the DRAFT RE-STATES is dropped before promotion — the system answering
P is the signal the user ASKED about P. So a question never becomes "your
assertion" (this turn OR a later one); a genuine assertion the draft does not echo
still accumulates. No question-syntax parsing.

Plus height-recall: `taller_than`/`shorter_than` are seeded premise->python over
P2048, so the comparison verifies against the two fetched heights.

Exercised through the REAL pipeline (real Extractor + substrate + walker +
aggregator; only the LLM draft/extraction/edit are transport-supplied).
"""

from __future__ import annotations

from typing import Any

from aedos.database import open_memory_db
from aedos.deployment.chat_wrapper import ChatWrapper
from aedos.layer1_extraction.extractor import Extractor
from aedos.layer3_substrate import Substrate
from aedos.layer3_substrate.predicate_distribution import PredicateDistributionOracle
from aedos.layer3_substrate.predicate_translation import PredicateTranslation
from aedos.layer3_substrate.resolver import EntityResolver
from aedos.layer3_substrate.subsumption import SubsumptionOracle
from aedos.layer4_sources.kb_protocol import (
    ResolutionCandidate, Statement, SubsumptionResult, TransitivePathResult,
)
from aedos.layer4_sources.kb_verifier import KBVerifier
from aedos.layer4_sources.python_verifier import PythonVerifier
from aedos.layer4_sources.tier_u import TierU
from aedos.layer4_sources.walker import Walker
from aedos.layer5_result.aggregator import Aggregator, base_verdict_of
from aedos.llm.client import LLMClient
from aedos.seed_loader import load_seeds_into_connection

_EIFFEL, _STATUE = "Q243", "Q9202"
_QUESTION = "Is the Eiffel Tower taller than the Statue of Liberty?"
_DRAFT = "The Eiffel Tower is taller than the Statue of Liberty."


class _Transport:
    """Branches extraction on `user_message`: the QUESTION and the DRAFT each yield
    a configurable claim list. Default: both yield taller_than(Eiffel, Statue) — the
    restated case at the heart of the bug."""

    def __init__(self, draft=_DRAFT, question_claims=None, draft_claims=None):
        self.draft = draft
        self.question_claims = question_claims or [("Eiffel Tower", "taller_than", "Statue of Liberty")]
        self.draft_claims = draft_claims or [("Eiffel Tower", "taller_than", "Statue of Liberty")]

    def chat(self, *a: Any, purpose: str | None = None, **kw: Any) -> str:
        if purpose == "chat:revise":
            return "The Eiffel Tower is taller than the Statue of Liberty."
        return self.draft

    def extract_with_tool(self, *a: Any, tool=None, purpose=None, **kw: Any):
        # The client calls (system, user_message, tool) POSITIONALLY.
        user_message = a[1] if len(a) > 1 else kw.get("user_message")
        if tool is None:
            for arg in a:
                if isinstance(arg, dict) and "name" in arg:
                    tool = arg
                    break
        name = tool["name"] if tool else ""
        if name == "extract_claims":
            triples = self.draft_claims if user_message == self.draft else self.question_claims
            return {"claims": [
                {"subject": s, "predicate": p, "object": o, "polarity": 1,
                 "source_text": f"{s} {p} {o}", "verb_tense": "present"}
                for (s, p, o) in triples
            ]}
        return {
            "object_type": "entity", "user_subject_required": 0, "distinct_slots": None,
            "routing_hint": "kb_resolvable", "kb_namespace": None, "kb_property": None,
            "slot_to_qualifier": None, "single_valued": 0,
            "verdict": "neither", "reason": "test",
        }


class _BaseKB:
    def resolve_entity(self, reference, local_context):
        qid = {"Eiffel Tower": _EIFFEL, "Statue of Liberty": _STATUE}.get(reference)
        return [ResolutionCandidate(kb_identifier=qid, score=0.95)] if qid else []

    def subsumption(self, a, b, relation_type):
        return SubsumptionResult(verdict="unrelated")

    def verify_transitive_path(self, *a, **kw):
        return TransitivePathResult(holds=False)

    def is_location_property(self, prop):
        return False

    def geo_container_types(self):
        return frozenset()

    def geographic_disjoint(self, a, b):
        return False

    def fetch_types(self, qids):
        return ({q: [] for q in qids}, None)

    def fetch_label(self, qid):
        return {_EIFFEL: "Eiffel Tower", _STATUE: "Statue of Liberty"}.get(qid)


class _NoHeightKB(_BaseKB):
    """Resolves the structures but has NO P2048 — the live bug condition."""
    def lookup_statements(self, entity, predicate):
        return []


class _HeightKB(_BaseKB):
    """P2048: Eiffel 330 m, Statue 93 m."""
    def lookup_statements(self, entity, predicate):
        if predicate == "P2048":
            return {_EIFFEL: [Statement(value="330", value_type="quantity")],
                    _STATUE: [Statement(value="93", value_type="quantity")]}.get(entity, [])
        return []


def _make_wrapper(kb, transport=None):
    db = open_memory_db()
    load_seeds_into_connection(db)
    client = LLMClient(_transport=transport or _Transport())
    pt = PredicateTranslation(db=db, llm_client=client)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(resolver=resolver, predicate_translation=pt,
                          subsumption=sub, predicate_distribution=pd)
    tier_u = TierU(db=db, predicate_translation=pt)
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    walker = Walker(tier_u=tier_u, kb_verifier=kb_verifier,
                    python_verifier=PythonVerifier(llm_client=client),
                    substrate=substrate, kb=kb)
    wrapper = ChatWrapper(extractor=Extractor(llm_client=client), walker=walker,
                          aggregator=Aggregator(), llm_client=client, tier_u=tier_u, kb=kb)
    return wrapper, db


def _taller_verdict(resp):
    cvs = [c for c in resp.verification_result.claim_verdicts
           if c.claim.predicate == "taller_than"]
    assert len(cvs) == 1, f"expected one taller_than claim, got {len(cvs)}"
    return cvs[0].verdict


# ---------------------------------------------------------------------------
# Self-grounding: the question must never become "your assertion".
# ---------------------------------------------------------------------------

def test_draft_does_not_self_ground_on_the_question():
    """No KB height data → the comparison cannot verify the real way. It must
    ABSTAIN, NOT `verified_given_assertion` — the user asked, they did not assert."""
    wrapper, db = _make_wrapper(_NoHeightKB())
    verdict = _taller_verdict(wrapper.respond(_QUESTION, {"asserting_party_id": "session:tall"}))
    assert verdict == "no_grounding_found", f"self-grounded: {verdict}"
    assert "given_assertion" not in verdict
    db.close()


def test_user_premise_is_promoted_after_the_walk():
    """Deferring the write does not lose the premise: it is in Tier U after the turn
    (available to FUTURE turns) — it was simply invisible to THIS turn's draft walk.
    (NOTE: the question's proposition is still recorded as a premise, so a re-asked
    question in the same session self-grounds — the residual that the interrogative
    guard would close; tracked separately.)"""
    wrapper, db = _make_wrapper(_NoHeightKB())
    wrapper.respond(_QUESTION, {"asserting_party_id": "session:tall"})
    rows = db.execute(
        "SELECT predicate, status FROM tier_u WHERE predicate='taller_than'"
    ).fetchall()
    assert len(rows) >= 1 and any(r["status"] == "asserted_unverified" for r in rows)
    db.close()


# ---------------------------------------------------------------------------
# Height-recall: with real heights the comparison verifies (not given-assertion).
# ---------------------------------------------------------------------------

def test_taller_than_verifies_against_real_heights():
    """With P2048 heights (Eiffel 330 > Statue 93) the comparison verifies via the
    premise->python channel — a PLAIN verified, not `verified_given_assertion`."""
    wrapper, db = _make_wrapper(_HeightKB())
    resp = wrapper.respond(_QUESTION, {"asserting_party_id": "session:tall2"})
    verdict = _taller_verdict(resp)
    assert base_verdict_of(verdict) == "verified"
    assert "given_assertion" not in verdict  # real grounding, not the user's word
    cv = [c for c in resp.verification_result.claim_verdicts
          if c.claim.predicate == "taller_than"][0]
    trace = resp.verification_result.per_claim_traces[cv.claim_id]
    assert trace.source_breakdown.get("python") == 1
    db.close()
