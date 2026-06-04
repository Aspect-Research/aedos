"""v0.16.3 Batch B (piece 1) — generation-time empirical direction validation.

Real-path tests: the DirectionValidator's logic runs for real; only the KB DATA
(example statements, lookup results, entity types) is supplied by a keyed mock
KB — exactly the discriminating shape used by the inverse-predicate suite. No
resolution is hardcoded (the validator works at the QID level and never resolves
a surface form), so a direction error fails loudly rather than passing.

Covers:
  - The validator's verdicts (correct / confirm / symmetric / suspect /
    unconfirmed) on real-shaped Wikidata data.
  - The generation hook: PredicateTranslation, with a wired validator, CORRECTS
    an inverted oracle direction at write time and (never-CONTRADICT posture)
    suppresses single_valued when direction cannot be confirmed.
"""

from __future__ import annotations

import sqlite3

from aedos.database import open_memory_db
from aedos.layer3_substrate.direction_validator import DirectionValidator
from aedos.layer3_substrate.predicate_translation import PredicateTranslation
from aedos.layer4_sources.kb_protocol import Statement
from aedos.llm.client import LLMClient

# Real Wikidata Q-numbers (only internal consistency matters).
_FRANCE, _PARIS, _GERMANY, _BERLIN = "Q142", "Q90", "Q183", "Q64"
_COUNTRY, _SOV_STATE, _CITY, _CAPITAL = "Q6256", "Q3624078", "Q515", "Q5119"
_HUMAN = "Q5"
_STANDARD = {"subject": "statement_subject", "object": "statement_value"}
_INVERSE = {"subject": "statement_value", "object": "statement_subject"}


class _KeyedKB:
    """KB whose example/lookup/type data is keyed on real QIDs. A lookup against
    the wrong entity returns [] — so the grounding probe cannot accidentally pass
    a wrong direction."""

    def __init__(self, examples, statements, types):
        self._examples = examples            # {P: [(s, v), ...]}
        self._statements = statements        # {(entity, P): [value_qid, ...]}
        self._types = types                  # {qid: [type_qid, ...]}

    def sample_property_examples(self, prop, limit=5):
        return list(self._examples.get(prop, []))[:limit]

    def lookup_statements(self, entity, predicate):
        return [
            Statement(value=v, value_type="entity")
            for v in self._statements.get((entity, predicate), [])
        ]

    def fetch_types(self, qids):
        return ({q: self._types.get(q, []) for q in qids}, None)


def _capital_kb():
    """P36: France->Paris, Germany->Berlin. Asymmetric (cities carry no P36).
    France/Germany typed country; Paris/Berlin typed city."""
    return _KeyedKB(
        examples={"P36": [(_FRANCE, _PARIS), (_GERMANY, _BERLIN)]},
        statements={
            (_FRANCE, "P36"): [_PARIS],
            (_GERMANY, "P36"): [_BERLIN],
            (_PARIS, "P36"): [],
            (_BERLIN, "P36"): [],
        },
        types={
            _FRANCE: [_COUNTRY, _SOV_STATE], _GERMANY: [_COUNTRY, _SOV_STATE],
            _PARIS: [_CITY, _CAPITAL], _BERLIN: [_CITY, _CAPITAL],
        },
    )


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------

