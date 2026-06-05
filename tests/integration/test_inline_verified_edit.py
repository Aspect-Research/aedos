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
from aedos.layer5_result.aggregator import Aggregator, ClaimVerdict, base_verdict_of
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


# ---------------------------------------------------------------------------
# Temporal-duplicate reconciliation (the over-refusal fix)
# ---------------------------------------------------------------------------

class TestTemporalDuplicateReconciliation:
    """The extractor emits temporal variants of one fact as separate claims with
    the SAME triple (e.g. 'X is president' → verified, 'X took office in May 2022'
    → holds_role(X, president) valid_from=2022-05 → no_grounding). Composing
    per-claim handed the editor 'keep X' AND 'remove X' for one triple → it struck
    the verified fact (over-refusal). Reconciliation collapses the triple with
    verified-beats-abstention precedence."""

    @staticmethod
    def _cv(s, p, o, verdict, cval=None, ctype=None, pol=1):
        c = Claim(claim_id="x", subject=s, predicate=p, object=o, polarity=pol,
                  source_text="", asserting_party="u", triage_decision=TriageDecision.VERIFY)
        return ClaimVerdict(claim_id=verdict + o, claim=c, verdict=verdict,
                            contradicting_value=cval, contradicting_value_type=ctype)

    def test_verified_wins_over_same_triple_abstention(self):
        from aedos.deployment.chat_wrapper import _reconcile_for_composition
        cvs = [
            self._cv("Tamás Sulyok", "holds_role", "President of Hungary", "verified"),
            self._cv("Tamás Sulyok", "holds_role", "President of Hungary", "no_grounding_found"),
            self._cv("Tamás Sulyok", "role_started", "2022-05", "no_grounding_found"),
        ]
        rec = _reconcile_for_composition(cvs)
        # The president triple collapses to ONE verified rep; the distinct
        # role_started triple is preserved.
        assert len(rec) == 2
        pres = [cv for cv in rec if cv.claim.predicate == "holds_role"]
        assert len(pres) == 1 and pres[0].verdict == "verified"
        assert any(cv.claim.predicate == "role_started" for cv in rec)

    def test_no_conflicting_instructions_for_temporal_duplicate(self):
        from aedos.deployment.chat_wrapper import _reconcile_for_composition, _revision_instructions
        cvs = [
            self._cv("Tamás Sulyok", "holds_role", "President of Hungary", "verified"),
            self._cv("Tamás Sulyok", "holds_role", "President of Hungary", "no_grounding_found"),
            self._cv("Tamás Sulyok", "role_started", "2022-05", "no_grounding_found"),
        ]
        lines = _revision_instructions(_reconcile_for_composition(cvs))
        assert any(l.startswith("VERIFIED") and "President of Hungary" in l for l in lines)
        assert any(l.startswith("REMOVE") and "role_started" in l for l in lines)
        # CRUCIAL: no instruction tells the editor to REMOVE the verified president.
        assert not any(l.startswith("REMOVE") and "holds_role" in l for l in lines)

    def test_contradicted_beats_verified_for_same_triple(self):
        """Conservative precedence: a contradiction in ANY scope wins, so a triple
        some scope refutes is never asserted plainly."""
        from aedos.deployment.chat_wrapper import _reconcile_for_composition
        cvs = [
            self._cv("X", "holds_role", "Y", "verified"),
            self._cv("X", "holds_role", "Y", "contradicted", cval="Z"),
        ]
        rec = _reconcile_for_composition(cvs)
        assert len(rec) == 1 and rec[0].verdict == "contradicted"

    def test_distinct_triples_all_preserved(self):
        from aedos.deployment.chat_wrapper import _reconcile_for_composition
        cvs = [
            self._cv("A", "p", "B", "verified"),
            self._cv("C", "p", "D", "no_grounding_found"),
            self._cv("A", "p", "B", "verified"),  # exact dup of the first
        ]
        rec = _reconcile_for_composition(cvs)
        assert len(rec) == 2  # the exact dup collapses; the distinct triple stays


