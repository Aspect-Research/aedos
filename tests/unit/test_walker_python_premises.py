"""Tests for the v0.16.1 WS3b premise -> Python channel in the walker.

Covers (docs/v0_16_1/00_implementation_plan.md WS3 Step 2; planning item 3 Step 2):

  PythonVerifier.verify(claim, premises) (python_verifier.py):
    * premises=None reproduces the prior 3-arg behavior exactly (back-compat).
    * a 4-arg generated verify() receives the fetched premises and can compute
      over them; a None / missing-premise return routes to abstain.

  Walker._gather_python_premises (walker.py):
    * NO premise_properties metadata -> ({}, [], False): behave exactly as today
      (no fetch, plain python literal).
    * a declared premise is resolved + fetched from KB -> threaded into verify().
    * GATE b (FAIL CLOSED): a declared premise slot that does not resolve, or
      whose KB property carries no usable value, returns None (abstain) — the
      verifier is never invoked with a fabricated input.
    * premise-property knowledge comes ONLY from meta.premise_properties (no
      hardcoded predicate->property table).
    * _premise_value_from_statements prefers `preferred`, agrees-or-abstains.

  Walker._record_python_premise_term (walker.py) + provenance (trace.py):
    * GATE 3: each premise is an AND-child of the verdict's ProvenanceTerm so the
      retraction footprint includes every premise row.
    * GATE a: an assertion-conditional premise propagates onto the python literal
      so chain_includes_assertion fires (-> *_given_assertion downstream).

These are unit-level — directly constructed stubs, mirroring test_walker_interval.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer4_sources.kb_protocol import Statement
from aedos.layer4_sources.kb_verifier import KBVerdict, KBVerdictType
from aedos.layer4_sources.python_verifier import PythonVerdict, PythonVerifier
from aedos.layer5_result.trace import (
    JustificationTrace,
    ProvenanceLiteral,
    TraceNode,
)
from aedos.layer4_sources.walker import (
    VerificationContext,
    Walker,
    _apply_assertion_designation,
)
from aedos.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Stubs (mirroring test_walker_interval.py)
# ---------------------------------------------------------------------------

class _StubResolver:
    def __init__(self, resolutions=None, raise_on_resolve=False):
        self._resolutions = resolutions or {}
        self._raise = raise_on_resolve

    def resolve(self, reference, local_context):
        if self._raise:
            raise RuntimeError("simulated resolver error")
        qid = self._resolutions.get(reference)
        return [qid] if qid else []

    def select(self, candidates, local_context):
        return candidates[0] if candidates else None


class _StubKBVerifier:
    def __init__(self, resolver):
        self._resolver = resolver

    def verify(self, claim, current_time=None, source_text=None):
        # A comparison predicate (routing_hint='python') has no kb_resolvable
        # binding, so the real KBVerifier abstains (NO_MATCH) and the walker
        # falls through to the python branch. Mirror that so _try_external_
        # grounding reaches the premise->python channel under test.
        return KBVerdict(verdict=KBVerdictType.NO_MATCH)


class _StubKB:
    def __init__(self, statements_by_key=None, raise_on_lookup=False):
        self._by_key = statements_by_key or {}
        self._raise = raise_on_lookup

    def lookup_statements(self, entity, prop):
        if self._raise:
            raise RuntimeError("simulated KB lookup error")
        return list(self._by_key.get((entity, prop), []))


class _StubMeta:
    def __init__(self, *, routing_hint="python", premise_properties=None,
                 kb_property=None, single_valued=False):
        self.routing_hint = routing_hint
        self.premise_properties = premise_properties
        self.kb_property = kb_property
        self.single_valued = single_valued


class _StubPT:
    def __init__(self, meta_by_pred):
        self._by_pred = meta_by_pred

    def consult(self, predicate, kb_namespace=None):
        meta = self._by_pred.get(predicate)
        if meta is None:
            raise KeyError(predicate)
        return meta


class _StubSubstrate:
    def __init__(self, pt):
        self.predicate_translation = pt


class _MockTransport:
    """Returns a fixed code blob for the python verifier; records every
    codegen invocation so a test can assert the verifier was (or was NOT)
    asked to assert on a given input (gate c)."""

    def __init__(self, code):
        self._code = code
        self.calls = 0

    def extract_with_tool(self, *a, purpose=None, **kw):
        self.calls += 1
        return {"code": self._code, "reasoning": "mock"}

    def chat(self, *a, **kw):
        return ""


def _python_verifier(code):
    return PythonVerifier(llm_client=LLMClient(_transport=_MockTransport(code)))


# A 4-arg comparison: subject birth year < object birth year, None on a
# missing premise (the canonical born_before codegen shape).
_BORN_BEFORE_CODE = (
    "def verify(s, p, o, premises):\n"
    "    sub = premises.get('subject')\n"
    "    obj = premises.get('object')\n"
    "    if not sub or not obj:\n"
    "        return None\n"
    "    return int(sub['value']) < int(obj['value'])\n"
)


def _make_walker(*, kb=None, resolver=None, meta_by_pred=None, python_verifier=None):
    resolver = resolver if resolver is not None else _StubResolver()
    kb_verifier = _StubKBVerifier(resolver)
    kb = kb if kb is not None else _StubKB()
    substrate = _StubSubstrate(_StubPT(meta_by_pred or {}))
    return Walker(
        tier_u=None,
        kb_verifier=kb_verifier,
        python_verifier=python_verifier,
        substrate=substrate,
        kb=kb,
    )


def _born_before_walker(code=_BORN_BEFORE_CODE, *, kb, resolver, predicate="born_before"):
    """A walker wired for a premise->python comparison channel, returning
    (walker, transport) so the test can inspect codegen invocations (gate c).
    `predicate` defaults to born_before; tests that must reach the LLM-codegen
    path (gate d) pass a NEUTRAL predicate the WS6 deterministic front-end does
    not recognize, so the deterministic verdict does not preempt the codegen."""
    transport = _MockTransport(code)
    pv = PythonVerifier(llm_client=LLMClient(_transport=transport))
    w = _make_walker(
        kb=kb, resolver=resolver, python_verifier=pv,
        meta_by_pred={predicate: _StubMeta(
            premise_properties={"subject": "P569", "object": "P569"})},
    )
    return w, transport


def _claim(subject="Newton", predicate="born_before", object_val="Einstein", polarity=1):
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


def _trace():
    return JustificationTrace(
        root=TraceNode(node_type="claim", content={}),
        source_breakdown={"tier_u": 0, "kb": 0, "python": 0},
    )


def _date_stmt(year, rank="normal"):
    return Statement(value=f"+{year}-01-01T00:00:00Z", value_type="date", rank=rank)


# ===========================================================================
# PythonVerifier.verify(premises=...) — task item (1)
# ===========================================================================

class TestPythonVerifierPremisesArg:
    def test_premises_none_reproduces_three_arg_behavior(self):
        # Legacy 3-arg generated code, premises=None: called with exactly 3 args.
        pv = _python_verifier("def verify(s, p, o): return s == o")
        r = pv.verify(_claim("a", "equals", "a"))
        assert r.verdict == "verified"
        assert "premises" not in r.inputs

    def test_premises_threaded_into_four_arg_verify(self):
        # A 4-arg verify() reads the fetched premise years and compares them.
        code = (
            "def verify(s, p, o, premises):\n"
            "    sub = premises.get('subject')\n"
            "    obj = premises.get('object')\n"
            "    if not sub or not obj:\n"
            "        return None\n"
            "    return int(sub['value']) < int(obj['value'])\n"
        )
        pv = _python_verifier(code)
        premises = {
            "subject": {"value": "1643", "source": "kb", "kb_property": "P569"},
            "object": {"value": "1879", "source": "kb", "kb_property": "P569"},
        }
        r = pv.verify(_claim(), premises=premises)
        assert r.verdict == "verified"
        assert r.inputs["premises"] == premises

    def test_missing_premise_returns_none_abstain(self):
        # GATE c: the generated code stays None-eligible — a missing premise
        # yields None (abstain), never a fabricated verdict.
        code = (
            "def verify(s, p, o, premises):\n"
            "    sub = premises.get('subject')\n"
            "    obj = premises.get('object')\n"
            "    if not sub or not obj:\n"
            "        return None\n"
            "    return int(sub['value']) < int(obj['value'])\n"
        )
        pv = _python_verifier(code)
        r = pv.verify(_claim(), premises={"subject": {"value": "1643"}})
        assert r.verdict == "no_terminal_result"

    def test_non_dict_premises_treated_as_no_premises(self):
        pv = _python_verifier("def verify(s, p, o): return True")
        r = pv.verify(_claim(), premises=["not", "a", "dict"])
        assert r.verdict == "verified"
        assert "premises" not in r.inputs


# ===========================================================================
# Walker._gather_python_premises — task item (2)
# ===========================================================================

class TestGatherPythonPremises:
    def test_no_premise_properties_behaves_as_today(self):
        # No premise_properties on the metadata -> ({}, [], False): no fetch.
        w = _make_walker(meta_by_pred={"equals": _StubMeta(premise_properties=None)})
        result = w._gather_python_premises(_claim("a", "equals", "a"), _ctx())
        assert result == ({}, [], False)

    def test_consult_miss_behaves_as_today(self):
        # A metadata consult failure must NOT block the plain python path.
        w = _make_walker(meta_by_pred={})  # consult raises KeyError
        result = w._gather_python_premises(_claim("a", "equals", "a"), _ctx())
        assert result == ({}, [], False)

    def test_both_slots_fetched_from_kb(self):
        kb = _StubKB({
            ("Q935", "P569"): [_date_stmt(1643)],   # Newton
            ("Q937", "P569"): [_date_stmt(1879)],   # Einstein
        })
        resolver = _StubResolver({"Newton": "Q935", "Einstein": "Q937"})
        w = _make_walker(
            kb=kb, resolver=resolver,
            meta_by_pred={"born_before": _StubMeta(
                premise_properties={"subject": "P569", "object": "P569"})},
        )
        premises, literals, assertion = w._gather_python_premises(_claim(), _ctx())
        assert premises["subject"]["value"] == "1643"
        assert premises["object"]["value"] == "1879"
        assert premises["subject"]["kb_property"] == "P569"
        assert len(literals) == 2
        # KB premises are externally grounded.
        assert assertion is False
        assert all(not l.assertion for l in literals)

    def test_literal_object_slot_only_subject_fetched(self):
        # founded_before: only the subject is a fetchable entity; the object is a
        # literal year (no premise property), so only the subject is fetched.
        kb = _StubKB({("Q95", "P571"): [_date_stmt(1998)]})  # Google
        resolver = _StubResolver({"Google": "Q95"})
        w = _make_walker(
            kb=kb, resolver=resolver,
            meta_by_pred={"founded_before": _StubMeta(
                premise_properties={"subject": "P571"})},
        )
        claim = _claim("Google", "founded_before", "1800")
        premises, literals, assertion = w._gather_python_premises(claim, _ctx())
        assert set(premises) == {"subject"}
        assert premises["subject"]["value"] == "1998"
        assert len(literals) == 1

    def test_fail_closed_on_unresolved_premise_slot(self):
        # GATE b: a declared premise slot that does not resolve -> None (abstain).
        kb = _StubKB({("Q935", "P569"): [_date_stmt(1643)]})
        resolver = _StubResolver({"Newton": "Q935"})  # Einstein unresolved
        w = _make_walker(
            kb=kb, resolver=resolver,
            meta_by_pred={"born_before": _StubMeta(
                premise_properties={"subject": "P569", "object": "P569"})},
        )
        assert w._gather_python_premises(_claim(), _ctx()) is None

    def test_fail_closed_on_missing_kb_value(self):
        # GATE b: a resolved slot whose KB property has no usable value -> None.
        kb = _StubKB({
            ("Q935", "P569"): [_date_stmt(1643)],
            ("Q937", "P569"): [],  # Einstein resolves but no birth date
        })
        resolver = _StubResolver({"Newton": "Q935", "Einstein": "Q937"})
        w = _make_walker(
            kb=kb, resolver=resolver,
            meta_by_pred={"born_before": _StubMeta(
                premise_properties={"subject": "P569", "object": "P569"})},
        )
        assert w._gather_python_premises(_claim(), _ctx()) is None

    def test_fail_closed_on_kb_lookup_error(self):
        # GATE b: a KB lookup exception is fail-closed (None), never fabricated.
        kb = _StubKB(raise_on_lookup=True)
        resolver = _StubResolver({"Newton": "Q935", "Einstein": "Q937"})
        w = _make_walker(
            kb=kb, resolver=resolver,
            meta_by_pred={"born_before": _StubMeta(
                premise_properties={"subject": "P569", "object": "P569"})},
        )
        assert w._gather_python_premises(_claim(), _ctx()) is None


# ===========================================================================
# Walker._premise_value_from_statements
# ===========================================================================

class TestPremiseValueFromStatements:
    def test_empty_is_none(self):
        assert Walker._premise_value_from_statements([]) is None

    def test_single_date_normalized_to_year(self):
        assert Walker._premise_value_from_statements([_date_stmt(1643)]) == "1643"

    def test_preferred_wins(self):
        stmts = [_date_stmt(1643), _date_stmt(1700, rank="preferred")]
        assert Walker._premise_value_from_statements(stmts) == "1700"

    def test_conflicting_no_preferred_abstains(self):
        # Two disagreeing values, none preferred -> None (never pick arbitrarily).
        assert Walker._premise_value_from_statements(
            [_date_stmt(1643), _date_stmt(1644)]
        ) is None

    def test_agreeing_values_collapse(self):
        assert Walker._premise_value_from_statements(
            [_date_stmt(1643), _date_stmt(1643)]
        ) == "1643"

    def test_deprecated_skipped(self):
        stmts = [_date_stmt(1900, rank="deprecated"), _date_stmt(1643)]
        assert Walker._premise_value_from_statements(stmts) == "1643"


# ===========================================================================
# Walker._record_python_premise_term — task items (3)+(4a)
# ===========================================================================

class TestRecordPythonPremiseTerm:
    def test_no_premises_is_plain_python_literal(self):
        # Back-compat: no fetched premises -> a single plain python OR-literal.
        w = _make_walker()
        trace = _trace()
        w._record_python_premise_term(trace, [], assertion=False)
        lits = trace.provenance.literals()
        assert len(lits) == 1
        assert lits[0].source == "python"
        assert not lits[0].assertion

    def test_premises_recorded_as_and_children(self):
        # GATE 3: the python literal AND every premise literal are conjoined, so
        # the retraction footprint includes every premise row.
        w = _make_walker()
        trace = _trace()
        premise_lits = [
            ProvenanceLiteral(source="kb", table="entity_resolution_cache", row_id=5),
            ProvenanceLiteral(source="kb", table="entity_resolution_cache", row_id=6),
        ]
        w._record_python_premise_term(trace, premise_lits, assertion=False)
        # One alternative, op='and', python + 2 premise literals.
        assert len(trace.provenance.children) == 1
        and_term = trace.provenance.children[0]
        assert and_term.op == "and"
        sources = sorted(l.source for l in and_term.literals())
        assert sources == ["kb", "kb", "python"]
        rows = and_term.source_rows()
        assert ("entity_resolution_cache", 5) in rows
        assert ("entity_resolution_cache", 6) in rows

    def test_assertion_premise_forces_chain_flag(self):
        # GATE a: an assertion-conditional premise propagates onto the python
        # literal so chain_includes_assertion fires (-> *_given_assertion).
        w = _make_walker()
        trace = _trace()
        premise_lits = [
            ProvenanceLiteral(source="tier_u", table="tier_u", row_id=9,
                              status="asserted_unverified", assertion=True),
        ]
        w._record_python_premise_term(trace, premise_lits, assertion=True)
        assert trace.chain_includes_assertion is True
        # The python literal itself carries the assertion flag.
        and_term = trace.provenance.children[0]
        py = [l for l in and_term.literals() if l.source == "python"][0]
        assert py.assertion is True


# ===========================================================================
# End-to-end soundness gates through _try_external_grounding +
# _apply_assertion_designation — the task's gates (a)-(e). These exercise the
# full python branch of the walker (resolve premise slots -> fetch KB ->
# thread into mock codegen -> compute -> verdict + provenance), not just the
# helpers, so the OBSERVABLE verdict family and retraction footprint are pinned.
# ===========================================================================

class TestPremiseToPythonChannelEndToEnd:
    def _newton_einstein_kb(self):
        # Newton 1643 < Einstein 1879 -> born_before holds.
        kb = _StubKB({
            ("Q935", "P569"): [_date_stmt(1643)],
            ("Q937", "P569"): [_date_stmt(1879)],
        })
        resolver = _StubResolver({"Newton": "Q935", "Einstein": "Q937"})
        return kb, resolver

    def test_gate_a_grounded_premises_plain_verified_with_and_term(self):
        # GATE (a): premises resolve from EXTERNALLY-GROUNDED KB statements,
        # mock codegen computes True -> VERIFIED (PLAIN, no chain flag), and the
        # ProvenanceTerm carries an AND-child per premise (retraction footprint).
        kb, resolver = self._newton_einstein_kb()
        w, transport = _born_before_walker(kb=kb, resolver=resolver)
        trace = _trace()
        verdict, source, llm, grounding = w._try_external_grounding(
            _claim(), _ctx(), trace
        )
        assert verdict == "verified"
        assert source == "python"
        # KB premises are externally grounded -> the verdict is PLAIN (the gate
        # that an asserted premise would have flipped, here stays off).
        assert trace.chain_includes_assertion is False
        designated = _apply_assertion_designation(verdict, trace)
        assert designated == "verified"  # NOT verified_given_assertion
        # The provenance is one AND-alternative: python + both KB premises.
        assert len(trace.provenance.children) == 1
        and_term = trace.provenance.children[0]
        assert and_term.op == "and"
        assert sorted(l.source for l in and_term.literals()) == ["kb", "kb", "python"]
        assert grounding["premise_count"] == 2

    def test_gate_b_asserted_premise_forces_given_assertion(self):
        # GATE (b): the SAME comparison, but a premise rests on an
        # asserted_unverified Tier-U row -> the verdict is chain-flagged
        # (*_given_assertion), NEVER a plain verified. The gather path here
        # fetches KB (assertion=False); the asserted-premise propagation is the
        # downstream wiring (_record_python_premise_term -> chain flag ->
        # _apply_assertion_designation), so pin it on a hand-built assertion
        # premise that mirrors what a Tier-U premise source would feed.
        kb, resolver = self._newton_einstein_kb()
        w, _transport = _born_before_walker(kb=kb, resolver=resolver)
        trace = _trace()
        # A python verdict derived over an asserted_unverified premise row.
        asserted_premise = [
            ProvenanceLiteral(source="tier_u", table="tier_u", row_id=42,
                              status="asserted_unverified", assertion=True),
        ]
        w._record_python_premise_term(trace, asserted_premise, assertion=True)
        # The base python computation verified; the asserted premise forces the
        # *_given_assertion dual — the verdict is NEVER laundered to plain.
        assert trace.chain_includes_assertion is True
        assert _apply_assertion_designation("verified", trace) == "verified_given_assertion"
        assert _apply_assertion_designation("contradicted", trace) == "contradicted_given_assertion"

    def test_gate_c_unresolved_premise_abstains_codegen_never_asked(self):
        # GATE (c) FAIL-CLOSED: a referenced premise that cannot be resolved ->
        # abstain (no terminal verdict; the walk's outer fallthrough yields
        # no_grounding_found), and the mock codegen is NEVER even asked to
        # assert on the missing input.
        kb = _StubKB({("Q935", "P569"): [_date_stmt(1643)]})
        resolver = _StubResolver({"Newton": "Q935"})  # Einstein unresolved
        w, transport = _born_before_walker(kb=kb, resolver=resolver)
        trace = _trace()
        verdict, source, llm, grounding = w._try_external_grounding(
            _claim(), _ctx(), trace
        )
        # Fail-closed: the python branch returns no terminal verdict.
        assert verdict is None
        # The verifier (and thus codegen) was never invoked on a fabricated input.
        assert transport.calls == 0
        # No provenance alternative was recorded; the chain flag stays off.
        assert trace.provenance.children == []
        # The walk's fallthrough designates this as a plain abstain.
        assert _apply_assertion_designation("no_grounding_found", trace) == "no_grounding_found"

    def test_gate_d_codegen_none_abstains(self):
        # GATE (d): the generated code returns None (insufficient inputs even
        # though premises resolved) -> no terminal verdict -> abstain. Premises
        # resolve fine; the code itself honestly abstains.
        none_code = "def verify(s, p, o, premises): return None\n"
        kb, resolver = self._newton_einstein_kb()
        # NEUTRAL predicate: the WS6 deterministic front-end does not recognize
        # 'premise_compare', so the verdict comes from the codegen (None) under
        # test, not the deterministic year-ordering path.
        w, transport = _born_before_walker(
            none_code, kb=kb, resolver=resolver, predicate="premise_compare")
        trace = _trace()
        verdict, source, llm, grounding = w._try_external_grounding(
            _claim(predicate="premise_compare"), _ctx(), trace
        )
        assert verdict is None
        # Codegen WAS invoked (premises resolved) but returned None -> abstain.
        assert transport.calls == 1

    def test_gate_d_codegen_raises_abstains(self):
        # GATE (d): the generated code raises -> sandbox failure ->
        # no_terminal_result -> no terminal walker verdict (abstain).
        boom_code = "def verify(s, p, o, premises): raise ValueError('boom')\n"
        kb, resolver = self._newton_einstein_kb()
        w, transport = _born_before_walker(
            boom_code, kb=kb, resolver=resolver, predicate="premise_compare")
        trace = _trace()
        verdict, source, llm, grounding = w._try_external_grounding(
            _claim(predicate="premise_compare"), _ctx(), trace
        )
        assert verdict is None

    def test_gate_e_back_compat_self_contained_claim(self):
        # GATE (e) back-compat: a self-contained claim ("100 greater_than 50")
        # whose predicate declares NO premise_properties takes the premises=None
        # path and behaves EXACTLY as the pre-WS3b python verify — legacy 3-arg
        # codegen, no premises threaded.
        code = "def verify(s, p, o): return int(s) > int(o)"
        transport = _MockTransport(code)
        pv = PythonVerifier(llm_client=LLMClient(_transport=transport))
        w = _make_walker(
            python_verifier=pv,
            meta_by_pred={"greater_than": _StubMeta(premise_properties=None)},
        )
        trace = _trace()
        verdict, source, llm, grounding = w._try_external_grounding(
            _claim("100", "greater_than", "50"), _ctx(), trace
        )
        assert verdict == "verified"
        assert source == "python"
        # No premises were fetched -> grounding records a zero premise count and
        # the provenance is a single PLAIN python literal (the historical shape).
        assert grounding["premise_count"] == 0
        assert len(trace.provenance.children) == 1
        plain = trace.provenance.children[0]
        assert plain.op == "lit"
        assert plain.literal.source == "python"
        assert plain.literal.assertion is False
        assert trace.chain_includes_assertion is False

    def test_gate_e_contradiction_self_contained(self):
        # GATE (e): the pre-WS3b contradiction path is unaffected — "5 > 9"
        # deterministically does not hold -> contradicted (plain).
        code = "def verify(s, p, o): return int(s) > int(o)"
        transport = _MockTransport(code)
        pv = PythonVerifier(llm_client=LLMClient(_transport=transport))
        w = _make_walker(
            python_verifier=pv,
            meta_by_pred={"greater_than": _StubMeta(premise_properties=None)},
        )
        trace = _trace()
        verdict, _source, _llm, _grounding = w._try_external_grounding(
            _claim("5", "greater_than", "9"), _ctx(), trace
        )
        assert verdict == "contradicted"
        assert trace.chain_includes_assertion is False