class TestDirectionValidator:
    def test_inverted_capital_is_corrected_to_standard(self):
        """The live bug: oracle gave an INVERSE map for `capital` though its
        subject is a country. The validator grounds France->Paris, sees the
        country sits on the statement-subject role, and CORRECTS to standard."""
        v = DirectionValidator(kb=_capital_kb())
        verdict = v.validate("P36", "wikidata", _INVERSE, [_COUNTRY], [_CITY, _CAPITAL])
        assert verdict.status == "corrected"
        assert verdict.direction == _STANDARD
        assert verdict.is_validated is True

    def test_standard_capital_is_confirmed(self):
        v = DirectionValidator(kb=_capital_kb())
        verdict = v.validate("P36", "wikidata", _STANDARD, [_COUNTRY], [_CITY, _CAPITAL])
        assert verdict.status == "confirmed"
        assert verdict.direction == _STANDARD

    def test_genuine_inverse_capital_of_is_confirmed(self):
        """capital_of's Aedos subject is the CITY (Paris is the capital of
        France). The inverse map is CORRECT and must be confirmed, not 'fixed'."""
        v = DirectionValidator(kb=_capital_kb())
        verdict = v.validate("P36", "wikidata", _INVERSE, [_CITY, _CAPITAL], [_COUNTRY])
        assert verdict.status == "confirmed"
        assert verdict.direction == _INVERSE

    def test_symmetric_property_is_direction_agnostic(self):
        """A symmetric property (both keyings ground) is direction-agnostic — the
        validator must not 'correct' it; neither map can mis-key the lookup."""
        kb = _KeyedKB(
            examples={"P26": [("Q1", "Q2")]},          # spouse
            statements={("Q1", "P26"): ["Q2"], ("Q2", "P26"): ["Q1"]},  # reciprocal
            types={"Q1": [_HUMAN], "Q2": [_HUMAN]},
        )
        v = DirectionValidator(kb=kb)
        verdict = v.validate("P26", "wikidata", _STANDARD, [_HUMAN], [_HUMAN])
        assert verdict.status == "symmetric"
        assert verdict.is_validated is True

    def test_neither_direction_grounds_is_suspect(self):
        """When the example grounds under NEITHER keying the property itself is
        suspect (likely the wrong P-id) — not a confident direction."""
        kb = _KeyedKB(
            examples={"P999": [(_FRANCE, _PARIS)]},
            statements={},  # nothing grounds
            types={_FRANCE: [_COUNTRY], _PARIS: [_CITY]},
        )
        v = DirectionValidator(kb=kb)
        verdict = v.validate("P999", "wikidata", _STANDARD, [_COUNTRY], [_CITY])
        assert verdict.status == "suspect"
        assert verdict.is_validated is False

    def test_no_entity_types_cannot_orient_is_unconfirmed(self):
        """Without declared entity-types the Aedos slot cannot be mapped to a KB
        role — unconfirmed (never a guessed direction)."""
        v = DirectionValidator(kb=_capital_kb())
        verdict = v.validate("P36", "wikidata", _STANDARD, None, None)
        assert verdict.status == "unconfirmed"
        assert verdict.is_validated is False

    def test_unwired_validator_is_noop(self):
        v = DirectionValidator(kb=None)
        verdict = v.validate("P36", "wikidata", _INVERSE, [_COUNTRY], [_CITY])
        assert verdict.status == "unconfirmed"
        assert verdict.is_validated is False

    def test_object_matches_statement_subject_role_is_unconfirmed(self):
        """The P131/stands_on trap (adversarial-review fix): subject types silent,
        object types match the statement-SUBJECT role (a city located in a region —
        the city IS the statement subject). The object-only signal must NOT infer
        inverse; it falls through to unconfirmed (never a confident wrong
        correction)."""
        _REGION = "Q9842"
        kb = _KeyedKB(
            examples={"P131": [(_PARIS, _REGION)]},          # city located in region
            statements={(_PARIS, "P131"): [_REGION], (_REGION, "P131"): []},
            types={_PARIS: [_CITY], _REGION: [_REGION]},
        )
        v = DirectionValidator(kb=kb)
        verdict = v.validate("P131", "wikidata", _STANDARD, None, [_CITY])
        assert verdict.status == "unconfirmed"
        assert verdict.reason == "cannot_orient_aedos_slot"
        assert verdict.is_validated is False

    def test_single_anomalous_reciprocal_does_not_flip_to_symmetric(self):
        """One reciprocal example among several asymmetric ones must NOT classify
        an asymmetric property as symmetric (adversarial-review fix [3]: symmetric
        requires UNANIMOUS reciprocity, not 'any one'). Uses a NON-anchor property
        so the mock examples (not the curated P36 anchor) drive the probe."""
        kb = _KeyedKB(
            examples={"P1376": [(_FRANCE, _PARIS), (_GERMANY, _BERLIN), ("Q1", "Q2")]},
            statements={
                (_FRANCE, "P1376"): [_PARIS], (_PARIS, "P1376"): [],
                (_GERMANY, "P1376"): [_BERLIN], (_BERLIN, "P1376"): [],
                ("Q1", "P1376"): ["Q2"], ("Q2", "P1376"): ["Q1"],  # anomalous reciprocal
            },
            types={
                _FRANCE: [_COUNTRY], _GERMANY: [_COUNTRY], "Q1": [_COUNTRY],
                _PARIS: [_CITY], _BERLIN: [_CITY], "Q2": [_CITY],
            },
        )
        v = DirectionValidator(kb=kb)
        verdict = v.validate("P1376", "wikidata", _STANDARD, [_COUNTRY], [_CITY])
        assert verdict.status == "confirmed"   # NOT symmetric

    def test_disagreeing_examples_cannot_orient(self):
        """Two ASYMMETRIC examples whose real types imply OPPOSITE orientations
        (one canonical country->city, one anomalous city->country) → no unanimous
        decision → unconfirmed (adversarial-review fix [5]: not first-example-wins,
        so an anomalous example cannot flip a correct direction). Non-anchor
        property so the mock examples drive the probe."""
        kb = _KeyedKB(
            examples={"P1376": [(_FRANCE, _PARIS), ("Q3", "Q4")]},
            statements={
                (_FRANCE, "P1376"): [_PARIS], (_PARIS, "P1376"): [],
                ("Q3", "P1376"): ["Q4"], ("Q4", "P1376"): [],   # anomalous: subject typed city
            },
            types={
                _FRANCE: [_COUNTRY], _PARIS: [_CITY],
                "Q3": [_CITY], "Q4": [_COUNTRY],
            },
        )
        v = DirectionValidator(kb=kb)
        verdict = v.validate("P1376", "wikidata", _STANDARD, [_COUNTRY], [_CITY])
        assert verdict.status == "unconfirmed"
        assert verdict.reason == "cannot_orient_aedos_slot"
        # It must NOT 'correct' the (correct) standard map.
        assert verdict.status != "corrected"