class _MockAggregator:
    def __init__(self, vr):
        self._vr = vr

    def aggregate(self, claims, per_claim_results, text_input):
        return self._vr


class _CapturingTransport(_Transport):
    """Captures the editor's user prompt so the test can assert respond() passed
    RECONCILED instructions (no conflicting president REMOVE)."""

    def __init__(self, draft, claims):
        super().__init__(draft, claims, revise="EDITED: Tamás Sulyok is the president of Hungary.")
        self.revise_prompt = None

    def chat(self, *a, purpose=None, **kw):
        if purpose == "chat:revise":
            msgs = kw.get("messages") or (a[1] if len(a) > 1 else [])
            self.revise_prompt = msgs[0].content if msgs else ""
        return super().chat(*a, purpose=purpose, **kw)


def test_respond_reconciles_temporal_duplicate_before_editing():
    """End-to-end wiring: a VerificationResult with the verified + same-triple
    abstained duplicate is reconciled before composition, so the editor is told to
    KEEP the verified president (not remove it) and the reply is the edit, not a
    refusal."""
    from aedos.deployment.chat_wrapper import ChatWrapper
    from aedos.layer5_result.aggregator import VerificationResult

    db = open_memory_db()
    load_seeds_into_connection(db)
    transport = _CapturingTransport(
        draft="Tamás Sulyok is the president of Hungary. He took office in May 2022.",
        claims=[("Tamás Sulyok", "holds_role", "President of Hungary")],
    )
    client = LLMClient(_transport=transport)
    kb = _KeyedKB()
    pt = PredicateTranslation(db=db, llm_client=client)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(resolver=resolver, predicate_translation=pt, subsumption=sub,
                          predicate_distribution=pd)
    tier_u = TierU(db=db, predicate_translation=pt)
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    walker = Walker(tier_u=tier_u, kb_verifier=kb_verifier,
                    python_verifier=PythonVerifier(), substrate=substrate, kb=None)

    def _cv(o, predicate, verdict):
        c = Claim(claim_id=verdict + predicate, subject="Tamás Sulyok", predicate=predicate,
                  object=o, polarity=1, source_text="", asserting_party="u",
                  triage_decision=TriageDecision.VERIFY)
        return ClaimVerdict(claim_id=verdict + predicate, claim=c, verdict=verdict)
    cvs = [
        _cv("President of Hungary", "holds_role", "verified"),
        _cv("President of Hungary", "holds_role", "no_grounding_found"),
        _cv("2022-05", "role_started", "no_grounding_found"),
    ]
    vr = VerificationResult(
        claims_extracted=[cv.claim for cv in cvs], per_claim_verdicts={},
        per_claim_traces={}, aggregate_metadata={}, audit_log_entries=[],
        text_input={"message": "Who is the president of Hungary?", "draft": transport.draft},
        claim_verdicts=cvs,
    )
    wrapper = ChatWrapper(extractor=Extractor(llm_client=client), walker=walker,
                          aggregator=_MockAggregator(vr), llm_client=client, tier_u=tier_u, kb=kb)

    resp = wrapper.respond("Who is the president of Hungary?", {"asserting_party_id": "session:hu"})
    # The editor was handed reconciled instructions: keep the president, remove the date.
    assert transport.revise_prompt is not None
    assert "VERIFIED" in transport.revise_prompt and "President of Hungary" in transport.revise_prompt
    assert "REMOVE — \"Tamás Sulyok role_started" in transport.revise_prompt
    assert "REMOVE — \"Tamás Sulyok holds_role" not in transport.revise_prompt   # NOT removed
    # The reply is the edit (president kept), not an over-refusal.
    assert resp.final_message == "EDITED: Tamás Sulyok is the president of Hungary."


# ---------------------------------------------------------------------------
# Present-fact-with-too-early-start (option-2 verifier fallback) → composition
# ---------------------------------------------------------------------------

