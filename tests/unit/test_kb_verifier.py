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
# v0.16.1 WS5a: the geo predicate cluster was relocated from CORE (kb_verifier)
# into the WikidataAdapter behind the kb_protocol seam. The mock KBs below mix
# in the relocated geo accessors so the verifier's location-disjoint /
# continent-widening paths exercise the SAME code, byte-for-byte.
from aedos.layer4_sources.kb_wikidata import (
    _CONTINENT_QIDS,
    _GEO_CONTAINER_TYPES,
    _LOCATION_KB_PROPERTIES,
    _geographic_disjoint,
)
from aedos.llm.client import LLMClient


class _GeoMixin:
    """v0.16.1 WS5a: gives a mock KB the relocated geographic protocol surface
    (`is_location_property` / `geo_container_types` / `geographic_disjoint`) by
    delegating to the adapter's relocated logic, driven by the mock's own
    `subsumption`. Keeps the verifier's geo paths byte-identical under mocks."""

    def is_location_property(self, kb_property):
        return kb_property in _LOCATION_KB_PROPERTIES

    def geo_container_types(self):
        return _GEO_CONTAINER_TYPES

    def geographic_disjoint(self, value_qid, expected_qid):
        return _geographic_disjoint(self.subsumption, value_qid, expected_qid)


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


class MockKB(_GeoMixin):
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


# ---------------------------------------------------------------------------
# TestValueMatchesApproximateYear  (v0.16.1 WS1: an approximate-year claim
# ("c. 1550", "circa 1550", "~1550") strips its leading approximation marker on
# the CLAIM side and matches the KB on EXACT year equality — never a fuzzy
# window. This is the unit-level surface of the confirmed §3.2 false-contradict
# defect: pre-fix, "c. 1550" failed `_BARE_YEAR_RE`, _value_matches returned
# False, and a single_valued date predicate with a KB statement present
# promoted to a (false) CONTRADICTED. The fix can only turn a previously-failing
# compare into a match when the KB year LITERALLY equals the claimed year — it
# can never false-verify. The precise-ISO strict compare is preserved.)
# ---------------------------------------------------------------------------

from aedos.layer4_sources.kb_verifier import _value_matches


class TestValueMatchesApproximateYear:
    @pytest.mark.parametrize("claim", [
        "c. 1550",
        "circa 1550",
        "ca. 1550",
        "~1550",
        "about 1550",
        "approximately 1550",
        "around 1550",
        "approx 1550",
        "approx. 1550",
    ])
    def test_approx_year_matches_kb_date_on_year_equality(self, claim):
        # KB holds a precise date whose YEAR is 1550. Every approximation marker
        # strips to "1550" and matches on year equality → True. This is the
        # match that, pre-fix, fell through to a false CONTRADICTED for a
        # single_valued date predicate (the PopQA "Tadhg Dall born_on c. 1550"
        # case, where the KB year was 1550).
        assert _value_matches("1550-01-01T00:00:00Z", claim) is True

    def test_approx_year_does_not_match_different_kb_year(self):
        # SOUNDNESS: the match is EXACT year equality, never a fuzzy +/-N window.
        # "c. 1550" against a KB year of 1600 is NOT a match (it abstains, and on
        # the contradiction side it must abstain too — covered end-to-end below).
        assert _value_matches("1600-01-01T00:00:00Z", "c. 1550") is False

    def test_bare_year_exact_match_unchanged(self):
        # The bare-year path (no marker) is untouched: "1550" vs KB "1550".
        assert _value_matches("1550", "1550") is True

    def test_bare_year_vs_precise_kb_date_unchanged(self):
        # A bare-year claim still matches a precise KB date on the year — the
        # pre-existing year-aware compare, unchanged by the marker strip.
        assert _value_matches("1879-03-14T00:00:00Z", "1879") is True

    def test_precise_year_mismatch_still_not_a_match(self):
        # A PRECISE (non-approx) bare-year claim that differs from the KB year is
        # NOT a match (it is the input to the legitimate single_valued
        # CONTRADICTED path): "1998" vs KB "1999" → False.
        assert _value_matches("1999", "1998") is False

    def test_two_precise_dates_compare_strictly(self):
        # The precise-ISO strict compare is preserved: a full claim date does NOT
        # enter the year-only path (it is not a bare year), so "1998-09-04" vs KB
        # "1998-09-04T00:00:00Z" stays a strict literal compare → False (and two
        # genuinely different precise dates likewise don't year-collapse to a
        # match). This pins that the approx-strip never loosened precise compares.
        assert _value_matches("1998-09-04T00:00:00Z", "1998-09-04") is False
        assert _value_matches("1998-01-01T00:00:00Z", "1998-09-04") is False

    def test_precise_approx_date_does_not_year_collapse(self):
        # An approximate but PRECISE date "c. 1550-03-01" returns None from the
        # stripper (remainder is not a bare year) and never enters the year path,
        # so it does NOT match KB year 1550 — it stays a strict literal compare.
        # This guards against the marker strip accidentally widening to dates.
        assert _value_matches("1550-01-01T00:00:00Z", "c. 1550-03-01") is False


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


