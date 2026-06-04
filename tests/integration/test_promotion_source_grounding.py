"""v0.16.3 — source-grounding promotion gate (the question-self-grounding fix).

A QUESTION must not seed Tier U with the LLM's own ANSWER. "What is the capital
of France?" over-extracts to (France, capital, Paris) where "Paris" is the model's
answer, absent from the source; promoting it let a later draft self-ground against
it (verified_given_assertion, KB bypassed). The gate requires BOTH subject and
object to appear in the user's message before promotion.

REAL-PATH: the mock transport supplies only the LLM's RAW claim JSON / draft text;
the REAL Extractor builds the Claim, the REAL `is_source_grounded` gate filters it,
REAL `promote_assertions` writes Tier U, and the REAL walk verifies against a keyed
KB. No resolution/verdict is hardcoded — the gate decides on the real (claim,
source) pair, and a keyed KB grounds the draft so the verdict is genuine.
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
from aedos.layer5_result.aggregator import Aggregator
from aedos.llm.client import LLMClient
from aedos.seed_loader import load_seeds_into_connection

_FRANCE, _PARIS = "Q142", "Q90"


class _Transport:
    """Returns the LLM's raw output. extract_claims ALWAYS yields the
    over-extracted (France, capital, Paris) — exactly what the real model emits
    for BOTH the question and the assertion; the gate (not the transport) is what
    must distinguish them. Draft text answers the question."""

    def __init__(self, draft: str, obj: str = "Paris") -> None:
        self.draft = draft
        self.obj = obj

    def chat(self, *a: Any, **kw: Any) -> str:
        return self.draft

    def extract_with_tool(self, *a: Any, tool=None, purpose=None, **kw: Any):
        if tool is None:
            for arg in a:
                if isinstance(arg, dict) and "name" in arg:
                    tool = arg
                    break
        name = tool["name"] if tool else ""
        if name == "extract_claims":
            return {"claims": [{
                "subject": "France", "predicate": "capital", "object": self.obj,
                "polarity": 1, "source_text": "France", "verb_tense": "present",
            }]}
        # substrate-oracle generation (unused for the seeded `capital` predicate)
        return {
            "object_type": "entity", "user_subject_required": 0, "distinct_slots": None,
            "routing_hint": "kb_resolvable", "kb_namespace": None, "kb_property": None,
            "slot_to_qualifier": None, "single_valued": 0,
            "verdict": "neither", "reason": "test",
        }


class _KeyedKB:
    """Grounds France/capital/Paris: France->Q142, Paris->Q90, and the KB fact
    France(Q142) P36 Paris(Q90). Safe defaults elsewhere so the verifier's direct
    value-match path grounds without geo/subsumption detours."""

    def resolve_entity(self, reference, local_context):
        qid = {"France": _FRANCE, "Paris": _PARIS}.get(reference)
        return [ResolutionCandidate(kb_identifier=qid, score=0.95)] if qid else []

    def lookup_statements(self, entity, predicate):
        if (entity, predicate) == (_FRANCE, "P36"):
            return [Statement(value=_PARIS, value_type="entity")]
        return []

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
        return None


def _make_wrapper(draft="The capital of France is Paris.", obj="Paris"):
    db = open_memory_db()
    load_seeds_into_connection(db)          # seeds the standard `capital` -> P36
    client = LLMClient(_transport=_Transport(draft=draft, obj=obj))
    kb = _KeyedKB()
    pt = PredicateTranslation(db=db, llm_client=client)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(resolver=resolver, predicate_translation=pt,
                          subsumption=sub, predicate_distribution=pd)
    tier_u = TierU(db=db, predicate_translation=pt)
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    walker = Walker(tier_u=tier_u, kb_verifier=kb_verifier,
                    python_verifier=PythonVerifier(), substrate=substrate, kb=None)
    wrapper = ChatWrapper(
        extractor=Extractor(llm_client=client), walker=walker,
        aggregator=Aggregator(), llm_client=client, tier_u=tier_u, kb=kb,
    )
    return wrapper, tier_u, db


def _capital_rows(db, party=None):
    q = ("SELECT id, status, asserting_party FROM tier_u "
         "WHERE subject='France' AND predicate='capital' AND object='Paris'")
    if party:
        q += f" AND asserting_party='{party}'"
    return db.execute(q).fetchall()


def _ctx(session):
    return {"asserting_party_id": f"session:{session}"}


class TestSourceGroundedGate:
    """Direct unit coverage of the is_source_grounded discriminator (the heart of
    the fix) — both directions plus the surfaced edges."""

    @staticmethod
    def _claim(subject, obj, party="session:x"):
        from aedos.layer1_extraction.extractor import Claim
        from aedos.layer1_extraction.triage import TriageDecision
        return Claim(claim_id="c", subject=subject, predicate="capital", object=obj,
                     polarity=1, source_text="", asserting_party=party,
                     triage_decision=TriageDecision.VERIFY)

    def test_question_answer_object_blocked(self):
        from aedos.layer4_sources.promotion import is_source_grounded
        assert is_source_grounded(
            self._claim("France", "Paris"), "What is the capital of France?"
        ) is False

    def test_genuine_assertion_passes(self):
        from aedos.layer4_sources.promotion import is_source_grounded
        assert is_source_grounded(
            self._claim("France", "Paris"), "France's capital is Paris."
        ) is True

    def test_first_person_subject_passes(self):
        from aedos.layer4_sources.promotion import is_source_grounded
        assert is_source_grounded(
            self._claim("session:x", "tea"), "I prefer tea"
        ) is True

    def test_multiword_verbatim_object_passes(self):
        from aedos.layer4_sources.promotion import is_source_grounded
        assert is_source_grounded(
            self._claim("Obama", "Harvard University"),
            "Obama studied at Harvard University",
        ) is True

    def test_empty_object_with_grounded_subject_passes(self):
        from aedos.layer4_sources.promotion import is_source_grounded
        assert is_source_grounded(
            self._claim("World War II", ""), "World War II happened"
        ) is True

    def test_subject_absent_blocked(self):
        from aedos.layer4_sources.promotion import is_source_grounded
        assert is_source_grounded(
            self._claim("Germany", "Berlin"), "What is the capital of France?"
        ) is False

    def test_llm_expanded_object_blocked_known_limitation(self):
        """Surfaced limitation: an object the LLM expands beyond the source span
        (decade '70s' -> '1970s') does NOT promote — fails SAFE, never unsound."""
        from aedos.layer4_sources.promotion import is_source_grounded
        assert is_source_grounded(
            self._claim("Einstein", "1970s"), "Einstein was born in the 70s"
        ) is False

    def test_empty_source_blocked(self):
        from aedos.layer4_sources.promotion import is_source_grounded
        assert is_source_grounded(self._claim("France", "Paris"), "") is False


class TestQuestionDoesNotSelfGround:
    def test_question_does_not_promote_and_grounds_via_kb(self):
        """The QUESTION must not write a France/capital/Paris premise to Tier U,
        and the draft must then ground via the KB → plain `verified`, NOT
        verified_given_assertion."""
        wrapper, tier_u, db = _make_wrapper()
        resp = wrapper.respond("What is the capital of France?", _ctx("q1"))

        assert _capital_rows(db) == []          # nothing promoted from the question
        cvs = resp.verification_result.claim_verdicts
        cap = [cv for cv in cvs if cv.claim.predicate == "capital"]
        assert cap, "expected a capital claim verdict"
        v = cap[0]
        assert v.verdict == "verified"          # plain verified — NOT _given_assertion
        assert "given_assertion" not in v.verdict
        db.close()

    def test_repeated_question_stays_plain_verified(self):
        """Asking twice must not self-ground on the second turn (no premise was
        ever written, so no asserted-assertion match)."""
        wrapper, tier_u, db = _make_wrapper()
        wrapper.respond("What is the capital of France?", _ctx("q2"))
        resp2 = wrapper.respond("What is the capital of France?", _ctx("q2"))
        assert _capital_rows(db) == []
        cap = [cv for cv in resp2.verification_result.claim_verdicts
               if cv.claim.predicate == "capital"]
        assert cap and cap[0].verdict == "verified"
        db.close()


class TestGenuineAssertionStillPromotes:
    def test_assertion_with_both_entities_promotes(self):
        """PRESERVATION (Cluster-2): a real stipulation — both France and Paris in
        the source — STILL promotes to Tier U. The promotion WRITE is
        asserted_unverified (proven via the audit log); the same-turn draft then
        KB-grounds it, which CORRECTLY upgrades the live row to externally_verified
        — both steps are the feature working."""
        from aedos.audit.log import query_events
        wrapper, tier_u, db = _make_wrapper()
        wrapper.respond("France's capital is Paris.", _ctx("a1"))
        # The premise row exists (promotion happened).
        assert len(_capital_rows(db, party="session:a1")) == 1
        # And it was WRITTEN as an asserted_unverified assertion premise.
        created = query_events(db, event_type="row_created")
        assert any(e["event_data"].get("status") == "asserted_unverified" for e in created)
        db.close()

    def test_assertion_the_kb_would_contradict_still_promotes(self):
        """A genuine assertion whose object the KB does NOT confirm
        (France capital Berlin — both entities in source) must STILL promote as
        asserted_unverified: the gate is about SOURCE-grounding, not KB truth, so
        the assertion path's downstream behavior is not suppressed."""
        wrapper, tier_u, db = _make_wrapper(
            draft="The capital of France is Berlin.", obj="Berlin",
        )
        wrapper.respond("France's capital is Berlin.", _ctx("a2"))
        rows = db.execute(
            "SELECT status FROM tier_u WHERE subject='France' AND predicate='capital' "
            "AND object='Berlin' AND asserting_party='session:a2'"
        ).fetchall()
        assert len(rows) == 1 and rows[0]["status"] == "asserted_unverified"
        db.close()
