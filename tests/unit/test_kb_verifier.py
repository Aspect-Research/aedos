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
