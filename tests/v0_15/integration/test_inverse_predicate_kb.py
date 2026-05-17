"""Integration coverage for Cluster D19 — KB verifier honors slot_to_qualifier.

These tests load the *actual* v0.15 seed pack into a fresh database and run the
KB verifier against the two inverse-mapped seed predicates (`capital_of` on P36,
`mother_of` on P25) and a standard-mapped one (`born_in` on P19, `has_capital`
on P36).

An inverse seed maps the Aedos subject to the KB ``statement_value`` and the
Aedos object to ``statement_subject``: the KB keys the statement on the *other*
entity. Pre-D19 the verifier always looked statements up on the claim's subject,
so every inverse-predicate claim queried the wrong entity, got nothing, and
abstained (NO_MATCH) regardless of truth.

The MockKB here keys ``lookup_statements`` on the entity (``(entity, predicate)``
tuple) — a verifier that looks up the wrong entity gets ``[]``. That is what
makes these tests discriminate the pre-fix from the post-fix code: against the
fixup-2 ``kb_verifier.py`` tests 1-5 and 7-8 fail (NO_MATCH / wrong reason) and
test 6 (the standard-path regression guard) passes both ways.
"""

from __future__ import annotations

import sys
from pathlib import Path

from src.aedos_v0_15.database import open_db
from src.aedos_v0_15.layer1_extraction.extractor import Claim
from src.aedos_v0_15.layer1_extraction.triage import TriageDecision
from src.aedos_v0_15.layer3_substrate.predicate_translation import PredicateTranslation
from src.aedos_v0_15.layer3_substrate.resolver import EntityResolver
from src.aedos_v0_15.layer4_sources.kb_protocol import (
    ResolutionCandidate, Statement, SubsumptionResult
)
from src.aedos_v0_15.layer4_sources.kb_verifier import KBVerdictType, KBVerifier
from src.aedos_v0_15.llm.client import LLMClient

_REPO_ROOT = Path(__file__).parents[3]

# Plausible (real) Wikidata Q-numbers — the MockKB defines its own resolution
# map, so exact identity does not matter, only consistency within a test.
_BERLIN, _GERMANY, _MUNICH = "Q64", "Q183", "Q1726"
_JESUS, _MARY = "Q302", "Q345"
_OBAMA, _HONOLULU = "Q76", "Q18094"


class _NoLLMTransport:
    """LLM transport that raises on use — proves the seeded rows are consulted
    directly, with no inline LLM generation."""

    def extract_with_tool(self, *a, **kw):
        raise AssertionError("LLM must not be called: predicate is seeded")

    def chat(self, *a, **kw):
        raise AssertionError("LLM must not be called")


class _MockKB:
    """KB whose ``lookup_statements`` is keyed on the entity — a lookup against
    the wrong entity returns ``[]``. This is what discriminates the D19 fix."""

    def __init__(self, resolutions: dict, statements: dict):
        self._resolutions = resolutions
        self._statements = statements

    def resolve_entity(self, reference, local_context):
        qid = self._resolutions.get(reference)
        return [ResolutionCandidate(kb_identifier=qid, score=0.95)] if qid else []

    def lookup_statements(self, entity, predicate):
        return list(self._statements.get((entity, predicate), []))

    def subsumption(self, entity_a, entity_b, relation_type):
        return SubsumptionResult(verdict="unrelated")


def _seeded_db(tmp_path):
    """Fresh DB with the real v0.15 seed pack loaded (the runbook Step 2
    sequence: open_db creates the schema, load_seeds inserts all 61 rows)."""
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from seeds.v0_15.load_seeds import load_seeds
    db_path = str(tmp_path / "seeded.db")
    open_db(db_path).close()
    load_seeds(db_path)
    return open_db(db_path)


def _claim(subject: str, predicate: str, object_val: str, polarity: int = 1) -> Claim:
    return Claim(
        claim_id="c1", subject=subject, predicate=predicate, object=object_val,
        polarity=polarity, source_text="test", asserting_party="user_test",
        triage_decision=TriageDecision.VERIFY,
    )


def _verifier(db, kb):
    pt = PredicateTranslation(db=db, llm_client=LLMClient(_transport=_NoLLMTransport()))
    resolver = EntityResolver(kb_protocol=kb, db=db)
    return KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)


# ---------------------------------------------------------------------------
# Test 1 — capital_of (inverse, P36) produces VERIFIED for a correct claim.
# Pre-D19: looked up P36 on Berlin (the claim subject), got nothing, NO_MATCH.
# ---------------------------------------------------------------------------

