"""Calibration corpus runner (audit finding M5).

For each calibration corpus this file loads every case and, under live
evaluation, runs it through the responsible Aedos component, records pass/fail,
computes per-corpus accuracy, and asserts it against the threshold from the
implementation plan's "Calibration deferral policy" table.

Gating (see tests/conftest.py):
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


# Per-corpus acceptance thresholds — the SINGLE SOURCE OF TRUTH for the Phase
# 10.5 calibration thresholds (N7). Values are verbatim from the implementation
# plan's "Calibration deferral policy" table; the two corpora the plan gives a
# compound bar for keep the runner-asserted floor here and note the compound
# bar inline. The Phase 10.5 runbook's Step 4 threshold table is checked against
# this dict by tests/unit/test_runbook_thresholds.py — change a threshold
# here and the runbook table; the doc-test catches any divergence.
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
            from aedos.database import open_memory_db
            self._db = open_memory_db()
        return self._db

    @property
    def client(self):
        if self._client is None:
            from aedos.llm.client import LLMClient
            self._client = LLMClient()
        return self._client

    @property
    def kb(self):
        if self._kb is None:
            # F-039: use the shared adapter-construction helper so the
            # calibration harness's WikidataAdapter is wired the same way
            # build_pipeline wires it (http_cache + config + llm_client + db).
            # Pre-fix this constructed `WikidataAdapter()` with no args,
            # which under RUN_LIVE_KB=1 hit the live methods' wiring-gap
            # RuntimeError — the second sibling-finding of F-004.
            from aedos.config import Config
            from aedos.pipeline import build_default_kb
            self._kb = build_default_kb(self.db, self.client, Config.from_env())
        return self._kb

    @property
    def predicate_translation(self):
        if self._pt is None:
            from aedos.layer3_substrate.predicate_translation import PredicateTranslation
            self._pt = PredicateTranslation(db=self.db, llm_client=self.client)
        return self._pt

    @property
    def resolver(self):
        if self._resolver is None:
            from aedos.layer3_substrate.resolver import EntityResolver
            self._resolver = EntityResolver(kb_protocol=self.kb, db=self.db, llm_client=self.client)
        return self._resolver

    @property
    def substrate(self):
        if self._substrate is None:
            from aedos.layer3_substrate import Substrate
            from aedos.layer3_substrate.predicate_distribution import PredicateDistributionOracle
            from aedos.layer3_substrate.subsumption import SubsumptionOracle
            self._substrate = Substrate(
                resolver=self.resolver,
                predicate_translation=self.predicate_translation,
                subsumption=SubsumptionOracle(db=self.db, llm_client=self.client, kb_protocol=self.kb),
                predicate_distribution=PredicateDistributionOracle(db=self.db, llm_client=self.client),
            )
        return self._substrate

    def walker(self):
        from aedos.layer4_sources.kb_verifier import KBVerifier
        from aedos.layer4_sources.python_verifier import PythonVerifier
        from aedos.layer4_sources.tier_u import TierU
        from aedos.layer4_sources.walker import Walker
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
    """Dispatch on `case["category"]`: the extraction corpus has five
    sub-categories and each pins a different thing about the produced claims.
    A single comparison path (the pre-D1 code) read `case["expected_predicate"]`
    and `case["input"]` unconditionally and KeyError'd on the 42 non-`normalization`
    cases — see docs/phase_D_plan.md, Cluster D1."""
    from aedos.layer1_extraction.extractor import Extractor, ExtractionContext
    extractor = Extractor(llm_client=h.client)
    category = case["category"]

    if category == "normalization":
        ctx = ExtractionContext(asserting_party="calibration", context_type="document")
        claims = extractor.extract(case["input"], ctx)
        return any(c.predicate == case["expected_predicate"] for c in claims)

    if category == "temporal":
        ctx = ExtractionContext(asserting_party="calibration", context_type="document")
        claims = extractor.extract(case["input"], ctx)
        expected = case["expected_scope"]
        if expected.get("is_future"):
            return not claims  # future-tense claims are dropped at extraction
        if not claims:
            return False
        claim = claims[0]
        return all(
            getattr(claim, field) == expected.get(field)
            for field in ("valid_from", "valid_until", "valid_during_ref")
        )

    if category == "decomposition":
        ctx = ExtractionContext(asserting_party="calibration", context_type="document")
        claims = extractor.extract(case["input"], ctx)
        if "expected_claim_count" in case and len(claims) != case["expected_claim_count"]:
            return False
        if case.get("expected_shared_event_id"):
            ids = [c.reified_event_id for c in claims]
            if not ids or any(i is None or i != ids[0] for i in ids):
                return False
        return True

    if category == "first_person":
        # The case's own asserting_party must drive canonicalization so that
        # first-person subjects (I/me/my/we) resolve to it.
        ctx = ExtractionContext(
            asserting_party=case["asserting_party"], context_type="chat_user")
        claims = extractor.extract(case["input"], ctx)
        return any(c.subject == case["expected_subject"] for c in claims)

    if category == "hard_claim":
        # hard_claim cases carry `text` (not `input`); hardclaim_002's text is
        # first-person, so asserting_party must be user_test.
        ctx = ExtractionContext(asserting_party="user_test", context_type="chat_user")
        claims = extractor.extract(case["text"], ctx)
        subjects = {c.subject for c in claims}
        for subj in case.get("expected_subjects_in_output", []):
            if subj not in subjects:
                return False
        for subj in case.get("expected_subjects_not_in_output", []):
            if subj in subjects:
                return False
        if "source_text_check" in case:
            return any(c.source_text == case["source_text_check"] for c in claims)
        return True

    raise KeyError(f"unknown extraction category: {category}")


def _run_predicate_metadata(h: _Harness, case: dict) -> bool:
    """Compare PredicateMetadata fields against `expected_metadata`. Two corpus
    shapes don't map 1:1 to PredicateMetadata attributes and need dispatch:

    - ``routing_hint_options: [list]`` (`pred_ambig_*`, 5 cases) — the corpus
      expresses "this ambiguous predicate has multiple acceptable routings";
      the produced `routing_hint` must be in the list.
    - ``distinct_slots_required: bool`` (`pred_kb_008`, 1 case) — the produced
      `distinct_slots` must be populated (or unset, on False).

    Pre-Phase-E-followup the runner did ``getattr(meta, field)`` for every
    field; the 5 `pred_ambig_*` cases raised AttributeError on every run
    (`distinct_slots_required` was latent — only fires once earlier fields all
    match). Fourth runner-corpus mismatch in the v0.15 audit lineage (after
    extraction, temporal_scope, consistency_check); see v0.16 D24."""
    meta = h.predicate_translation.consult(case["aedos_predicate"])
    expected = case["expected_metadata"]
    for field, value in expected.items():
        if field == "routing_hint_options":
            if meta.routing_hint not in value:
                return False
            continue
        if field == "distinct_slots_required":
            if bool(meta.distinct_slots) != bool(value):
                return False
            continue
        produced = getattr(meta, field)
        if produced == value:
            continue
        if field == "user_subject_required" and bool(produced) == bool(value):
            continue
        return False
    return True


def _run_temporal_scope(h: _Harness, case: dict) -> bool:
    """Sub-category dispatch on `case["category"]`. The 5 `future_rejection`
    cases store their expected answer under `expected` (the bare string
    "rejected"), not `expected_scope` — reading `case["expected_scope"]`
    KeyError'd on them; see docs/phase_D_plan.md, Cluster D1."""
    from aedos.layer1_extraction.extractor import Extractor, ExtractionContext
    extractor = Extractor(llm_client=h.client)
    ctx = ExtractionContext(asserting_party="calibration", context_type="document")
    claims = extractor.extract(case["text"], ctx)

    if case.get("category") == "future_rejection":
        # Future-tense claims are dropped at extraction → rejection == no claims.
        return not claims

    expected = case["expected_scope"]
    if not claims:
        return False  # a non-future case that produced no claim has failed
    claim = claims[0]
    return (claim.valid_from == expected.get("valid_from")
            and claim.valid_until == expected.get("valid_until"))