class _MultiPropKB(_GeoMixin):
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
# v0.16.1 WS5a: these tests use the genuine continent Q-ids / location P-ids,
# now relocated into the WikidataAdapter (kb_wikidata._CONTINENT_QIDS /
# _LOCATION_KB_PROPERTIES, imported at module top), plus the real location
# property P131, driving the relocated geographic_disjoint path (a) directly:
# the subject is `a_subsumed_by_b` one continent under "part_of" and `unrelated`
# to the claimed (different) continent.
# ===========================================================================


class TestKBVerifierNoStatementsDisjointArm:
    # Sanity: the genuine constants the new arm is gated on hold the Q-ids the
    # tests rely on, so a constant rename can't make these pass vacuously.
    def test_constants_are_genuine(self):
        assert "Q46" in _CONTINENT_QIDS  # Europe
        assert "Q15" in _CONTINENT_QIDS  # Africa
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


# ===========================================================================
# v0.16.1 WS1 — APPROXIMATE-DATE FALSE-CONTRADICT FIX (end-to-end)
#
# The confirmed §3.2 defect: a functional ('time' + single_valued) date
# predicate (born_on / P569) whose KB statement is present but did NOT
# year-match an APPROXIMATE-year claim promoted to a (false) CONTRADICTED.
# Two soundness guarantees, proven end-to-end through verify():
#   (1) when the KB date's year EQUALS the approximate claim's year, the claim
#       VERIFIES (the year-equality match in _value_matches) — never a false
#       contradict, never a false verify (matches only on exact year equality);
#   (2) when the approximate claim's year DIFFERS from the KB year, the verdict
#       is NO_MATCH/abstain — an approximation ("around 1550") cannot soundly
#       contradict a nearby exact KB date; the marker disclaims precision.
# A PRECISE (no-marker) wrong year still CONTRADICTS — the legitimate
# single_valued date-contradiction path is unchanged (the N1/value-type tests'
# sibling). These mirror TestKBVerifierTimeObjectTypeGate's date-predicate
# pattern (object_type="time", single_valued=1, kb_property="P569").
# ===========================================================================


class TestKBVerifierApproximateDate:
    def test_approx_year_equals_kb_year_is_verified(self):
        # The PopQA "Tadhg Dall born_on c. 1550" shape. KB statement is a precise
        # date whose YEAR is 1550; the approximate claim "c. 1550" year-matches →
        # VERIFIED. Pre-fix this fell through to a false CONTRADICTED.
        stmts = [Statement(value="1550-01-01T00:00:00Z", value_type="date")]
        verifier = _make_verifier(
            stmts, object_type="time", single_valued=1, kb_property="P569"
        )
        result = verifier.verify(_claim(predicate="born_on", object_val="c. 1550"))
        assert result.verdict == KBVerdictType.VERIFIED

    def test_approx_year_differs_from_kb_year_abstains_not_contradicts(self):
        # The contradiction-suppression backstop: the approximate claim "c. 1550"
        # does NOT year-match the KB date (year 1600). A single_valued date
        # predicate would, pre-fix, promote this to CONTRADICTED. The fix
        # downgrades it to NO_MATCH/abstain — an approximation may never contradict
        # a nearby exact date. NOT CONTRADICTED, NOT VERIFIED.
        stmts = [Statement(value="1600-01-01T00:00:00Z", value_type="date")]
        verifier = _make_verifier(
            stmts, object_type="time", single_valued=1, kb_property="P569"
        )
        result = verifier.verify(_claim(predicate="born_on", object_val="c. 1550"))
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.trace.get("abstention_reason") == "approximate_date_no_year_match"

    def test_precise_wrong_year_still_contradicts(self):
        # Control / regression pin: a PRECISE (no-marker) wrong year on the same
        # functional date predicate still CONTRADICTS. The approx-suppression
        # only loosens APPROXIMATE claims toward abstain; a bare "1600" vs KB year
        # 1550 is the legitimate single_valued contradiction and must be unchanged.
        stmts = [Statement(value="1550-01-01T00:00:00Z", value_type="date")]
        verifier = _make_verifier(
            stmts, object_type="time", single_valued=1, kb_property="P569"
        )
        result = verifier.verify(_claim(predicate="born_on", object_val="1600"))
        assert result.verdict == KBVerdictType.CONTRADICTED