def test_capital_of_correct_claim_is_verified(tmp_path):
    """capital_of(Berlin, Germany) against the KB statement `Germany P36 Berlin`
    is VERIFIED. capital_of's seed maps the Aedos subject to statement_value, so
    the lookup must key on Germany (the object); the trace records the
    inverted direction."""
    db = _seeded_db(tmp_path)
    kb = _MockKB(
        resolutions={"Berlin": _BERLIN, "Germany": _GERMANY},
        statements={(_GERMANY, "P36"): [Statement(value=_BERLIN, value_type="entity")]},
    )
    result = _verifier(db, kb).verify(_claim("Berlin", "capital_of", "Germany"))
    assert result.verdict == KBVerdictType.VERIFIED
    assert result.trace.get("lookup_inverted") is True
    # The statement was found on Germany — the KB statement subject.
    assert result.subject_kb_id == _GERMANY
    db.close()


# ---------------------------------------------------------------------------
# Test 2 — capital_of (inverse, single_valued) CONTRADICTED on a wrong claim.
# ---------------------------------------------------------------------------

def test_capital_of_wrong_functional_value_is_contradicted(tmp_path):
    """capital_of(Munich, Germany) against `Germany P36 Berlin`: capital_of is
    single_valued, Munich resolves to a real-but-different Q-number, so the
    mismatch is a genuine CONTRADICTED. Pre-D19 this returned NO_MATCH (the
    lookup keyed on Munich found nothing)."""
    db = _seeded_db(tmp_path)
    kb = _MockKB(
        resolutions={"Berlin": _BERLIN, "Germany": _GERMANY, "Munich": _MUNICH},
        statements={(_GERMANY, "P36"): [Statement(value=_BERLIN, value_type="entity")]},
    )
    result = _verifier(db, kb).verify(_claim("Munich", "capital_of", "Germany"))
    assert result.verdict == KBVerdictType.CONTRADICTED
    assert result.trace.get("lookup_inverted") is True
    db.close()


# ---------------------------------------------------------------------------
# Test 3 — capital_of and has_capital are symmetric on the same KB data (N5).
# ---------------------------------------------------------------------------

def test_capital_of_and_has_capital_are_symmetric(tmp_path):
    """capital_of(Berlin, Germany) and has_capital(Germany, Berlin) produce the
    same verdict against the same KB statement `Germany P36 Berlin`. capital_of
    is inverse-mapped, has_capital is standard — both must reach VERIFIED. This
    is the end-to-end correctness check behind N5's `_is_inverse_mapping`."""
    db = _seeded_db(tmp_path)
    kb = _MockKB(
        resolutions={"Berlin": _BERLIN, "Germany": _GERMANY},
        statements={(_GERMANY, "P36"): [Statement(value=_BERLIN, value_type="entity")]},
    )
    verifier = _verifier(db, kb)
    capital_of = verifier.verify(_claim("Berlin", "capital_of", "Germany"))
    has_capital = verifier.verify(_claim("Germany", "has_capital", "Berlin"))
    assert capital_of.verdict == has_capital.verdict == KBVerdictType.VERIFIED
    # Inverse vs standard: the trace records opposite lookup directions even
    # though the verdict is the same.
    assert capital_of.trace.get("lookup_inverted") is True
    assert has_capital.trace.get("lookup_inverted") is False
    db.close()


# ---------------------------------------------------------------------------
# Test 4 — inverse-predicate resolution failure on the value slot abstains (N1).
# ---------------------------------------------------------------------------

def test_capital_of_unresolvable_capital_abstains(tmp_path):
    """capital_of("FooCity", Germany) where "FooCity" does not resolve. Under
    the inverse mapping the Aedos subject ("FooCity") is the *expected value*,
    so this is an N1 resolution failure: the verifier must abstain (NO_MATCH),
    never CONTRADICTED — comparing an unresolved string against KB Q-numbers is
    not evidence of falsity. N1's resolution-failure-abstain now applies to the
    inverted slot."""
    db = _seeded_db(tmp_path)
    kb = _MockKB(
        resolutions={"Germany": _GERMANY},  # "FooCity" absent
        statements={(_GERMANY, "P36"): [Statement(value=_BERLIN, value_type="entity")]},
    )
    result = _verifier(db, kb).verify(_claim("FooCity", "capital_of", "Germany"))
    assert result.verdict == KBVerdictType.NO_MATCH
    assert result.trace.get("lookup_inverted") is True
    # The expected value (the Aedos subject, under inversion) failed to resolve.
    assert result.trace.get("abstention_reason") == "object_unresolved"
    db.close()