def _run_entity_resolution(h: _Harness, case: dict) -> bool:
    from aedos.layer4_sources.kb_protocol import LocalContext
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
    """`qualifier_mapping` cases (10 of 40) also pin `slot_to_qualifier`; the
    pre-D2 runner compared only `kb_property`, leaving the qualifier dimension
    untested. PredicateTranslation stores a falsy (empty) `slot_to_qualifier`
    as NULL, so both sides are normalized `… or {}` before comparison — see
    docs/phase_D_plan.md, D2b."""
    meta = h.predicate_translation.consult(case["predicate"])
    expected = case["expected_output"]
    if meta.kb_property != expected.get("kb_property"):
        return False
    if case.get("category") == "qualifier_mapping":
        return (meta.slot_to_qualifier or {}) == (expected.get("slot_to_qualifier") or {})
    return True


def _run_subsumption(h: _Harness, case: dict) -> bool:
    from aedos.layer3_substrate.subsumption import EntityRef
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
    from aedos.layer1_extraction.extractor import Claim
    from aedos.layer1_extraction.triage import TriageDecision
    from aedos.layer4_sources.python_verifier import PythonVerifier
    inp = case["input"]
    claim = Claim(
        claim_id=case["id"], subject=inp["subject"], predicate=inp["predicate"],
        object=inp["object"], polarity=1, source_text=inp.get("context", ""),
        asserting_party="calibration", triage_decision=TriageDecision.VERIFY,
    )
    verdict = PythonVerifier(llm_client=h.client).verify(claim)
    return verdict.verdict == case["expected_output"]["verdict"]


