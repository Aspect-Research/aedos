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

    def __init__(self, statements: list[Statement], resolutions: dict | None = None,
                 labels: dict | None = None, property_ontology: dict | None = None,
                 subsumptions: dict | None = None):
        self._statements = statements
        self._resolutions = dict(_DEFAULT_RESOLUTIONS)
        if resolutions:
            self._resolutions.update(resolutions)
        self._labels = dict(labels or {})
        # prop_id -> ontology dict (subject_type_qids / value_type_qids / ...).
        self._property_ontology = dict(property_ontology or {})
        # (entity_a, entity_b, relation_type) -> verdict string. Absent pairs
        # fall back to "unrelated" (the pre-existing default).
        self._subsumptions = dict(subsumptions or {})

    def fetch_label(self, qid):
        # Canonical KB label for a Q-id (the resolved-entity name-match consults
        # this). Absent by default so most mock KBs expose no labels.
        return self._labels.get(qid)

    def fetch_property_ontology(self, prop):
        # The property's Wikidata constraints (value_type_qids etc.). Empty for
        # props not configured — mirrors the adapter's fail-open empty ontology.
        return self._property_ontology.get(
            prop, {"subject_type_qids": [], "value_type_qids": []}
        )

    def resolve_entity(self, reference, local_context):
        qid = self._resolutions.get(reference)
        if qid is None:
            return []
        return [ResolutionCandidate(kb_identifier=qid, score=0.9)]

    def lookup_statements(self, entity, predicate):
        return list(self._statements)

    def subsumption(self, entity_a, entity_b, relation_type):
        verdict = self._subsumptions.get((entity_a, entity_b, relation_type), "unrelated")
        return SubsumptionResult(verdict=verdict)


def _make_verifier(statements, routing_hint="kb_resolvable", kb_property="P39",
                   object_type="entity", single_valued=0, resolutions=None,
                   slot_to_qualifier=None, labels=None, property_ontology=None,
                   subsumptions=None):
    db = open_memory_db()
    transport = MockTransport(routing_hint, kb_property, object_type, single_valued,
                              slot_to_qualifier)
    client = LLMClient(_transport=transport)
    pt = PredicateTranslation(db=db, llm_client=client)
    kb = MockKB(statements, resolutions, labels, property_ontology, subsumptions)
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

    def test_too_early_present_fact_verifies_with_scope_unconfirmed(self):
        # v0.16.4: claim says "since 2005" but the (still-current) statement only
        # starts 2009. The entity DOES currently hold the value, so the PRESENT
        # base fact verifies — and the trace flags `temporal_scope_unconfirmed`
        # because the claimed start precedes the actual one (it is NOT confirmed).
        # Pre-v0.16.4 this was a flat NO_MATCH (over-refusal of an answerable fact).
        stmts = [Statement(
            value="Q11696", value_type="entity",
            qualifiers={"P580": "2009-01-20"}
        )]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim(valid_from="2005-01-01"))
        assert result.verdict == KBVerdictType.VERIFIED
        assert result.trace.get("temporal_scope_unconfirmed") is True

    def test_too_early_start_but_role_ended_returns_no_match(self):
        # The rescue is gated to a CURRENTLY-held value: if the statement has
        # provably ENDED (P582 in the past), the entity no longer holds it, so the
        # present fact is false and the verifier still abstains — never asserting
        # "is president" for someone whose term ended.
        stmts = [Statement(
            value="Q11696", value_type="entity",
            qualifiers={"P580": "2009-01-20", "P582": "2017-01-20"}
        )]
        verifier = _make_verifier(stmts)
        result = verifier.verify(_claim(valid_from="2005-01-01"))
        assert result.verdict == KBVerdictType.NO_MATCH
        assert not result.trace.get("temporal_scope_unconfirmed")


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

    def test_resolved_entity_vs_literal_abstains_not_contradicts(self):
        # §3.2 false-contradict fix (the birth_name case): an `entity` predicate
        # whose object RESOLVES to a Q-id (a person name resolving to the person
        # entity) but whose KB statement holds the LITERAL string of the SAME
        # surface form. S3 permits `literal` for an `entity` object_type (for
        # literal-vs-literal external-id compares), but here the claim resolved to
        # an entity — a resolved-entity-vs-literal cross-kind compare can never be
        # a sound contradiction. NO_MATCH, never CONTRADICTED, despite identical
        # surface text ("Jorge Mario Bergoglio" vs "Jorge Mario Bergoglio").
        stmts = [Statement(value="Jorge Mario Bergoglio", value_type="literal")]
        verifier = _make_verifier(
            stmts, object_type="entity", single_valued=1, kb_property="P1477",
            resolutions={"Jorge Mario Bergoglio": "Q450675"},
        )
        result = verifier.verify(
            _claim(predicate="birth_name", object_val="Jorge Mario Bergoglio")
        )
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.trace.get("abstention_reason") == "entity_claim_vs_literal_value"

    def test_resolved_entity_vs_untagged_qid_value_still_contradicts(self):
        # Control for the untagged-value_type fallback: a real entity value that
        # the adapter left untagged (value_type=None) but whose value is a Q-id is
        # treated as an entity — so a genuine entity-vs-entity mismatch still
        # CONTRADICTS (the guard does not over-abstain on untagged Q-ids).
        stmts = [Statement(value="Q60", value_type=None)]  # untagged, but a Q-id
        verifier = _make_verifier(
            stmts, object_type="entity", single_valued=1, kb_property="P19"
        )
        result = verifier.verify(_claim(object_val="Honolulu"))
        assert result.verdict == KBVerdictType.CONTRADICTED


# ---------------------------------------------------------------------------
# TestEntityNameMatch  (§3.2 false-contradict fix: the famous-entity QID tangle).
# A single_valued ENTITY claim whose value surface form resolved to a DIFFERENT
# same-named QID than the KB statement holds must NOT contradict when the KB
# value IS named by the claim's surface form — the resolver just picked the wrong
# same-named node (e.g. "Tokyo" → the special-wards QID while Japan's P36 is
# Q1490, the metropolis, which isn't `city`-typed so the value-type filter
# excludes it). It is the SAME real-world referent, so VERIFY.
# ---------------------------------------------------------------------------