# ===========================================================================
# v0.16.1 WS2 — OCCUPATION-COPULA POSITIVE GROUNDING (P106) + FAIL-CLOSED GATE
#
# A profession copula "X is a guitarist" extracts as instance_of → primary P31
# (a person's P31 is Q5 human, so it never grounds the occupation). The seed
# now also synthesizes a value-type-gated P106 (occupation) candidate binding.
# The binding loop verifies the asserted occupation against the subject's P106
# set. The POSITIVE P106 path is FAIL-CLOSED type-gated: it may VERIFY only
# when the resolved object is PROVABLY an occupation/profession class. A
# non-occupation copula ("X is a river") falls through the gate to P31; a wrong
# occupation abstains (P106 single_valued=0, never contradicts).
# ===========================================================================

# Q-ids used below: Q12737077 occupation, Q28640 profession (the gate's value-
# type constraint); Q5982903 = guitarist (a confirmed occupation); Q4022 = river
# (NOT an occupation); Q177220 = singer (a confirmed occupation, the KB's actual
# P106 value used for the wrong-occupation abstain case).
_OCCUPATION_VT = ["Q12737077", "Q28640"]


def _gated_p106_meta(predicate="instance_of"):
    """meta with the seed-synthesized [P31 (primary), P106 (value-type-gated
    occupation candidate)] shape."""
    return _make_meta(predicate, [
        PredicateBinding(kb_namespace="wikidata", kb_property="P31",
                         single_valued=False, source="legacy_scalar"),
        PredicateBinding(kb_namespace="wikidata", kb_property="P106",
                         single_valued=False, object_entity_types=_OCCUPATION_VT,
                         source="candidate", value_type_gated=True),
    ])


