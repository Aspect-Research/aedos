"""Tests for KBVerifier — verify, contradict, no_match, scope, polarity.

Fix-up note (C1, M4): claims now carry natural-language objects which the
verifier resolves through the entity resolver, rather than pre-resolved
Q-numbers fed straight in (the rigged inputs the audit flagged in M4).
"""

from __future__ import annotations

import pytest

from aedos.database import open_memory_db
from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer3_substrate.predicate_translation import PredicateTranslation
from aedos.layer3_substrate.resolver import EntityResolver
from aedos.layer4_sources.kb_protocol import (
    LocalContext, ResolutionCandidate, Statement, SubsumptionResult
)
from aedos.layer4_sources.kb_verifier import KBVerdict, KBVerdictType, KBVerifier
from aedos.llm.client import LLMClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockTransport:
    def __init__(self, routing_hint="kb_resolvable", kb_property="P39",
                 object_type="entity", single_valued=0, slot_to_qualifier=None):
        self._hint = routing_hint
        self._prop = kb_property
        self._object_type = object_type
        self._single_valued = single_valued
        self._sq = slot_to_qualifier

    def extract_with_tool(self, *a, **kw):
        return {
            "object_type": self._object_type,
            "user_subject_required": 0,
            "distinct_slots": None,
            "routing_hint": self._hint,
            "kb_namespace": "wikidata" if self._hint == "kb_resolvable" else None,
            "kb_property": self._prop if self._hint == "kb_resolvable" else None,
            "slot_to_qualifier": self._sq,
            "single_valued": self._single_valued,
            "reason": "test",
        }

    def chat(self, *a, **kw):
        return ""


# Natural-language reference -> Wikidata Q-number, as the resolver would produce.
_DEFAULT_RESOLUTIONS = {
    "Obama": "Q76",
    "President of the United States": "Q11696",
    "United States Senator": "Q4416090",
    "Honolulu": "Q18094",
    "New York City": "Q60",
}


class MockKB:
    """KB whose resolve_entity maps known references to Q-numbers."""

    def __init__(self, statements: list[Statement], resolutions: dict | None = None):
        self._statements = statements
        self._resolutions = dict(_DEFAULT_RESOLUTIONS)
        if resolutions:
            self._resolutions.update(resolutions)

    def resolve_entity(self, reference, local_context):
        qid = self._resolutions.get(reference)
        if qid is None:
            return []
        return [ResolutionCandidate(kb_identifier=qid, score=0.9)]

    def lookup_statements(self, entity, predicate):
        return list(self._statements)

    def subsumption(self, entity_a, entity_b, relation_type):
        return SubsumptionResult(verdict="unrelated")


def _make_verifier(statements, routing_hint="kb_resolvable", kb_property="P39",
                   object_type="entity", single_valued=0, resolutions=None,
                   slot_to_qualifier=None):
    db = open_memory_db()
    transport = MockTransport(routing_hint, kb_property, object_type, single_valued,
                              slot_to_qualifier)
    client = LLMClient(_transport=transport)
    pt = PredicateTranslation(db=db, llm_client=client)
    kb = MockKB(statements, resolutions)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    return KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)


def _claim(subject="Obama", predicate="holds_role",
           object_val="President of the United States", polarity=1,
           valid_from=None, valid_until=None):
    return Claim(
        claim_id="c1",
        subject=subject,
        predicate=predicate,
        object=object_val,
        polarity=polarity,
        source_text="test",
        asserting_party="user_test",
        triage_decision=TriageDecision.VERIFY,
        valid_from=valid_from,
        valid_until=valid_until,
    )


# ---------------------------------------------------------------------------
# TestKBVerdictDataclass
# ---------------------------------------------------------------------------

class TestKBVerdictDataclass:
    def test_fields_present(self):
        v = KBVerdict(verdict=KBVerdictType.VERIFIED)
        assert v.verdict == KBVerdictType.VERIFIED
        assert v.matched_statement is None
        assert v.subject_kb_id is None
        assert v.trace == {}


# ---------------------------------------------------------------------------
# TestKBVerifierVerify
# ---------------------------------------------------------------------------

class TestKBVerifierVerify:
    def test_verified_when_value_matches(self):
        stmts = [Statement(value="Q11696", value_type="entity")]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim())
        assert result.verdict == KBVerdictType.VERIFIED

    def test_verified_returns_matched_statement(self):
        stmt = Statement(value="Q11696", value_type="entity")
        verifier = _make_verifier([stmt])
        result = verifier.verify(_claim())
        assert result.matched_statement is not None
        assert result.matched_statement.value == "Q11696"

    def test_verified_sets_subject_kb_id(self):
        stmts = [Statement(value="Q11696", value_type="entity")]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim())
        assert result.subject_kb_id == "Q76"

    def test_no_match_when_no_statements(self):
        verifier = _make_verifier([])
        result = verifier.verify(_claim())
        assert result.verdict == KBVerdictType.NO_MATCH

    def test_no_kb_path_when_not_kb_resolvable(self):
        stmts = [Statement(value="Q11696", value_type="entity")]
        verifier = _make_verifier(stmts, routing_hint="user_authoritative")
        result = verifier.verify(_claim())
        assert result.verdict == KBVerdictType.NO_KB_PATH

    def test_no_kb_path_when_predicate_translation_fails(self):
        db = open_memory_db()

        class FailingTransport:
            def extract_with_tool(self, *a, **kw): raise RuntimeError("fail")
            def chat(self, *a, **kw): return ""

        pt = PredicateTranslation(db=db, llm_client=LLMClient(_transport=FailingTransport()))
        kb = MockKB([])
        resolver = EntityResolver(kb_protocol=kb, db=db)
        verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
        result = verifier.verify(_claim())
        assert result.verdict == KBVerdictType.NO_KB_PATH

    def test_subject_resolution_failure_is_no_match(self):
        # An unknown subject reference resolves to nothing.
        stmts = [Statement(value="Q11696", value_type="entity")]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim(subject="Some Unknown Person Xyzzy"))
        assert result.verdict == KBVerdictType.NO_MATCH