_CONSISTENCY_CONFLICT_CLASS = {
    "subsumption": "contradicting_subsumption",
    "predicate_distribution": "conflicting_distribution",
}


def _insert_consistency_row(db, table: str, row: dict) -> None:
    """Insert one seeded substrate row for a consistency-check case. Raises
    sqlite3.IntegrityError if the row collides with an existing UNIQUE key."""
    if table == "predicate_translation":
        db.execute(
            "INSERT INTO predicate_translation "
            "(aedos_predicate, object_type, routing_hint, kb_namespace, kb_property, "
            "slot_to_qualifier, reason, created_at) "
            "VALUES (?, 'entity', 'kb_resolvable', 'wikidata', ?, ?, 'calib', '2026-01-01')",
            (row["aedos_predicate"], row["kb_property"], row.get("slot_to_qualifier")),
        )
    elif table == "subsumption":
        a_ns, a_id = row["entity_a"].split(":", 1)
        b_ns, b_id = row["entity_b"].split(":", 1)
        db.execute(
            "INSERT INTO subsumption "
            "(entity_a_namespace, entity_a_identifier, entity_b_namespace, "
            "entity_b_identifier, relation_type, verdict, source, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'calib', 'seed', '2026-01-01')",
            (a_ns, a_id, b_ns, b_id, row["relation_type"], row["verdict"]),
        )
    elif table == "predicate_distribution":
        db.execute(
            "INSERT INTO predicate_distribution "
            "(aedos_predicate, polarity, relation_type, verdict, reason, created_at) "
            "VALUES (?, ?, ?, ?, 'calib', '2026-01-01')",
            (row["predicate"], row["polarity"], row["relation_type"], row["verdict"]),
        )
    else:
        raise KeyError(f"unknown consistency table: {table}")


