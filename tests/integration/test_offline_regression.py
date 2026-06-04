"""Offline (mocked, no live API/network) regression net for the v0.16.x
soundness + coverage fixes — the DURABLE replacement for the operator-only live
medium bar. Lives under tests/integration so it runs in the gated suite / CI
(no live calls — pure mock doubles).

Each case drives the REAL verdict pipeline — the real `Walker` (routing +
discover/verify), the real `KBVerifier` (the WS1/WS2/WS5 verdict code), and the
real `Aggregator.compose_statement_verdict` (the WS3 traced rollup AedosRunner
uses) — against MOCK KB + LLM doubles. The mocks supply only the KB facts and
the predicate-metadata routing; every verdict decision is made by production
code. The benchmark harness's `compute_metrics` then confirms the run would
clear the two HARD soundness gates (`false_verified == 0`,
`false_contradicted == 0`).

Pinned fixes:
  - mhd_018-shape "Vatican is in Africa" -> contradicted   (WS5a geo-disjoint)
  - circa-date "Tadhg ... born c. 1550" vs KB year 1550 -> NOT contradicted
    (WS1 approximate-date false-contradict fix)
  - copula-occupation "Robby Krieger is a guitarist" -> verified via P106 (WS2)
  - cycle-2 geo place gate "Germany in the EU" / "Williams part_of Consortium"
    -> NOT contradicted (C2-1/C2-2: a non-place object can't be a disjoint
    sub-region)
  - cycle-2 multi-value single_valued "France founded_on 843" (P571 = {843, 1958})
    -> NOT contradicted (C2-3: never contradict a value the KB holds)
  - cycle-2 comparison-phrase object "founded_in_year 'before 1800'" -> NOT
    contradicted (C2-FC1: never contradict on an object that doesn't parse to a
    year; the csu_003 false-contradict the final MB surfaced)

The three cycle-2 shapes were the false-contradicts the WS7 false_contradicted
gate surfaced in the final v0.16.1 medium bar; they are pinned here through the
SAME compute_metrics/soundness_gates path so a CI regression that reopens any of
them trips `false_contradicted == 0`, not only the unit pins.
"""

from __future__ import annotations

from aedos.database import open_memory_db
from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer3_substrate import Substrate
from aedos.layer3_substrate.predicate_translation import (
    PredicateBinding,
    PredicateMetadata,
)
from aedos.layer3_substrate.resolver import EntityResolver
from aedos.layer4_sources.kb_protocol import (
    ResolutionCandidate,
    Statement,
    SubsumptionResult,
)
from aedos.layer4_sources.kb_verifier import KBVerifier
from aedos.layer4_sources.tier_u import TierU
from aedos.layer4_sources.walker import VerificationContext, Walker
from aedos.layer5_result.aggregator import Aggregator
# v0.16.1 WS5a: the geo predicate cluster lives in the WikidataAdapter behind
# the kb_protocol seam. The mock KB mixes in the relocated accessors so the
# walker/verifier geo-disjoint path runs the SAME production code under mocks.
from aedos.layer4_sources.kb_wikidata import (
    _GEO_CONTAINER_TYPES,
    _LOCATION_KB_PROPERTIES,
    _geographic_disjoint,
)
from tests.evaluation.benchmark import (
    BenchmarkCase,
    RunResult,
    compute_metrics,
    soundness_gates,
)


# ---------------------------------------------------------------------------
# Mock doubles (no live API / network)
# ---------------------------------------------------------------------------

class _GeoMixin:
    """Relocated geographic protocol surface, delegating to the adapter's real
    logic driven by the mock's own `subsumption` — keeps the geo-disjoint path
    byte-identical under mocks."""

    def is_location_property(self, kb_property):
        return kb_property in _LOCATION_KB_PROPERTIES

    def geo_container_types(self):
        return _GEO_CONTAINER_TYPES

    def geographic_disjoint(self, value_qid, expected_qid):
        return _geographic_disjoint(self.subsumption, value_qid, expected_qid)


class _MockKB(_GeoMixin):
    """KB keyed BY PROPERTY for lookups, with configurable resolutions and
    pairwise subsumptions. Nothing here decides a verdict — it only returns
    facts the production verifier reasons over."""

    def __init__(self, statements_by_property=None, resolutions=None, subsumptions=None):
        self._by_prop = statements_by_property or {}
        self._resolutions = resolutions or {}
        self._subsumptions = subsumptions or {}

    def resolve_entity(self, reference, local_context):
        qid = self._resolutions.get(reference)
        return [ResolutionCandidate(kb_identifier=qid, score=0.95)] if qid else []

    def lookup_statements(self, entity, predicate):
        return list(self._by_prop.get(predicate, []))

    def subsumption(self, entity_a, entity_b, relation_type):
        verdict = self._subsumptions.get((entity_a, entity_b, relation_type), "unrelated")
        return SubsumptionResult(verdict=verdict)