class TestEntityNameMatch:
    def test_famous_entity_qid_tangle_verifies_not_contradicts(self):
        # Japan's capital (P36) is Q1490 (Tokyo). The claim's "Tokyo" resolves to
        # Q7473516 (a different same-named QID). Q-id mismatch would CONTRADICT,
        # but Q1490's label IS "Tokyo" (the claim's surface) → VERIFIED.
        stmts = [Statement(value="Q1490", value_type="entity")]
        verifier = _make_verifier(
            stmts, object_type="entity", single_valued=1, kb_property="P36",
            resolutions={"Japan": "Q17", "Tokyo": "Q7473516"},
            labels={"Q1490": "Tokyo"},
        )
        result = verifier.verify(
            _claim(subject="Japan", predicate="capital", object_val="Tokyo")
        )
        assert result.verdict == KBVerdictType.VERIFIED
        assert result.trace.get("entity_name_match") is True

    def test_genuine_name_mismatch_still_contradicts(self):
        # Control: a DIFFERENTLY-named value (Kyoto) does not match the KB value's
        # label ("Tokyo"), so the functional contradiction stands — the guard is
        # selective, not a blanket suppression.
        stmts = [Statement(value="Q1490", value_type="entity")]
        verifier = _make_verifier(
            stmts, object_type="entity", single_valued=1, kb_property="P36",
            resolutions={"Japan": "Q17", "Kyoto": "Q34600"},
            labels={"Q1490": "Tokyo"},
        )
        result = verifier.verify(
            _claim(subject="Japan", predicate="capital", object_val="Kyoto")
        )
        assert result.verdict == KBVerdictType.CONTRADICTED

    def test_name_match_negation_contradicts(self):
        # Polarity: "Japan's capital is NOT Tokyo" (polarity 0) → the positive
        # content VERIFIES via the name-match, inverted to CONTRADICTED ("not
        # Tokyo" is false). Soundness in the negated direction.
        stmts = [Statement(value="Q1490", value_type="entity")]
        verifier = _make_verifier(
            stmts, object_type="entity", single_valued=1, kb_property="P36",
            resolutions={"Japan": "Q17", "Tokyo": "Q7473516"},
            labels={"Q1490": "Tokyo"},
        )
        result = verifier.verify(
            _claim(subject="Japan", predicate="capital", object_val="Tokyo", polarity=0)
        )
        assert result.verdict == KBVerdictType.CONTRADICTED

    def test_no_label_available_leaves_contradiction(self):
        # Fail-closed: when the KB exposes no label for the value (fetch_label
        # returns None), the guard does not fire and the existing verdict stands.
        stmts = [Statement(value="Q1490", value_type="entity")]
        verifier = _make_verifier(
            stmts, object_type="entity", single_valued=1, kb_property="P36",
            resolutions={"Japan": "Q17", "Tokyo": "Q7473516"},
            # no labels → fetch_label returns None
        )
        result = verifier.verify(
            _claim(subject="Japan", predicate="capital", object_val="Tokyo")
        )
        assert result.verdict == KBVerdictType.CONTRADICTED

    def test_ended_role_with_matching_label_still_contradicts(self):
        # §3.2 CRITICAL: the name-match must NOT rescue an E4 temporal-currency
        # contradiction. "Obama holds_role President" (present, unscoped) where the
        # P39 statement value MATCHED (Q11696) but ENDED in 2017 → CONTRADICTED.
        # The KB value's label equals the claim surface, but the value already
        # MATCHED, so the name-match is suppressed (it only rescues VALUE-MISMATCH
        # contradictions). Otherwise this would false-VERIFY "Obama is the President
        # of the United States" in 2026 — the wrong-pope/ended-role regression.
        stmt = Statement(
            value="Q11696", value_type="entity",
            qualifiers={"P580": "2009-01-20", "P582": "2017-01-20"},
        )
        verifier = _make_verifier(
            [stmt], object_type="entity", single_valued=0, kb_property="P39",
            labels={"Q11696": "President of the United States"},
        )
        result = verifier.verify(
            _claim(subject="Obama", predicate="holds_role",
                   object_val="President of the United States"),
            current_time="2026-06-03T00:00:00+00:00",
        )
        assert result.verdict == KBVerdictType.CONTRADICTED
        assert result.trace.get("entity_name_match") is not True


# ---------------------------------------------------------------------------
# TestPropertyConstraintValidation: the constraint-validation layer. The
# CONTRADICT value-type guard sources the value-type constraint from the KB
# PROPERTY's own Wikidata constraints (P2302, via fetch_property_ontology) when
# the oracle/seed left the binding untyped — so a claim object that provably
# violates the property's value-type abstains instead of false-contradicting.
# The constraint "falls out of the data" rather than the oracle's guess.
# ---------------------------------------------------------------------------

class TestPropertyConstraintValidation:
    def test_contradiction_blocked_when_object_violates_property_value_type(self):
        # single_valued entity predicate, binding has NO object_entity_types. The
        # property's value-type constraint (from the ontology) is Q5 (human). The
        # claim object resolves to Q999, provably UNRELATED to Q5. The KB value
        # differs, so without the guard this would CONTRADICT — but the object
        # provably violates the property's value-type, so abstain.
        stmts = [Statement(value="Q42", value_type="entity")]
        verifier = _make_verifier(
            stmts, object_type="entity", single_valued=1, kb_property="P50",
            resolutions={"Obama": "Q76", "SomeBook": "Q999"},
            property_ontology={"P50": {"subject_type_qids": [], "value_type_qids": ["Q5"]}},
        )
        result = verifier.verify(_claim(predicate="authored", object_val="SomeBook"))
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.trace.get("abstention_reason") == "value_type_incompatible_binding"

    def test_contradiction_proceeds_when_object_satisfies_property_value_type(self):
        # Control: same shape, but the object Q999 IS provably an instance of the
        # value-type class Q5 → the guard permits → the functional mismatch
        # CONTRADICTS as before (the layer is selective, not a blanket suppress).
        stmts = [Statement(value="Q42", value_type="entity")]
        verifier = _make_verifier(
            stmts, object_type="entity", single_valued=1, kb_property="P50",
            resolutions={"Obama": "Q76", "SomeAuthor": "Q999"},
            property_ontology={"P50": {"subject_type_qids": [], "value_type_qids": ["Q5"]}},
            subsumptions={("Q999", "Q5", "is_a"): "a_subsumed_by_b"},
        )
        result = verifier.verify(_claim(predicate="authored", object_val="SomeAuthor"))
        assert result.verdict == KBVerdictType.CONTRADICTED

    def test_fail_open_when_property_has_no_value_type_constraint(self):
        # Fail-open: no value-type constraint for the property (empty ontology) →
        # the guard permits → contradiction proceeds, exactly as before the layer.
        stmts = [Statement(value="Q42", value_type="entity")]
        verifier = _make_verifier(
            stmts, object_type="entity", single_valued=1, kb_property="P50",
            resolutions={"Obama": "Q76", "SomeBook": "Q999"},
            # no property_ontology entry → fetch returns empty value_type_qids
        )
        result = verifier.verify(_claim(predicate="authored", object_val="SomeBook"))
        assert result.verdict == KBVerdictType.CONTRADICTED

    def test_declared_binding_types_take_precedence_over_property_ontology(self):
        # When the binding DOES declare object_entity_types, those are used (the
        # property-ontology fallback only fires for an untyped binding). Here the
        # binding has no declared types, so the fallback supplies Q5; this pins
        # that the fallback path is what fires (companion to the blocked test).
        stmts = [Statement(value="Q42", value_type="entity")]
        verifier = _make_verifier(
            stmts, object_type="entity", single_valued=1, kb_property="P50",
            resolutions={"Obama": "Q76", "SomeBook": "Q999"},
            property_ontology={"P50": {"subject_type_qids": ["Q386724"], "value_type_qids": ["Q5"]}},
        )
        result = verifier.verify(_claim(predicate="authored", object_val="SomeBook"))
        # Q999 provably unrelated to the value-type Q5 -> abstain (subject-type
        # Q386724 is not consulted by this value-side guard).
        assert result.verdict == KBVerdictType.NO_MATCH


# ---------------------------------------------------------------------------
# TestAllValuesSubsumptionUpgrade (Change 2-NR): the directed subsumption upgrade
# tries EVERY held value, not just the first iterated one — so a multi-valued
# subject whose container chain sits on a SIBLING value still VERIFIES,
# order-independent. (This is what makes the walker's functional discovery-skip
# non-regressive: the directed upgrade owns the true container case.)
# ---------------------------------------------------------------------------