# ---------------------------------------------------------------------------
# TestKBVerifierObjectResolution  (M4: object entities are resolved, not
# string-compared against KB Q-numbers)
# ---------------------------------------------------------------------------

class TestKBVerifierObjectResolution:
    def test_natural_language_object_is_resolved(self):
        # The claim object is a natural-language reference; the verifier must
        # resolve it to a Q-number before comparing against the KB statement.
        stmts = [Statement(value="Q11696", value_type="entity")]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim(object_val="President of the United States"))
        assert result.verdict == KBVerdictType.VERIFIED
        assert result.trace.get("value_resolved") is True
        assert result.trace.get("value_entity") == "Q11696"

    def test_object_resolution_failure_falls_back_to_literal_compare(self):
        # When the object does not resolve to an entity, the verifier falls
        # back to a case-insensitive literal comparison.
        stmts = [Statement(value="potus", value_type="literal")]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim(object_val="POTUS"))
        assert result.verdict == KBVerdictType.VERIFIED
        assert result.trace.get("value_resolved") is False


# ---------------------------------------------------------------------------
# TestKBVerifierSingleValued  (M4: only functional predicates contradict on a
# value mismatch; multi-valued predicates yield no_match)
# ---------------------------------------------------------------------------

class TestKBVerifierSingleValued:
    def test_multivalued_mismatch_is_no_match(self):
        # holds_role / P39 is multi-valued: a person holds many positions, so a
        # statement for a different position does NOT contradict the claim.
        stmts = [Statement(value="Q4416090", value_type="entity")]  # Senator
        verifier = _make_verifier(stmts, single_valued=0)
        result = verifier.verify(_claim(object_val="President of the United States"))
        assert result.verdict == KBVerdictType.NO_MATCH

    def test_single_valued_mismatch_is_contradicted(self):
        # born_in / P19 is functional: a person has one birthplace, so a
        # statement for a different place contradicts the claim. This is the
        # N1 "legitimate contradiction" path — the object "New York City"
        # resolves to a real (different) Q-number, so CONTRADICTED is correct.
        stmts = [Statement(value="Q18094", value_type="entity")]  # Honolulu
        verifier = _make_verifier(stmts, kb_property="P19", single_valued=1)
        result = verifier.verify(_claim(predicate="born_in", object_val="New York City"))
        assert result.verdict == KBVerdictType.CONTRADICTED
        # N1: a functional-predicate contradiction only fires when the object
        # actually resolved. Contrast TestKBVerifierN1ResolutionFailure below.
        assert result.trace.get("value_resolved") is True

    def test_single_valued_match_is_verified(self):
        stmts = [Statement(value="Q18094", value_type="entity")]
        verifier = _make_verifier(stmts, kb_property="P19", single_valued=1)
        result = verifier.verify(_claim(predicate="born_in", object_val="Honolulu"))
        assert result.verdict == KBVerdictType.VERIFIED


# ---------------------------------------------------------------------------
# TestKBVerifierN1ResolutionFailure  (N1: a functional predicate whose object
# reference fails to resolve must abstain, not contradict — resolution failure
# is a false-abstain source per architecture 3.2, never a false-contradiction
# source. The trace records why the verdict abstained.)
# ---------------------------------------------------------------------------

class TestKBVerifierN1ResolutionFailure:
    def test_single_valued_unresolved_object_is_no_match(self):
        # born_in is functional, but "Some Unknown Place Xyzzy" does not resolve
        # to a KB entity. Comparing an unresolved string against KB Q-numbers
        # never matches — that non-match is a resolution failure, not evidence
        # the claim is false. Pre-N1 this returned CONTRADICTED.
        stmts = [Statement(value="Q18094", value_type="entity")]  # Honolulu
        verifier = _make_verifier(stmts, kb_property="P19", single_valued=1)
        result = verifier.verify(
            _claim(predicate="born_in", object_val="Some Unknown Place Xyzzy")
        )
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.trace.get("value_resolved") is False
        assert result.trace.get("abstention_reason") == "value_unresolved"

    def test_negated_single_valued_unresolved_object_stays_no_match(self):
        # NO_MATCH from a resolution failure is polarity-invariant: a negated
        # claim with an unresolved object is still an abstention, not a verified.
        stmts = [Statement(value="Q18094", value_type="entity")]
        verifier = _make_verifier(stmts, kb_property="P19", single_valued=1)
        result = verifier.verify(
            _claim(predicate="born_in", object_val="Some Unknown Place Xyzzy", polarity=0)
        )
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.trace.get("abstention_reason") == "value_unresolved"

    def test_no_statements_trace_records_reason(self):
        # The trace distinguishes a resolution failure ("value_unresolved")
        # from a genuine absence of evidence ("no_statements").
        verifier = _make_verifier([])
        result = verifier.verify(_claim())
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.trace.get("abstention_reason") == "no_statements"

    def test_multivalued_no_match_trace_records_reason(self):
        # A multi-valued predicate whose object resolved but matched nothing
        # abstains with the "no_matching_statement" reason.
        stmts = [Statement(value="Q4416090", value_type="entity")]  # Senator
        verifier = _make_verifier(stmts, single_valued=0)
        result = verifier.verify(_claim(object_val="President of the United States"))
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.trace.get("abstention_reason") == "no_matching_statement"