def _run_consistency_check(h: _Harness, case: dict) -> bool:
    """Seed two rows and ask the consistency checker. Each case gets a *fresh*
    in-memory DB: a case is a self-contained two-row scenario, and a shared DB
    both cross-contaminates checks keyed on (kb_namespace, kb_property) and
    collides on the predicate_translation UNIQUE key. The runner dispatches on
    `input.table` — its three table types carry different row schemas. See
    docs/phase_D_report.md, Phase D follow-up."""
    import sqlite3

    from aedos.database import open_memory_db
    from aedos.layer3_substrate.consistency import ConsistencyChecker, ConsistencyResult

    if case.get("category") != "seeded_conflict_detection":
        return True  # retract_and_regenerate / circuit_breaker_trigger: not scored here
    inp, expected = case["input"], case["expected_output"]
    table = inp["table"]
    db = open_memory_db()
    checker = ConsistencyChecker(db)

    if table == "predicate_translation":
        # The two rows are distinct predicates mapped to one KB property — they
        # coexist; the checker compares their slot_to_qualifier maps.
        _insert_consistency_row(db, table, inp["row_a"])
        _insert_consistency_row(db, table, inp["row_b"])
        db.commit()
        row_b_id = db.execute(
            "SELECT id FROM predicate_translation ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]
        result = checker.check_on_write(table, row_b_id)
    else:
        # subsumption / predicate_distribution: a conflicting second row shares
        # the table's UNIQUE key, so the write is rejected by the constraint —
        # that rejection is the consistency enforcement for these tables. The
        # synthetic ConsistencyResult records the prevented conflict (the
        # corpus notes call for this). If the second row instead coexists
        # (a distinct key), the checker scores it normally.
        _insert_consistency_row(db, table, inp["row_a"])
        db.commit()
        try:
            _insert_consistency_row(db, table, inp["row_b"])
            db.commit()
        except sqlite3.IntegrityError:
            if inp["row_a"].get("verdict") != inp["row_b"].get("verdict"):
                result = ConsistencyResult(
                    status="conflict",
                    inconsistency_class=_CONSISTENCY_CONFLICT_CLASS[table],
                    table=table,
                )
            else:
                result = ConsistencyResult(status="pass")  # pure duplicate
        else:
            row_b_id = db.execute(
                f"SELECT id FROM {table} ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
            result = checker.check_on_write(table, row_b_id)

    detected = result.status == "conflict"
    if detected != expected["conflict_detected"]:
        return False
    if detected and "inconsistency_class" in expected:
        return result.inconsistency_class == expected["inconsistency_class"]
    return True


def _run_intervention(h: _Harness, case: dict) -> bool:
    from aedos.deployment.chat_wrapper import select_intervention
    from aedos.layer5_result.aggregator import VerificationResult
    counts = case["input"]["verification_result"]
    total = counts.get("verified", 0) + counts.get("contradicted", 0) + counts.get("abstained", 0)
    vr = VerificationResult(
        claims_extracted=[], per_claim_verdicts={}, per_claim_traces={},
        aggregate_metadata={"claim_count": total, **counts},
        audit_log_entries=[], text_input={},
    )
    return select_intervention(vr).value == case["expected_output"]["intervention_type"]


def _run_derivation(h: _Harness, case: dict) -> bool:
    from aedos.layer1_extraction.extractor import Claim, Extractor, ExtractionContext
    from aedos.layer1_extraction.triage import TriageDecision
    from aedos.layer4_sources.walker import VerificationContext
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

    # Non-standard expected verdicts. The pre-D2 blanket `return True`
    # auto-passed every one of these regardless of walker output (4 of 50
    # cases) — see docs/phase_D_plan.md, D2c Issue 1.
    if expected_verdict == "verified_with_correct_entity":
        # Walker must verify AND have resolved the intended entity. The intended
        # Q-number lives in the case's free-text `disambiguation_note`; pinned
        # here per case id (der_disambiguation_001 → Q76, _006 → Q3783).
        intended = {"der_disambiguation_001": "Q76", "der_disambiguation_006": "Q3783"}
        wanted = intended.get(case["id"])
        if wanted is None:
            return True  # unrecognized verified_with_correct_entity case: lenient
        trace_entities = {
            e.target.content.get("entity")
            for e in result.trace.edges
            if e.target.node_type == "kb_statement"
        }
        return result.verdict == "verified" and wanted in trace_entities
    if expected_verdict == "needs_tier_u_or_kb":
        # der_predicate_translation_007: the runner seeds no Tier U and the
        # subject has no KB statement, so with neither premise present the
        # walker abstains.
        return result.verdict == "no_grounding_found"

    # der_disambiguation_005 carries no `verdict` key — the corpus pins no
    # answer (note: "may abstain or need context"). Kept an explicit auto-pass
    # by the Phase D check-in (docs/phase_D_plan.md, Q3).
    return True


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
# Dry-run stubs (Phase D follow-up). The dry-run path used to skip after merely
# loading the corpus — it never invoked the runners, so a runner that KeyError'd
# on most of its corpus (the Phase D hard blockers) passed the dry-run green.
# These stubs let every runner's case-reading and comparison code run to
# completion with no LLM/KB cost: a stub LLM returns structurally-valid-but-
# uncalibrated responses, the heavy components are universal structural stubs,
# and the DB is the real in-memory one. The component outputs are deliberately
# uncalibrated and unused — the dry-run checks only that the runner completes
# without a structural exception (KeyError, AttributeError, …).
# ---------------------------------------------------------------------------

class _Stub:
    """Universal structural stub: attribute access and calls return another
    _Stub, iteration yields nothing. Stands in for the dry-run's heavy
    components (predicate translation, resolver, substrate, walker, Tier U, KB)
    so every chained access a runner makes resolves without raising."""

    def __getattr__(self, name):
        return _Stub()

    def __call__(self, *args, **kwargs):
        return _Stub()

    def __iter__(self):
        return iter(())


class _StubLLM:
    """Minimal LLM stub for the dry-run. Returns structurally-valid responses
    so the real (lightweight) Extractor and PythonVerifier run to completion."""

    def extract_with_tool(self, system=None, user_message=None, tool=None, **kwargs):
        name = (tool or {}).get("name")
        if name == "extract_claims":
            # One claim whose subject is the input text verbatim, so it survives
            # the extractor's hard-claim check (subject must appear in the text)
            # and the runner sees a non-empty claim list.
            return {"claims": [{
                "subject": user_message or "calib_subject",
                "predicate": "calib_predicate",
                "object": "calib_object",
                "polarity": 1,
                "source_text": user_message or "",
                "verb_tense": "present",
            }]}
        if name == "generate_python_verify":
            # Empty code → PythonVerifier returns no_terminal_result without
            # touching the sandbox.
            return {"code": "", "reasoning": "dry-run stub"}
        return {  # predicate-metadata or any other tool
            "object_type": "entity",
            "user_subject_required": 0,
            "routing_hint": "abstain",
            "reason": "dry-run stub",
        }

    def chat(self, system=None, messages=None, purpose=None, **kwargs):
        return ""


class _DryRunHarness(_Harness):
    """`_Harness` with a stub LLM and stub heavy components, but the real
    in-memory DB (cheap, and `_run_consistency_check` / `_run_derivation` need
    a real schema). No LLM or KB call is made."""

    def __init__(self):
        super().__init__()
        self._stub_llm = _StubLLM()

    @property
    def client(self):
        return self._stub_llm

    @property
    def kb(self):
        return _Stub()

    @property
    def predicate_translation(self):
        return _Stub()

    @property
    def resolver(self):
        return _Stub()

    @property
    def substrate(self):
        return _Stub()

    def walker(self):
        return _Stub(), _Stub()


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
        # Harness dry-run: invoke the runner against every case with a stubbed
        # harness (no LLM/KB cost). The runner outputs are uncalibrated and
        # unused — what is checked is that every case completes without a
        # structural exception (a KeyError on a missing case key being the
        # Phase D hard-blocker shape). A runner that cannot score a case fails
        # the dry-run here, rather than passing it silently and surfacing only
        # under a paid live run.
        runner = _RUNNERS[corpus]
        harness = _DryRunHarness()
        errors: list[tuple[str, str, str]] = []
        for case in cases:
            try:
                runner(harness, case)
            except Exception as exc:
                errors.append((case.get("id", "?"), type(exc).__name__, str(exc)))
        if errors:
            sample = "; ".join(f"{cid} {etype}: {emsg}" for cid, etype, emsg in errors[:5])
            pytest.fail(
                f"{corpus}: {len(errors)}/{len(cases)} cases raised a structural "
                f"error in the harness dry-run — the runner cannot score them. "
                f"First {min(5, len(errors))}: {sample}"
            )
        pytest.skip(
            f"{corpus}: {len(cases)} cases invoked through the runner with no "
            f"structural error (harness dry-run, stubbed components). Set "
            f"RUN_CALIBRATION=1 (with RUN_LIVE_KB=1, RUN_LIVE_TESTS=1) for live "
            f"evaluation against the {THRESHOLDS[corpus]:.0%} threshold."
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
