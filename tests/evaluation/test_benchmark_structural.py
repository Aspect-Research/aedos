"""
Structural test for the benchmark harness.

Confirms the test set parses, all failure modes are covered, and the
metrics/report machinery works end-to-end with mock results.
Execution of the actual Aedos vs baseline comparison is deferred to Phase 10.5.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_TEST_SET_PATH = Path(__file__).parent / "medium_bar_test_set.jsonl"

_EXPECTED_FAILURE_MODES = {
    "multi_hop_distribution",
    "cross_source_unification",
    "entity_disambiguation",
    "predicate_translation",
    "belief_revision",
    "principled_abstention",
}


# ---------------------------------------------------------------------------
# Test set file checks
# ---------------------------------------------------------------------------

class TestTestSetFile:
    @pytest.fixture(scope="class")
    def cases_raw(self):
        return [json.loads(l) for l in _TEST_SET_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]

    def test_file_exists(self):
        assert _TEST_SET_PATH.exists()

    def test_at_least_100_cases(self, cases_raw):
        assert len(cases_raw) >= 100

    def test_at_most_150_cases(self, cases_raw):
        assert len(cases_raw) <= 150

    def test_all_required_fields(self, cases_raw):
        for d in cases_raw:
            assert "case_id" in d
            assert "statement" in d
            assert "ground_truth" in d
            assert "failure_mode" in d

    def test_ground_truth_values_valid(self, cases_raw):
        valid = {"verified", "contradicted", "abstain"}
        for d in cases_raw:
            assert d["ground_truth"] in valid, f"{d['case_id']}: invalid ground_truth {d['ground_truth']!r}"

    def test_all_six_failure_modes_covered(self, cases_raw):
        modes = {d["failure_mode"] for d in cases_raw}
        missing = _EXPECTED_FAILURE_MODES - modes
        assert not missing, f"Test set missing failure modes: {missing}"

    def test_no_duplicate_case_ids(self, cases_raw):
        ids = [d["case_id"] for d in cases_raw]
        assert len(ids) == len(set(ids))

    def test_failure_mode_distribution_at_least_10_each(self, cases_raw):
        from collections import Counter
        counts = Counter(d["failure_mode"] for d in cases_raw)
        for mode in _EXPECTED_FAILURE_MODES:
            assert counts[mode] >= 10, f"Failure mode {mode!r} has only {counts[mode]} cases (need ≥10)"

    def test_statements_non_empty(self, cases_raw):
        for d in cases_raw:
            assert d["statement"].strip(), f"{d['case_id']}: empty statement"


# ---------------------------------------------------------------------------
# Benchmark harness structural test
# ---------------------------------------------------------------------------

class TestBenchmarkHarness:
    @pytest.fixture(scope="class")
    def cases(self):
        from tests.evaluation.benchmark import load_test_set
        return load_test_set()

    def test_loads_all_cases(self, cases):
        assert len(cases) >= 100

    def test_metrics_perfect_mock(self, cases):
        from tests.evaluation.benchmark import RunResult, compute_metrics
        mock_results = [RunResult(case_id=c.case_id, verdict=c.ground_truth) for c in cases]
        metrics = compute_metrics(cases, mock_results)
        assert metrics.accuracy == 1.0
        assert metrics.false_verified == 0

    def test_metrics_all_wrong_mock(self, cases):
        from tests.evaluation.benchmark import RunResult, compute_metrics
        wrong_map = {"verified": "contradicted", "contradicted": "verified", "abstain": "verified"}
        results = [RunResult(case_id=c.case_id, verdict=wrong_map[c.ground_truth]) for c in cases]
        metrics = compute_metrics(cases, results)
        assert metrics.accuracy < 0.5

    def test_per_failure_mode_keys(self, cases):
        from tests.evaluation.benchmark import RunResult, compute_metrics
        mock_results = [RunResult(case_id=c.case_id, verdict=c.ground_truth) for c in cases]
        metrics = compute_metrics(cases, mock_results)
        for mode in _EXPECTED_FAILURE_MODES:
            assert mode in metrics.per_failure_mode

    def test_report_generation(self, cases):
        from tests.evaluation.benchmark import RunResult, generate_report
        mock_results = [RunResult(case_id=c.case_id, verdict=c.ground_truth) for c in cases]
        report = generate_report(cases, mock_results, mock_results)
        assert "Aedos v0.15" in report
        assert "Accuracy" in report

    def test_structural_self_test_passes(self):
        from tests.evaluation.benchmark import _structural_test
        assert _structural_test()

    def test_aedos_runner_with_null_pipeline(self, cases):
        from tests.evaluation.benchmark import AedosRunner
        runner = AedosRunner(pipeline=None)
        result = runner.run_case(cases[0])
        assert result.verdict == "no_grounding_found"

    def test_baseline_runner_with_null_client(self, cases):
        from tests.evaluation.benchmark import BaselineRunner
        runner = BaselineRunner(llm_client=None)
        result = runner.run_case(cases[0])
        assert result.verdict == "no_grounding_found"

    def test_normalize_verdict_mapping(self):
        from tests.evaluation.benchmark import _normalize_verdict
        assert _normalize_verdict("verified") == "verified"
        assert _normalize_verdict("contradicted") == "contradicted"
        assert _normalize_verdict("no_grounding_found") == "abstain"
        assert _normalize_verdict("error") == "abstain"

    def test_validate_harness(self, tmp_path):
        # M5 Step 6 wiring check: build_pipeline against mocks, run one case
        # through each runner, write a report. This is the discriminating test
        # for the stale-signature fix — with AedosRunner.run_case's pre-fix
        # signatures, run_case reports `error` and _validate_harness raises.
        from tests.evaluation.benchmark import _validate_harness
        out = tmp_path / "harness_report.md"
        assert _validate_harness(output_path=out) is True
        assert out.exists()
        assert out.read_text(encoding="utf-8").strip()

    def test_metrics_synthetic_mix(self):
        # A synthetic mix of correct / false-verified / false-abstain results
        # confirms compute_metrics produces the right counts and rates.
        from tests.evaluation.benchmark import (
            BenchmarkCase, RunResult, compute_metrics,
        )
        cases = [
            BenchmarkCase("v1", "s", "verified", "multi_hop_distribution", ""),
            BenchmarkCase("v2", "s", "verified", "multi_hop_distribution", ""),
            BenchmarkCase("c1", "s", "contradicted", "belief_revision", ""),
            BenchmarkCase("a1", "s", "abstain", "principled_abstention", ""),
        ]
        results = [
            RunResult("v1", "verified"),            # correct
            RunResult("v2", "no_grounding_found"),  # false abstain (true claim abstained)
            RunResult("c1", "verified"),            # false verified (contradicted -> verified)
            RunResult("a1", "no_grounding_found"),  # correct abstain
        ]
        m = compute_metrics(cases, results)
        assert m.total == 4
        assert m.correct == 2
        assert m.accuracy == 0.5
        assert m.false_verified == 1
        assert m.false_verified_rate == 0.25
        assert m.false_abstain == 1
        assert m.false_abstain_rate == 0.5  # 1 of 2 verified-ground-truth cases
        assert m.per_failure_mode["multi_hop_distribution"]["accuracy"] == 0.5
        assert m.per_failure_mode["belief_revision"]["accuracy"] == 0.0
        assert m.per_failure_mode["principled_abstention"]["accuracy"] == 1.0

    def test_report_renders_all_four_criteria(self):
        # generate_report must render all four Phase 10.5 acceptance criteria
        # the runbook lists (false-verified, accuracy delta, no-regression,
        # significant improvement).
        from tests.evaluation.benchmark import (
            BenchmarkCase, RunResult, generate_report,
        )
        cases = [
            BenchmarkCase("v1", "s", "verified", "multi_hop_distribution", ""),
            BenchmarkCase("c1", "s", "contradicted", "belief_revision", ""),
        ]
        aedos = [RunResult("v1", "verified"), RunResult("c1", "contradicted")]
        baseline = [RunResult("v1", "no_grounding_found"), RunResult("c1", "verified")]
        report = generate_report(cases, aedos, baseline)
        assert "False-verified" in report
        assert "baseline + 15pp" in report
        assert "No-regression multi_hop_distribution" in report
        assert "Significant improvement" in report