# ---------------------------------------------------------------------------
# TestKBVerifierPolarity  (C1: the verifier honors claim polarity)
# ---------------------------------------------------------------------------

class TestKBVerifierPolarity:
    def test_negated_claim_kb_supports_positive_is_contradicted(self):
        # The C1 audit case: a negated claim whose positive form the KB
        # supports must be CONTRADICTED, not VERIFIED. The object "POTUS" does
        # not resolve, so the positive form matches the literal statement value
        # both before and after the fix — isolating the polarity defect.
        stmts = [Statement(value="potus", value_type="literal")]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim(object_val="POTUS", polarity=0))
        assert result.verdict == KBVerdictType.CONTRADICTED

    def test_negated_claim_no_statements_stays_no_match(self):
        # NO_MATCH is polarity-invariant: absence of a statement is not evidence
        # that the negation holds.
        verifier = _make_verifier([])
        result = verifier.verify(_claim(polarity=0))
        assert result.verdict == KBVerdictType.NO_MATCH

    def test_negated_claim_functional_mismatch_is_verified(self):
        # "Obama was NOT born in New York City" — the KB has him born in
        # Honolulu (functional predicate), so the negated claim is VERIFIED.
        stmts = [Statement(value="Q18094", value_type="entity")]  # Honolulu
        verifier = _make_verifier(stmts, kb_property="P19", single_valued=1)
        result = verifier.verify(_claim(predicate="born_in", object_val="New York City", polarity=0))
        assert result.verdict == KBVerdictType.VERIFIED

    def test_asserted_claim_value_match_still_verified(self):
        # Sanity: polarity=1 is unchanged by the polarity logic.
        stmts = [Statement(value="Q11696", value_type="entity")]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim(polarity=1))
        assert result.verdict == KBVerdictType.VERIFIED

    def test_trace_records_polarity(self):
        stmts = [Statement(value="Q11696", value_type="entity")]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim(polarity=0))
        assert result.trace.get("polarity") == 0
        assert result.trace.get("positive_verdict") == "verified"


# ---------------------------------------------------------------------------
# TestKBVerifierTemporalScope
# ---------------------------------------------------------------------------

class TestKBVerifierTemporalScope:
    def test_scope_compatible_no_qualifiers(self):
        stmts = [Statement(value="Q11696", value_type="entity")]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim(valid_from="2010-01-01", valid_until="2016-01-01"))
        assert result.verdict == KBVerdictType.VERIFIED

    def test_scope_compatible_with_matching_qualifiers(self):
        stmts = [Statement(
            value="Q11696", value_type="entity",
            qualifiers={"P580": "2009-01-20", "P582": "2017-01-20"}
        )]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim(valid_from="2010-01-01", valid_until="2016-01-01"))
        assert result.verdict == KBVerdictType.VERIFIED

    def test_scope_mismatch_returns_no_match(self):
        # Claim says 2005 but statement only starts 2009 -> scope incompatible.
        stmts = [Statement(
            value="Q11696", value_type="entity",
            qualifiers={"P580": "2009-01-20"}
        )]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim(valid_from="2005-01-01"))
        assert result.verdict == KBVerdictType.NO_MATCH


# ---------------------------------------------------------------------------
# TestKBVerifierCaseInsensitive
# ---------------------------------------------------------------------------

class TestKBVerifierCaseInsensitive:
    def test_case_insensitive_literal_match(self):
        # The bare object "President" does not resolve to an entity; the
        # verifier falls back to a case-insensitive literal comparison.
        stmts = [Statement(value="president", value_type="literal")]
        verifier = _make_verifier(stmts, kb_property="P39")
        result = verifier.verify(_claim(object_val="President"))
        assert result.verdict == KBVerdictType.VERIFIED


# ---------------------------------------------------------------------------
# TestKBVerifierInverseMapping  (D19: the verifier honors slot_to_qualifier;
# an uninterpretable mapping abstains cleanly rather than guessing or crashing.
# Behavioral coverage of the inverse seed predicates lives in the integration
# test tests/integration/test_inverse_predicate_kb.py.)
# ---------------------------------------------------------------------------

