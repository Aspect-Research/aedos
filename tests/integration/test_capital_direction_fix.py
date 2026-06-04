"""v0.16.3 regression — the `capital` predicate-direction fix (Defects 1 & 2).

Background (live bug, reproduced on the deployed box). The claim
`France / capital / Paris` abstained with kb:0 and the durable audit mislabeled
the resolved QIDs:

  - Defect 1 (grounding): the oracle cold-started the predicate `capital` (and
    `capital_is`) with an INVERTED slot_to_qualifier
    ({"subject":"statement_value","object":"statement_subject"}) — contradicting
    its own entity-types. The verifier faithfully keyed the P36 lookup on the
    claim's OBJECT (Paris, which has no P36) and abstained.
  - Defect 2 (labeling): the walker stamped the KB *lookup* entity as
    `resolved_subject_qid` without consulting `lookup_inverted`, so an inverse
    binding reported the claim's OBJECT as the subject and left the value None.

The fix is correct metadata (seed pack now pins `capital`/`capital_is` to the
STANDARD has_capital direction) + correct labeling (the verifier records the
resolved QIDs by AEDOS slot on every path; the walker stamps them by slot).

These tests run the REAL resolve→verify(→walk) path: resolution flows through
the actual `EntityResolver` + the keyed KB (no hardcoded/pre-resolved QID is
fed to the verifier — a MockTransport that hardcodes resolution masked both
defects once already). The keyed KB returns `[]` for a lookup against the wrong
entity, so a direction error cannot accidentally pass.
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
from aedos.layer4_sources.kb_protocol import (
    ResolutionCandidate, Statement, SubsumptionResult, TransitivePathResult,
)
from aedos.layer4_sources.kb_verifier import KBVerdictType, KBVerifier
from aedos.layer4_sources.python_verifier import PythonVerifier
from aedos.layer4_sources.tier_u import TierU
from aedos.layer4_sources.walker import VerificationContext, Walker
from aedos.llm.client import LLMClient

_REPO_ROOT = Path(__file__).parents[2]

# Real Wikidata Q-numbers (consistency within the test is what matters).
_FRANCE, _PARIS = "Q142", "Q90"


class _NoGenTransport:
    """LLM transport for the substrate. It ALLOWS the benign substrate oracle
    calls a walk may make (subsumption / predicate-distribution / entity
    selection) but ASSERTS if asked to GENERATE predicate metadata — proving the
    pinned SEED row for `capital` is consulted, never re-generated (a
    regeneration is exactly what would re-introduce the inverted map)."""

    def extract_with_tool(self, *a, purpose=None, **kw):
        if purpose == "substrate:predicate_translation":
            raise AssertionError(
                "predicate metadata must come from the pinned seed row, not the oracle"
            )
        if purpose == "substrate:predicate_distribution":
            return {"verdict": "neither", "reason": "test"}
        if purpose == "substrate:subsumption":
            return {"verdict": "unrelated", "reason": "test"}
        return {}

    def chat(self, *a, **kw):
        return ""


class _KeyedKB:
    """KB whose ``lookup_statements`` is keyed on (entity, property): a lookup
    against the WRONG entity returns ``[]``. That is what makes a direction error
    fail loudly instead of silently passing."""

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

    def verify_transitive_path(self, source, target, kb_property, relation_type=None):
        return TransitivePathResult(holds=False)


def _seeded_db(tmp_path):
    """Fresh DB with the real seed pack loaded (includes the v0.16.3 pinned
    `capital`/`capital_is` rows)."""
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from seeds.load_seeds import load_seeds
    db_path = str(tmp_path / "seeded.db")
    open_db(db_path).close()
    load_seeds(db_path)
    return open_db(db_path)


def _france_paris_kb():
    """France→Q142, Paris→Q90, and the single KB fact `France(Q142) P36 Paris(Q90)`
    keyed on the country — so a lookup keyed on Paris (the inverted-direction bug)
    finds nothing."""
    return _KeyedKB(
        resolutions={"France": _FRANCE, "Paris": _PARIS},
        statements={(_FRANCE, "P36"): [Statement(value=_PARIS, value_type="entity")]},
    )


def _walker(db, kb):
    client = LLMClient(_transport=_NoGenTransport())
    pt = PredicateTranslation(db=db, llm_client=client)
    resolver = EntityResolver(kb_protocol=kb, db=db)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(
        resolver=resolver, predicate_translation=pt, subsumption=sub,
        predicate_distribution=pd,
    )
    tier_u = TierU(db=db, predicate_translation=pt)
    kb_verifier = KBVerifier(
        kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt,
    )
    return Walker(
        tier_u=tier_u, kb_verifier=kb_verifier, python_verifier=PythonVerifier(),
        substrate=substrate, kb=None,
    )


def _verifier(db, kb):
    pt = PredicateTranslation(db=db, llm_client=LLMClient(_transport=_NoGenTransport()))
    resolver = EntityResolver(kb_protocol=kb, db=db)
    return KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)


def _claim(subject, predicate, object_val, polarity=1):
    return Claim(
        claim_id="c1", subject=subject, predicate=predicate, object=object_val,
        polarity=polarity, source_text="test", asserting_party="user_test",
        triage_decision=TriageDecision.VERIFY,
    )


def _ctx():
    return VerificationContext(
        current_time=datetime.now(timezone.utc).isoformat(),
        asserting_party="user_test",
    )


# ---------------------------------------------------------------------------
# Defect 1 (data): the pinned `capital` / `capital_is` rows are STANDARD.
# ---------------------------------------------------------------------------

def test_capital_predicates_are_pinned_standard_direction(tmp_path):
    """The seed pack pins `capital` and `capital_is` to the standard has_capital
    direction (subject→statement_subject), with country/city entity-types — NOT
    the inverted shape the oracle generated."""
    db = _seeded_db(tmp_path)
    pt = PredicateTranslation(db=db, llm_client=LLMClient(_transport=_NoGenTransport()))
    for pred in ("capital", "capital_is"):
        meta = pt.consult(pred)
        assert meta.kb_property == "P36"
        assert meta.slot_to_qualifier == {
            "subject": "statement_subject", "object": "statement_value",
        }, f"{pred} must be STANDARD direction, got {meta.slot_to_qualifier}"
        assert meta.single_valued is True
        assert meta.subject_entity_types == ["Q6256"]          # country
        assert meta.object_entity_types == ["Q515", "Q5119"]   # city / capital
    db.close()


# ---------------------------------------------------------------------------
# Defects 1 & 2 end-to-end through the WALKER (the durable-audit stamp).
# ---------------------------------------------------------------------------

def test_france_capital_paris_grounds_and_labels_correctly(tmp_path):
    """`France / capital / Paris` grounds VERIFIED via the KB (NOT
    verified_given_assertion — the claim is never written to Tier U), and the
    walk_metadata stamp labels the resolved QIDs by AEDOS slot:
    subject=Q142 (France), object=Q90 (Paris)."""
    db = _seeded_db(tmp_path)
    walker = _walker(db, _france_paris_kb())
    result = walker.walk(_claim("France", "capital", "Paris"), _ctx())

    # Defect 1: KB grounding restored (was no_grounding_found).
    assert result.verdict == "verified"
    assert result.trace.source_breakdown.get("kb", 0) >= 1

    # Defect 2: resolved QIDs labeled by claim slot, not KB statement position.
    wm = result.trace.walk_metadata
    assert wm.get("resolved_subject_qid") == _FRANCE   # claim subject France
    assert wm.get("resolved_value_qid") == _PARIS      # claim object Paris
    db.close()


def test_capital_of_inverse_binds_claim_subject_not_lookup_entity(tmp_path):
    """Guard against over-correcting the direction logic: a GENUINELY inverse
    predicate (`capital_of`, the seed's inverse P36 mapping) still binds the
    claim's subject to its own resolution. `Paris capital_of France` grounds
    VERIFIED, and the stamp labels subject=Q90 (Paris, the claim subject) and
    object=Q142 (France) — even though the KB statement is keyed on France.

    This is the discriminating assertion for Defect 2 on the inverse path: the
    pre-fix walker stamped the KB lookup entity (France, Q142) as the subject."""
    db = _seeded_db(tmp_path)
    walker = _walker(db, _france_paris_kb())
    result = walker.walk(_claim("Paris", "capital_of", "France"), _ctx())

    assert result.verdict == "verified"
    assert result.trace.source_breakdown.get("kb", 0) >= 1

    wm = result.trace.walk_metadata
    assert wm.get("resolved_subject_qid") == _PARIS    # claim subject Paris
    assert wm.get("resolved_value_qid") == _FRANCE     # claim object France
    db.close()


# ---------------------------------------------------------------------------
# Negative control: the INVERTED metadata (the bug) abstains — proving the
# tests above discriminate the fix and the pinned direction is load-bearing.
# ---------------------------------------------------------------------------

def test_inverted_capital_metadata_abstains_proving_direction_matters(tmp_path):
    """With the bug's INVERTED `capital` metadata (subject→statement_value), the
    real verify path keys the P36 lookup on Paris (no P36) and abstains
    NO_MATCH. This is the kb:0 the live box showed; it confirms the standard
    pin is what flips the verdict to VERIFIED in the test above."""
    db = _seeded_db(tmp_path)
    # Overwrite the pinned row with the inverted shape that caused the bug.
    db.execute(
        "UPDATE predicate_translation "
        "SET slot_to_qualifier=?, bindings=NULL "
        "WHERE aedos_predicate='capital' AND kb_namespace='wikidata' "
        "AND retracted_at IS NULL",
        ('{"subject": "statement_value", "object": "statement_subject"}',),
    )
    db.commit()

    result = _verifier(db, _france_paris_kb()).verify(_claim("France", "capital", "Paris"))
    assert result.verdict == KBVerdictType.NO_MATCH
    assert result.trace.get("lookup_inverted") is True
    # The inverted lookup keyed on the claim object (Paris) — the KB lookup entity.
    assert result.subject_kb_id == _PARIS
    db.close()
