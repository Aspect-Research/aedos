"""
Medium-bar evaluation benchmark for Aedos v0.15.

Written in Phase 10. Execution is DEFERRED TO PHASE 10.5.
This module contains:
  - BenchmarkRunner: runs Aedos against the test set
  - BaselineRunner: runs an LLM-only forward pass
  - MetricsComputer: accuracy, false-verified rate, per-failure-mode breakdown
  - Structural self-test: confirms the harness works against mocks/fixtures

Phase 10.5 acceptance thresholds (from implementation plan §Phase 10):
  - Aedos false-verified rate ≤ 5%
  - Aedos overall accuracy ≥ baseline + 15pp on curated test set
  - Aedos accuracy ≥ baseline on every failure mode (no regression)
  - Aedos accuracy significantly higher on ≥ 4 of 6 failure modes

Usage (Phase 10.5 operator-supervised):
    RUN_LIVE_TESTS=1 RUN_LIVE_KB=1 python -m tests.v0_15.evaluation.benchmark \\
        --test-set tests/v0_15/evaluation/medium_bar_test_set.jsonl \\
        --output docs/v0_15/evaluation_results.md
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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
        if self._pipeline is None:
            return RunResult(case_id=case.case_id, verdict="no_grounding_found")
        extractor, walker, aggregator = self._pipeline
        start = time.monotonic()
        try:
            claims = extractor.extract(case.statement, context={})
            if not claims:
                return RunResult(case_id=case.case_id, verdict="no_grounding_found",
                                 latency_seconds=time.monotonic() - start)
            results = [walker.walk(c) for c in claims]
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
        if self._client is None:
            return RunResult(case_id=case.case_id, verdict="no_grounding_found")
        start = time.monotonic()
        try:
            prompt = self.BASELINE_PROMPT_TEMPLATE.format(statement=case.statement)
            response = self._client.chat([{"role": "user", "content": prompt}])
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

    # Per-failure-mode no-regression check
    for mode in sorted(aedos_metrics.per_failure_mode):
        a_acc = aedos_metrics.per_failure_mode[mode]["accuracy"]
        b_acc = baseline_metrics.per_failure_mode.get(mode, {}).get("accuracy", 0.0)
        status = "PASS" if a_acc >= b_acc else "FAIL"
        lines.append(f"No-regression {mode}: {status} (Aedos {a_acc:.1%} vs baseline {b_acc:.1%})")

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


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Aedos v0.15 medium-bar evaluation")
    parser.add_argument("--test-set", type=Path, default=_TEST_SET_PATH)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--structural-test", action="store_true")
    args = parser.parse_args()

    if args.structural_test:
        ok = _structural_test()
        print("Structural test: PASS" if ok else "Structural test: FAIL")
    else:
        if not (os.environ.get("RUN_LIVE_TESTS") == "1" and os.environ.get("RUN_LIVE_KB") == "1"):
            print("Live evaluation requires RUN_LIVE_TESTS=1 and RUN_LIVE_KB=1. "
                  "Use --structural-test for mock validation.")
            raise SystemExit(1)

        cases = load_test_set(args.test_set)
        print(f"Loaded {len(cases)} cases from {args.test_set}")
        print("Live evaluation not yet implemented — deferred to Phase 10.5.")
