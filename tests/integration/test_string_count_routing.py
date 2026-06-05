"""v0.16.4 — string-count claims route to the python tier and verify exactly.

Live gap (reproduced): "How many vowels does the word 'superstrawberry' have?"
extracted `has(the word 'superstrawberry', "4 vowels")` — a GENERIC `has`
predicate. Routing is decided from the bare predicate name alone, so `has`
routed non-python and the walk abstained (no_grounding, source_breakdown
python:0), even though the word is a literal and python could count it.

The fix shapes such claims as a specific count predicate (`vowel_count`, …) which
is SEEDED routing_hint='python'; the python verifier's exact string-count
front-end computes the count over the subject literal — VERIFIED/CONTRADICTED,
no LLM. These tests run the REAL substrate (seed-backed predicate translation) +
REAL walker routing + REAL PythonVerifier deterministic front-end (no LLM).
"""

from __future__ import annotations

import sys
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
from aedos.layer4_sources.kb_protocol import SubsumptionResult, TransitivePathResult
from aedos.layer4_sources.kb_verifier import KBVerifier
from aedos.layer4_sources.python_verifier import PythonVerifier
from aedos.layer4_sources.tier_u import TierU
from aedos.layer4_sources.walker import VerificationContext, Walker
from aedos.llm.client import LLMClient

_REPO_ROOT = Path(__file__).parents[2]


class _NoGenTransport:
    """Allows benign substrate oracle calls but ASSERTS on predicate-translation
    GENERATION (proving the SEEDED vowel_count row is consulted, not regenerated)
    and on python CODEGEN (proving the deterministic front-end computed the count,
    not the LLM)."""

    def extract_with_tool(self, *a, purpose=None, **kw):
        if purpose == "substrate:predicate_translation":
            raise AssertionError("count predicate must come from the seed row, not the oracle")
        if purpose == "substrate:predicate_distribution":
            return {"verdict": "neither", "reason": "test"}
        if purpose == "substrate:subsumption":
            return {"verdict": "unrelated", "reason": "test"}
        if purpose == "python_verifier":
            raise AssertionError("count must be computed by the deterministic front-end, not codegen")
        return {}

    def chat(self, *a, **kw):
        return ""


class _StubKB:
    """No KB facts — a count predicate (kb_property=null) must abstain in the KB
    verifier and fall through to the python gate regardless."""

    def resolve_entity(self, reference, local_context):
        return []

    def lookup_statements(self, entity, predicate):
        return []

    def subsumption(self, a, b, relation_type):
        return SubsumptionResult(verdict="unrelated")

    def verify_transitive_path(self, *a, **kw):
        return TransitivePathResult(holds=False)


def _seeded_db(tmp_path):
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from seeds.load_seeds import load_seeds
    db_path = str(tmp_path / "seeded.db")
    open_db(db_path).close()
    load_seeds(db_path)
    return open_db(db_path)


def _walker(db):
    client = LLMClient(_transport=_NoGenTransport())
    kb = _StubKB()
    pt = PredicateTranslation(db=db, llm_client=client)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(resolver=resolver, predicate_translation=pt,
                          subsumption=sub, predicate_distribution=pd)
    tier_u = TierU(db=db, predicate_translation=pt)
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    return Walker(tier_u=tier_u, kb_verifier=kb_verifier,
                  python_verifier=PythonVerifier(llm_client=client),
                  substrate=substrate, kb=None)


def _claim(subject, predicate, obj, polarity=1):
    return Claim(claim_id="c1", subject=subject, predicate=predicate, object=obj,
                 polarity=polarity, source_text="test", asserting_party="user_test",
                 triage_decision=TriageDecision.VERIFY)


def _ctx():
    return VerificationContext(
        current_time=datetime.now(timezone.utc).isoformat(), asserting_party="user_test")


# ---------------------------------------------------------------------------
# The seeded predicate routes to python.
# ---------------------------------------------------------------------------

def test_count_predicates_are_seeded_python(tmp_path):
    db = _seeded_db(tmp_path)
    pt = PredicateTranslation(db=db, llm_client=LLMClient(_transport=_NoGenTransport()))
    for pred in ("vowel_count", "consonant_count", "letter_count",
                 "character_count", "word_count", "syllable_count"):
        meta = pt.consult(pred)
        assert meta.routing_hint == "python", f"{pred} must route to python, got {meta.routing_hint}"
        assert meta.kb_property is None
    db.close()


# ---------------------------------------------------------------------------
# End-to-end: the live "superstrawberry" claim now verifies via python.
# ---------------------------------------------------------------------------

def test_superstrawberry_vowel_count_verifies_via_python(tmp_path):
    db = _seeded_db(tmp_path)
    r = _walker(db).walk(_claim("superstrawberry", "vowel_count", "4"), _ctx())
    assert r.verdict == "verified"
    assert r.trace.source_breakdown.get("python") == 1   # genuinely routed to python
    db.close()


def test_wrong_vowel_count_is_contradicted_via_python(tmp_path):
    db = _seeded_db(tmp_path)
    r = _walker(db).walk(_claim("superstrawberry", "vowel_count", "9"), _ctx())
    assert r.verdict == "contradicted"
    assert r.trace.source_breakdown.get("python") == 1
    db.close()


def test_letter_count_verifies_via_python(tmp_path):
    db = _seeded_db(tmp_path)
    r = _walker(db).walk(_claim("cat", "letter_count", "3"), _ctx())
    assert r.verdict == "verified"
    assert r.trace.source_breakdown.get("python") == 1
    db.close()