class _StubPT:
    """PredicateTranslation stand-in: `consult` returns a fixed
    PredicateMetadata so the test pins the exact routing + binding shape the
    walker and verifier both read. No LLM call is ever made."""

    def __init__(self, meta: PredicateMetadata):
        self._meta = meta

    def consult(self, predicate, kb_namespace=None):
        return self._meta


def _meta(predicate, bindings, *, object_type="entity", single_valued=0):
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
        reason="offline-regression",
        created_at="t",
        bindings=bindings,
        single_valued=single_valued,
    )


def _build(meta: PredicateMetadata, kb: _MockKB):
    """Construct the REAL Walker + Substrate + KBVerifier + Aggregator + TierU
    over a fresh in-memory DB. Tier U is empty (every lookup misses), so the
    walk falls through to the real external-grounding KB path."""
    db = open_memory_db()
    pt = _StubPT(meta)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    # The walker reads routing only off substrate.predicate_translation.consult;
    # the kb_resolvable cases here never touch subsumption / predicate_distribution.
    substrate = Substrate(
        resolver=resolver,
        predicate_translation=pt,
        subsumption=None,
        predicate_distribution=None,
    )
    walker = Walker(
        tier_u=TierU(db=db),
        kb_verifier=kb_verifier,
        python_verifier=None,
        substrate=substrate,
        kb=kb,
    )
    aggregator = Aggregator(db=db)
    return walker, aggregator


def _claim(subject, predicate, obj, polarity=1):
    return Claim(
        claim_id="c1",
        subject=subject,
        predicate=predicate,
        object=obj,
        polarity=polarity,
        source_text=f"{subject} {predicate} {obj}",
        asserting_party="offline_regression",
        triage_decision=TriageDecision.VERIFY,
    )


def _verdict(meta, kb, claim):
    """Run the claim through the real walk + the real statement-verdict
    composition — exactly the path AedosRunner uses — and return the verdict."""
    walker, aggregator = _build(meta, kb)
    ctx = VerificationContext(
        current_time="2026-01-01T00:00:00Z",
        asserting_party="offline_regression",
        source_text=claim.source_text,
    )
    result = walker.walk(claim, ctx)
    statement = aggregator.compose_statement_verdict([result], source_text=claim.source_text)
    return statement.verdict


# ---------------------------------------------------------------------------
# Pinned regression cases
# ---------------------------------------------------------------------------