class TestAllValuesSubsumptionUpgrade:
    def test_container_claim_verifies_via_sibling_value_first_iterated_misses(self):
        # Obama-shape: P19 = [hospital Q6366688 (iterated FIRST, NOT in USA),
        # Honolulu Q18094 (part_of USA)]. "born_in USA" must VERIFY via Honolulu ⊆
        # USA even though the hospital iterated first (pre-fix: only the first
        # mismatch was checked → abstain).
        stmts = [
            Statement(value="Q6366688", value_type="entity"),  # hospital, first
            Statement(value="Q18094", value_type="entity"),    # Honolulu, second
        ]
        verifier = _make_verifier(
            stmts, object_type="entity", single_valued=1, kb_property="P19",
            resolutions={"Obama": "Q76", "USA": "Q30"},
            subsumptions={("Q18094", "Q30", "part_of"): "a_subsumed_by_b"},
        )
        result = verifier.verify(_claim(predicate="born_in", object_val="USA"))
        assert result.verdict == KBVerdictType.VERIFIED

    def test_no_held_value_subsumes_still_abstains(self):
        # Control: neither held value is subsumed by the claim object → NO_MATCH
        # (the all-values loop only verifies on a genuine containment).
        stmts = [
            Statement(value="Q6366688", value_type="entity"),
            Statement(value="Q18094", value_type="entity"),
        ]
        verifier = _make_verifier(
            stmts, object_type="entity", single_valued=1, kb_property="P19",
            resolutions={"Obama": "Q76", "Kenya": "Q114"},
            # no subsumption to Q114 → all unrelated
        )
        result = verifier.verify(_claim(predicate="born_in", object_val="Kenya"))
        assert result.verdict == KBVerdictType.NO_MATCH

    def test_is_a_value_subsumption_does_not_false_verify(self):
        # §3.2 (adversarial-review catch): a non-location single_valued entity
        # predicate (occupation P106) holding MULTIPLE values, where a SIBLING
        # value is_a the claim object, must ABSTAIN — not VERIFY. The upgrade is
        # part_of-ONLY, so the is_a subsumption is excluded and the multi-value
        # guard abstains. (Pre-fix the all-values is_a upgrade false-verified here,
        # bypassing the multi_valued_single_valued_predicate guard.)
        stmts = [
            Statement(value="Q111", value_type="entity"),
            Statement(value="Q222", value_type="entity"),
        ]
        verifier = _make_verifier(
            stmts, object_type="entity", single_valued=1, kb_property="P106",
            resolutions={"Obama": "Q76", "river": "Q4022"},
            subsumptions={("Q222", "Q4022", "is_a"): "a_subsumed_by_b"},
        )
        result = verifier.verify(_claim(predicate="works_as", object_val="river"))
        assert result.verdict == KBVerdictType.NO_MATCH

    def test_single_value_is_a_subsumption_does_not_false_verify(self):
        # The pre-existing single-value variant the same gate closes: ONE held
        # value that is_a the claim object on a non-location predicate must NOT
        # VERIFY (part_of-only excludes the is_a value subsumption).
        stmts = [Statement(value="Q222", value_type="entity")]
        verifier = _make_verifier(
            stmts, object_type="entity", single_valued=1, kb_property="P106",
            resolutions={"Obama": "Q76", "river": "Q4022"},
            subsumptions={("Q222", "Q4022", "is_a"): "a_subsumed_by_b"},
        )
        result = verifier.verify(_claim(predicate="works_as", object_val="river"))
        assert result.verdict != KBVerdictType.VERIFIED


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
        # `literal` is in the allowed value-type set for object_type='time'. A
        # functional time predicate whose KB DATE literal genuinely DISAGREES with
        # the claim (different year, both assert year) CONTRADICTS (E2 precision-
        # aware mismatch).
        verifier = _make_verifier(
            [Statement(value="1850-01-01T00:00:00Z", value_type="literal")],
            object_type="time", single_valued=1, kb_property="P569",
        )
        result = verifier.verify(_claim(predicate="born_on", object_val="1879"))
        assert result.verdict == KBVerdictType.CONTRADICTED

    def test_time_predicate_abstains_on_non_date_literal(self):
        # E2 (§3.2): a date claim vs a NON-date KB literal cannot be soundly
        # compared (the KB value isn't a date — likely a mis-mapped predicate), so
        # ABSTAIN rather than fabricate a contradiction.
        verifier = _make_verifier(
            [Statement(value="not-a-year-literal", value_type="literal")],
            object_type="time", single_valued=1, kb_property="P569",
        )
        result = verifier.verify(_claim(predicate="born_on", object_val="1879"))
        assert result.verdict == KBVerdictType.NO_MATCH

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

from aedos.layer4_sources.kb_verifier import (
    _date_parts,
    _date_relation,
    _end_provably_future,
    _end_provably_past,
    _value_matches,
)


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
        # E2 precision-aware: the SAME date in different formats/precisions MATCHES
        # ("1998-09-04" vs KB "1998-09-04T00:00:00Z" — identical day), but two
        # GENUINELY different precise dates do NOT (different day, both day-precise).
        assert _value_matches("1998-09-04T00:00:00Z", "1998-09-04") is True
        assert _value_matches("1998-01-01T00:00:00Z", "1998-09-04") is False


class TestNaturalLanguageDateMatching:
    """E2: natural-language date objects ('December 17, 1936') compare precision-
    aware against Wikidata ISO dates. Verify when the KB is at least as precise and
    agrees; contradict ONLY on a year disagreement (Wikidata month/day may be a
    precision placeholder, so a month/day diff abstains, never contradicts)."""

    def test_nl_date_matches_iso(self):
        kb = "1936-12-17T00:00:00Z"
        assert _value_matches(kb, "December 17, 1936") is True
        assert _value_matches(kb, "17 December 1936") is True
        assert _value_matches(kb, "Dec 17 1936") is True

    def test_nl_date_matches_iso_other(self):
        assert _value_matches("2013-03-13T00:00:00Z", "March 13, 2013") is True

    def test_year_claim_matches_precise_kb(self):
        assert _value_matches("1998-09-04T00:00:00Z", "1998") is True

    def test_date_relation_year_mismatch_is_mismatch(self):
        assert _date_relation("1994", "1998-09-04T00:00:00Z") == "mismatch"
        assert _date_relation("December 17, 1936", "1850-01-01T00:00:00Z") == "mismatch"

    def test_date_relation_same_year_day_diff_abstains(self):
        # Month/day differ but the year agrees: incomparable (KB month/day may be a
        # placeholder; never contradict on it). NOT 'mismatch'.
        assert _date_relation("December 18, 1936", "1936-12-17T00:00:00Z") is None

    def test_date_relation_claim_finer_than_kb_abstains(self):
        # Claim day-precise, KB year-only ('1936') -> KB can't confirm the day.
        assert _date_relation("December 17, 1936", "1936") is None

    def test_date_relation_comparison_phrase_incomparable(self):
        # 'before 1800' / 'the early 1900s' are unparseable as a clean date.
        assert _date_relation("before 1800", "2001-01-15T00:00:00Z") is None
        assert _date_relation("the early 1900s", "1905-01-01T00:00:00Z") is None

    def test_date_relation_non_date_incomparable(self):
        assert _date_relation("Q42", "France") is None
        assert _date_relation("60000000", "1998-09-04T00:00:00Z") is None

    def test_pope_birthdate_nl_verifies_end_to_end(self):
        # The reported case: born_on 'December 17, 1936' vs Wikidata P569
        # '1936-12-17' now VERIFIES (NL date parsed, day==day).
        verifier = _make_verifier(
            [Statement(value="1936-12-17T00:00:00Z", value_type="literal")],
            object_type="time", single_valued=1, kb_property="P569",
        )
        result = verifier.verify(
            _claim(predicate="born_on", object_val="December 17, 1936")
        )
        assert result.verdict == KBVerdictType.VERIFIED

    def test_precise_approx_date_does_not_year_collapse(self):
        # An approximate but PRECISE date "c. 1550-03-01" returns None from the
        # stripper (remainder is not a bare year) and never enters the year path,
        # so it does NOT match KB year 1550 — it stays a strict literal compare.
        # This guards against the marker strip accidentally widening to dates.
        assert _value_matches("1550-01-01T00:00:00Z", "c. 1550-03-01") is False