class TestKBVerifierInverseMapping:
    def test_unsupported_slot_to_qualifier_is_no_kb_path(self):
        # A slot_to_qualifier the verifier cannot interpret — here the subject
        # is mapped to a qualifier, which is neither statement_subject nor
        # statement_value. _lookup_targets cannot decide a direction, so verify
        # abstains with NO_KB_PATH and a clear trace note. It must NOT crash
        # (no NotImplementedError) and must NOT silently guess the standard
        # direction. Pre-D19 the verifier ignored slot_to_qualifier entirely
        # and would have returned VERIFIED here.
        stmts = [Statement(value="Q11696", value_type="entity")]
        verifier = _make_verifier(
            stmts,
            slot_to_qualifier={"subject": "qualifier:P580", "object": "statement_value"},
        )
        result = verifier.verify(_claim())
        assert result.verdict == KBVerdictType.NO_KB_PATH
        assert result.trace.get("reason") == "unsupported_slot_to_qualifier"


# ---------------------------------------------------------------------------
# TestKBVerifierValueTypeGuard  (S3 generalization: a value mismatch only
# contradicts when the KB statement's datatype is compatible with the
# predicate's object_type. A predicate mis-mapped to a wrong-datatype property
# abstains instead of fabricating a contradiction — this is the general
# replacement for the hand-curated published→P50 row.)
# ---------------------------------------------------------------------------

class TestKBVerifierValueTypeGuard:
    def test_type_mismatched_statement_abstains_not_contradicts(self):
        # single_valued entity predicate, but the looked-up statement is a DATE
        # (as it would be if the oracle mis-mapped the predicate to P585 point-
        # in-time). The expected value resolves (Honolulu→Q18094) so this is not
        # a resolution-failure abstain; without the guard the functional branch
        # would return CONTRADICTED on the entity-vs-date mismatch.
        stmts = [Statement(value="1869-01-01T00:00:00Z", value_type="date")]
        verifier = _make_verifier(
            stmts, object_type="entity", single_valued=1, kb_property="P585"
        )
        result = verifier.verify(_claim(object_val="Honolulu"))
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.trace.get("abstention_reason") == "value_type_object_type_mismatch"

    def test_compatible_type_still_contradicts(self):
        # Control: same functional entity predicate, but the statement value is
        # an ENTITY that doesn't match the (resolved) expected value. The guard
        # permits the contradiction — types are compatible.
        stmts = [Statement(value="Q60", value_type="entity")]  # New York City
        verifier = _make_verifier(
            stmts, object_type="entity", single_valued=1, kb_property="P19"
        )
        result = verifier.verify(_claim(object_val="Honolulu"))
        assert result.verdict == KBVerdictType.CONTRADICTED


# ---------------------------------------------------------------------------
# TestKBVerifierTimeObjectTypeGate  (v0.16 WS6 §A.4: the date→time
# reconciliation makes the _OBJECT_TYPE_COMPATIBLE_VALUE_TYPES gate
# 'time' → {date, literal} LIVE for the date predicates. A functional
# 'time' predicate may CONTRADICT only on a statement whose value_type is
# date or literal; an entity/quantity-typed statement abstains instead of
# fabricating a contradiction. Before §A.4 these rows carried object_type
# 'date', which keys to None in the gate map and short-circuited to
# "don't block" — so the gate was a no-op.)
# ---------------------------------------------------------------------------

class TestKBVerifierTimeObjectTypeGate:
    def test_time_predicate_contradicts_on_date_value_type(self):
        # born_on / P569 is functional + object_type='time'. The KB statement is
        # a DATE (1869) that does not match the claimed year (1879) → the
        # value-type gate permits the contradiction (time → {date, literal}).
        stmts = [Statement(value="1869-01-01T00:00:00Z", value_type="date")]
        verifier = _make_verifier(
            stmts, object_type="time", single_valued=1, kb_property="P569"
        )
        result = verifier.verify(
            _claim(predicate="born_on", object_val="1879")
        )
        assert result.verdict == KBVerdictType.CONTRADICTED

    def test_time_predicate_contradicts_on_literal_value_type(self):
        # literal is also in the allowed set for object_type='time'. A
        # non-matching literal statement on a functional time predicate
        # contradicts.
        stmts = [Statement(value="not-a-year-literal", value_type="literal")]
        verifier = _make_verifier(
            stmts, object_type="time", single_valued=1, kb_property="P569"
        )
        result = verifier.verify(
            _claim(predicate="born_on", object_val="1879")
        )
        assert result.verdict == KBVerdictType.CONTRADICTED

    def test_time_predicate_abstains_on_entity_value_type(self):
        # The KB statement is an ENTITY (as it would be if the time predicate
        # were mis-mapped to a wikibase-item property). entity ∉ {date,
        # literal} for object_type='time', so the functional-contradiction
        # branch is blocked: NO_MATCH with the value-type-mismatch reason, NOT
        # a fabricated CONTRADICTED. This is the gate the §A.4 reconciliation
        # turned on.
        stmts = [Statement(value="Q18094", value_type="entity")]  # Honolulu
        verifier = _make_verifier(
            stmts, object_type="time", single_valued=1, kb_property="P569"
        )
        result = verifier.verify(
            _claim(predicate="born_on", object_val="1879")
        )
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.trace.get("abstention_reason") == "value_type_object_type_mismatch"

    def test_time_predicate_abstains_on_quantity_value_type(self):
        # quantity ∉ {date, literal} for object_type='time' → also blocked.
        stmts = [Statement(value="42", value_type="quantity")]
        verifier = _make_verifier(
            stmts, object_type="time", single_valued=1, kb_property="P569"
        )
        result = verifier.verify(
            _claim(predicate="born_on", object_val="1879")
        )
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.trace.get("abstention_reason") == "value_type_object_type_mismatch"

    def test_time_predicate_verifies_on_matching_year(self):
        # Control: a matching date statement (year-aware compare) VERIFIES,
        # confirming the reconciliation did not break the positive path for the
        # date predicates.
        stmts = [Statement(value="1879-03-14T00:00:00Z", value_type="date")]
        verifier = _make_verifier(
            stmts, object_type="time", single_valued=1, kb_property="P569"
        )
        result = verifier.verify(
            _claim(predicate="born_on", object_val="1879")
        )
        assert result.verdict == KBVerdictType.VERIFIED