# ---------------------------------------------------------------------------
# Test 5 — inverse-predicate resolution failure on the lookup-entity slot.
# ---------------------------------------------------------------------------

def test_capital_of_unresolvable_country_abstains(tmp_path):
    """capital_of(Berlin, "FooCountry") where "FooCountry" does not resolve.
    Under the inverse mapping the Aedos object is the KB lookup entity, so its
    resolution failure abstains with `subject_unresolved` (the KB statement
    subject could not be resolved). Pre-D19 the lookup keyed on Berlin and
    abstained for the unrelated reason `no_statements`."""
    db = _seeded_db(tmp_path)
    kb = _MockKB(
        resolutions={"Berlin": _BERLIN},  # "FooCountry" absent
        statements={(_GERMANY, "P36"): [Statement(value=_BERLIN, value_type="entity")]},
    )
    result = _verifier(db, kb).verify(_claim("Berlin", "capital_of", "FooCountry"))
    assert result.verdict == KBVerdictType.NO_MATCH
    assert result.trace.get("lookup_inverted") is True
    assert result.trace.get("abstention_reason") == "subject_unresolved"
    db.close()


# ---------------------------------------------------------------------------
# Test 6 — the standard-mapping path is unchanged (regression guard).
# This test passes against both the pre-fix and post-fix code: born_in is a
# standard predicate and D19 must not alter its behavior.
# ---------------------------------------------------------------------------

def test_born_in_standard_path_still_verified(tmp_path):
    """Regression guard: born_in (standard P19 mapping) still verifies a correct
    claim. D19 changes the inverse path; the standard path must be untouched.
    Passes both pre-fix and post-fix — it guards against a regression, it does
    not discriminate the fix."""
    db = _seeded_db(tmp_path)
    kb = _MockKB(
        resolutions={"Obama": _OBAMA, "Honolulu": _HONOLULU},
        statements={(_OBAMA, "P19"): [Statement(value=_HONOLULU, value_type="entity")]},
    )
    result = _verifier(db, kb).verify(_claim("Obama", "born_in", "Honolulu"))
    assert result.verdict == KBVerdictType.VERIFIED
    db.close()


# ---------------------------------------------------------------------------
# Test 7 — the standard path runs through the D19 code and is flagged as such.
# ---------------------------------------------------------------------------

def test_born_in_records_lookup_inverted_false(tmp_path):
    """A standard predicate's trace records `lookup_inverted=False`. This
    confirms the standard path goes through `_lookup_targets` and is correctly
    classified as non-inverted (pre-D19 the trace had no `lookup_inverted`
    key)."""
    db = _seeded_db(tmp_path)
    kb = _MockKB(
        resolutions={"Obama": _OBAMA, "Honolulu": _HONOLULU},
        statements={(_OBAMA, "P19"): [Statement(value=_HONOLULU, value_type="entity")]},
    )
    result = _verifier(db, kb).verify(_claim("Obama", "born_in", "Honolulu"))
    assert result.trace.get("lookup_inverted") is False
    assert result.subject_kb_id == _OBAMA
    db.close()


# ---------------------------------------------------------------------------
# Test 8 — mother_of, the second inverse seed (P25, multi-valued).
# ---------------------------------------------------------------------------

def test_mother_of_inverted_multivalued_is_verified(tmp_path):
    """mother_of(Mary, Jesus) — the second inverse seed. Wikidata P25 (mother)
    keys the statement on the child, so the lookup must key on Jesus (the Aedos
    object). mother_of is multi-valued (single_valued=0), exercising the
    inverse x multi-valued combination. Against `Jesus P25 Mary` → VERIFIED."""
    db = _seeded_db(tmp_path)
    kb = _MockKB(
        resolutions={"Mary": _MARY, "Jesus": _JESUS},
        statements={(_JESUS, "P25"): [Statement(value=_MARY, value_type="entity")]},
    )
    result = _verifier(db, kb).verify(_claim("Mary", "mother_of", "Jesus"))
    assert result.verdict == KBVerdictType.VERIFIED
    assert result.trace.get("lookup_inverted") is True
    assert result.subject_kb_id == _JESUS
    db.close()
