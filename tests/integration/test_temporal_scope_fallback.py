"""v0.16.4 — present-fact-with-too-early-start fallback (option-2 verifier rescue).

Live over-refusal (reproduced): "Who is the president of Hungary?" drafted
"Tamás Sulyok is the president of Hungary. He took office in May 2022." The
extractor folded the (wrong) 2022 start into the present-tense role claim, so the
ONLY holds_role claim carried valid_from=2022. The KB records the role (started
2024), but the scope check abstains because the claimed "since 2022" precedes the
actual start — leaving zero verified claims and an over-refusal DECLINE.

The fallback: when a value-matching statement is CURRENT (the entity holds the
role now) and the SOLE scope conflict is the claim's lower bound being genuinely
earlier than the actual start, the PRESENT base fact verifies and the trace is
stamped `temporal_scope_unconfirmed` — composition asserts the present fact and
drops the unconfirmed date. SOUND: a past-scoped claim is never rescued, an ended
role is never rescued, and a coarser-grained but consistent date ("since 2024" vs
a March-2024 start) is NOT flagged.

Real-path: resolution flows through the real EntityResolver + a keyed KB; no
pre-resolved QID is fed to the verifier. A direction/scope error cannot silently
pass — the keyed KB returns [] for a lookup against the wrong entity.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from aedos.database import open_db
from aedos.layer1_extraction.extractor import Claim
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
from aedos.layer4_sources.walker import VerificationContext, Walker
from aedos.llm.client import LLMClient

_REPO_ROOT = Path(__file__).parents[2]
_SULYOK, _PRES = "Q28599854", "Q520765"
# A fixed "now" so the term's 2024 start is in the past (current) and provable.
_NOW = "2026-06-05T00:00:00Z"


class _NoGenTransport:
    def extract_with_tool(self, *a, purpose=None, **kw):
        if purpose == "substrate:predicate_translation":
            raise AssertionError("must use the pinned seed row, not the oracle")
        if purpose == "substrate:predicate_distribution":
            return {"verdict": "neither", "reason": "test"}
        if purpose == "substrate:subsumption":
            return {"verdict": "unrelated", "reason": "test"}
        return {}

    def chat(self, *a, **kw):
        return ""


class _KeyedKB:
    def __init__(self, p580="2024-03-05T00:00:00Z", p582=None):
        quals = {"P580": p580}
        if p582:
            quals["P582"] = p582
        self._stmt = Statement(value=_PRES, value_type="entity", qualifiers=quals)

    def resolve_entity(self, reference, local_context):
        qid = {"Tamás Sulyok": _SULYOK, "President of Hungary": _PRES}.get(reference)
        return [ResolutionCandidate(kb_identifier=qid, score=0.95)] if qid else []

    def lookup_statements(self, entity, predicate):
        if (entity, predicate) == (_SULYOK, "P39"):
            return [self._stmt]
        return []

    def subsumption(self, a, b, relation_type):
        return SubsumptionResult(verdict="unrelated")

    def verify_transitive_path(self, source, target, kb_property, relation_type=None):
        return TransitivePathResult(holds=False)


def _seeded_db(tmp_path):
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from seeds.load_seeds import load_seeds
    db_path = str(tmp_path / "seeded.db")
    open_db(db_path).close()
    load_seeds(db_path)
    return open_db(db_path)


def _walker(db, kb):
    client = LLMClient(_transport=_NoGenTransport())
    pt = PredicateTranslation(db=db, llm_client=client)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(resolver=resolver, predicate_translation=pt,
                          subsumption=sub, predicate_distribution=pd)
    tier_u = TierU(db=db, predicate_translation=pt)
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    return Walker(tier_u=tier_u, kb_verifier=kb_verifier,
                  python_verifier=PythonVerifier(), substrate=substrate, kb=None)


def _claim(valid_from=None):
    c = Claim(claim_id="c1", subject="Tamás Sulyok", predicate="holds_role",
              object="President of Hungary", polarity=1, source_text="test",
              asserting_party="user_test", triage_decision=TriageDecision.VERIFY)
    return replace(c, valid_from=valid_from) if valid_from is not None else c


def _ctx():
    return VerificationContext(current_time=_NOW, asserting_party="user_test")


def _run(tmp_path, valid_from=None, **kbkw):
    db = _seeded_db(tmp_path)
    try:
        return _walker(db, _KeyedKB(**kbkw)).walk(_claim(valid_from), _ctx())
    finally:
        db.close()


# ---------------------------------------------------------------------------
# The rescue (and its observability stamp).
# ---------------------------------------------------------------------------

def test_since_2022_present_fact_verifies_with_scope_unconfirmed(tmp_path):
    """The live case: the role started 2024, the claim says "since 2022". The
    PRESENT fact verifies and the trace flags the too-early date as unconfirmed —
    instead of a no_grounding over-refusal."""
    r = _run(tmp_path, valid_from="2022")
    assert r.verdict == "verified"
    assert r.trace.walk_metadata.get("temporal_scope_unconfirmed") is True
    # The KB does record the role for the entity (the value IS known).
    assert r.trace.walk_metadata.get("value_known_entity") is True


def test_since_2023_also_too_early(tmp_path):
    """Any genuinely-earlier year is rescued + flagged (2023 < the 2024 start)."""
    r = _run(tmp_path, valid_from="2023")
    assert r.verdict == "verified"
    assert r.trace.walk_metadata.get("temporal_scope_unconfirmed") is True


# ---------------------------------------------------------------------------
# Controls — the fallback must NOT fire / NOT flag.
# ---------------------------------------------------------------------------

def test_unscoped_present_is_plain_verified_no_flag(tmp_path):
    """A bare present-tense claim (no valid_from) verifies plainly — the fallback
    is for explicit, too-early lower bounds only."""
    r = _run(tmp_path, valid_from=None)
    assert r.verdict == "verified"
    assert r.trace.walk_metadata.get("temporal_scope_unconfirmed") is None


def test_same_year_coarser_grain_is_not_flagged(tmp_path):
    """"since 2024" against a "2024-03-05" start is the SAME year (containment) —
    consistent, not too early. It must NOT be falsely flagged as unconfirmed; the
    fallback leaves the verdict untouched (a granularity quirk, out of scope)."""
    r = _run(tmp_path, valid_from="2024")
    assert r.trace.walk_metadata.get("temporal_scope_unconfirmed") is None


def test_ended_role_is_not_rescued(tmp_path):
    """If the role has provably ENDED (P582 in the past), the entity does NOT hold
    it now — the present fact is false, so the fallback must not rescue it."""
    r = _run(tmp_path, valid_from="2022", p582="2025-01-01T00:00:00Z")
    assert r.verdict != "verified"
    assert r.trace.walk_metadata.get("temporal_scope_unconfirmed") is None


def test_past_scoped_claim_is_not_rescued(tmp_path):
    """A PAST claim ("was president, ending before now") is never rescued by a
    current statement — its current value says nothing about the past window."""
    from aedos.layer1_extraction.temporal import BEFORE_PRESENT
    db = _seeded_db(tmp_path)
    try:
        c = replace(_claim(valid_from="2022"), valid_until=BEFORE_PRESENT)
        r = _walker(db, _KeyedKB()).walk(c, _ctx())
    finally:
        db.close()
    assert r.trace.walk_metadata.get("temporal_scope_unconfirmed") is None