class TestTemporalScopeUnconfirmedComposition:
    """A `verified` claim flagged `temporal_scope_unconfirmed` (present base fact
    holds; the claimed "since <date>" precedes the value's actual start and could
    not be confirmed). Composition must: count it as VERIFIED (so the turn is not a
    DECLINE), INTERVENE so the editor runs, and instruct the editor to assert the
    present fact WITHOUT the unconfirmed date."""

    @staticmethod
    def _cv(s, p, o, verdict, *, ts=False, pol=1):
        c = Claim(claim_id="c", subject=s, predicate=p, object=o, polarity=pol,
                  source_text="", asserting_party="u", triage_decision=TriageDecision.VERIFY)
        return ClaimVerdict(claim_id=s + p + o, claim=c, verdict=verdict,
                            temporal_scope_unconfirmed=ts)

    def test_lone_temporal_unconfirmed_intervenes_not_passthrough(self):
        """A present fact whose date is unconfirmed must NOT pass through verbatim
        (the draft may carry the wrong date) — it INTERVENES so the editor edits."""
        from aedos.deployment.chat_wrapper import select_interventions, InterventionType
        cvs = [self._cv("Sulyok", "holds_role", "President of Hungary", "verified", ts=True)]
        plan = select_interventions(cvs)
        assert plan.overall == InterventionType.INTERVENE       # editor will run
        # Surfaced as a non-problematic caveat (verified), so never a DECLINE.
        assert len(plan.per_claim_actions) == 1
        assert "could NOT confirm" in plan.per_claim_actions[0].annotation

    def test_temporal_unconfirmed_with_abstention_is_not_decline(self):
        """The live shape: the present role (verified-but-date-unconfirmed) +
        a separate abstained start-date claim. verified_count >= 1 → INTERVENE, not
        the over-refusal DECLINE that zero-verified would give."""
        from aedos.deployment.chat_wrapper import select_interventions, InterventionType
        cvs = [
            self._cv("Sulyok", "holds_role", "President of Hungary", "verified", ts=True),
            self._cv("Sulyok", "role_started", "2022", "no_grounding_found"),
        ]
        plan = select_interventions(cvs)
        assert plan.overall == InterventionType.INTERVENE

    def test_instruction_asserts_present_drops_date(self):
        from aedos.deployment.chat_wrapper import _revision_instructions
        cvs = [self._cv("Sulyok", "holds_role", "President of Hungary", "verified", ts=True)]
        lines = _revision_instructions(cvs)
        joined = "\n".join(lines)
        assert any(l.startswith("PRESENT-ONLY") for l in lines)
        assert "do NOT state the claimed start date" in joined
        # It is NOT a plain "VERIFIED keep" (that would keep a wrong since-date).
        assert not any(l.startswith("VERIFIED") for l in lines)

    def test_reconcile_clean_verified_outranks_temporal_caveat(self):
        """Same triple, one clean verified (scope OK) + one temporal-unconfirmed:
        the clean one represents the group (no needless date-drop)."""
        from aedos.deployment.chat_wrapper import _reconcile_for_composition
        cvs = [
            self._cv("X", "holds_role", "Y", "verified", ts=True),
            self._cv("X", "holds_role", "Y", "verified", ts=False),
        ]
        rec = _reconcile_for_composition(cvs)
        assert len(rec) == 1 and rec[0].temporal_scope_unconfirmed is False

    def test_reconcile_temporal_caveat_outranks_abstention(self):
        """Same triple, temporal-unconfirmed verified + abstention: the verified
        (present fact) wins — the answer is not lost."""
        from aedos.deployment.chat_wrapper import _reconcile_for_composition
        cvs = [
            self._cv("X", "holds_role", "Y", "no_grounding_found"),
            self._cv("X", "holds_role", "Y", "verified", ts=True),
        ]
        rec = _reconcile_for_composition(cvs)
        assert len(rec) == 1 and base_verdict_of(rec[0].verdict) == "verified"
        assert rec[0].temporal_scope_unconfirmed is True


