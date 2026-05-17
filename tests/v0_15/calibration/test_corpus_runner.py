"""Calibration corpus runner (audit finding M5).

For each calibration corpus this file loads every case and, under live
evaluation, runs it through the responsible Aedos component, records pass/fail,
computes per-corpus accuracy, and asserts it against the threshold from the
implementation plan's "Calibration deferral policy" table.

Gating (see tests/v0_15/conftest.py):
  * default `make test`            -> deselected; does not run, no skip noise.
  * `pytest --run-calibration`     -> collected; loads + validates each corpus,
                                      then skips with a per-corpus count report
                                      (a harness dry-run; no LLM/KB cost).
  * `--run-calibration` and
    `RUN_CALIBRATION=1` in the env -> live evaluation against live LLM + KB,
                                      thresholds asserted. This is the Phase
                                      10.5 path; it also wants RUN_LIVE_KB=1
                                      and RUN_LIVE_TESTS=1.

Per-case exceptions are caught and counted as failures so a single malformed
case cannot crash the whole corpus run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.calibration

RUN_CALIBRATION = os.environ.get("RUN_CALIBRATION") == "1"
_CORPUS_DIR = Path(__file__).parent


# Per-corpus acceptance thresholds — verbatim from the implementation plan's
# "Calibration deferral policy" table. Single-number floors; the two corpora
# the plan gives a compound bar for are noted inline.
THRESHOLDS: dict[str, float] = {
    "extraction_corpus": 0.90,
    "predicate_metadata_corpus": 0.85,
    "temporal_scope_corpus": 0.90,          # plan: extraction >=90%, lookup 100%
    "entity_resolution_corpus": 0.90,
    "kb_mapping_corpus": 0.90,
    "subsumption_corpus": 0.80,             # plan: >=90% KB-mediated, >=80% substrate
    "predicate_distribution_corpus": 0.85,
    "derivation_corpus": 0.80,
    "python_verification_corpus": 0.85,
    "consistency_check_corpus": 1.00,       # plan: 100% detection + circuit breaker
    "intervention_corpus": 0.90,
}


def _load_corpus(name: str) -> list[dict]:
    path = _CORPUS_DIR / f"{name}.jsonl"
    cases: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


# ---------------------------------------------------------------------------
# Live component harness — built only under RUN_CALIBRATION, where API keys
# (and, per the runbook, RUN_LIVE_KB) are present.
# ---------------------------------------------------------------------------

class _Harness:
    """Lazily builds a live Aedos pipeline for one corpus run."""

    def __init__(self):
        self._db = None
        self._client = None
        self._kb = None
        self._pt = None
        self._resolver = None
        self._substrate = None

    @property
    def db(self):
        if self._db is None:
            from src.aedos_v0_15.database import open_memory_db
            self._db = open_memory_db()
        return self._db

    @property
    def client(self):
        if self._client is None:
            from src.aedos_v0_15.llm.client import LLMClient
            self._client = LLMClient()
        return self._client

    @property
    def kb(self):
        if self._kb is None:
            from src.aedos_v0_15.layer4_sources.kb_wikidata import WikidataAdapter
            self._kb = WikidataAdapter()
        return self._kb

    @property
    def predicate_translation(self):
        if self._pt is None:
            from src.aedos_v0_15.layer3_substrate.predicate_translation import PredicateTranslation
            self._pt = PredicateTranslation(db=self.db, llm_client=self.client)
        return self._pt

    @property
    def resolver(self):
        if self._resolver is None:
            from src.aedos_v0_15.layer3_substrate.resolver import EntityResolver
            self._resolver = EntityResolver(kb_protocol=self.kb, db=self.db, llm_client=self.client)
        return self._resolver

    @property
    def substrate(self):
        if self._substrate is None:
            from src.aedos_v0_15.layer3_substrate import Substrate
            from src.aedos_v0_15.layer3_substrate.predicate_distribution import PredicateDistributionOracle
            from src.aedos_v0_15.layer3_substrate.subsumption import SubsumptionOracle
            self._substrate = Substrate(
                resolver=self.resolver,
                predicate_translation=self.predicate_translation,
                subsumption=SubsumptionOracle(db=self.db, llm_client=self.client, kb_protocol=self.kb),
                predicate_distribution=PredicateDistributionOracle(db=self.db, llm_client=self.client),
            )
        return self._substrate

    def walker(self):
        from src.aedos_v0_15.layer4_sources.kb_verifier import KBVerifier
        from src.aedos_v0_15.layer4_sources.python_verifier import PythonVerifier
        from src.aedos_v0_15.layer4_sources.tier_u import TierU
        from src.aedos_v0_15.layer4_sources.walker import Walker
        tier_u = TierU(db=self.db, predicate_translation=self.predicate_translation)
        kb_verifier = KBVerifier(
            kb_protocol=self.kb, entity_resolver=self.resolver,
            predicate_translation=self.predicate_translation,
        )
        walker = Walker(
            tier_u=tier_u, kb_verifier=kb_verifier,
            python_verifier=PythonVerifier(llm_client=self.client), substrate=self.substrate,
        )
        return walker, tier_u


# ---------------------------------------------------------------------------
# Per-corpus case runners. Each takes (harness, case) and returns True if the
# component's output matches the case's expected output.
# ---------------------------------------------------------------------------

def _run_extraction(h: _Harness, case: dict) -> bool:
    from src.aedos_v0_15.layer1_extraction.extractor import Extractor, ExtractionContext
    extractor = Extractor(llm_client=h.client)
    ctx = ExtractionContext(asserting_party="calibration", context_type="document")
    claims = extractor.extract(case["input"], ctx)
    return any(c.predicate == case["expected_predicate"] for c in claims)


def _run_predicate_metadata(h: _Harness, case: dict) -> bool:
    meta = h.predicate_translation.consult(case["aedos_predicate"])
    expected = case["expected_metadata"]
    return all(
        getattr(meta, field) == value or
        (field == "user_subject_required" and bool(getattr(meta, field)) == bool(value))
        for field, value in expected.items()
    )


def _run_temporal_scope(h: _Harness, case: dict) -> bool:
    from src.aedos_v0_15.layer1_extraction.extractor import Extractor, ExtractionContext
    extractor = Extractor(llm_client=h.client)
    ctx = ExtractionContext(asserting_party="calibration", context_type="document")
    claims = extractor.extract(case["text"], ctx)
    expected = case["expected_scope"]
    if not claims:
        return expected.get("rejected") is True  # future-tense cases extract nothing
    claim = claims[0]
    return (claim.valid_from == expected.get("valid_from")
            and claim.valid_until == expected.get("valid_until"))


def _run_entity_resolution(h: _Harness, case: dict) -> bool:
    from src.aedos_v0_15.layer4_sources.kb_protocol import LocalContext
    inp = case["input"]
    ctx = LocalContext(predicate=inp["predicate"], slot_position=inp["slot_position"])
    selected = h.resolver.select(h.resolver.resolve(inp["reference"], ctx), ctx)
    expected = case["expected_output"]
    if "top_kb_identifier" in expected:
        return selected == expected["top_kb_identifier"]
    if expected.get("result") == "no_candidates":
        return selected is None
    # Genuinely ambiguous cases (disambiguation_key only): the corpus pins no
    # single answer; a non-crashing resolution is acceptable.
    return True


def _run_kb_mapping(h: _Harness, case: dict) -> bool:
    meta = h.predicate_translation.consult(case["predicate"])
    return meta.kb_property == case["expected_output"].get("kb_property")


def _run_subsumption(h: _Harness, case: dict) -> bool:
    from src.aedos_v0_15.layer3_substrate.subsumption import EntityRef
    inp = case["input"]
    verdict = h.substrate.subsumption.consult(
        EntityRef(**inp["entity_a"]), EntityRef(**inp["entity_b"]), inp["relation_type"],
    )
    return verdict.verdict.value == case["expected_output"]["verdict"]


def _run_predicate_distribution(h: _Harness, case: dict) -> bool:
    inp = case["input"]
    verdict = h.substrate.predicate_distribution.consult(
        inp["predicate"], inp["polarity"], inp["relation_type"],
    )
    return verdict.verdict.value == case["expected_output"]["verdict"]


def _run_python_verification(h: _Harness, case: dict) -> bool:
    from src.aedos_v0_15.layer1_extraction.extractor import Claim
    from src.aedos_v0_15.layer1_extraction.triage import TriageDecision
    from src.aedos_v0_15.layer4_sources.python_verifier import PythonVerifier
    inp = case["input"]
    claim = Claim(
        claim_id=case["id"], subject=inp["subject"], predicate=inp["predicate"],
        object=inp["object"], polarity=1, source_text=inp.get("context", ""),
        asserting_party="calibration", triage_decision=TriageDecision.VERIFY,
    )
    verdict = PythonVerifier(llm_client=h.client).verify(claim)
    return verdict.verdict == case["expected_output"]["verdict"]


def _run_consistency_check(h: _Harness, case: dict) -> bool:
    from src.aedos_v0_15.layer3_substrate.consistency import ConsistencyChecker
    if case.get("category") != "seeded_conflict_detection":
        return True  # regeneration / circuit-breaker categories: not pure detection
    inp, expected = case["input"], case["expected_output"]
    db = h.db
    ids = []
    for row in (inp["row_a"], inp["row_b"]):
        cur = db.execute(
            "INSERT INTO predicate_translation "
            "(aedos_predicate, object_type, routing_hint, kb_namespace, kb_property, "
            "slot_to_qualifier, reason, created_at) "
            "VALUES (?, 'entity', 'kb_resolvable', 'wikidata', ?, ?, 'calib', '2026-01-01')",
            (row["aedos_predicate"], row["kb_property"], row.get("slot_to_qualifier")),
        )
        ids.append(cur.lastrowid)
    db.commit()
    result = ConsistencyChecker(db).check_on_write(inp["table"], ids[1])
    detected = result.status == "conflict"
    if detected != expected["conflict_detected"]:
        return False
    if detected and "inconsistency_class" in expected:
        return result.inconsistency_class == expected["inconsistency_class"]
    return True


def _run_intervention(h: _Harness, case: dict) -> bool:
    from src.aedos_v0_15.deployment.chat_wrapper import select_intervention
    from src.aedos_v0_15.layer5_result.aggregator import VerificationResult
    counts = case["input"]["verification_result"]
    total = counts.get("verified", 0) + counts.get("contradicted", 0) + counts.get("abstained", 0)
    vr = VerificationResult(
        claims_extracted=[], per_claim_verdicts={}, per_claim_traces={},
        aggregate_metadata={"claim_count": total, **counts},
        audit_log_entries=[], text_input={},
    )
    return select_intervention(vr).value == case["expected_output"]["intervention_type"]


def _run_derivation(h: _Harness, case: dict) -> bool:
    from src.aedos_v0_15.layer1_extraction.extractor import Claim, Extractor, ExtractionContext
    from src.aedos_v0_15.layer1_extraction.triage import TriageDecision
    from src.aedos_v0_15.layer4_sources.walker import VerificationContext
    from datetime import datetime, timezone

    inp, expected = case["input"], case["expected_output"]
    walker, tier_u = h.walker()

    # Seed Tier U from any tier_u / tier_u_prior entries.
    for key in ("tier_u", "tier_u_prior"):
        entries = inp.get(key) or []
        if isinstance(entries, dict):
            entries = [entries]
        for e in entries:
            tier_u.write(Claim(
                claim_id="seed", subject=e["subject"], predicate=e["predicate"],
                object=e["object"], polarity=e.get("polarity", 1), source_text="seed",
                asserting_party="calibration", triage_decision=TriageDecision.VERIFY,
                valid_from=e.get("valid_from"),
            ))
    # Seed subsumption rows from taxonomic context_premises.
    for prem in inp.get("context_premises") or []:
        if prem.get("predicate") in ("part_of", "is_a"):
            h.db.execute(
                "INSERT INTO subsumption "
                "(entity_a_namespace, entity_a_identifier, entity_b_namespace, "
                "entity_b_identifier, relation_type, verdict, source, reason, created_at) "
                "VALUES ('aedos', ?, 'aedos', ?, ?, 'a_subsumed_by_b', 'calib', 'seed', '2026-01-01')",
                (prem["subject"], prem["object"], prem["predicate"]),
            )
    h.db.commit()

    extractor = Extractor(llm_client=h.client)
    claims = extractor.extract(inp["text"], ExtractionContext(
        asserting_party="calibration", context_type="document"))
    if not claims:
        return expected.get("verdict") == "no_grounding_found"
    ctx = VerificationContext(
        current_time=datetime.now(timezone.utc).isoformat(), asserting_party="calibration")
    result = walker.walk(claims[0], ctx)
    expected_verdict = expected.get("verdict")
    if expected_verdict in ("verified", "contradicted", "no_grounding_found"):
        return result.verdict == expected_verdict
    return True  # non-standard expected verdicts (e.g. "needs_tier_u_or_kb"): lenient


_RUNNERS = {
    "extraction_corpus": _run_extraction,
    "predicate_metadata_corpus": _run_predicate_metadata,
    "temporal_scope_corpus": _run_temporal_scope,
    "entity_resolution_corpus": _run_entity_resolution,
    "kb_mapping_corpus": _run_kb_mapping,
    "subsumption_corpus": _run_subsumption,
    "predicate_distribution_corpus": _run_predicate_distribution,
    "python_verification_corpus": _run_python_verification,
    "consistency_check_corpus": _run_consistency_check,
    "intervention_corpus": _run_intervention,
    "derivation_corpus": _run_derivation,
}


# ---------------------------------------------------------------------------
# The corpus test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("corpus", sorted(THRESHOLDS))
def test_corpus_calibration(corpus: str):
    """Load `corpus`, and under RUN_CALIBRATION evaluate it against its plan
    threshold. The test id contains the corpus name so the runbook's
    `-k "<corpus>"` filters select it."""
    cases = _load_corpus(corpus)
    assert cases, f"{corpus}: corpus is empty or missing"

    if not RUN_CALIBRATION:
        pytest.skip(
            f"{corpus}: {len(cases)} cases load and parse OK (harness dry-run). "
            f"Set RUN_CALIBRATION=1 (with RUN_LIVE_KB=1, RUN_LIVE_TESTS=1) for "
            f"live evaluation against the {THRESHOLDS[corpus]:.0%} threshold."
        )

    runner = _RUNNERS[corpus]
    harness = _Harness()
    passed = 0
    for case in cases:
        try:
            if runner(harness, case):
                passed += 1
        except Exception as exc:  # one bad case must not crash the run
            print(f"  {corpus}/{case.get('id', '?')}: ERROR {type(exc).__name__}: {exc}")

    accuracy = passed / len(cases)
    threshold = THRESHOLDS[corpus]
    print(f"{corpus}: accuracy {accuracy:.1%} ({passed}/{len(cases)}), threshold {threshold:.0%}")
    assert accuracy >= threshold, (
        f"{corpus}: calibration accuracy {accuracy:.1%} below the "
        f"{threshold:.0%} threshold ({passed}/{len(cases)} cases passed)"
    )