class TestKBVerifierOccupationCopulaPositiveGate:
    def test_confirmed_occupation_verifies_via_p106(self):
        # "Robby Krieger is a guitarist". P31 holds Q5 (human) — no match. P106
        # holds Q5982903 (guitarist) — matches the resolved object, and the
        # object IS_A occupation (a_subsumed_by_b Q12737077) → the fail-closed
        # positive gate is satisfied ⇒ VERIFIED via P106.
        kb = _MultiPropKB(
            statements_by_property={
                "P31": [Statement(value="Q5", value_type="entity")],       # human
                "P106": [Statement(value="Q5982903", value_type="entity")],  # guitarist
            },
            resolutions={"guitarist": "Q5982903"},
            subsumptions={("Q5982903", "Q12737077", "is_a"): "a_subsumed_by_b"},
        )
        verifier = _make_multi_verifier(kb, _gated_p106_meta())
        result = verifier.verify(_claim(subject="Obama", predicate="instance_of",
                                        object_val="guitarist"))
        assert result.verdict == KBVerdictType.VERIFIED
        assert result.trace.get("property") == "P106"

    def test_non_occupation_copula_falls_through_gate_to_p31(self):
        # "Paris is a city" — the object (Q515 city) is PROVABLY not an
        # occupation (unrelated to both Q12737077 and Q28640), so the P106
        # positive gate fails closed (abstains). P31 holds Q515 (city) and the
        # resolved object matches ⇒ VERIFIED via the primary P31 binding, NOT
        # P106. The gate prevented a false-verify through occupation.
        kb = _MultiPropKB(
            statements_by_property={
                "P31": [Statement(value="Q515", value_type="entity")],   # city
                "P106": [Statement(value="Q515", value_type="entity")],  # (would spuriously match)
            },
            resolutions={"city": "Q515", "Paris": "Q90"},
            subsumptions={
                ("Q515", "Q12737077", "is_a"): "unrelated",
                ("Q515", "Q28640", "is_a"): "unrelated",
            },
        )
        verifier = _make_multi_verifier(kb, _gated_p106_meta())
        result = verifier.verify(_claim(subject="Paris", predicate="instance_of",
                                        object_val="city"))
        assert result.verdict == KBVerdictType.VERIFIED
        # The winning binding is P31, not the gated P106.
        assert result.trace.get("property") == "P31"
        tried = {t["property"]: t for t in result.trace["bindings_tried"]}
        assert tried["P106"]["verdict"] == "no_match"
        assert tried["P106"]["abstention_reason"] == "value_type_unconfirmed_positive_gate"

    def test_river_copula_p106_abstains_no_false_verify(self):
        # "The Amazon is a river" — P31 has NO matching statement (subject's P31
        # is e.g. Q4022 river but object resolves to a different river class) and
        # P106 would spuriously match the object, but the object (Q4022 river) is
        # provably NOT an occupation → P106 positive gate fails closed. No
        # binding grounds positively ⇒ NO_MATCH (abstain), never a false-verify.
        kb = _MultiPropKB(
            statements_by_property={
                "P31": [Statement(value="Q5", value_type="entity")],     # mismatch (human)
                "P106": [Statement(value="Q4022", value_type="entity")],
            },
            resolutions={"river": "Q4022", "Amazon": "Q3783"},
            subsumptions={
                ("Q4022", "Q12737077", "is_a"): "unrelated",
                ("Q4022", "Q28640", "is_a"): "unrelated",
            },
        )
        verifier = _make_multi_verifier(kb, _gated_p106_meta())
        result = verifier.verify(_claim(subject="Amazon", predicate="instance_of",
                                        object_val="river"))
        assert result.verdict == KBVerdictType.NO_MATCH

    def test_wrong_occupation_abstains_never_contradicts(self):
        # "X is a guitarist" but the KB P106 set holds only Q177220 (singer) —
        # the object (guitarist, a CONFIRMED occupation) resolves and is_a
        # occupation, so the gate would PERMIT a positive verify, but P106 has no
        # matching statement. Because P106 is single_valued=0, the mismatch is
        # NO_MATCH (abstain), NEVER CONTRADICTED. P31 (Q5 human) also abstains.
        kb = _MultiPropKB(
            statements_by_property={
                "P31": [Statement(value="Q5", value_type="entity")],
                "P106": [Statement(value="Q177220", value_type="entity")],  # singer
            },
            resolutions={"guitarist": "Q5982903"},
            subsumptions={("Q5982903", "Q12737077", "is_a"): "a_subsumed_by_b"},
        )
        verifier = _make_multi_verifier(kb, _gated_p106_meta())
        result = verifier.verify(_claim(subject="Obama", predicate="instance_of",
                                        object_val="guitarist"))
        assert result.verdict == KBVerdictType.NO_MATCH

    def test_gate_fails_closed_on_kb_uncertainty(self):
        # The fail-CLOSED dual of the CONTRADICTED gate's fail-open: when the
        # object resolves and P106 matches but subsumption is UNCERTAIN (not
        # provably a_subsumed_by_b / equivalent — here b_subsumed_by_a), the
        # positive gate does NOT confirm ⇒ the P106 verify is blocked. No new
        # positive grounding surface on uncertainty (§3.2).
        kb = _MultiPropKB(
            statements_by_property={
                "P31": [Statement(value="Q5", value_type="entity")],
                "P106": [Statement(value="Q5982903", value_type="entity")],
            },
            resolutions={"guitarist": "Q5982903"},
            subsumptions={
                ("Q5982903", "Q12737077", "is_a"): "b_subsumed_by_a",  # uncertain
                ("Q5982903", "Q28640", "is_a"): "unrelated",
            },
        )
        verifier = _make_multi_verifier(kb, _gated_p106_meta())
        result = verifier.verify(_claim(subject="Obama", predicate="instance_of",
                                        object_val="guitarist"))
        assert result.verdict == KBVerdictType.NO_MATCH

    def test_object_confirms_value_type_equality_short_circuit(self):
        # Direct unit on the gate helper: an object Q-id equal to a declared
        # value-type class confirms without a subsumption probe.
        kb = _MultiPropKB(statements_by_property={})
        verifier = _make_multi_verifier(kb, _gated_p106_meta())
        b = PredicateBinding(kb_namespace="wikidata", kb_property="P106",
                             object_entity_types=_OCCUPATION_VT, value_type_gated=True)
        assert verifier._object_confirms_value_type("Q12737077", b) is True
        # No constraint → cannot confirm → fail closed.
        b2 = PredicateBinding(kb_namespace="wikidata", kb_property="P106",
                              object_entity_types=None, value_type_gated=True)
        assert verifier._object_confirms_value_type("Q5982903", b2) is False
        # Unresolved object → fail closed.
        assert verifier._object_confirms_value_type(None, b) is False