# ===========================================================================
# v0.16 WS1 — MULTI-PROPERTY BINDING ARBITRATION
#
# The substrate now holds a RANKED LIST of (predicate -> KB property) bindings;
# verify() loops them and arbitrates: VERIFIED if ANY binding grounds
# positively (recording every chain in trace['bindings_tried']); CONTRADICTED
# only from a single_valued binding that passes the value-type gate; else
# NO_MATCH/NO_KB_PATH. These tests construct multi-binding metadata directly
# and inject it via a stub PredicateTranslation.
# ===========================================================================

from aedos.layer3_substrate.predicate_translation import (
    PredicateBinding,
    PredicateMetadata,
)


class _StubPT:
    """A PredicateTranslation stand-in whose consult() returns a fixed
    PredicateMetadata, letting a test pin an exact multi-binding shape."""

    def __init__(self, meta: PredicateMetadata):
        self._meta = meta

    def consult(self, predicate, kb_namespace=None):
        return self._meta


class _MultiPropKB:
    """KB whose lookup_statements is keyed BY PROPERTY, so different bindings
    ground against different statement sets. subsumption is configurable for
    the value-type gate (copula) tests."""

    def __init__(self, statements_by_property, resolutions=None, subsumptions=None):
        self._by_prop = statements_by_property
        self._resolutions = dict(_DEFAULT_RESOLUTIONS)
        if resolutions:
            self._resolutions.update(resolutions)
        # (a_qid, b_qid, relation) -> verdict; default "unrelated".
        self._subsumptions = subsumptions or {}

    def resolve_entity(self, reference, local_context):
        qid = self._resolutions.get(reference)
        return [ResolutionCandidate(kb_identifier=qid, score=0.9)] if qid else []

    def lookup_statements(self, entity, predicate):
        return list(self._by_prop.get(predicate, []))

    def subsumption(self, entity_a, entity_b, relation_type):
        verdict = self._subsumptions.get((entity_a, entity_b, relation_type), "unrelated")
        return SubsumptionResult(verdict=verdict)


def _make_meta(predicate, bindings, object_type="entity"):
    return PredicateMetadata(
        id=1,
        aedos_predicate=predicate,
        object_type=object_type,
        user_subject_required=False,
        distinct_slots=None,
        routing_hint="kb_resolvable",
        kb_namespace=None,
        kb_property=None,
        slot_to_qualifier=None,
        reason="test",
        created_at="t",
        bindings=bindings,
    )


def _make_multi_verifier(kb, meta):
    db = open_memory_db()
    resolver = EntityResolver(kb_protocol=kb, db=db)
    return KBVerifier(
        kb_protocol=kb,
        entity_resolver=resolver,
        predicate_translation=_StubPT(meta),
    )