# ---------------------------------------------------------------------------
# The generation hook (real generate -> store path)
# ---------------------------------------------------------------------------

class _OracleTransport:
    """Mock the predicate-metadata ORACLE (the LLM). Mocking the oracle is not
    hardcoding resolution — the DirectionValidator's KB probe is the real path
    under test. Returns whatever metadata the test configures for `capital`."""

    def __init__(self, slot_to_qualifier, single_valued=1):
        self._sq = slot_to_qualifier
        self._single_valued = single_valued

    def extract_with_tool(self, *a, **kw):
        return {
            "object_type": "entity",
            "user_subject_required": 0,
            "distinct_slots": None,
            "routing_hint": "kb_resolvable",
            "kb_namespace": "wikidata",
            "kb_property": "P36",
            "slot_to_qualifier": self._sq,
            "single_valued": self._single_valued,
            "subject_entity_types": [_COUNTRY],
            "object_entity_types": [_CITY, _CAPITAL],
            "reason": "test oracle",
        }

    def chat(self, *a, **kw):
        return ""


def _pt(oracle_sq, kb, single_valued=1):
    db = open_memory_db()
    db.row_factory = sqlite3.Row
    pt = PredicateTranslation(
        db=db,
        llm_client=LLMClient(_transport=_OracleTransport(oracle_sq, single_valued)),
        direction_validator=DirectionValidator(kb=kb),
    )
    return pt, db


class TestGenerationHook:
    def test_inverted_oracle_direction_is_corrected_at_write(self):
        """consult('capital') with an oracle that emits the INVERTED map yields a
        STORED row with the STANDARD map — the empirical validator overrode the
        oracle at generation time. single_valued is SUPPRESSED on a correction
        (the direction was re-derived from the oracle's own untrusted types, so it
        may not license a CONTRADICTED — adversarial-review fix [2])."""
        pt, db = _pt(_INVERSE, _capital_kb())
        meta = pt.consult("capital")
        assert meta.slot_to_qualifier == _STANDARD
        assert meta.bindings[0].slot_to_qualifier == _STANDARD
        assert meta.single_valued is False          # corrected → no contradiction license
        assert meta.bindings[0].single_valued is False
        # Persisted row agrees.
        row = db.execute(
            "SELECT slot_to_qualifier, single_valued FROM predicate_translation "
            "WHERE aedos_predicate='capital'"
        ).fetchone()
        assert row["slot_to_qualifier"] == (
            '{"subject": "statement_subject", "object": "statement_value"}'
        )
        assert row["single_valued"] == 0
        db.close()

    def test_confirmed_direction_is_left_intact(self):
        pt, db = _pt(_STANDARD, _capital_kb())
        meta = pt.consult("capital")
        assert meta.slot_to_qualifier == _STANDARD
        assert meta.single_valued is True
        db.close()

    def test_unconfirmable_direction_suppresses_contradiction_license(self):
        """When the validator cannot confirm direction (no example sourced), the
        never-CONTRADICT posture forces single_valued=0 while keeping the oracle
        direction for positive grounding."""
        empty_kb = _KeyedKB(examples={}, statements={}, types={})
        pt, db = _pt(_STANDARD, empty_kb, single_valued=1)
        meta = pt.consult("capital")
        assert meta.slot_to_qualifier == _STANDARD     # oracle direction kept
        assert meta.single_valued is False             # contradiction license removed
        assert meta.bindings[0].single_valued is False
        db.close()

    def test_validator_absent_preserves_oracle_behavior(self):
        """No validator wired → generation behaves exactly as before (oracle
        direction + single_valued untouched), even for an inverted map."""
        db = open_memory_db()
        db.row_factory = sqlite3.Row
        pt = PredicateTranslation(
            db=db,
            llm_client=LLMClient(_transport=_OracleTransport(_INVERSE, single_valued=1)),
        )
        meta = pt.consult("capital")
        assert meta.slot_to_qualifier == _INVERSE      # unchanged (no validation)
        assert meta.single_valued is True
        db.close()