# ===========================================================================
# v0.16.1 WS4 — SLING distant-supervision binding (ACTIVATED, verify-only)
#
# A SLING binding is a low-rank, single_valued=False, value_type_gated=True
# candidate the substrate proposes for a long-tail edge. Through the binding
# loop it must:
#   * VERIFY a matching claim ONLY when the resolved object PROVABLY satisfies
#     the binding's value-type (the fail-closed positive gate);
#   * ABSTAIN (never false-verify) when the object's type can't be confirmed;
#   * NEVER drive CONTRADICTED (single_valued=False).
# These pin the soundness contract that makes activation safe.
# ===========================================================================

# Q-id reused below: Q12737077 occupation (the SLING binding's value-type).
_SLING_VT = ["Q12737077"]


def _sling_only_meta(predicate="works_as", value_types=_SLING_VT):
    """meta whose ONLY binding is a SLING candidate (mirrors what
    SlingFallback.propose_bindings produces: source='sling',
    single_valued=False, low rank, value_type_gated=True)."""
    return _make_meta(predicate, [
        PredicateBinding(kb_namespace="wikidata", kb_property="P106",
                         single_valued=False, object_entity_types=value_types,
                         source="sling", rank=0.1, value_type_gated=True),
    ])


class TestKBVerifierSlingBinding:
    def test_sling_verifies_only_through_value_type_gate(self):
        # The SLING binding's P106 set matches the resolved object AND the object
        # is_a occupation (Q12737077) ⇒ the fail-closed positive gate is
        # satisfied ⇒ VERIFIED. This is the ONLY path a SLING binding can verify.
        kb = _MultiPropKB(
            statements_by_property={
                "P106": [Statement(value="Q5982903", value_type="entity")],  # guitarist
            },
            resolutions={"guitarist": "Q5982903"},
            subsumptions={("Q5982903", "Q12737077", "is_a"): "a_subsumed_by_b"},
        )
        verifier = _make_multi_verifier(kb, _sling_only_meta())
        result = verifier.verify(_claim(subject="Obama", predicate="works_as",
                                        object_val="guitarist"))
        assert result.verdict == KBVerdictType.VERIFIED
        assert result.trace.get("property") == "P106"
        tried = {t["property"]: t for t in result.trace["bindings_tried"]}
        assert tried["P106"]["source"] == "sling"

    def test_sling_abstains_when_value_type_unconfirmed(self):
        # The P106 statement matches the resolved object, but the object is
        # provably NOT an occupation (unrelated) ⇒ the fail-closed positive gate
        # blocks the verify ⇒ NO_MATCH (abstain). A noisy co-occurrence binding
        # cannot false-verify on an unconfirmed object type.
        kb = _MultiPropKB(
            statements_by_property={
                "P106": [Statement(value="Q4022", value_type="entity")],  # river
            },
            resolutions={"river": "Q4022", "Amazon": "Q3783"},
            subsumptions={("Q4022", "Q12737077", "is_a"): "unrelated"},
        )
        verifier = _make_multi_verifier(kb, _sling_only_meta())
        result = verifier.verify(_claim(subject="Amazon", predicate="works_as",
                                        object_val="river"))
        assert result.verdict == KBVerdictType.NO_MATCH
        tried = {t["property"]: t for t in result.trace["bindings_tried"]}
        assert tried["P106"]["abstention_reason"] == "value_type_unconfirmed_positive_gate"

    def test_sling_never_contradicts(self):
        # Even with a value-type-confirmed object whose value does NOT match the
        # KB P106 set, a SLING binding (single_valued=False) NEVER contradicts —
        # the mismatch is NO_MATCH (abstain), per §3.2.
        kb = _MultiPropKB(
            statements_by_property={
                "P106": [Statement(value="Q177220", value_type="entity")],  # singer
            },
            resolutions={"guitarist": "Q5982903"},
            subsumptions={("Q5982903", "Q12737077", "is_a"): "a_subsumed_by_b"},
        )
        verifier = _make_multi_verifier(kb, _sling_only_meta())
        result = verifier.verify(_claim(subject="Obama", predicate="works_as",
                                        object_val="guitarist"))
        assert result.verdict == KBVerdictType.NO_MATCH