class TestBceEra:
    """Review finding 3 (§3.2 false-verify): Wikidata serializes a BCE date with a
    leading '-' ('-0044-03-15' = 44 BC) which dateutil silently drops. A BCE date
    must NOT match the same-magnitude CE date (~2x|year| apart)."""

    def test_bce_does_not_match_ce(self):
        assert _value_matches("-1200-01-01T00:00:00Z", "1200") is False
        assert _value_matches("-1200-01-01T00:00:00Z", "1200-01-01") is False
        assert _value_matches("-0044-03-15T00:00:00Z", "0044-03-15") is False

    def test_ce_claim_does_not_match_bce_kb(self):
        # The dangerous direction: an ordinary 4-digit CE claim vs a BCE KB value.
        assert _date_relation("1200", "-1200-01-01T00:00:00Z") == "mismatch"
        assert _date_relation("753", "-0753-01-01T00:00:00Z") is None  # 3-digit: no year token

    def test_bce_matches_same_bce(self):
        # Same era, same magnitude -> still a clean match.
        assert _value_matches("-0044-03-15T00:00:00Z", "-0044-03-15") is True

    def test_bce_year_is_negative(self):
        assert _date_parts("-1200-01-01") == (-1200, 1, 1)
        assert _date_parts("1200-01-01") == (1200, 1, 1)

    def test_zero_padded_sub100_year_not_expanded(self):
        # Hardening: dateutil expands a BARE '0079' to 1979 / '0044' to 2044. The
        # literal 4-digit token is authoritative, so an ancient zero-padded year
        # never mis-parses and never drives a spurious year 'mismatch'.
        assert _date_parts("0079") == (79, None, None)
        assert _date_parts("0044") == (44, None, None)
        assert _date_relation("0079", "+0079-03-15T00:00:00Z") == "match"
        assert _date_relation("0044", "+0044-03-15T00:00:00Z") == "match"
        assert _date_parts("-0044-03-15") == (-44, 3, 15)


class TestDatePlaceholderCap:
    """Review finding 4 (§3.2 false-verify): a Wikidata year-precision date is
    stored as YYYY-01-01 and a month-precision date as YYYY-MM-01. Since we do not
    capture wikibase:timePrecision, a KB day of 1 (and month of 1) may be a
    placeholder — so a claim FINER than the trustworthy precision must abstain, not
    match the mask. A real day-precise KB date (day != 1) is unaffected."""

    def test_day_one_placeholder_does_not_verify_finer_claim(self):
        # 'January 1, 2020' must NOT verify against a possible year-placeholder.
        assert _value_matches("+2020-01-01T00:00:00Z", "January 1, 2020") is False

    def test_month_start_placeholder_does_not_verify_day_claim(self):
        # 'March 1, 2020' (day) vs '+2020-03-01' (possible month-placeholder).
        assert _value_matches("+2020-03-01T00:00:00Z", "March 1, 2020") is False

    def test_month_claim_matches_month_placeholder(self):
        # A claim only as precise as the trustworthy KB precision still verifies.
        assert _value_matches("+2020-03-01T00:00:00Z", "March 2020") is True

    def test_year_claim_matches_jan_one_placeholder(self):
        # Year is never a placeholder, so a bare-year claim still verifies.
        assert _value_matches("+2020-01-01T00:00:00Z", "2020") is True

    def test_real_day_precise_kb_unaffected(self):
        # KB day != 1 is genuinely day-precise -> a matching NL day verifies.
        assert _value_matches("1936-12-17T00:00:00Z", "December 17, 1936") is True


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

    def test_no_statements_disjoint_nongeographic_object_abstains(self):
        # v0.16.1 cycle-2 FULL-STACK REGRESSION (the medium-bar false-contradict):
        # "Germany (Q183) located_in the European Union (Q458)". The P131 binding
        # finds NO statement. The OLD path (b) false-CONTRADICTED: under the
        # part_of alternation both Germany and the EU are a_subsumed_by_b the same
        # continent (Europe Q46) — the EU carries P30=Europe — and Germany<->EU is
        # `unrelated` in both directions (EU membership rides P463, invisible to
        # P131/P30/P17). The path-b object GATE now requires the EXPECTED object
        # to be a confirmed geographic PLACE; the EU is NOT (every is_a place
        # probe returns the default 'unrelated'), so the gate fails closed =>
        # geographic_disjoint False => the disjoint arm does NOT fire => NO_MATCH
        # (ABSTAIN), never CONTRADICTED. The subsumption-UPGRADE arm also does not
        # fire (Germany is not subsumed by the EU under part_of). §3.2.
        kb = _MultiPropKB(
            statements_by_property={"P131": []},
            resolutions={"Germany": "Q183", "European Union": "Q458"},
            subsumptions={
                ("Q183", "Q46", "part_of"): "a_subsumed_by_b",  # Germany ⊂ Europe
                ("Q458", "Q46", "part_of"): "a_subsumed_by_b",  # EU ⊂ Europe (P30)
                # EU is_a <place class> => all 'unrelated' (gate fails closed);
                # Germany ⊂ EU => 'unrelated' (no subsumption-upgrade either)
            },
        )
        meta = _make_meta("located_in", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P131", source="oracle"),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(
            _claim(subject="Germany", predicate="located_in", object_val="European Union")
        )
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.trace.get("no_statements_disjoint_fallback") is None

    def test_no_statements_disjoint_organization_object_abstains(self):
        # v0.16.1 cycle-2 FULL-STACK REGRESSION: "Williams College (Q49112)
        # part_of the Consortium (Q_consortium)". P361 is a location property, so
        # an ORGANIZATIONAL part_of reached the disjoint arm. Even with a
        # hypothetical shared-continent ancestor and mutual non-containment, the
        # consortium is not a confirmed geographic place => the path-b gate fails
        # closed => geographic_disjoint False => NO_MATCH (ABSTAIN), never
        # CONTRADICTED. §3.2.
        assert "P361" in _LOCATION_KB_PROPERTIES
        kb = _MultiPropKB(
            statements_by_property={"P361": []},
            resolutions={"Williams College": "Q49112", "Consortium": "Q_consortium"},
            subsumptions={
                ("Q49112", "Q46", "part_of"): "a_subsumed_by_b",       # hypoth.
                ("Q_consortium", "Q46", "part_of"): "a_subsumed_by_b",  # hypoth.
                # consortium is_a <place class> => all 'unrelated' (gate fails closed)
            },
        )
        meta = _make_meta("part_of", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P361", source="oracle"),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(
            _claim(subject="Williams College", predicate="part_of", object_val="Consortium")
        )
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.trace.get("no_statements_disjoint_fallback") is None

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
# SNVP-1 (soundness gate) — NO-STATEMENTS SUBSUMPTION-UPGRADE IS LOCATION-ONLY
#
# Regression pin for the SNVP-1 fix: the no-statements subsumption-UPGRADE
# (VERIFIED) arm is gated on `_is_location_property(binding.kb_property)`,
# symmetric with the DISJOINT arm. Before the fix the arm was
# predicate-AGNOSTIC: ANY binding whose subject was `a_subsumed_by_b` the
# claim object (under part_of / is_a) false-VERIFIED, even for a non-location
# predicate. Shape it closes: "Amazon works_as river" off "Amazon is_a river"
# — works_as routes to P106 (NOT a location property), the subject is
# subsumed by the object, and with NO P106 statement the old arm would
# promote to VERIFIED. The fix makes a non-location property fall through to
# NO_MATCH (abstain) — §3.2 soundness-over-completeness.
#
# Both binding shapes the fix closes are pinned: a value_type_gated candidate
# binding (the WS2 P106 occupation shape) AND a non-gated legacy_scalar
# binding. The value_type / value_type_gated fields are inert in the
# no-statements arm (the positive value-type gate only runs once statements
# exist), so the location gate is the ONLY thing that blocks the false-verify
# in either shape. The geo case ("Cairo located_in Africa", a LOCATION
# predicate) is re-asserted to still VERIFY — the fix preserves it.
# ===========================================================================


