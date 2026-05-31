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
        assert "# Aedos Medium-Bar Evaluation Results" in report
        assert "Accuracy" in report
        # The perfect mock passes both hard soundness gates.
        assert "HARD GATE false_verified == 0: PASS" in report
        assert "HARD GATE false_contradicted == 0: PASS" in report

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

    def test_false_contradicted_counter(self):
        # v0.16.1 WS1: the symmetric false-contradict counter. §3.2 forbids a
        # false-contradict as much as a false-verify. A `contradicted` prediction
        # on a case whose ground truth is NOT contradicted counts as a
        # false-contradict, broken out by the gt bucket it stole from; a
        # `contradicted` prediction on a gt=contradicted case is correct and
        # contributes 0. Pure measurement — no verdict logic is exercised here.
        from tests.evaluation.benchmark import (
            BenchmarkCase, RunResult, compute_metrics,
        )
        cases = [
            BenchmarkCase("v1", "s", "verified", "multi_hop_distribution", ""),
            BenchmarkCase("a1", "s", "abstain", "principled_abstention", ""),
            BenchmarkCase("c1", "s", "contradicted", "belief_revision", ""),
            BenchmarkCase("c2", "s", "contradicted", "belief_revision", ""),
        ]
        results = [
            RunResult("v1", "contradicted"),       # false-contradict (stole from verified)
            RunResult("a1", "contradicted"),       # false-contradict (stole from abstain)
            RunResult("c1", "contradicted"),       # correct — NOT a false-contradict
            RunResult("c2", "no_grounding_found"), # a (false) abstain, not a false-contradict
        ]
        m = compute_metrics(cases, results)
        assert m.false_contradicted == 2
        assert m.false_contradicted_gt_verified == 1
        assert m.false_contradicted_gt_abstain == 1
        assert m.false_contradicted_rate == 0.5  # 2 of 4 cases
        # A correct contradicted prediction never counts toward false-contradict.
        assert m.per_failure_mode["belief_revision"]["correct"] == 1

    def test_false_contradicted_zero_when_only_correct_contradictions(self):
        # The gt=contradicted, predicted=contradicted case yields 0 — the
        # discriminating other half of the counter (a green contradiction must
        # never inflate the metric).
        from tests.evaluation.benchmark import (
            BenchmarkCase, RunResult, compute_metrics,
        )
        cases = [
            BenchmarkCase("c1", "s", "contradicted", "belief_revision", ""),
            BenchmarkCase("c2", "s", "contradicted", "belief_revision", ""),
        ]
        results = [
            RunResult("c1", "contradicted"),
            RunResult("c2", "contradicted"),
        ]
        m = compute_metrics(cases, results)
        assert m.false_contradicted == 0
        assert m.false_contradicted_rate == 0.0
        assert m.false_contradicted_gt_verified == 0
        assert m.false_contradicted_gt_abstain == 0

    def test_report_renders_false_contradicted_delta(self):
        # generate_report surfaces the symmetric false-contradicted delta line
        # alongside the false-verified delta (the observability the WS7 harness
        # gate will read).
        from tests.evaluation.benchmark import (
            BenchmarkCase, RunResult, generate_report,
        )
        cases = [
            BenchmarkCase("v1", "s", "verified", "multi_hop_distribution", ""),
            BenchmarkCase("c1", "s", "contradicted", "belief_revision", ""),
        ]
        aedos = [RunResult("v1", "verified"), RunResult("c1", "contradicted")]
        baseline = [RunResult("v1", "contradicted"), RunResult("c1", "verified")]
        report = generate_report(cases, aedos, baseline)
        assert "False-contradicted delta" in report
        assert "False-contradicted rate" in report

    def test_report_renders_v016_gates_and_tracked(self):
        # v0.16.1 WS7: generate_report renders the two HARD soundness gates
        # (false_verified, false_contradicted) plus the TRACKED (not gated)
        # accuracy / false-abstain / per-mode lines. The stale v0.15
        # "+15pp-vs-baseline" framing is gone.
        from tests.evaluation.benchmark import (
            BenchmarkCase, RunResult, generate_report,
        )
        cases = [
            BenchmarkCase("v1", "s", "verified", "multi_hop_distribution", ""),
            BenchmarkCase("c1", "s", "contradicted", "belief_revision", ""),
        ]
        # Sound run: verify the verified case, contradict the contradicted one.
        aedos = [RunResult("v1", "verified"), RunResult("c1", "contradicted")]
        baseline = [RunResult("v1", "no_grounding_found"), RunResult("c1", "verified")]
        report = generate_report(cases, aedos, baseline)
        assert "HARD soundness gates" in report
        assert "HARD GATE false_verified == 0: PASS" in report
        assert "HARD GATE false_contradicted == 0: PASS" in report
        assert "OVERALL SOUNDNESS: PASS" in report
        assert "Tracked (reported, NOT gated)" in report
        assert "Accuracy:" in report
        assert "False-abstain rate:" in report
        assert "Mode multi_hop_distribution:" in report
        # The stale v0.15 framing must be gone.
        assert "baseline + 15pp" not in report
        assert "Significant improvement" not in report

    def test_report_fails_hard_gates_on_unsound_run(self):
        # The discriminating other half: a false-verify OR a false-contradict
        # flips the corresponding HARD GATE to FAIL and OVERALL to FAIL.
        from tests.evaluation.benchmark import (
            BenchmarkCase, RunResult, generate_report, compute_metrics,
            soundness_gates,
        )
        cases = [
            BenchmarkCase("c1", "s", "contradicted", "belief_revision", ""),
            BenchmarkCase("v1", "s", "verified", "multi_hop_distribution", ""),
        ]
        # c1 (a false claim) wrongly VERIFIED → false_verified; v1 (a true claim)
        # wrongly CONTRADICTED → false_contradicted.
        aedos = [RunResult("c1", "verified"), RunResult("v1", "contradicted")]
        baseline = [RunResult("c1", "no_grounding_found"), RunResult("v1", "no_grounding_found")]
        report = generate_report(cases, aedos, baseline)
        assert "HARD GATE false_verified == 0: FAIL" in report
        assert "HARD GATE false_contradicted == 0: FAIL" in report
        assert "OVERALL SOUNDNESS: FAIL" in report
        gates = soundness_gates(compute_metrics(cases, aedos))
        assert gates["false_verified == 0"] is False
        assert gates["false_contradicted == 0"] is False

    def test_run_tracked_writes_jsonl_and_flags_soundness(self, tmp_path):
        # v0.16.1 WS7: run_tracked folds the per-instance watchdog + live FV/FC
        # counters + incremental JSONL into the standing harness. With a stub
        # runner it writes one flushed JSON line per case and flags a
        # false-verify / false-contradict in the per-case record.
        import json as _json
        from tests.evaluation.benchmark import (
            BenchmarkCase, RunResult, run_tracked,
        )

        class _StubRunner:
            def __init__(self, verdicts):
                self._v = verdicts
            def run_case(self, case):
                return RunResult(case.case_id, self._v[case.case_id])

        cases = [
            BenchmarkCase("v1", "s", "verified", "multi_hop_distribution", ""),
            BenchmarkCase("c1", "s", "contradicted", "belief_revision", ""),
            BenchmarkCase("a1", "s", "abstain", "principled_abstention", ""),
        ]
        # v1 correct; c1 wrongly verified (false-verify); a1 wrongly contradicted
        # (false-contradict).
        runner = _StubRunner({"v1": "verified", "c1": "verified", "a1": "contradicted"})
        jsonl = tmp_path / "tracked.jsonl"
        results = run_tracked(runner, cases, "aedos", jsonl, emit=lambda _m: None)
        assert [r.verdict for r in results] == ["verified", "verified", "contradicted"]
        lines = [_json.loads(l) for l in jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 3
        by_id = {l["case_id"]: l for l in lines}
        assert by_id["c1"]["false_verified"] is True
        assert by_id["a1"]["false_contradicted"] is True
        assert by_id["v1"]["false_verified"] is False
        assert by_id["v1"]["false_contradicted"] is False