class TestOfflineRegression:
    def test_mhd_018_vatican_in_africa_contradicted(self):
        # WS5a geo-disjoint. The Vatican (Q237) has no P131 statement but is
        # part_of Europe (Q46) and unrelated to the claimed container Africa
        # (Q15) — geographically disjoint => CONTRADICTED. (mhd_018 shape.)
        kb = _MockKB(
            statements_by_property={"P131": []},
            resolutions={"Vatican": "Q237", "Africa": "Q15"},
            subsumptions={("Q237", "Q46", "part_of"): "a_subsumed_by_b"},
        )
        meta = _meta("located_in", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P131", source="oracle"),
        ])
        verdict = _verdict(meta, kb, _claim("Vatican", "located_in", "Africa"))
        assert verdict == "contradicted"

    def test_circa_date_not_contradicted(self):
        # WS1 approximate-date fix. "born c. 1550" against KB year 1550 must
        # MATCH on year-equality (verified), never CONTRADICTED — the
        # false-contradict the v0.16 review missed. born_on is single_valued, so
        # pre-fix it flipped to contradicted; post-fix it verifies.
        # value_type="literal" mirrors the REAL WikidataAdapter: its SPARQL
        # binds `IF(isURI(?value), "entity", "literal")`, so a P569 date literal
        # is tagged "literal" (never "time"/"date"). single_valued=True on the
        # BINDING (not just the metadata) is what licenses the contradiction
        # promotion — without it the predicate is multi-valued and can never
        # contradict, so the pin below would pass vacuously.
        kb = _MockKB(
            statements_by_property={"P569": [Statement(value="1550-01-01T00:00:00Z", value_type="literal")]},
            resolutions={"Tadhg Dall O hUiginn": "Q1"},
        )
        meta = _meta("born_on", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P569", source="oracle",
                             single_valued=True),
        ], object_type="time", single_valued=1)
        verdict = _verdict(meta, kb, _claim("Tadhg Dall O hUiginn", "born_on", "c. 1550"))
        assert verdict != "contradicted"
        assert verdict == "verified"

    def test_circa_date_mismatch_abstains_not_contradicts(self):
        # The soundness dual: "born c. 1550" against KB year 1600 must ABSTAIN
        # (an approximation cannot soundly contradict a nearby exact date), never
        # CONTRADICTED — even though born_on is single_valued.
        # Faithful mock (value_type="literal") + single_valued BINDING so the
        # single-valued contradiction-promotion path is actually reachable —
        # this is what makes the abstain assertion a genuine pin of the WS1
        # suppression (line kb_verifier _is_approx_year). With value_type="time"
        # or a multi-valued binding the verdict abstains for an unrelated reason
        # and the pin is vacuous; here, reverting the WS1 suppression flips this
        # to CONTRADICTED.
        kb = _MockKB(
            statements_by_property={"P569": [Statement(value="1600-01-01T00:00:00Z", value_type="literal")]},
            resolutions={"Tadhg Dall O hUiginn": "Q1"},
        )
        meta = _meta("born_on", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P569", source="oracle",
                             single_valued=True),
        ], object_type="time", single_valued=1)
        verdict = _verdict(meta, kb, _claim("Tadhg Dall O hUiginn", "born_on", "c. 1550"))
        assert verdict != "contradicted"

    def test_copula_occupation_verified_via_p106(self):
        # WS2 occupation-copula grounding. "Robby Krieger is a guitarist"
        # routes the instance_of copula to bindings [P31, P106]; P31 (Q5 human)
        # never grounds the occupation, but P106 holds the guitarist Q-id and
        # the resolved object is a confirmed occupation class (P106's value
        # type) => VERIFIED via the positive P106 path.
        guitarist_qid = "Q855091"
        occupation_class = "Q28640"  # profession (P106 value type)
        kb = _MockKB(
            statements_by_property={
                "P31": [Statement(value="Q5", value_type="entity")],         # human (no match)
                "P106": [Statement(value=guitarist_qid, value_type="entity")],  # guitarist (match)
            },
            resolutions={"Robby Krieger": "Q314459", "guitarist": guitarist_qid},
            # The object (guitarist) is a confirmed occupation/profession class,
            # gating the positive P106 match (fail-closed otherwise).
            subsumptions={(guitarist_qid, occupation_class, "is_a"): "a_subsumed_by_b"},
        )
        # This pin guards the WS2 DISCOVERY side: the synthesized value_type_
        # gated P106 candidate binding producing a VERIFIED through the POSITIVE
        # grounding path. It is NOT a test of the gate's BLOCKING behavior — that
        # (a wrong-value-type object failing to confirm => abstain) is pinned in
        # test_kb_verifier.py (TestKBVerifierCopulaValueTypeFix / the WS2 positive
        # gate). Here the gate is satisfied (the `subsumptions` entry proves the
        # object IS an occupation/profession class), so it licenses the verify.
        # The P106 candidate is synthesized exactly as production does it
        # (predicate_translation.py WS2): source="candidate", value_type_gated=True.
        # value_type_gated is LOAD-BEARING because it routes the positive match
        # through the FAIL-CLOSED `_object_confirms_value_type` gate, so the
        # occupation evidence above is what licenses VERIFIED. Without
        # value_type_gated the binding would verify on a plain value match and the
        # WS2 grounding path would never be exercised (a vacuous pin); reverting
        # the WS2 positive gate flips this to abstain (the discovery is lost).
        meta = _meta("instance_of", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P31", source="oracle"),
            PredicateBinding(
                kb_namespace="wikidata", kb_property="P106", source="candidate",
                object_entity_types=[occupation_class], value_type_gated=True,
            ),
        ])
        verdict = _verdict(meta, kb, _claim("Robby Krieger", "instance_of", "guitarist"))
        assert verdict == "verified"

    def test_copula_nonoccupation_object_not_verified_via_p106(self):
        # WS2 fail-closed gate, end-to-end negative dual of the case above. The
        # copula object does NOT subsume into the occupation/profession class, so
        # the value_type_gated P106 candidate binding must NOT verify even when a
        # P106 statement VALUE happens to match the resolved object. Shape:
        # "Amazon is a river" routed [P31, P106]; the P106 statement value equals
        # the resolved object (river Q4022), so `_compare_positive` would return
        # VERIFIED — but the object is NOT a confirmed occupation class (no
        # subsumption entry => `unrelated`), so the FAIL-CLOSED
        # `_object_confirms_value_type` gate blocks the positive grounding and the
        # binding abstains. P31 (Q5 human, multi-valued) does not match the river
        # object either => overall NOT verified, NOT contradicted (abstain). This
        # is the false-verify the WS2 positive gate closes; reverting the gate
        # flips this to a (false) verified.
        river_qid = "Q4022"
        occupation_class = "Q28640"  # profession (P106 value type)
        kb = _MockKB(
            statements_by_property={
                "P31": [Statement(value="Q5", value_type="entity")],          # human (no match)
                "P106": [Statement(value=river_qid, value_type="entity")],     # matches object value
            },
            resolutions={"Amazon": "Q3783", "river": river_qid},
            # No (river_qid, occupation_class, "is_a") entry => `unrelated` =>
            # the object is NOT a confirmed occupation class => gate fails closed.
            subsumptions={},
        )
        meta = _meta("instance_of", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P31", source="oracle"),
            PredicateBinding(
                kb_namespace="wikidata", kb_property="P106", source="candidate",
                object_entity_types=[occupation_class], value_type_gated=True,
            ),
        ])
        verdict = _verdict(meta, kb, _claim("Amazon", "instance_of", "river"))
        assert verdict != "verified"
        assert verdict != "contradicted"

    def test_germany_in_eu_not_contradicted(self):
        # cycle-2 C2-1/C2-2 geo place gate, mhd_002 shape. "Germany located_in
        # the European Union" routes located_in -> P131; Germany carries no P131
        # statement, so the no-statements geo-disjoint arm runs
        # _geographic_disjoint(Germany Q183, EU Q458). Pre-fix path b fired
        # (Germany and the EU are both subsumed by Europe Q46 and mutually
        # unrelated) and FALSE-CONTRADICTED a TRUE membership claim. The cycle-2
        # gate requires the EXPECTED object to be a subsumption-confirmed
        # geographic PLACE; the EU is a union, not in _GEO_PLACE_CLASSES (no
        # is_a place entry below), so the gate fails closed -> disjoint False ->
        # NO_MATCH (abstain). NON-VACUOUS: drop the gate and path b restores
        # CONTRADICTED.
        kb = _MockKB(
            statements_by_property={"P131": []},
            resolutions={"Germany": "Q183", "the European Union": "Q458"},
            subsumptions={
                ("Q183", "Q46", "part_of"): "a_subsumed_by_b",  # Germany in Europe
                ("Q458", "Q46", "part_of"): "a_subsumed_by_b",  # EU in Europe
                # (Q183,Q458)/(Q458,Q183) default 'unrelated' -> mutual non-containment;
                # NO (Q458, <place_class>, "is_a") entry -> EU not a confirmed place.
            },
        )
        meta = _meta("located_in", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P131", source="oracle"),
        ])
        verdict = _verdict(meta, kb, _claim("Germany", "located_in", "the European Union"))
        assert verdict != "contradicted"

    def test_williams_part_of_consortium_not_contradicted(self):
        # cycle-2 C2-1/C2-2 geo place gate, pt_004 shape. "Williams College
        # part_of the Consortium ..." routes part_of -> P361 (in the geographic
        # part_of alternation, so a location property). Williams carries no P361
        # statement -> the no-statements geo-disjoint arm runs
        # _geographic_disjoint(Williams Q49205, Consortium). Pre-fix path b fired
        # (both subsumed by North America Q49, mutually unrelated) and
        # FALSE-CONTRADICTED a TRUE membership claim. A consortium is an
        # organization, not a geographic place (no is_a place entry), so the gate
        # fails closed -> NO_MATCH (abstain). NON-VACUOUS: drop the gate -> path b
        # restores CONTRADICTED.
        kb = _MockKB(
            statements_by_property={"P361": []},
            resolutions={
                "Williams College": "Q49166",
                "the Consortium of Liberal Arts Colleges": "Q5165061",  # illustrative Q
            },
            subsumptions={
                ("Q49166", "Q49", "part_of"): "a_subsumed_by_b",    # Williams in North America
                ("Q5165061", "Q49", "part_of"): "a_subsumed_by_b",  # consortium sits on the same continent
                # mutual unrelated (default); NO is_a place entry for the consortium.
            },
        )
        meta = _meta("part_of", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P361", source="oracle"),
        ])
        verdict = _verdict(
            meta, kb,
            _claim("Williams College", "part_of", "the Consortium of Liberal Arts Colleges"),
        )
        assert verdict != "contradicted"

    def test_france_843_multi_value_not_contradicted(self):
        # cycle-2 C2-3 multi-value single_valued, pt_006 shape. "France
        # founded_on 843" routes founded_on -> P571 (single_valued). France's
        # P571 holds MULTIPLE distinct inception years — West Francia +0843 and
        # Fifth Republic 1958. The literal claim "843" (3 digits) year-matches
        # neither, and the subject presents >1 distinct (year-normalized) value,
        # so a non-match is NOT a functional conflict -> NO_MATCH (abstain),
        # never contradicting a value the KB actually holds. value_type="literal"
        # mirrors the real adapter (P571 dates bind as literals). NON-VACUOUS:
        # drop the multi-value gate -> the single_valued path CONTRADICTS off the
        # 1958 statement.
        kb = _MockKB(
            statements_by_property={"P571": [
                Statement(value="+0843-01-01T00:00:00Z", value_type="literal"),
                Statement(value="1958-10-04T00:00:00Z", value_type="literal"),
            ]},
            resolutions={"France": "Q142"},
        )
        meta = _meta("founded_on", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P571", source="oracle",
                             single_valued=True),
        ], object_type="time", single_valued=1)
        verdict = _verdict(meta, kb, _claim("France", "founded_on", "843"))
        assert verdict != "contradicted"

    def test_founded_before_comparison_phrase_not_contradicted(self):
        # cycle-2 C2-FC1, csu_003 shape. "founded before 1800" is sometimes
        # extracted as a founded_in_year claim whose OBJECT is the literal
        # comparison phrase "before 1800", while a vague subject ("a university")
        # resolves to a specific entity holding a single KB inception date (2001
        # here). The object does not parse to a year, so comparing the KB date
        # against it is ill-defined -> NO_MATCH (abstain), never CONTRADICTED.
        # NON-VACUOUS: a clean 4-digit wrong-year object still contradicts (pinned
        # in test_kb_verifier.test_clean_wrong_year_object_still_contradicts);
        # dropping the parse guard flips this to contradicted.
        kb = _MockKB(
            statements_by_property={"P571": [
                Statement(value="2001-01-15T00:00:00Z", value_type="literal")]},
            resolutions={"a university": "Q42"},
        )
        meta = _meta("founded_in_year", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P571", source="oracle",
                             single_valued=True),
        ], object_type="time", single_valued=1)
        verdict = _verdict(meta, kb, _claim("a university", "founded_in_year", "before 1800"))
        assert verdict != "contradicted"

    def test_cycle2_shapes_clear_false_contradicted_gate(self):
        # End-to-end harness gate for the cycle-2 shapes — the durable CI net the
        # WS7 false_contradicted gate is built on. Each verdict is produced by a
        # real walk above and fed through compute_metrics / soundness_gates.
        # ground_truth is the abstain class: the §3.2-SAFE outcome the post-fix
        # engine produces under these minimal mocks (it has no positive
        # membership evidence to VERIFY, so it grounds to abstain rather than
        # verify). The pin's job is the false_contradicted gate, not coverage —
        # a regression that reopens any of these false-contradicts flips the
        # verdict to "contradicted" (gt != contradicted) and trips
        # false_contradicted == 0.
        germany_kb = _MockKB(
            statements_by_property={"P131": []},
            resolutions={"Germany": "Q183", "the European Union": "Q458"},
            subsumptions={
                ("Q183", "Q46", "part_of"): "a_subsumed_by_b",
                ("Q458", "Q46", "part_of"): "a_subsumed_by_b",
            })
        germany_meta = _meta("located_in", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P131", source="oracle")])

        williams_kb = _MockKB(
            statements_by_property={"P361": []},
            resolutions={"Williams College": "Q49166",
                         "the Consortium of Liberal Arts Colleges": "Q5165061"},
            subsumptions={
                ("Q49166", "Q49", "part_of"): "a_subsumed_by_b",
                ("Q5165061", "Q49", "part_of"): "a_subsumed_by_b",
            })
        williams_meta = _meta("part_of", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P361", source="oracle")])

        france_kb = _MockKB(
            statements_by_property={"P571": [
                Statement(value="+0843-01-01T00:00:00Z", value_type="literal"),
                Statement(value="1958-10-04T00:00:00Z", value_type="literal")]},
            resolutions={"France": "Q142"})
        france_meta = _meta("founded_on", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P571", source="oracle",
                             single_valued=True)],
            object_type="time", single_valued=1)

        founded_before_kb = _MockKB(
            statements_by_property={"P571": [
                Statement(value="2001-01-15T00:00:00Z", value_type="literal")]},
            resolutions={"a university": "Q42"})
        founded_before_meta = _meta("founded_in_year", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P571", source="oracle",
                             single_valued=True)],
            object_type="time", single_valued=1)

        runs = [
            ("germany", "abstain",
             _verdict(germany_meta, germany_kb,
                      _claim("Germany", "located_in", "the European Union"))),
            ("williams", "abstain",
             _verdict(williams_meta, williams_kb,
                      _claim("Williams College", "part_of",
                             "the Consortium of Liberal Arts Colleges"))),
            ("france", "abstain",
             _verdict(france_meta, france_kb, _claim("France", "founded_on", "843"))),
            ("founded_before", "abstain",
             _verdict(founded_before_meta, founded_before_kb,
                      _claim("a university", "founded_in_year", "before 1800"))),
        ]
        cases = [BenchmarkCase(cid, "s", gt, "regression", "") for cid, gt, _ in runs]
        results = [RunResult(cid, verdict) for cid, _, verdict in runs]

        metrics = compute_metrics(cases, results)
        gates = soundness_gates(metrics)
        assert gates["false_verified == 0"] is True, [r.verdict for r in results]
        assert gates["false_contradicted == 0"] is True, [r.verdict for r in results]
        # None of the cycle-2 shapes may contradict (the soundness property).
        assert all(r.verdict != "contradicted" for r in results), [r.verdict for r in results]

    def test_pinned_cases_clear_both_hard_soundness_gates(self):
        # End-to-end: feed the pinned verdicts (each produced by a real walk
        # above) through the benchmark harness's compute_metrics and confirm
        # BOTH hard gates pass and accuracy is perfect — the offline net mirrors
        # exactly what the live harness would gate on.
        vatican_kb = _MockKB(
            statements_by_property={"P131": []},
            resolutions={"Vatican": "Q237", "Africa": "Q15"},
            subsumptions={("Q237", "Q46", "part_of"): "a_subsumed_by_b"})
        vatican_meta = _meta("located_in", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P131", source="oracle")])

        circa_kb = _MockKB(
            statements_by_property={"P569": [Statement(value="1550-01-01T00:00:00Z", value_type="literal")]},
            resolutions={"Tadhg": "Q1"})
        circa_meta = _meta("born_on", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P569", source="oracle",
                             single_valued=True)],
            object_type="time", single_valued=1)

        guitarist = "Q855091"
        cop_kb = _MockKB(
            statements_by_property={
                "P31": [Statement(value="Q5", value_type="entity")],
                "P106": [Statement(value=guitarist, value_type="entity")]},
            resolutions={"Krieger": "Q314459", "guitarist": guitarist},
            subsumptions={(guitarist, "Q28640", "is_a"): "a_subsumed_by_b"})
        cop_meta = _meta("instance_of", [
            PredicateBinding(kb_namespace="wikidata", kb_property="P31", source="oracle"),
            PredicateBinding(kb_namespace="wikidata", kb_property="P106", source="candidate",
                             object_entity_types=["Q28640"], value_type_gated=True)])

        runs = [
            ("vatican", "contradicted",
             _verdict(vatican_meta, vatican_kb, _claim("Vatican", "located_in", "Africa"))),
            ("circa", "verified",
             _verdict(circa_meta, circa_kb, _claim("Tadhg", "born_on", "c. 1550"))),
            ("copula", "verified",
             _verdict(cop_meta, cop_kb, _claim("Krieger", "instance_of", "guitarist"))),
        ]
        cases = [BenchmarkCase(cid, "s", gt, "regression", "") for cid, gt, _ in runs]
        results = [RunResult(cid, verdict) for cid, _, verdict in runs]

        metrics = compute_metrics(cases, results)
        gates = soundness_gates(metrics)
        assert gates["false_verified == 0"] is True, [r.verdict for r in results]
        assert gates["false_contradicted == 0"] is True, [r.verdict for r in results]
        assert metrics.accuracy == 1.0