class TestKBVerifierNoStatementsSubsumptionLocationGate:
    def test_constant_nonlocation_property_genuine(self):
        # Guard against a constant rename making the pins pass vacuously: the
        # property the false-verify shape uses (P106 occupation) is genuinely
        # NOT a location-containment property, while the geo property (P131) is.
        assert "P106" not in _LOCATION_KB_PROPERTIES
        assert "P131" in _LOCATION_KB_PROPERTIES

    def test_nonlocation_value_type_gated_binding_subsumed_does_not_verify(self):
        # "Amazon works_as river": works_as → P106 (NOT a location property),
        # single_valued=False, NO statement on the property. The mock KB reports
        # subsumption(subject Q3783, object Q4022, part_of) = a_subsumed_by_b —
        # the subject IS subsumed by the object — so _subsumption_upgrades would
        # return True. But the object (a river class) is NOT a location/
        # containment class, and P106 is not in _LOCATION_KB_PROPERTIES, so the
        # location gate blocks the no-statements VERIFIED arm ⇒ NO_MATCH/abstain,
        # NOT VERIFIED. Pinned for a VALUE_TYPE_GATED candidate binding (the WS2
        # P106 occupation shape); the value-type fields are inert here because
        # the positive value-type gate only runs once statements exist.
        kb = _MultiPropKB(
            statements_by_property={"P106": []},  # no statement on this property
            resolutions={"Amazon": "Q3783", "river": "Q4022"},
            subsumptions={
                ("Q3783", "Q4022", "part_of"): "a_subsumed_by_b",  # subject ⊂ object
                ("Q3783", "Q4022", "is_a"): "a_subsumed_by_b",     # and taxonomically
            },
        )
        meta = _make_meta("works_as", [
            PredicateBinding(
                kb_namespace="wikidata", kb_property="P106",
                single_valued=False,
                value_type_gated=True,
                object_entity_types=["Q28640"],  # profession (the gate's value-type)
                source="ontology_p2302",
            ),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(
            _claim(subject="Amazon", predicate="works_as", object_val="river")
        )
        assert result.verdict == KBVerdictType.NO_MATCH
        # The false-verify arm was NOT taken.
        assert result.trace.get("no_statements_subsumption_fallback") is None
        assert result.trace.get("abstention_reason") == "no_statements"

    def test_nonlocation_legacy_scalar_binding_subsumed_does_not_verify(self):
        # Same false-verify shape, but a NON-gated legacy_scalar binding (no
        # value_type_gated flag, no value-type constraint). The fix closes this
        # path too: P106 is not a location property, so the no-statements
        # subsumption-UPGRADE arm is skipped despite the a_subsumed_by_b
        # subsumption ⇒ NO_MATCH/abstain, NOT VERIFIED.
        kb = _MultiPropKB(
            statements_by_property={"P106": []},
            resolutions={"Amazon": "Q3783", "river": "Q4022"},
            subsumptions={
                ("Q3783", "Q4022", "part_of"): "a_subsumed_by_b",
                ("Q3783", "Q4022", "is_a"): "a_subsumed_by_b",
            },
        )
        meta = _make_meta("works_as", [
            PredicateBinding(
                kb_namespace="wikidata", kb_property="P106",
                single_valued=False,
                value_type_gated=False,
                object_entity_types=None,
                source="legacy_scalar",
            ),
        ])
        verifier = _make_multi_verifier(kb, meta)
        result = verifier.verify(
            _claim(subject="Amazon", predicate="works_as", object_val="river")
        )
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.trace.get("no_statements_subsumption_fallback") is None
        assert result.trace.get("abstention_reason") == "no_statements"

    def test_location_predicate_no_statements_subsumption_still_verifies(self):
        # The fix PRESERVES the geo case: "Cairo located_in Africa" — P131 IS a
        # location property, NO P131 statement, but Cairo ⊂ Africa via part_of ⇒
        # the no-statements subsumption-UPGRADE arm fires and VERIFIES. This is
        # the same pin as TestKBVerifierNoStatementsDisjointArm's ordering proof,
        # re-asserted here as the positive counterpart to the two abstain pins
        # above (same KB shape, location property instead of P106).
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
# C2-3 (§3.2) — a predicate the oracle marked single_valued can still hold
# MULTIPLE DISTINCT values for one subject in the KB data (France P571
# inception = {843 West Francia, 1958 Fifth Republic, ...}). The confirmed
# medium-bar false-contradict pt_006 "France was founded in 843": the claim
# matched none of the returned statements, the first non-matching one was the
# Fifth-Republic 1958 date, and the single_valued promotion CONTRADICTED it —
# unsound, because 843 is itself one of the KB's held values. The fix:
# the single_valued CONTRADICTED branch fires only when the subject presents a
# SINGLE distinct mismatch value (a genuine functional conflict); when it holds
# MULTIPLE distinct values and the claim matched none, abstain (NO_MATCH). The
# VERIFIED match-any loop runs across ALL statements, so a claim matching ANY
# held value still verifies first. The genuine single-value contradiction
# (born_on, one distinct KB date) is preserved — see TestKBVerifierSingleValued
# / TestKBVerifierTimeObjectTypeGate, and the explicit control below.
# ===========================================================================


class TestKBVerifierMultiValuedSingleValued:
    def test_date_predicate_multi_value_claim_matches_none_abstains(self):
        # The France-843 shape (the medium-bar pt_006 false-contradict). P571 is
        # marked single_valued=1, but the KB holds two distinct inception dates
        # (843 West Francia, 1958 Fifth Republic); the claim's year matches
        # neither held value. Pre-fix this promoted to CONTRADICTED off the FIRST
        # non-matching statement (1958). The fix downgrades it to NO_MATCH — a KB
        # property with multiple distinct values is not functionally
        # single-valued here, so a non-match cannot soundly contradict. NOT
        # CONTRADICTED. (Uses 4-digit held/claim years so the demonstration is
        # independent of sub-1000-year normalization; the real France 843 value
        # exercises the same multi-value guard and likewise abstains.)
        stmts = [
            Statement(value="1958-10-04T00:00:00Z", value_type="date"),
            Statement(value="1792-09-21T00:00:00Z", value_type="date"),
        ]
        verifier = _make_verifier(
            stmts, object_type="time", single_valued=1, kb_property="P571"
        )
        result = verifier.verify(_claim(predicate="founded_on", object_val="1700"))
        assert result.verdict == KBVerdictType.NO_MATCH
        assert (
            result.trace.get("abstention_reason")
            == "multi_valued_single_valued_predicate"
        )

    def test_date_predicate_multi_value_claim_matches_one_verifies(self):
        # The pt_006 truth-class control: the claim DOES match ONE of the
        # multiple held values (year 1958). The match-any loop verifies on that
        # statement before any contradiction reasoning — never contradicting a
        # value the KB holds.
        stmts = [
            Statement(value="1958-10-04T00:00:00Z", value_type="date"),
            Statement(value="1792-09-21T00:00:00Z", value_type="date"),
        ]
        verifier = _make_verifier(
            stmts, object_type="time", single_valued=1, kb_property="P571"
        )
        result = verifier.verify(_claim(predicate="founded_on", object_val="1958"))
        assert result.verdict == KBVerdictType.VERIFIED

    def test_entity_predicate_multi_value_claim_matches_none_abstains(self):
        # Same guarantee for an entity-valued single_valued predicate that holds
        # multiple distinct Q-ids: the claim's resolved object matches neither →
        # abstain, never contradict.
        stmts = [
            Statement(value="Q18094", value_type="entity"),  # Honolulu
            Statement(value="Q60", value_type="entity"),      # New York City
        ]
        verifier = _make_verifier(
            stmts, kb_property="P19", single_valued=1,
            resolutions={"Some Other City": "Q1490"},  # Tokyo, matches neither
        )
        result = verifier.verify(
            _claim(predicate="born_in", object_val="Some Other City")
        )
        assert result.verdict == KBVerdictType.NO_MATCH
        assert (
            result.trace.get("abstention_reason")
            == "multi_valued_single_valued_predicate"
        )

    def test_single_distinct_value_still_contradicts(self):
        # Regression pin for the preserved genuine contradiction: when the
        # subject holds exactly ONE distinct value (here repeated across two
        # statement rows), a non-matching precise claim still CONTRADICTS — the
        # multi-value guard only loosens GENUINELY multi-valued subjects.
        stmts = [
            Statement(value="1550-01-01T00:00:00Z", value_type="date"),
            Statement(value="1550-01-01T00:00:00Z", value_type="date"),
        ]
        verifier = _make_verifier(
            stmts, object_type="time", single_valued=1, kb_property="P569"
        )
        result = verifier.verify(_claim(predicate="born_on", object_val="1600"))
        assert result.verdict == KBVerdictType.CONTRADICTED

    def test_same_year_differing_precision_still_contradicts(self):
        # C2S-1 regression pin. A single_valued DATE predicate whose subject
        # holds two NORMAL-rank statements at differing precision for the SAME
        # year — day-precision "+1879-03-14..." and year-precision
        # "+1879-01-01..." (a coarsening of one birth fact; the common Wikidata
        # pattern, since only DeprecatedRank is filtered). A precise WRONG-year
        # claim ("1900") year-matches neither. Keying the distinctness set on the
        # RAW strings would count this subject as multi-valued and over-abstain;
        # keying on the year-normalized value collapses both rows to "1879" (one
        # distinct value), so the genuine wrong-year contradiction is preserved.
        # Without C2S-1 this regressed to NO_MATCH (a §3.2-safe but avoidable
        # over-abstention).
        stmts = [
            Statement(value="+1879-03-14T00:00:00Z", value_type="date"),
            Statement(value="+1879-01-01T00:00:00Z", value_type="date"),
        ]
        verifier = _make_verifier(
            stmts, object_type="time", single_valued=1, kb_property="P569"
        )
        result = verifier.verify(_claim(predicate="born_on", object_val="1900"))
        assert result.verdict == KBVerdictType.CONTRADICTED

    def test_approx_year_matches_one_of_multi_value_verifies(self):
        # WS1 (approximate date) × C2-3 (multi-value single_valued) interplay.
        # A single_valued date predicate (P571) whose subject holds MULTIPLE
        # distinct inception years; the claim is an APPROXIMATE year "c. 1958"
        # that year-matches ONE held value (1958). The match-any loop strips the
        # approximation marker on the claim side, year-matches the 1958
        # statement, and VERIFIES — before any contradiction reasoning. The
        # approximate marker and the multi-value subject both run, and neither
        # turns a held value into a contradiction.
        stmts = [
            Statement(value="1958-10-04T00:00:00Z", value_type="date"),
            Statement(value="1792-09-21T00:00:00Z", value_type="date"),
        ]
        verifier = _make_verifier(
            stmts, object_type="time", single_valued=1, kb_property="P571"
        )
        result = verifier.verify(_claim(predicate="founded_on", object_val="c. 1958"))
        assert result.verdict == KBVerdictType.VERIFIED

    def test_approx_year_matches_none_of_multi_value_abstains(self):
        # WS1 × C2-3 interplay, miss case. The approximate claim "c. 1700"
        # year-matches NEITHER held value (1958, 1792). Two independent §3.2
        # backstops both forbid a contradiction here: (1) the subject presents
        # multiple distinct values, so it is not functionally single-valued; and
        # (2) an approximation may never contradict a nearby exact date. The
        # multi-value guard fires first (it precedes the approx guard in
        # _compare_positive), so the abstention reason is the multi-value one.
        # Either way the outcome is abstain, NOT CONTRADICTED.
        stmts = [
            Statement(value="1958-10-04T00:00:00Z", value_type="date"),
            Statement(value="1792-09-21T00:00:00Z", value_type="date"),
        ]
        verifier = _make_verifier(
            stmts, object_type="time", single_valued=1, kb_property="P571"
        )
        result = verifier.verify(_claim(predicate="founded_on", object_val="c. 1700"))
        assert result.verdict == KBVerdictType.NO_MATCH
        assert (
            result.trace.get("abstention_reason")
            == "multi_valued_single_valued_predicate"
        )

    def test_literal_france_843_multi_value_abstains_not_contradicts(self):
        # The verbatim France-843 medium-bar pt_006 shape, with the REAL sub-1000
        # year values the KB stores (West Francia 843 = "+0843-...", Fifth
        # Republic 1958). The literal claim object "843" is 3 digits, so it never
        # enters the 4-digit year-normalized compare (a documented limitation of
        # _BARE_YEAR_RE / _normalize_date_value) and matches no statement
        # literally. Pre-fix that promoted to a false CONTRADICTED off the 1958
        # statement; the multi-value guard now downgrades it to NO_MATCH/abstain.
        # This pins the SAFE behavior: §3.2 paramount is "never contradict", and
        # the sub-1000-year claim correctly abstains rather than VERIFYing or
        # CONTRADICTing. (If the year normalizer is later widened to <4-digit
        # years this should flip to VERIFIED via match-any, never CONTRADICTED.)
        stmts = [
            Statement(value="+0843-01-01T00:00:00Z", value_type="date"),
            Statement(value="1958-10-04T00:00:00Z", value_type="date"),
        ]
        verifier = _make_verifier(
            stmts, object_type="time", single_valued=1, kb_property="P571"
        )
        result = verifier.verify(_claim(predicate="founded_on", object_val="843"))
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.verdict != KBVerdictType.CONTRADICTED
        assert (
            result.trace.get("abstention_reason")
            == "multi_valued_single_valued_predicate"
        )

    def test_comparison_phrase_object_abstains_not_contradicts(self):
        # C2-FC1 regression pin (csu_003 shape, "founded before 1800"). The
        # extractor sometimes maps "founded before YYYY" to a founded_in_year
        # claim whose OBJECT is the literal comparison phrase "before 1800", and
        # a vague subject ("a university") resolves to some specific entity with
        # a single KB inception date (here 2001). The claim object does not
        # parse to a year, so comparing the KB date against it is ill-defined:
        # a non-match is a PARSE failure, not falsity. Pre-C2-FC1 this single
        # distinct KB value promoted to a false CONTRADICTED; the parse guard
        # now abstains. §3.2 (never contradict on an unparseable object).
        stmts = [Statement(value="2001-01-15T00:00:00Z", value_type="literal")]
        verifier = _make_verifier(
            stmts, object_type="time", single_valued=1, kb_property="P571"
        )
        result = verifier.verify(
            _claim(predicate="founded_in_year", object_val="before 1800")
        )
        assert result.verdict == KBVerdictType.NO_MATCH
        assert result.verdict != KBVerdictType.CONTRADICTED
        assert result.trace.get("abstention_reason") == "date_not_a_clean_mismatch"

    def test_clean_wrong_year_object_still_contradicts(self):
        # C2-FC1 non-vacuity / preservation dual. A CLEAN 4-digit year object
        # ("1850") that does not match the single KB inception year (1793) is a
        # genuine functional conflict and MUST still CONTRADICT — the parse
        # guard only suppresses objects that do NOT normalize to a year. This
        # pins that the guard did not over-broaden into masking real wrong-year
        # contradictions.
        stmts = [Statement(value="1793-01-01T00:00:00Z", value_type="literal")]
        verifier = _make_verifier(
            stmts, object_type="time", single_valued=1, kb_property="P571"
        )
        result = verifier.verify(
            _claim(predicate="founded_in_year", object_val="1850")
        )
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


# ---------------------------------------------------------------------------
# TestEndDateOrdering (E4): precision-aware ordering of a P582 end date vs now.
# `provably_past` is the STRICT gate for the level-2 contradiction (its latest
# possible instant must precede now); `provably_future` keeps a not-yet-over term
# current. A year-precision end in the CURRENT year is neither (ambiguous → safe).
# ---------------------------------------------------------------------------

class TestEndDateOrdering:
    NOW = "2026-06-03T00:00:00+00:00"

    def test_provably_past_day(self):
        assert _end_provably_past("2017-01-20", self.NOW) is True

    def test_provably_past_prior_year(self):
        assert _end_provably_past("2025", self.NOW) is True

    def test_current_year_not_provably_past(self):
        # Year precision: '2026' could denote any day in 2026 (incl. future) — its
        # latest instant (Dec 31) is not before now, so NOT provably past.
        assert _end_provably_past("2026", self.NOW) is False

    def test_future_not_past(self):
        assert _end_provably_past("2099", self.NOW) is False

    def test_unparseable_not_past(self):
        assert _end_provably_past("ongoing", self.NOW) is False
        assert _end_provably_past(None, self.NOW) is False

    def test_provably_future(self):
        assert _end_provably_future("2099", self.NOW) is True
        assert _end_provably_future("2027-03-01", self.NOW) is True

    def test_current_year_not_provably_future(self):
        assert _end_provably_future("2026", self.NOW) is False

    def test_past_not_future(self):
        assert _end_provably_future("2017-01-20", self.NOW) is False


# ---------------------------------------------------------------------------
# TestTemporalCurrencyRoles (E4 §3.2): a present-tense role/state claim must not
# verify off an ENDED statement (level 1), and a present-tense claim whose value
# matched ONLY provably-ended statements is CONTRADICTED (level 2 — the wrong-pope
# catch). A PAST claim ("X was the pope") still verifies off the ended statement.
# All cases pin current_time for determinism.
# ---------------------------------------------------------------------------

class TestTemporalCurrencyRoles:
    NOW = "2026-06-03T00:00:00+00:00"

    def _ended(self, end="2017-01-20", start="2009-01-20", value="Q11696"):
        return Statement(
            value=value, value_type="entity",
            qualifiers={"P580": start, "P582": end},
        )

    def test_present_role_ended_contradicts(self):
        # "Obama holds_role President" (present tense) but the P39 statement ENDED
        # in 2017 -> the present assertion is false -> CONTRADICTED (E4 level 2).
        v = _make_verifier([self._ended()], single_valued=0)
        r = v.verify(_claim(), current_time=self.NOW)
        assert r.verdict == KBVerdictType.CONTRADICTED

    def test_past_role_ended_verifies(self):
        # "Obama WAS president" (valid_until == before_present) verifies off the
        # SAME ended statement — a past claim asserts no present currency, so
        # neither level fires. This is the critical "don't contradict history" case.
        v = _make_verifier([self._ended()], single_valued=0)
        r = v.verify(_claim(valid_until="before_present"), current_time=self.NOW)
        assert r.verdict == KBVerdictType.VERIFIED

    def test_present_role_ongoing_verifies(self):
        # An ongoing statement (no P582) verifies a present-tense claim.
        stmt = Statement(value="Q11696", value_type="entity",
                         qualifiers={"P580": "2009-01-20"})
        v = _make_verifier([stmt], single_valued=0)
        r = v.verify(_claim(), current_time=self.NOW)
        assert r.verdict == KBVerdictType.VERIFIED

    def test_present_role_future_end_verifies(self):
        # A provably-future end (a term not yet over) is still current -> verify.
        v = _make_verifier([self._ended(end="2099-01-20")], single_valued=0)
        r = v.verify(_claim(), current_time=self.NOW)
        assert r.verdict == KBVerdictType.VERIFIED

    def test_present_role_end_this_year_abstains(self):
        # Year-precision end in the CURRENT year is ambiguous (could be a past or a
        # future day) -> neither provably past nor future -> abstain, NEVER
        # contradict (matched_not_past blocks level 2).
        v = _make_verifier([self._ended(end="2026")], single_valued=0)
        r = v.verify(_claim(), current_time=self.NOW)
        assert r.verdict == KBVerdictType.NO_MATCH

    def test_present_role_mixed_current_and_ended_verifies(self):
        # One ended + one ongoing statement for the SAME value -> the current one
        # verifies; the ended one never forces a contradiction.
        ongoing = Statement(value="Q11696", value_type="entity",
                            qualifiers={"P580": "2009-01-20"})
        v = _make_verifier([self._ended(), ongoing], single_valued=0)
        r = v.verify(_claim(), current_time=self.NOW)
        assert r.verdict == KBVerdictType.VERIFIED

    def test_present_role_all_ended_contradicts(self):
        # Multiple matching statements, ALL provably ended -> CONTRADICTED.
        v = _make_verifier(
            [self._ended(end="2013-01-20"),
             self._ended(start="2013-01-20", end="2017-01-20")],
            single_valued=0,
        )
        r = v.verify(_claim(), current_time=self.NOW)
        assert r.verdict == KBVerdictType.CONTRADICTED

    def test_present_role_ended_different_value_abstains(self):
        # The ended statement is a DIFFERENT role (Senator) — value doesn't match
        # the claim ("President") -> no contradiction, abstain. A present role claim
        # is never contradicted off an UNRELATED ended role.
        senator = Statement(value="Q4416090", value_type="entity",
                            qualifiers={"P580": "2005-01-03", "P582": "2008-11-16"})
        v = _make_verifier([senator], single_valued=0)
        r = v.verify(_claim(object_val="President of the United States"),
                     current_time=self.NOW)
        assert r.verdict == KBVerdictType.NO_MATCH

    def test_negated_present_role_ended_verifies(self):
        # Polarity: "Obama is NOT president" (polarity 0) when the role ended -> the
        # positive content CONTRADICTS, inverted to VERIFIED ("not president" is
        # true now). Soundness in the negated direction.
        v = _make_verifier([self._ended()], single_valued=0)
        r = v.verify(_claim(polarity=0), current_time=self.NOW)
        assert r.verdict == KBVerdictType.VERIFIED

    def test_scoped_since_claim_off_ended_abstains_not_contradicts(self):
        # "X has held the role since 2009" (valid_from set, valid_until None) reaches
        # the present, so level 1 makes the ended statement incompatible -> abstain.
        # It is NOT fully unscoped, so level 2 does NOT contradict (safe: abstain).
        v = _make_verifier([self._ended()], single_valued=0)
        r = v.verify(_claim(valid_from="2009-01-20"), current_time=self.NOW)
        assert r.verdict == KBVerdictType.NO_MATCH

    # --- Review finding 1 (§3.2 false-verify): a role/term that has NOT YET BEGUN
    # (statement START provably in the future) must not verify a present claim. ---

    def test_present_role_future_start_abstains(self):
        # Future P580, no end: an announced/scheduled term not yet begun -> abstain.
        stmt = Statement(value="Q11696", value_type="entity",
                         qualifiers={"P580": "2030-01-01"})
        v = _make_verifier([stmt], single_valued=0)
        r = v.verify(_claim(), current_time=self.NOW)
        assert r.verdict == KBVerdictType.NO_MATCH

    def test_present_role_fully_future_interval_abstains(self):
        # A fully-future [start, end] term -> abstain (the provably-future END alone
        # used to slip past the currency gate).
        v = _make_verifier([self._ended(start="2030-01-01", end="2035-01-01")],
                           single_valued=0)
        r = v.verify(_claim(), current_time=self.NOW)
        assert r.verdict == KBVerdictType.NO_MATCH

    def test_past_role_future_start_abstains(self):
        # A PAST claim ("X was president") also cannot be satisfied by a not-yet-
        # begun term.
        stmt = Statement(value="Q11696", value_type="entity",
                         qualifiers={"P580": "2030-01-01"})
        v = _make_verifier([stmt], single_valued=0)
        r = v.verify(_claim(valid_until="before_present"), current_time=self.NOW)
        assert r.verdict == KBVerdictType.NO_MATCH

    def test_present_role_past_start_no_end_still_verifies(self):
        # Control: a genuinely-ongoing statement (past start, no end) is NOT provably
        # future and still verifies a present claim — the start gate stays narrow.
        stmt = Statement(value="Q11696", value_type="entity",
                         qualifiers={"P580": "2009-01-20"})
        v = _make_verifier([stmt], single_valued=0)
        r = v.verify(_claim(), current_time=self.NOW)
        assert r.verdict == KBVerdictType.VERIFIED

    # --- Review findings 2 & 5 (§3.2 false-contradict): temporal currency is gated
    # to ENTITY role/state values. A DATE predicate carrying a stray P582 on a
    # value-matching statement must VERIFY (the claim is true), never CONTRADICT. ---

    def test_date_predicate_with_stray_end_verifies_not_contradicts(self):
        stmt = Statement(value="1879-03-14T00:00:00Z", value_type="literal",
                         qualifiers={"P580": "1879-03-14", "P582": "1900-01-01"})
        v = _make_verifier([stmt], object_type="time", single_valued=1,
                           kb_property="P569")
        r = v.verify(_claim(predicate="born_on", object_val="1879"),
                     current_time=self.NOW)
        assert r.verdict == KBVerdictType.VERIFIED


# ---------------------------------------------------------------------------
# v0.16.2 directed-over-enumerate METADATA signal. `functional_entity_predicate`
# is the walker's "skip ALL neighbor enumeration" gate for a functional entity
# predicate. Unlike functional_value_known / value_known_entity (which are
# `bool(statements) and ...`), it is derived from binding METADATA, so it is
# present on the NO_MATCH paths that never looked up statements — the fix for the
# live "Obama born_in Kenya" fanout (subject unresolved / no statement on P19).
# ---------------------------------------------------------------------------


class TestFunctionalEntityPredicateSignal:
    NOW = "2026-06-04T00:00:00+00:00"

    def _trace(self, statements, *, subject, single_valued, object_type, kb_property,
               resolutions):
        v = _make_verifier(statements, kb_property=kb_property, object_type=object_type,
                           single_valued=single_valued, resolutions=resolutions)
        claim = _claim(subject=subject, predicate="rel", object_val="Kenya")
        return v.verify(claim, current_time=self.NOW).trace

    def test_present_on_no_statements_path(self):
        # Subject resolves but carries no statement on the property → no_statements
        # NO_MATCH. The metadata signal is True even though statements is empty.
        t = self._trace([], subject="Obama", single_valued=1, object_type="entity",
                        kb_property="P19", resolutions={"Kenya": "Q114"})
        assert t.get("functional_entity_predicate") is True
        assert t.get("functional_value_known") is False  # statements-based: False
        assert t.get("value_known_entity") is False

    def test_present_on_subject_unresolved_path(self):
        # Subject does not resolve at all → subject_resolution_failed NO_MATCH.
        t = self._trace([], subject="Zxqwobama", single_valued=1, object_type="entity",
                        kb_property="P19", resolutions={"Kenya": "Q114"})
        assert t.get("functional_entity_predicate") is True

    def test_absent_for_non_functional_predicate(self):
        # single_valued=0 → not a functional entity predicate (enumeration may
        # legitimately ground), so the signal must be False.
        t = self._trace([], subject="Obama", single_valued=0, object_type="entity",
                        kb_property="P19", resolutions={"Obama": "Q76", "Kenya": "Q114"})
        assert t.get("functional_entity_predicate") is False

    def test_absent_for_non_entity_predicate(self):
        # object_type='time' (a functional DATE predicate) is not an ENTITY
        # predicate → no part_of/is_a containment substitution applies → False.
        t = self._trace([], subject="Obama", single_valued=1, object_type="time",
                        kb_property="P569", resolutions={"Obama": "Q76"})
        assert t.get("functional_entity_predicate") is False