# Real pipeline + REAL aggregator: the live "since 2022" claim flows verifier →
# walker stamp → aggregator threading → composition → editor, end to end.

_SULYOK, _PRES = "Q28599854", "Q520765"


class _HungaryKB:
    """Sulyok currently holds President of Hungary (P39), started 2024-03 (safely
    in the past). A 'since 2022' claim is value-correct but the start is too early
    → present fact verifies, date flagged unconfirmed."""

    def resolve_entity(self, reference, local_context):
        qid = {"Tamás Sulyok": _SULYOK, "President of Hungary": _PRES}.get(reference)
        return [ResolutionCandidate(kb_identifier=qid, score=0.95)] if qid else []

    def lookup_statements(self, entity, predicate):
        if (entity, predicate) == (_SULYOK, "P39"):
            return [Statement(value=_PRES, value_type="entity",
                              qualifiers={"P580": "2024-03-05T00:00:00Z"})]
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
        return {_SULYOK: "Tamás Sulyok", _PRES: "President of Hungary"}.get(qid)


class _HungaryTransport:
    """Emits ONE holds_role claim carrying valid_from='2022' (the extractor's
    folded-in too-early start), and a capturing editor that drops the date."""

    EDIT = "Tamás Sulyok is the president of Hungary."

    def __init__(self):
        self.revise_calls = 0
        self.revise_prompt = None

    def chat(self, *a, purpose=None, **kw):
        if purpose == "chat:revise":
            self.revise_calls += 1
            msgs = kw.get("messages") or (a[1] if len(a) > 1 else [])
            self.revise_prompt = msgs[0].content if msgs else ""
            return self.EDIT
        return "Tamás Sulyok is the president of Hungary, in office since 2022."

    def extract_with_tool(self, *a, tool=None, purpose=None, **kw):
        if tool is None:
            for arg in a:
                if isinstance(arg, dict) and "name" in arg:
                    tool = arg
                    break
        name = tool["name"] if tool else ""
        if name == "extract_claims":
            return {"claims": [{
                "subject": "Tamás Sulyok", "predicate": "holds_role",
                "object": "President of Hungary", "polarity": 1,
                "source_text": "Tamás Sulyok is the president of Hungary",
                "verb_tense": "present", "valid_from": "2022",
            }]}
        return {
            "object_type": "entity", "user_subject_required": 0, "distinct_slots": None,
            "routing_hint": "kb_resolvable", "kb_namespace": None, "kb_property": None,
            "slot_to_qualifier": None, "single_valued": 0,
            "verdict": "neither", "reason": "test",
        }


def test_since_2022_present_fact_edits_to_dropped_date_end_to_end():
    """Full real path (real Extractor + walk + REAL Aggregator + composition): the
    'since 2022' claim verifies its present fact with temporal_scope_unconfirmed,
    so the turn INTERVENES (not DECLINE), the editor is told PRESENT-ONLY, and the
    reply asserts the president with the unconfirmed date dropped."""
    from aedos.deployment.chat_wrapper import InterventionType
    db = open_memory_db()
    load_seeds_into_connection(db)
    transport = _HungaryTransport()
    client = LLMClient(_transport=transport)
    kb = _HungaryKB()
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

    resp = wrapper.respond("Who is the president of Hungary?", {"asserting_party_id": "session:hu2"})

    # Aggregator threaded the flag onto the verified claim (honest observability).
    hv = [c for c in resp.verification_result.claim_verdicts if c.claim.predicate == "holds_role"]
    assert len(hv) == 1
    assert base_verdict_of(hv[0].verdict) == "verified"
    assert hv[0].temporal_scope_unconfirmed is True
    # Not an over-refusal: INTERVENE, editor invoked, reply is the edit.
    assert resp.intervention_plan.overall == InterventionType.INTERVENE
    assert transport.revise_calls == 1
    assert transport.revise_prompt is not None and "PRESENT-ONLY" in transport.revise_prompt
    assert resp.final_message == _HungaryTransport.EDIT
    db.close()
