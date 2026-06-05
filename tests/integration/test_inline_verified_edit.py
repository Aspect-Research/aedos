"""v0.16.4 — inline verified-edit of the final reply.

Instead of appending an "Aedos verification notes" section, a final
constrained-rewrite step folds the per-claim verdicts INTO the reply. These tests
exercise the real respond() path (real Extractor, real walk against a keyed KB,
real select_interventions, real revise_response wiring); only the LLM's draft and
the LLM editor's rewrite are supplied by the transport (there is no real LLM in
tests). The transport branches on `purpose`, so the editor invocation is genuine:
the test asserts WHEN it runs, what it's handed, and how its output / failure is
used.
"""

from __future__ import annotations

from typing import Any

from aedos.database import open_memory_db
from aedos.deployment.chat_wrapper import (
    ChatWrapper, _revision_instructions, revise_response,
)
from aedos.layer1_extraction.extractor import Extractor
from aedos.layer1_extraction.triage import TriageDecision
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
from aedos.layer5_result.aggregator import Aggregator, ClaimVerdict
from aedos.layer1_extraction.extractor import Claim
from aedos.llm.client import LLMClient
from aedos.seed_loader import load_seeds_into_connection

_FRANCE, _PARIS, _LYON = "Q142", "Q90", "Q456"
_REVISED = "The capital of France is Paris."


class _Transport:
    """LLM transport branching on purpose. `draft`/`draft_obj` drive the draft and
    its single extracted claim (France, capital, <draft_obj>); `revise` is what the
    editor returns ("RAISE" makes the editor call throw)."""

    def __init__(self, draft, claims, revise=_REVISED):
        # claims: list of (subject, predicate, object) the extractor will emit.
        self.draft = draft
        self.claims = claims
        self.revise = revise
        self.revise_calls = 0

    def chat(self, *a: Any, purpose: str | None = None, **kw: Any) -> str:
        if purpose == "chat:revise":
            self.revise_calls += 1
            if self.revise == "RAISE":
                raise RuntimeError("editor llm down")
            return self.revise
        return self.draft

    def extract_with_tool(self, *a: Any, tool=None, purpose=None, **kw: Any):
        if tool is None:
            for arg in a:
                if isinstance(arg, dict) and "name" in arg:
                    tool = arg
                    break
        name = tool["name"] if tool else ""
        if name == "extract_claims":
            return {"claims": [
                {"subject": s, "predicate": p, "object": o,
                 "polarity": 1, "source_text": s, "verb_tense": "present"}
                for (s, p, o) in self.claims
            ]}
        return {
            "object_type": "entity", "user_subject_required": 0, "distinct_slots": None,
            "routing_hint": "kb_resolvable", "kb_namespace": None, "kb_property": None,
            "slot_to_qualifier": None, "single_valued": 0,
            "verdict": "neither", "reason": "test",
        }


class _KeyedKB:
    """France(Q142) P36 Paris(Q90). Resolves France/Paris/Lyon. capital is
    single_valued (seed), so France/capital/Lyon → CONTRADICTED with value Paris."""

    def resolve_entity(self, reference, local_context):
        qid = {"France": _FRANCE, "Paris": _PARIS, "Lyon": _LYON}.get(reference)
        return [ResolutionCandidate(kb_identifier=qid, score=0.95)] if qid else []

    def lookup_statements(self, entity, predicate):
        if (entity, predicate) == (_FRANCE, "P36"):
            return [Statement(value=_PARIS, value_type="entity")]
        return []

    def subsumption(self, a, b, relation_type):
        # Lyon and Paris are cities → satisfy capital's object value-type
        # constraint (Q515 city / Q5119 capital), so a wrong-city claim CAN
        # contradict (otherwise the verifier soundly abstains).
        if a in (_LYON, _PARIS) and b in ("Q515", "Q5119"):
            return SubsumptionResult(verdict="a_subsumed_by_b")
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
        return {_PARIS: "Paris", _LYON: "Lyon", _FRANCE: "France"}.get(qid)


def _make_wrapper(draft, claims, revise=_REVISED):
    db = open_memory_db()
    load_seeds_into_connection(db)
    transport = _Transport(draft, claims, revise)
    client = LLMClient(_transport=transport)
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
    wrapper = ChatWrapper(extractor=Extractor(llm_client=client), walker=walker,
                          aggregator=Aggregator(), llm_client=client, tier_u=tier_u, kb=kb)
    return wrapper, transport


_Q = "What is the capital of France?"


# ---------------------------------------------------------------------------
# respond() wiring — when the editor runs and how its output is used
# ---------------------------------------------------------------------------

