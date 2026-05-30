"""Integration coverage for the M4 seed backfill + N1 coupling (fix-up 2).

These tests load the *actual* v0.15 seed pack into a fresh database and run the
KB verifier against it. They are the load-bearing demonstration that the M4
seed backfill and the N1 resolution-failure abstain had to ship together:

  * test_seeded_born_in_contradicts_wrong_resolved_place — M4 backfill: a
    seeded functional predicate now produces CONTRADICTED on a legitimate miss.
    Pre-backfill (single_valued defaulted to 0) this returned NO_MATCH.

  * test_seeded_born_in_unresolvable_place_abstains — the coupling: born_in is
    single_valued after the backfill, but an unresolvable object yields
    NO_MATCH, not a false CONTRADICTED (N1). Backfilling the seeds *without*
    the N1 fix would turn this into a confident false contradiction of a
    true-but-hard-to-resolve claim — which is why M4 and N1 ship together.
"""

from __future__ import annotations

import sys
from pathlib import Path

from aedos.database import open_db
from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer3_substrate.predicate_translation import PredicateTranslation
from aedos.layer3_substrate.resolver import EntityResolver
from aedos.layer4_sources.kb_protocol import (
    ResolutionCandidate, Statement, SubsumptionResult
)
from aedos.layer4_sources.kb_verifier import KBVerdictType, KBVerifier
from aedos.llm.client import LLMClient

_REPO_ROOT = Path(__file__).parents[2]


class _NoLLMTransport:
    """LLM transport that raises on use — proves the seeded born_in row is
    consulted directly, with no inline LLM generation."""

    def extract_with_tool(self, *a, **kw):
        raise AssertionError("LLM must not be called: born_in is a seeded predicate")

    def chat(self, *a, **kw):
        raise AssertionError("LLM must not be called")


class _MockKB:
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
    """Fresh DB with the real v0.15 seed pack loaded (open_db creates the
    schema; load_seeds inserts all 61 rows — the runbook Step 2 sequence)."""
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from seeds.load_seeds import load_seeds
    db_path = str(tmp_path / "seeded.db")
    open_db(db_path).close()
    load_seeds(db_path)
    return open_db(db_path)


def _born_in_claim(place: str) -> Claim:
    return Claim(
        claim_id="c1", subject="Obama", predicate="born_in", object=place,
        polarity=1, source_text="test", asserting_party="user_test",
        triage_decision=TriageDecision.VERIFY,
    )


def _verifier(db, kb):
    pt = PredicateTranslation(db=db, llm_client=LLMClient(_transport=_NoLLMTransport()))
    resolver = EntityResolver(kb_protocol=kb, db=db)
    return KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)


def test_seeded_born_in_is_single_valued(tmp_path):
    """Sanity: the seeded born_in row loads with single_valued=1 (M4 backfill).
    Pre-backfill it loaded with the column default 0."""
    db = _seeded_db(tmp_path)
    pt = PredicateTranslation(db=db, llm_client=LLMClient(_transport=_NoLLMTransport()))
    meta = pt.consult("born_in")
    assert meta.single_valued is True
    db.close()


def test_seeded_born_in_contradicts_wrong_resolved_place(tmp_path):
    """M4 backfill activation: with born_in seeded single_valued=1, a claim the
    KB contradicts (object resolves to a real but different Q-number) is
    CONTRADICTED. Pre-backfill, born_in loaded single_valued=0 and this case
    fell through to NO_MATCH."""
    db = _seeded_db(tmp_path)
    kb = _MockKB(
        resolutions={"Obama": "Q76", "Chicago": "Q1297", "Honolulu": "Q18094"},
        statements={("Q76", "P19"): [Statement(value="Q18094", value_type="entity")]},
    )
    result = _verifier(db, kb).verify(_born_in_claim("Chicago"))
    assert result.verdict == KBVerdictType.CONTRADICTED
    assert result.trace.get("value_resolved") is True
    db.close()


def test_seeded_born_in_unresolvable_place_abstains(tmp_path):
    """The coupling (load-bearing): born_in is single_valued after the backfill,
    but "Foobar" does not resolve to a KB entity. N1 makes this NO_MATCH with a
    value_unresolved abstention reason — NOT a false CONTRADICTED.

    Against the intermediate state (seeds backfilled, kb_verifier.py at the
    fixup-1 revision) this case returns CONTRADICTED — the false contradiction
    the coupling exists to prevent. That is why M4 and N1 ship in one commit."""
    db = _seeded_db(tmp_path)
    kb = _MockKB(
        resolutions={"Obama": "Q76", "Honolulu": "Q18094"},  # "Foobar" absent
        statements={("Q76", "P19"): [Statement(value="Q18094", value_type="entity")]},
    )
    result = _verifier(db, kb).verify(_born_in_claim("Foobar"))
    assert result.verdict == KBVerdictType.NO_MATCH
    assert result.trace.get("value_resolved") is False
    assert result.trace.get("abstention_reason") == "value_unresolved"
    db.close()
