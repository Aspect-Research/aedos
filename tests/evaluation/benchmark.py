"""
Medium-bar evaluation benchmark for Aedos v0.15.

The live runner is implemented (fix-up 2, M5 Step 6). The medium-bar evaluation
itself is run under operator supervision in Phase 10.5 — it requires live LLM
and live Wikidata calls.

This module contains:
  - AedosRunner: runs the full Aedos pipeline against the test set
  - BaselineRunner: runs an LLM-only forward pass
  - compute_metrics / generate_report: accuracy, false-verified rate,
    false-abstain rate, per-failure-mode breakdown
  - _structural_test: checks the metrics/report machinery against mock results
  - _validate_harness: builds the production pipeline against mocks and runs one
    case through each runner — confirms the wiring without live API cost

Phase 10.5 acceptance thresholds (implementation plan §Phase 10):
  - Aedos false-verified rate ≤ 5%
  - Aedos overall accuracy ≥ baseline + 15pp on the curated test set
  - Aedos accuracy ≥ baseline on every failure mode (no regression)
  - Aedos accuracy significantly higher on ≥ 4 of 6 failure modes

Usage:
    # harness wiring check (mocked, no API cost — part of `make test`):
    py -m tests.evaluation.benchmark --validate-harness
    # live evaluation (Phase 10.5, operator-supervised):
    RUN_LIVE_TESTS=1 RUN_LIVE_KB=1 py -m tests.evaluation.benchmark \\
        --test-set tests/evaluation/medium_bar_test_set.jsonl \\
        --output docs/evaluation_results.md
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Make the `aedos` package importable when this module is run directly
# (py -m tests.evaluation.benchmark) without an editable install — this mirrors
# what pyproject's pytest `pythonpath` does for the test run.
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_TEST_SET_PATH = Path(__file__).parent / "medium_bar_test_set.jsonl"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class BenchmarkCase:
    case_id: str
    statement: str
    ground_truth: str  # verified | contradicted | abstain
    failure_mode: str
    notes: str


@dataclass
class RunResult:
    case_id: str
    verdict: str  # verified | contradicted | no_grounding_found | error
    latency_seconds: float = 0.0
    extra: dict = field(default_factory=dict)


@dataclass
class EvaluationMetrics:
    total: int
    correct: int
    accuracy: float
    false_verified: int
    false_verified_rate: float
    false_abstain: int
    false_abstain_rate: float
    per_failure_mode: dict[str, dict]

    def summary(self) -> str:
        lines = [
            f"Total cases: {self.total}",
            f"Accuracy: {self.accuracy:.1%} ({self.correct}/{self.total})",
            f"False-verified rate: {self.false_verified_rate:.1%} ({self.false_verified})",
            f"False-abstain rate: {self.false_abstain_rate:.1%} ({self.false_abstain})",
            "",
            "Per-failure-mode breakdown:",
        ]
        for mode, stats in sorted(self.per_failure_mode.items()):
            lines.append(
                f"  {mode}: {stats['accuracy']:.1%} ({stats['correct']}/{stats['total']})"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Test set loader
# ---------------------------------------------------------------------------

def load_test_set(path: Path = _TEST_SET_PATH) -> list[BenchmarkCase]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        cases.append(BenchmarkCase(
            case_id=d["case_id"],
            statement=d["statement"],
            ground_truth=d["ground_truth"],
            failure_mode=d["failure_mode"],
            notes=d.get("notes", ""),
        ))
    return cases


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------

def _normalize_verdict(verdict: str) -> str:
    """Map Aedos internal verdicts to benchmark verdicts."""
    if verdict == "verified":
        return "verified"
    if verdict == "contradicted":
        return "contradicted"
    return "abstain"


def compute_metrics(cases: list[BenchmarkCase], results: list[RunResult]) -> EvaluationMetrics:
    result_map = {r.case_id: r for r in results}
    total = len(cases)
    correct = 0
    false_verified = 0
    false_abstain = 0
    total_verified_ground_truth = sum(1 for c in cases if c.ground_truth == "verified")

    per_mode: dict[str, dict] = {}
    for case in cases:
        mode = case.failure_mode
        if mode not in per_mode:
            per_mode[mode] = {"total": 0, "correct": 0}
        per_mode[mode]["total"] += 1

        result = result_map.get(case.case_id)
        predicted = _normalize_verdict(result.verdict) if result else "abstain"
        is_correct = predicted == case.ground_truth

        if is_correct:
            correct += 1
            per_mode[mode]["correct"] += 1

        if predicted == "verified" and case.ground_truth != "verified":
            false_verified += 1
        if predicted == "abstain" and case.ground_truth == "verified":
            false_abstain += 1

    for mode in per_mode:
        n = per_mode[mode]["total"]
        c = per_mode[mode]["correct"]
        per_mode[mode]["accuracy"] = c / n if n > 0 else 0.0

    return EvaluationMetrics(
        total=total,
        correct=correct,
        accuracy=correct / total if total > 0 else 0.0,
        false_verified=false_verified,
        false_verified_rate=false_verified / total if total > 0 else 0.0,
        false_abstain=false_abstain,
        false_abstain_rate=false_abstain / total_verified_ground_truth if total_verified_ground_truth > 0 else 0.0,
        per_failure_mode=per_mode,
    )


# ---------------------------------------------------------------------------
# Aedos runner
# ---------------------------------------------------------------------------

class AedosRunner:
    """Run Aedos v0.15 against the test set."""

    def __init__(self, pipeline=None):
        self._pipeline = pipeline

    def run_case(self, case: BenchmarkCase) -> RunResult:
        import time
        from datetime import datetime, timezone

        from aedos.layer1_extraction.extractor import ExtractionContext
        from aedos.layer1_extraction.triage import TriageDecision
        from aedos.layer4_sources.walker import VerificationContext

        if self._pipeline is None:
            return RunResult(case_id=case.case_id, verdict="no_grounding_found")
        extractor, walker, aggregator = self._pipeline
        start = time.monotonic()
        try:
            ctx = ExtractionContext(asserting_party="benchmark", context_type="document")
            claims = extractor.extract(case.statement, ctx)
            # Only VERIFY-triaged claims are verified (matches the chat-wrapper).
            claims = [c for c in claims if c.triage_decision == TriageDecision.VERIFY]
            if not claims:
                return RunResult(case_id=case.case_id, verdict="no_grounding_found",
                                 latency_seconds=time.monotonic() - start)
            vctx = VerificationContext(
                current_time=datetime.now(timezone.utc).isoformat(),
                asserting_party="benchmark",
            )
            results = [walker.walk(c, vctx) for c in claims]
            vr = aggregator.aggregate(claims, results)
            verdicts = list(vr.per_claim_verdicts.values())
            verdict = verdicts[0] if len(verdicts) == 1 else (
                "contradicted" if "contradicted" in verdicts else
                ("verified" if all(v == "verified" for v in verdicts) else "no_grounding_found")
            )
        except Exception as exc:
            verdict = "error"
        latency = time.monotonic() - start
        return RunResult(case_id=case.case_id, verdict=verdict, latency_seconds=latency)

    def run_all(self, cases: list[BenchmarkCase]) -> list[RunResult]:
        return [self.run_case(c) for c in cases]


# ---------------------------------------------------------------------------
# Baseline runner (LLM-only)
# ---------------------------------------------------------------------------

class BaselineRunner:
    """
    LLM-only baseline: one forward pass per statement.
    The LLM is asked to evaluate correctness without any architectural support.
    """

    BASELINE_PROMPT_TEMPLATE = (
        "Evaluate whether the following statement is factually correct, incorrect, or uncertain. "
        "Respond with exactly one of: VERIFIED, CONTRADICTED, or ABSTAIN.\n\n"
        "Statement: {statement}\n\nVerdict:"
    )

    def __init__(self, llm_client=None):
        self._client = llm_client

    def run_case(self, case: BenchmarkCase) -> RunResult:
        import time

        from aedos.llm.client import ChatMessage

        if self._client is None:
            return RunResult(case_id=case.case_id, verdict="no_grounding_found")
        start = time.monotonic()
        try:
            prompt = self.BASELINE_PROMPT_TEMPLATE.format(statement=case.statement)
            response = self._client.chat(
                system="",
                messages=[ChatMessage(role="user", content=prompt)],
                purpose="chat",
            )
            text = response.strip().upper()
            if "VERIFIED" in text:
                verdict = "verified"
            elif "CONTRADICTED" in text:
                verdict = "contradicted"
            else:
                verdict = "no_grounding_found"
        except Exception:
            verdict = "error"
        return RunResult(case_id=case.case_id, verdict=verdict, latency_seconds=time.monotonic() - start)

    def run_all(self, cases: list[BenchmarkCase]) -> list[RunResult]:
        return [self.run_case(c) for c in cases]


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(
    cases: list[BenchmarkCase],
    aedos_results: list[RunResult],
    baseline_results: list[RunResult],
    output_path: Optional[Path] = None,
) -> str:
    aedos_metrics = compute_metrics(cases, aedos_results)
    baseline_metrics = compute_metrics(cases, baseline_results)

    lines = [
        "# Aedos v0.15 Medium-Bar Evaluation Results",
        "",
        "## Aedos v0.15",
        aedos_metrics.summary(),
        "",
        "## LLM-Only Baseline",
        baseline_metrics.summary(),
        "",
        "## Comparison",
        f"Accuracy delta: {aedos_metrics.accuracy - baseline_metrics.accuracy:+.1%}",
        f"False-verified delta: {aedos_metrics.false_verified_rate - baseline_metrics.false_verified_rate:+.1%}",
        "",
        "## Phase 10.5 Acceptance Criteria",
        f"False-verified ≤ 5%: {'PASS' if aedos_metrics.false_verified_rate <= 0.05 else 'FAIL'} "
        f"(actual: {aedos_metrics.false_verified_rate:.1%})",
        f"Accuracy ≥ baseline + 15pp: "
        f"{'PASS' if aedos_metrics.accuracy >= baseline_metrics.accuracy + 0.15 else 'FAIL'} "
        f"(delta: {aedos_metrics.accuracy - baseline_metrics.accuracy:+.1%})",
    ]

    # Per-failure-mode no-regression check (criterion 3).
    big_gains = 0
    for mode in sorted(aedos_metrics.per_failure_mode):
        a_acc = aedos_metrics.per_failure_mode[mode]["accuracy"]
        b_acc = baseline_metrics.per_failure_mode.get(mode, {}).get("accuracy", 0.0)
        status = "PASS" if a_acc >= b_acc else "FAIL"
        lines.append(f"No-regression {mode}: {status} (Aedos {a_acc:.1%} vs baseline {b_acc:.1%})")
        if a_acc >= b_acc + 0.20:
            big_gains += 1

    # Criterion 4: significant improvement on >= 4 of 6 modes. The plan says
    # "significantly higher"; the runbook operationalizes that as >= +20pp.
    n_modes = len(aedos_metrics.per_failure_mode)
    lines.append(
        f"Significant improvement (>= +20pp) on >= 4 of 6 modes: "
        f"{'PASS' if big_gains >= 4 else 'FAIL'} ({big_gains}/{n_modes} modes)"
    )

    report = "\n".join(lines)
    if output_path is not None:
        output_path.write_text(report, encoding="utf-8")
    return report


# ---------------------------------------------------------------------------
# Structural self-test (always runs — confirms harness works)
# ---------------------------------------------------------------------------

def _structural_test():
    """Run benchmark harness against mock data. Returns True if all checks pass."""
    cases = load_test_set()
    assert len(cases) >= 100, f"Test set has only {len(cases)} cases (need ≥100)"

    failure_modes = {c.failure_mode for c in cases}
    expected_modes = {
        "multi_hop_distribution",
        "cross_source_unification",
        "entity_disambiguation",
        "predicate_translation",
        "belief_revision",
        "principled_abstention",
    }
    missing = expected_modes - failure_modes
    assert not missing, f"Test set missing failure modes: {missing}"

    # Mock runner
    mock_results = [RunResult(case_id=c.case_id, verdict=c.ground_truth) for c in cases]
    metrics = compute_metrics(cases, mock_results)
    assert metrics.accuracy == 1.0, f"Perfect mock results should give 100% accuracy, got {metrics.accuracy}"
    assert metrics.false_verified == 0

    # Generate report
    report = generate_report(cases, mock_results, mock_results)
    assert "Aedos v0.15" in report

    return True


# ---------------------------------------------------------------------------
# Harness validation — builds the production pipeline against mocks and runs one
# case through each runner. Confirms the M5-Step-6 wiring (pipeline build, the
# runner signatures, generate_report) without consuming live API. Runs as part
# of the default mocked suite via tests/evaluation/test_benchmark_structural.py.
# ---------------------------------------------------------------------------

class _HarnessTransport:
    """Minimal mock LLM transport: returns safe canned tool outputs keyed by
    tool name, so the full pipeline can be exercised once with no live API."""

    def extract_with_tool(self, system, user_message, tool, model="", purpose=None):
        name = tool.get("name", "")
        if name == "extract_claims":
            # One claim whose subject is a substring of the text (hard-claim
            # check) and whose predicate triages to VERIFY (located_in is in
            # triage._ALWAYS_VERIFY) — so the walk is genuinely exercised.
            head = (user_message.strip().split() or ["entity"])[0]
            return {"claims": [{
                "subject": head, "predicate": "located_in", "object": head,
                "polarity": 1, "source_text": user_message.strip()[:60] or head,
                "verb_tense": "present",
            }]}
        if name == "generate_predicate_metadata":
            return {
                "object_type": "entity", "user_subject_required": 0,
                "distinct_slots": None, "routing_hint": "abstain",
                "kb_namespace": None, "kb_property": None,
                "slot_to_qualifier": None, "single_valued": 0,
                "reason": "harness mock",
            }
        if name == "generate_subsumption_verdict":
            return {"verdict": "unrelated", "reason": "harness mock"}
        if name == "generate_python_verify":
            return {"code": "def verify(subject, predicate, obj):\n    return None",
                    "reasoning": "harness mock"}
        # predicate-distribution tool and any other: a gate-closed verdict.
        return {"verdict": "neither", "reason": "harness mock"}

    def chat(self, system, messages, model="", purpose=None):
        return "ABSTAIN"


class _HarnessKB:
    """Mock KB that grounds nothing — the harness walk abstains cleanly."""

    def resolve_entity(self, reference, local_context):
        return []

    def lookup_statements(self, entity, predicate):
        return []

    def subsumption(self, entity_a, entity_b, relation_type):
        from aedos.layer4_sources.kb_protocol import SubsumptionResult
        return SubsumptionResult(verdict="unrelated")


def _validate_harness(
    test_set_path: Path = _TEST_SET_PATH,
    output_path: Optional[Path] = None,
) -> bool:
    """Build the production pipeline against mocks and run one case through each
    runner. Confirms the wiring — pipeline construction, the runner signatures,
    generate_report — without consuming live API. Returns True on success;
    raises AssertionError if the wiring is broken (e.g. a stale runner signature
    makes run_case report an `error` verdict).
    """
    from aedos.database import open_memory_db
    from aedos.llm.client import LLMClient
    from aedos.pipeline import build_pipeline

    cases = load_test_set(test_set_path)
    assert cases, "test set is empty"

    client = LLMClient(_transport=_HarnessTransport())
    pipeline = build_pipeline(open_memory_db(), llm_client=client, kb=_HarnessKB())

    aedos = AedosRunner(pipeline=(pipeline.extractor, pipeline.walker, pipeline.aggregator))
    baseline = BaselineRunner(llm_client=client)

    a = aedos.run_case(cases[0])
    b = baseline.run_case(cases[0])
    # An "error" verdict means the runner caught an exception — the wiring is
    # broken (e.g. a stale walker.walk / extractor.extract signature).
    assert a.verdict != "error", "AedosRunner errored — pipeline wiring is broken"
    assert b.verdict != "error", "BaselineRunner errored — wiring is broken"
    assert a.verdict in ("verified", "contradicted", "no_grounding_found")
    assert b.verdict in ("verified", "contradicted", "no_grounding_found")

    report = generate_report(cases, [a], [b], output_path=output_path)
    assert "# Aedos v0.15 Medium-Bar Evaluation Results" in report
    assert "Phase 10.5 Acceptance Criteria" in report
    if output_path is not None:
        assert output_path.exists() and output_path.read_text(encoding="utf-8").strip()
    return True


# ---------------------------------------------------------------------------
# Live evaluation entrypoint (Phase 10.5)
# ---------------------------------------------------------------------------

def _run_live(args) -> int:
    """Run the medium-bar evaluation live against the production pipeline.
    Returns a process exit code."""
    from aedos.database import open_db
    from aedos.pipeline import build_pipeline

    db_path = os.environ.get("AEDOS_DB_PATH", "aedos_phase10_5.db")
    cases = load_test_set(args.test_set)
    print(f"Loaded {len(cases)} cases from {args.test_set}")
    print(f"Database: {db_path} (load the seed pack first — runbook Step 2)")

    pipeline = build_pipeline(open_db(db_path))
    aedos = AedosRunner(pipeline=(pipeline.extractor, pipeline.walker, pipeline.aggregator))
    baseline = BaselineRunner(llm_client=pipeline.llm_client)

    if args.baseline_only:
        print(f"Running baseline only over {len(cases)} cases ...")
        print(compute_metrics(cases, baseline.run_all(cases)).summary())
        return 0
    if args.aedos_only:
        print(f"Running Aedos only over {len(cases)} cases ...")
        print(compute_metrics(cases, aedos.run_all(cases)).summary())
        return 0

    print(f"Running LLM-only baseline over {len(cases)} cases ...")
    baseline_results = baseline.run_all(cases)
    print(f"Running Aedos pipeline over {len(cases)} cases ...")
    aedos_results = aedos.run_all(cases)

    report = generate_report(cases, aedos_results, baseline_results, output_path=args.output)
    print()
    print(report)
    if args.output is not None:
        print(f"\nResults written to {args.output}")
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Aedos v0.15 medium-bar evaluation")
    parser.add_argument("--test-set", type=Path, default=_TEST_SET_PATH)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--structural-test", action="store_true",
                        help="Check the metrics/report machinery against mock results.")
    parser.add_argument("--validate-harness", action="store_true",
                        help="Build the production pipeline against mocks and run one "
                             "case through each runner — confirms wiring, no live API.")
    parser.add_argument("--baseline-only", action="store_true",
                        help="Development: run only the LLM-only baseline.")
    parser.add_argument("--aedos-only", action="store_true",
                        help="Development: run only the Aedos pipeline.")
    args = parser.parse_args()

    if args.structural_test:
        ok = _structural_test()
        print("Structural test: PASS" if ok else "Structural test: FAIL")
        raise SystemExit(0 if ok else 1)

    if args.validate_harness:
        ok = _validate_harness(args.test_set)
        print("Harness validation: PASS" if ok else "Harness validation: FAIL")
        raise SystemExit(0 if ok else 1)

    # Live evaluation — Phase 10.5 only. Must use live calls, never mocks.
    if not (os.environ.get("RUN_LIVE_TESTS") == "1" and os.environ.get("RUN_LIVE_KB") == "1"):
        print("Live evaluation requires RUN_LIVE_TESTS=1 and RUN_LIVE_KB=1 "
              "(Phase 10.5). Use --validate-harness for a mocked wiring check.")
        raise SystemExit(1)

    raise SystemExit(_run_live(args))