class TestKBVerifierMultiBindingArbitration:
    def test_any_binding_grounds_positively_is_verified(self):
        # Two bindings: P39 (the primary) finds NO matching statement; P106
        # (a candidate) DOES. VERIFIED wins because ANY binding grounds.
        kb = _MultiPropKB(
            statements_by_property={
                "P39": [Statement(value="Q4416090", value_type="entity")],  # wrong
                "P106": [Statement(value="Q11696", value_type="entity")],   # right
            }
        )
        meta = _make_meta("works_as", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P39", source="oracle"),
            PredicateBinding(kb_namespace="wikidata", kb_property="P106", source="ontology_p2302"),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(_claim(object_val="President of the United States"))
        assert result.verdict == KBVerdictType.VERIFIED

    def test_both_bindings_recorded_in_trace(self):
        # bindings_tried records EVERY chain attempted, even after VERIFIED is
        # decided, so the observability surface can show what was considered.
        kb = _MultiPropKB(
            statements_by_property={
                "P39": [Statement(value="Q4416090", value_type="entity")],
                "P106": [Statement(value="Q11696", value_type="entity")],
            }
        )
        meta = _make_meta("works_as", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P39", source="oracle"),
            PredicateBinding(kb_namespace="wikidata", kb_property="P106", source="ontology_p2302"),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(_claim(object_val="President of the United States"))
        tried = result.trace.get("bindings_tried")
        assert tried is not None
        props = {t["property"] for t in tried}
        assert props == {"P39", "P106"}
        # The winning binding's verdict appears among the tried set.
        verdicts = {t["property"]: t["verdict"] for t in tried}
        assert verdicts["P106"] == "verified"

    def test_positive_grounding_beats_a_contradiction(self):
        # Arbitration order (Decision 1): a positive grounding from one binding
        # WINS over a single_valued contradiction from another. P19 (functional)
        # would contradict (KB has NYC, claim says Honolulu), but P106 grounds
        # positively → overall VERIFIED, never CONTRADICTED.
        kb = _MultiPropKB(
            statements_by_property={
                "P19": [Statement(value="Q60", value_type="entity")],       # NYC (mismatch)
                "P106": [Statement(value="Q18094", value_type="entity")],   # Honolulu (match)
            }
        )
        meta = _make_meta("born_or_works", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P19",
                             single_valued=True, source="oracle"),
            PredicateBinding(kb_namespace="wikidata", kb_property="P106", source="ontology_p2302"),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(_claim(object_val="Honolulu"))
        assert result.verdict == KBVerdictType.VERIFIED

    def test_single_binding_path_is_byte_identical(self):
        # Back-compat: a single binding runs the loop exactly once and produces
        # the same verdict/trace as the pre-v0.16 single-property path.
        kb = _MultiPropKB(
            statements_by_property={"P39": [Statement(value="Q11696", value_type="entity")]}
        )
        meta = _make_meta("holds_role", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P39", source="legacy_scalar"),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(_claim())
        assert result.verdict == KBVerdictType.VERIFIED
        assert result.trace.get("property") == "P39"
        # bindings_tried still recorded, with the one binding.
        assert len(result.trace["bindings_tried"]) == 1

    def test_no_binding_has_kb_property_is_no_kb_path(self):
        # When no binding carries a kb_property, the result is NO_KB_PATH —
        # identical to the pre-v0.16 `not meta.kb_property` abstention.
        kb = _MultiPropKB(statements_by_property={})
        meta = _make_meta("vague_pred", [
            PredicateBinding(kb_namespace=None, kb_property=None, source="oracle"),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(_claim())
        assert result.verdict == KBVerdictType.NO_KB_PATH


class TestKBVerifierCopulaValueTypeFix:
    """v0.16 WS1 P31-vs-P106 copula fix. A copula "X is a physicist" routes
    ambiguously to P31 (instance-of) and P106 (occupation). The resolved object
    (an occupation) satisfies P106's value-type but NOT P31's — so only the
    P106 binding may drive a contradiction. _object_satisfies_value_type fails
    OPEN (permits the contradiction) when no value-type constraint is present.
    """

    def test_instance_of_binding_cannot_contradict_when_object_fails_value_type(self):
        # P31 binding declares a value-type constraint (must be a class, e.g.
        # Q5 human). The resolved object "physicist" (Q169470) is PROVABLY
        # unrelated to that value-type → the P31 binding's contradiction is
        # blocked (value-type gate), so it abstains with NO_MATCH.
        kb = _MultiPropKB(
            statements_by_property={
                # P31 of the subject is Q5 (human); claim object is physicist
                # (Q169470) which does not match → single_valued mismatch.
                "P31": [Statement(value="Q5", value_type="entity")],
            },
            resolutions={"physicist": "Q169470"},
            subsumptions={
                # The object (Q169470 occupation) is unrelated to the declared
                # value-type Q16889133 (class) — provable failure → block.
                ("Q169470", "Q16889133", "is_a"): "unrelated",
            },
        )
        meta = _make_meta("is_a", [
            PredicateBinding(
                kb_namespace="wikidata", kb_property="P31",
                single_valued=True,
                object_entity_types=["Q16889133"],  # class — the value-type constraint
                source="oracle",
            ),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(_claim(subject="Obama", object_val="physicist"))
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.trace.get("abstention_reason") == "value_type_incompatible_binding"

    def test_occupation_binding_can_contradict_when_object_satisfies_value_type(self):
        # P106 (occupation) declares value-type Q28640 (profession). The object
        # "physicist" (Q169470) IS_A profession → satisfies the constraint, so
        # the single_valued P106 binding is permitted to contradict the
        # mismatching KB statement (KB occupation is Q82594, not physicist).
        kb = _MultiPropKB(
            statements_by_property={
                "P106": [Statement(value="Q82594", value_type="entity")],  # computer scientist
            },
            resolutions={"physicist": "Q169470"},
            subsumptions={
                ("Q169470", "Q28640", "is_a"): "a_subsumed_by_b",  # physicist IS_A profession
            },
        )
        meta = _make_meta("is_a", [
            PredicateBinding(
                kb_namespace="wikidata", kb_property="P106",
                single_valued=True,
                object_entity_types=["Q28640"],  # profession — the value-type constraint
                source="oracle",
            ),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(_claim(subject="Obama", object_val="physicist"))
        assert result.verdict == KBVerdictType.CONTRADICTED

    def test_value_type_gate_fails_open_when_no_constraint(self):
        # A single_valued binding with NO value-type constraint (object_entity_
        # types empty) keeps the legacy contradiction behavior — the gate must
        # NOT block when it knows nothing about the value-type. This is the
        # invariant that keeps all existing single_valued contradiction tests
        # green (born_in / P19 has no declared object_entity_types here).
        kb = _MultiPropKB(
            statements_by_property={"P19": [Statement(value="Q60", value_type="entity")]},  # NYC
            resolutions={"Honolulu": "Q18094"},
        )
        meta = _make_meta("born_in", [
            PredicateBinding(
                kb_namespace="wikidata", kb_property="P19",
                single_valued=True,
                object_entity_types=None,  # no value-type constraint → fail open
                source="legacy_scalar",
            ),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(_claim(predicate="born_in", object_val="Honolulu"))
        assert result.verdict == KBVerdictType.CONTRADICTED
        # Decision 5: the contradicting KB value is surfaced for the correction.
        assert result.trace.get("contradicting_value") == "Q60"

    def test_value_type_gate_fails_open_on_kb_uncertainty(self):
        # When the object resolves but subsumption is UNCERTAIN (not provably
        # `unrelated` to the declared value-type — here b_subsumed_by_a), the
        # gate fails OPEN and permits the contradiction. Soundness is preserved
        # by only blocking on a PROVABLE value-type failure.
        kb = _MultiPropKB(
            statements_by_property={"P19": [Statement(value="Q60", value_type="entity")]},
            resolutions={"Honolulu": "Q18094"},
            subsumptions={
                ("Q18094", "Q515", "is_a"): "b_subsumed_by_a",  # uncertain w.r.t. failure
            },
        )
        meta = _make_meta("born_in", [
            PredicateBinding(
                kb_namespace="wikidata", kb_property="P19",
                single_valued=True,
                object_entity_types=["Q515"],  # city — value-type constraint present
                source="oracle",
            ),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(_claim(predicate="born_in", object_val="Honolulu"))
        assert result.verdict == KBVerdictType.CONTRADICTED


# ===========================================================================
# v0.16 — NO-STATEMENTS DISJOINT-FALLBACK CONTRADICTED ARM
#
# Symmetric counterpart to the no-statements subsumption-UPGRADE (VERIFIED)
# arm. When a LOCATION-property binding finds NO statement on the lookup
# subject for that property, but the subject is geographically DISJOINT from
# the claimed container, the arm returns CONTRADICTED (the "Vatican is in
# Africa" shape — the Vatican has no P131 statement, only P30=Europe, so the
# in-statements disjoint path never fires). Soundness mirrors the in-statements
# arm: entity object, value_resolved, standard (non-inverted) direction, both
# sides resolved to Q-ids, kb_property in _LOCATION_KB_PROPERTIES, and the
# fail-closed _location_disjoint helper requiring positive KB subsumption into
# a DIFFERENT continent. The VERIFIED subsumption-upgrade arm runs FIRST, so a
# true "X in [right continent]" verifies and never reaches this arm.
#
# These tests import the genuine CONTINENT_QIDS / _LOCATION_KB_PROPERTIES and
# use real continent Q-ids (Q46 Europe, Q15 Africa) plus the real location
# property P131, driving the _location_disjoint path (a) directly: the subject
# is `a_subsumed_by_b` one continent under "part_of" and `unrelated` to the
# claimed (different) continent.
# ===========================================================================

from aedos.layer4_sources.kb_verifier import CONTINENT_QIDS, _LOCATION_KB_PROPERTIES


class TestKBVerifierNoStatementsDisjointArm:
    # Sanity: the genuine constants the new arm is gated on hold the Q-ids the
    # tests rely on, so a constant rename can't make these pass vacuously.
    def test_constants_are_genuine(self):
        assert "Q46" in CONTINENT_QIDS  # Europe
        assert "Q15" in CONTINENT_QIDS  # Africa
        assert "P131" in _LOCATION_KB_PROPERTIES

    def test_no_statements_disjoint_contradicts(self):
        # The Vatican/Africa shape. P131 binding finds NO statement for the
        # subject. The subject (Q237 Vatican) resolves; the claim object
        # "Africa" resolves to Q15 (a genuine CONTINENT_QID). _location_disjoint
        # path (a): the subject is a_subsumed_by_b Q46 (Europe, a DIFFERENT
        # continent) under "part_of", and unrelated to Q15 (Africa) — so the
        # subject is geographically disjoint from the claimed container.
        # polarity 1 ⇒ CONTRADICTED, with the no_statements_disjoint_fallback
        # trace key set.
        kb = _MultiPropKB(
            statements_by_property={"P131": []},  # no statement on this property
            resolutions={"Vatican": "Q237", "Africa": "Q15"},
            subsumptions={
                ("Q237", "Q46", "part_of"): "a_subsumed_by_b",  # Vatican ⊂ Europe
                # unrelated to Africa (default for ("Q237","Q15","part_of"))
            },
        )
        meta = _make_meta("located_in", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P131", source="oracle"),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(
            _claim(subject="Vatican", predicate="located_in", object_val="Africa")
        )
        assert result.verdict == KBVerdictType.CONTRADICTED
        assert result.trace.get("no_statements_disjoint_fallback") is True

    def test_no_statements_disjoint_unrelated_abstains(self):
        # Same no-statements shape, but the subject is `unrelated` to ALL
        # continents (no subsumption entries at all → every part_of probe
        # returns the default "unrelated"). _location_disjoint cannot confirm a
        # different-continent ancestor, so it returns False (fail-closed) and
        # the arm does NOT contradict ⇒ NO_MATCH, not CONTRADICTED.
        kb = _MultiPropKB(
            statements_by_property={"P131": []},
            resolutions={"Atlantis": "Q9999", "Africa": "Q15"},
            subsumptions={},  # unrelated to every continent
        )
        meta = _make_meta("located_in", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P131", source="oracle"),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(
            _claim(subject="Atlantis", predicate="located_in", object_val="Africa")
        )
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.trace.get("no_statements_disjoint_fallback") is None
        assert result.trace.get("abstention_reason") == "no_statements"

    def test_no_statements_subsumption_upgrade_still_verifies_first(self):
        # Ordering proof: the subject is a_subsumed_by_b the EXPECTED continent
        # (Q15 Africa). The pre-existing no-statements subsumption-UPGRADE arm
        # runs FIRST and returns VERIFIED — the new disjoint arm is never
        # reached (it would otherwise mis-evaluate). "Cairo is in Africa": no
        # P131 statement, but Cairo ⊂ Africa via part_of ⇒ VERIFIED.
        kb = _MultiPropKB(
            statements_by_property={"P131": []},
            resolutions={"Cairo": "Q85", "Africa": "Q15"},
            subsumptions={
                ("Q85", "Q15", "part_of"): "a_subsumed_by_b",  # Cairo ⊂ Africa
            },
        )
        meta = _make_meta("located_in", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P131", source="oracle"),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(
            _claim(subject="Cairo", predicate="located_in", object_val="Africa")
        )
        assert result.verdict == KBVerdictType.VERIFIED
        assert result.trace.get("no_statements_subsumption_fallback") is True
        # The disjoint arm was not reached.
        assert result.trace.get("no_statements_disjoint_fallback") is None

    def test_no_statements_disjoint_skipped_for_nonlocation_property(self):
        # The kb_property gate: a binding whose property is NOT in
        # _LOCATION_KB_PROPERTIES (here P108 employer) must NEVER trigger the
        # disjoint arm, even with a disjoint-looking subsumption present. Two
        # distinct entities can both satisfy a relational predicate without
        # contradicting ⇒ NO_MATCH, never CONTRADICTED.
        assert "P108" not in _LOCATION_KB_PROPERTIES
        kb = _MultiPropKB(
            statements_by_property={"P108": []},
            resolutions={"Vatican": "Q237", "Africa": "Q15"},
            subsumptions={
                ("Q237", "Q46", "part_of"): "a_subsumed_by_b",  # would be disjoint
            },
        )
        meta = _make_meta("employed_by", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P108", source="oracle"),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(
            _claim(subject="Vatican", predicate="employed_by", object_val="Africa")
        )
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.trace.get("no_statements_disjoint_fallback") is None

    def test_no_statements_disjoint_negated_claim_verifies(self):
        # The Vatican/Africa shape but the claim is NEGATED ("The Vatican is NOT
        # in Africa"). The positive content is CONTRADICTED (disjoint), and
        # _apply_polarity flips a CONTRADICTED positive verdict to VERIFIED for
        # polarity 0 ⇒ VERIFIED, still via the no_statements_disjoint_fallback
        # arm.
        kb = _MultiPropKB(
            statements_by_property={"P131": []},
            resolutions={"Vatican": "Q237", "Africa": "Q15"},
            subsumptions={
                ("Q237", "Q46", "part_of"): "a_subsumed_by_b",  # Vatican ⊂ Europe
            },
        )
        meta = _make_meta("located_in", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P131", source="oracle"),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(
            _claim(subject="Vatican", predicate="located_in", object_val="Africa", polarity=0)
        )
        assert result.verdict == KBVerdictType.VERIFIED
        assert result.trace.get("no_statements_disjoint_fallback") is True
        assert result.trace.get("positive_verdict") == "contradicted"
        assert result.trace.get("polarity") == 0

    def test_no_statements_disjoint_skipped_for_inverse_binding(self):
        # The `not lookup_inverted` gate. An inverse binding (slot_to_qualifier
        # maps the Aedos subject to statement_value) keys the lookup on the
        # claim's OBJECT and treats the subject as the expected value. The
        # disjoint arm requires the standard direction, so an inverse binding —
        # even with a disjoint-looking subsumption — must skip it ⇒ NO_MATCH.
        #
        # Inverse direction: lookup_ref = claim.object ("Africa" → Q15),
        # expected_ref = claim.subject ("Vatican" → Q237). Both resolve, so
        # value_resolved is True; statements empty; lookup_inverted is True ⇒
        # the disjoint arm is gated off.
        kb = _MultiPropKB(
            statements_by_property={"P131": []},
            resolutions={"Vatican": "Q237", "Africa": "Q15"},
            subsumptions={
                ("Q237", "Q46", "part_of"): "a_subsumed_by_b",
            },
        )
        meta = _make_meta("contains_place", [
            PredicateBinding(
                kb_namespace="wikidata", kb_property="P131",
                slot_to_qualifier={"subject": "statement_value", "object": "statement_subject"},
                source="oracle",
            ),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(
            _claim(subject="Vatican", predicate="contains_place", object_val="Africa")
        )
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.trace.get("lookup_inverted") is True
        assert result.trace.get("no_statements_disjoint_fallback") is None