class TestInlineEditWiring:
    def test_all_verified_passthrough_returns_draft_verbatim_no_editor(self):
        wrapper, t = _make_wrapper(
            draft="The capital of France is Paris.", claims=[("France", "capital", "Paris")],
        )
        resp = wrapper.respond(_Q, {"asserting_party_id": "session:pt"})
        assert resp.final_message == "The capital of France is Paris."   # draft verbatim
        assert t.revise_calls == 0                                       # editor NOT invoked
        assert "Aedos verification notes" not in resp.final_message

    def test_contradicted_invokes_editor_and_uses_its_output(self):
        wrapper, t = _make_wrapper(
            draft="The capital of France is Lyon.", claims=[("France", "capital", "Lyon")],
        )
        resp = wrapper.respond(_Q, {"asserting_party_id": "session:iv"})
        assert t.revise_calls == 1                       # editor ran (INTERVENE: verified spine? see below)
        assert resp.final_message == _REVISED            # final_message IS the edit
        assert "Aedos verification notes" not in resp.final_message   # no appended notes
        # The structured observability still records the TRUE verdict (honest audit).
        cv = [c for c in resp.verification_result.claim_verdicts if c.claim.predicate == "capital"][0]
        assert cv.verdict == "contradicted"
        assert cv.contradicting_value == _PARIS

    def test_editor_failure_falls_back_to_deterministic_notes(self):
        wrapper, t = _make_wrapper(
            draft="The capital of France is Lyon.", claims=[("France", "capital", "Lyon")],
            revise="RAISE",
        )
        resp = wrapper.respond(_Q, {"asserting_party_id": "session:fb"})
        assert t.revise_calls == 1                                  # editor was attempted
        # Fail-safe: deterministic draft + notes composition, never broken/blank.
        assert resp.final_message.startswith("The capital of France is Lyon.")
        assert "Aedos verification notes" in resp.final_message

    def test_decline_returns_honest_message_without_running_editor(self):
        """Adversarial-review D1: a DECLINE turn (zero verified, multiple problems)
        must NOT ship a free LLM rewrite over zero-verified content. It returns the
        deterministic honest message and never invokes the editor."""
        wrapper, t = _make_wrapper(
            draft="Zorgon invented Florbix and Blivet is located in Quux.",
            claims=[("Zorgon", "invented", "Florbix"), ("Blivet", "located_in", "Quux")],
        )
        resp = wrapper.respond(_Q, {"asserting_party_id": "session:dc"})
        # Both unverifiable → DECLINE.
        assert resp.intervention_plan.overall.value == "decline"
        assert t.revise_calls == 0                                   # editor NOT invoked
        assert resp.final_message == "I couldn't verify enough of this to answer confidently."
        # Structured detail still records both unverified claims.
        verdicts = {cv.verdict for cv in resp.verification_result.claim_verdicts}
        assert verdicts and all("verified" not in v for v in verdicts)


# ---------------------------------------------------------------------------
# Instruction builder (deterministic, the heart of the policy)
# ---------------------------------------------------------------------------

class TestRevisionInstructions:
    @staticmethod
    def _cv(s, p, o, verdict, cval=None, ctype=None, ab=None, pol=1):
        c = Claim(claim_id="c", subject=s, predicate=p, object=o, polarity=pol,
                  source_text="", asserting_party="u", triage_decision=TriageDecision.VERIFY)
        return ClaimVerdict(claim_id="c", claim=c, verdict=verdict, abstention_reason=ab,
                            contradicting_value=cval, contradicting_value_type=ctype)

    def test_each_verdict_maps_to_the_right_instruction(self):
        label = {"Q90": "Paris"}.get
        cvs = [
            self._cv("France", "capital", "Lyon", "contradicted", cval="Q90", ctype="entity"),
            self._cv("Berlin", "located_in", "Asia", "contradicted"),       # no value
            self._cv("X", "invented", "time travel", "no_grounding_found"),
            self._cv("user", "prefers", "tea", "verified_given_assertion"),
            self._cv("Water", "boils_at", "100C", "verified"),
            self._cv("noise", "is_a", "thing", "no_grounding_found", ab="not_checkworthy"),
        ]
        lines = _revision_instructions(cvs, label_fetcher=label)
        joined = "\n".join(lines)
        assert any(l.startswith("CORRECT") and "Paris" in l for l in lines)   # entity Q-id labeled
        assert 'REMOVE — "Berlin located_in Asia"' in joined                  # contradicted-no-value
        assert 'REMOVE — "X invented time travel"' in joined                  # abstain
        assert any(l.startswith("CAVEAT") for l in lines)                     # conditional
        assert any(l.startswith("VERIFIED") for l in lines)                   # plain verified
        assert "not_checkworthy" not in joined and "noise" not in joined      # quiet: no instruction

    def test_revise_response_error_returns_none(self):
        class Boom:
            def chat(self, **kw):
                raise RuntimeError("down")
        assert revise_response("q", "d", [], Boom()) is None

    def test_is_blank_catches_near_empty_fragments(self):
        from aedos.deployment.chat_wrapper import _is_blank
        for blank in ["", "   ", "\n\n", ".", "- ", "—", "* \n- ", None]:
            assert _is_blank(blank) is True
        for real in ["Paris", "N/A", "I couldn't verify it.", "0"]:
            assert _is_blank(real) is False
