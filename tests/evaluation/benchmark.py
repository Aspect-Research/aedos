"""
Standing medium-bar evaluation harness for Aedos (v0.16.x).

This is the ONE committed entry point for the live medium bar. It subsumes the
per-instance supervision that used to be duplicated across the one-off
`scripts/medium_bar_step1_*.py` wrappers: a per-instance watchdog, LIVE
false-verified AND false-contradicted counters, and incremental per-case JSONL
logging so a long live run is monitorable and loses no data if interrupted.

This module contains:
  - AedosRunner: runs the full Aedos pipeline against the test set
  - BaselineRunner: runs an LLM-only forward pass
  - run_tracked: per-instance supervision (watchdog + live FV/FC counters +
    incremental JSONL) — the standing replacement for the ad-hoc scripts
  - compute_metrics / generate_report: accuracy, false-verified, the WS1
    false-CONTRADICTED counter, false-abstain rate, per-failure-mode breakdown
  - _structural_test: checks the metrics/report machinery against mock results
  - _validate_harness: builds the production pipeline against mocks and runs one
    case through each runner — confirms the wiring without live API cost

v0.16.x acceptance gates (replacing the stale v0.15 "+15pp-vs-baseline"
framing). §3.2 soundness is paramount and the harness ENFORCES it:
  - HARD FAIL: Aedos false_verified == 0   (never verify a false claim)
  - HARD FAIL: Aedos false_contradicted == 0  (the WS1 metric — §3.2 forbids
    a false-contradict as much as a false-verify; abstention is the safe outcome)
  - TRACKED (reported, never gated): accuracy, false-abstain, per-failure-mode
    accuracy and baseline deltas. Over-abstention is a coverage symptom to
    measure and improve, not a release blocker.

`generate_report` prints PASS/FAIL on the two hard soundness gates and tracks
the rest; a nonzero exit code on the live entry point reflects a gate FAIL.

Usage:
    # harness wiring check (mocked, no API cost — part of `make test`):
    py -m tests.evaluation.benchmark --validate-harness
    # live evaluation (operator-supervised), with per-instance tracking:
    RUN_LIVE_TESTS=1 RUN_LIVE_KB=1 py -m tests.evaluation.benchmark --track \\
        --test-set tests/evaluation/medium_bar_test_set.jsonl \\
        --output docs/evaluation_results.md
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

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
    # v0.16.1 WS1: symmetric false-CONTRADICT counter — §3.2 forbids
    # false-contradict as much as false-verify, but the harness historically
    # only counted false-verifies. `false_contradicted` counts predictions of
    # `contradicted` whose ground truth is NOT `contradicted`, broken out by
    # the ground-truth bucket it stole from (verified vs abstain). Measurement
    # only — no verdict logic changes.
    false_contradicted: int = 0
    false_contradicted_rate: float = 0.0
    false_contradicted_gt_verified: int = 0
    false_contradicted_gt_abstain: int = 0

    def summary(self) -> str:
        lines = [
            f"Total cases: {self.total}",
            f"Accuracy: {self.accuracy:.1%} ({self.correct}/{self.total})",
            f"False-verified rate: {self.false_verified_rate:.1%} ({self.false_verified})",
            f"False-contradicted rate: {self.false_contradicted_rate:.1%} "
            f"({self.false_contradicted}; gt=verified {self.false_contradicted_gt_verified}, "
            f"gt=abstain {self.false_contradicted_gt_abstain})",
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
    false_contradicted = 0
    false_contradicted_gt_verified = 0
    false_contradicted_gt_abstain = 0
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
        # v0.16.1 WS1: symmetric false-contradict — a contradicted verdict on a
        # case whose ground truth is not contradicted, broken out by gt bucket.
        if predicted == "contradicted" and case.ground_truth != "contradicted":
            false_contradicted += 1
            if case.ground_truth == "verified":
                false_contradicted_gt_verified += 1
            else:
                false_contradicted_gt_abstain += 1

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
        false_contradicted=false_contradicted,
        false_contradicted_rate=false_contradicted / total if total > 0 else 0.0,
        false_contradicted_gt_verified=false_contradicted_gt_verified,
        false_contradicted_gt_abstain=false_contradicted_gt_abstain,
    )


# ---------------------------------------------------------------------------
# Aedos runner
# ---------------------------------------------------------------------------

class AedosRunner:
    """Run the full Aedos pipeline against the test set."""

    def __init__(self, pipeline=None):
        self._pipeline = pipeline

    def run_case(self, case: BenchmarkCase) -> RunResult:
        import time
        from datetime import datetime, timezone

        from aedos.layer1_extraction.extractor import ExtractionContext
        from aedos.layer4_sources.walker import VerificationContext

        if self._pipeline is None:
            return RunResult(case_id=case.case_id, verdict="no_grounding_found")
        extractor, walker, aggregator = self._pipeline
        start = time.monotonic()
        try:
            ctx = ExtractionContext(asserting_party="benchmark", context_type="document")
            claims = extractor.extract(case.statement, ctx)
            # v0.16 WS4 (4c): exclude extraction-layer-reasoned claims
            # (not_checkworthy + malformed) from the benchmark rollup — the
            # benchmark scores groundable claims. Mirrors the chat-wrapper
            # promotion gate (abstention_reason is None) now that the draft
            # VERIFY-filter is removed.
            claims = [c for c in claims if c.abstention_reason is None]
            if not claims:
                return RunResult(case_id=case.case_id, verdict="no_grounding_found",
                                 latency_seconds=time.monotonic() - start)
            # Phase H D47: thread the statement as source_text so the
            # Wikipedia normalizer's Stage 2 has context for disambiguation.
            vctx = VerificationContext(
                current_time=datetime.now(timezone.utc).isoformat(),
                asserting_party="benchmark",
                source_text=case.statement,
            )
            results = [walker.walk(c, vctx) for c in claims]
            aggregator.aggregate(claims, results)
            # v0.16.1 WS3 Step 1: the compound-statement conjunction is now a
            # real TRACED operation on the aggregator (an op="and" provenance
            # term over the per-claim sub-traces), not an inline boolean here.
            # `compose_statement_verdict` collapses each conjunct's chain-flagged
            # verdict (*_given_assertion) to its base verdict internally —
            # exactly what the old inline `_strip_chain_flag` did — and applies
            # the identical monotone semantics (contradicted-wins; verified iff
            # ALL verified; else no_grounding_found). Verdict-neutral.
            statement = aggregator.compose_statement_verdict(
                results, source_text=case.statement
            )
            verdict = statement.verdict
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
# Per-instance supervision (folded in from scripts/medium_bar_step1_run.py)
#
# The standing harness supervises each live case so a long operator-run is
# monitorable and loses no data on interruption:
#   - a per-instance watchdog flags a case still running at 120/300/600s
#     (it does NOT kill — the KB/LLM clients carry their own timeouts — it just
#     records a possible hang so it can be investigated);
#   - live false-VERIFIED AND false-CONTRADICTED counters surface a §3.2
#     soundness breach the instant it occurs, not only at the end-of-run report;
#   - an incremental per-case JSONL is flushed after every case.
# Metrics/report still go through compute_metrics / generate_report unchanged.
# ---------------------------------------------------------------------------

def _emit(msg: str) -> None:
    print(msg, flush=True)


def _watchdog(label: str, stop: threading.Event, marks=(120, 300, 600)) -> None:
    t0 = time.time()
    for mark in marks:
        remaining = mark - (time.time() - t0)
        if remaining > 0 and stop.wait(timeout=remaining):
            return
        if not stop.is_set():
            _emit(f"    [WATCHDOG] {label} still running at "
                  f"{int(time.time() - t0)}s — possible hang")


def _bucket(verdict: str) -> str:
    """The ground-truth bucket a raw verdict maps to (verified / contradicted /
    abstain), matching `_normalize_verdict`."""
    if verdict == "verified":
        return "verified"
    if verdict == "contradicted":
        return "contradicted"
    return "abstain"


def run_tracked(
    runner,
    cases: list[BenchmarkCase],
    kind: str,
    jsonl_path: Optional[Path] = None,
    *,
    emit: Callable[[str], None] = _emit,
) -> list[RunResult]:
    """Run `runner` over `cases` with per-instance supervision.

    Wraps each `runner.run_case` in a watchdog, maintains live verdict tallies
    plus LIVE false-verified AND false-contradicted counters (the §3.2 metrics —
    any nonzero is surfaced the moment it happens), and — when `jsonl_path` is
    given — writes one flushed JSON line per case so progress is monitorable and
    interruption-safe. Returns the same `list[RunResult]` as `runner.run_all`,
    so compute_metrics / generate_report consume it unchanged.
    """
    gt = {c.case_id: c.ground_truth for c in cases}
    results: list[RunResult] = []
    tally = {"verified": 0, "contradicted": 0, "no_grounding_found": 0, "error": 0}
    false_verified = 0
    false_contradicted = 0
    jf = open(jsonl_path, "w", encoding="utf-8") if jsonl_path is not None else None
    try:
        for i, c in enumerate(cases, 1):
            stop = threading.Event()
            wd = threading.Thread(
                target=_watchdog, args=(f"{kind} {c.case_id}", stop), daemon=True
            )
            wd.start()
            r = runner.run_case(c)
            stop.set()
            results.append(r)
            tally[r.verdict] = tally.get(r.verdict, 0) + 1
            bucket = _bucket(r.verdict)
            fv = bucket == "verified" and gt[c.case_id] != "verified"
            fc = bucket == "contradicted" and gt[c.case_id] != "contradicted"
            if fv:
                false_verified += 1
            if fc:
                false_contradicted += 1
            if jf is not None:
                jf.write(json.dumps({
                    "i": i, "case_id": c.case_id, "failure_mode": c.failure_mode,
                    "ground_truth": gt[c.case_id], "verdict": r.verdict,
                    "bucket": bucket, "false_verified": fv, "false_contradicted": fc,
                    "latency_s": round(r.latency_seconds, 1),
                }) + "\n")
                jf.flush()
            flag = ""
            if fv:
                flag = "  <<< FALSE-VERIFIED (soundness!)"
            elif fc:
                flag = "  <<< FALSE-CONTRADICTED (soundness!)"
            elif r.verdict == "error":
                flag = "  <<ERROR>>"
            emit(
                f"  {kind} [{i:3}/{len(cases)}] {c.case_id:20} "
                f"gt={gt[c.case_id]:11} -> {r.verdict:20} {r.latency_seconds:6.1f}s | "
                f"FV={false_verified} FC={false_contradicted} "
                f"V={tally['verified']} C={tally['contradicted']} "
                f"A={tally['no_grounding_found']} E={tally['error']}{flag}"
            )
    finally:
        if jf is not None:
            jf.close()
    return results


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def soundness_gates(metrics: EvaluationMetrics) -> dict[str, bool]:
    """v0.16.x HARD soundness gates. §3.2 forbids a false-verify AND a
    false-contradict equally; abstention is the safe outcome. Both must be
    ZERO. Returns {gate_name: passed}. The live entry point exits nonzero when
    any gate fails."""
    return {
        "false_verified == 0": metrics.false_verified == 0,
        "false_contradicted == 0": metrics.false_contradicted == 0,
    }


def generate_report(
    cases: list[BenchmarkCase],
    aedos_results: list[RunResult],
    baseline_results: list[RunResult],
    output_path: Optional[Path] = None,
) -> str:
    aedos_metrics = compute_metrics(cases, aedos_results)
    baseline_metrics = compute_metrics(cases, baseline_results)

    gates = soundness_gates(aedos_metrics)

    lines = [
        "# Aedos Medium-Bar Evaluation Results",
        "",
        "## Aedos",
        aedos_metrics.summary(),
        "",
        "## LLM-Only Baseline",
        baseline_metrics.summary(),
        "",
        "## Comparison",
        f"Accuracy delta: {aedos_metrics.accuracy - baseline_metrics.accuracy:+.1%}",
        f"False-verified delta: {aedos_metrics.false_verified_rate - baseline_metrics.false_verified_rate:+.1%}",
        f"False-contradicted delta: "
        f"{aedos_metrics.false_contradicted_rate - baseline_metrics.false_contradicted_rate:+.1%}",
        "",
        "## Acceptance — HARD soundness gates (v0.16.x, §3.2)",
        "These two MUST pass; abstention is the safe outcome, accuracy is tracked not gated.",
        f"HARD GATE false_verified == 0: "
        f"{'PASS' if gates['false_verified == 0'] else 'FAIL'} "
        f"(false_verified={aedos_metrics.false_verified}, "
        f"rate {aedos_metrics.false_verified_rate:.1%})",
        f"HARD GATE false_contradicted == 0: "
        f"{'PASS' if gates['false_contradicted == 0'] else 'FAIL'} "
        f"(false_contradicted={aedos_metrics.false_contradicted}: "
        f"gt=verified {aedos_metrics.false_contradicted_gt_verified}, "
        f"gt=abstain {aedos_metrics.false_contradicted_gt_abstain}; "
        f"rate {aedos_metrics.false_contradicted_rate:.1%})",
        f"OVERALL SOUNDNESS: {'PASS' if all(gates.values()) else 'FAIL'}",
        "",
        "## Tracked (reported, NOT gated)",
        f"Accuracy: {aedos_metrics.accuracy:.1%} "
        f"(baseline {baseline_metrics.accuracy:.1%}, "
        f"delta {aedos_metrics.accuracy - baseline_metrics.accuracy:+.1%})",
        f"False-abstain rate: {aedos_metrics.false_abstain_rate:.1%} "
        f"({aedos_metrics.false_abstain})",
    ]

    # Per-failure-mode accuracy + baseline delta — TRACKED, never gated.
    for mode in sorted(aedos_metrics.per_failure_mode):
        a_acc = aedos_metrics.per_failure_mode[mode]["accuracy"]
        b_acc = baseline_metrics.per_failure_mode.get(mode, {}).get("accuracy", 0.0)
        lines.append(
            f"Mode {mode}: Aedos {a_acc:.1%} vs baseline {b_acc:.1%} "
            f"(delta {a_acc - b_acc:+.1%})"
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

    # Generate report — the v0.16.x hard soundness gates must both PASS on the
    # perfect mock (0 false-verified, 0 false-contradicted).
    report = generate_report(cases, mock_results, mock_results)
    assert "# Aedos Medium-Bar Evaluation Results" in report
    assert "HARD GATE false_verified == 0: PASS" in report
    assert "HARD GATE false_contradicted == 0: PASS" in report
    assert "OVERALL SOUNDNESS: PASS" in report

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
    assert "# Aedos Medium-Bar Evaluation Results" in report
    assert "HARD soundness gates" in report
    assert "HARD GATE false_verified == 0" in report
    assert "HARD GATE false_contradicted == 0" in report
    if output_path is not None:
        assert output_path.exists() and output_path.read_text(encoding="utf-8").strip()

    # Exercise the per-instance supervision wrapper on the mocked runners too —
    # confirms run_tracked's wiring (watchdog thread + JSONL + live counters)
    # without live API cost. The JSONL must contain one flushed line per case.
    if output_path is not None:
        jsonl = output_path.with_suffix(".jsonl")
        tracked = run_tracked(aedos, cases[:1], "aedos", jsonl, emit=lambda _m: None)
        assert len(tracked) == 1 and tracked[0].verdict != "error"
        assert jsonl.exists() and jsonl.read_text(encoding="utf-8").strip()
    return True


# ---------------------------------------------------------------------------
# Live evaluation entrypoint (Phase 10.5)
# ---------------------------------------------------------------------------

def _metrics_to_dict(m: EvaluationMetrics) -> dict:
    return {
        "total": m.total, "correct": m.correct, "accuracy": m.accuracy,
        "false_verified": m.false_verified,
        "false_verified_rate": m.false_verified_rate,
        "false_contradicted": m.false_contradicted,
        "false_contradicted_rate": m.false_contradicted_rate,
        "false_contradicted_gt_verified": m.false_contradicted_gt_verified,
        "false_contradicted_gt_abstain": m.false_contradicted_gt_abstain,
        "false_abstain": m.false_abstain,
        "false_abstain_rate": m.false_abstain_rate,
        "per_failure_mode": m.per_failure_mode,
    }


def _run_one(runner, cases, kind, jsonl_path, tracked: bool):
    """Run a single runner over `cases`, optionally with per-instance tracking."""
    if tracked:
        return run_tracked(runner, cases, kind, jsonl_path)
    return runner.run_all(cases)


def _run_live(args) -> int:
    """Run the medium-bar evaluation live against the production pipeline, with
    per-instance supervision (watchdog + live FV/FC counters + incremental
    JSONL) when --track is set. Returns a process exit code: nonzero iff a HARD
    soundness gate (false_verified / false_contradicted) FAILS."""
    from aedos.database import open_db
    from aedos.pipeline import build_pipeline

    db_path = os.environ.get("AEDOS_DB_PATH", "aedos_phase10_5.db")
    cases = load_test_set(args.test_set)
    _emit(f"Loaded {len(cases)} cases from {args.test_set}")
    _emit(f"Database: {db_path} (load the seed pack first — runbook Step 2)")
    _emit(f"Per-instance tracking: {'ON' if args.track else 'off'} | tag={args.tag}")

    started = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    pipeline = build_pipeline(open_db(db_path))
    aedos = AedosRunner(pipeline=(pipeline.extractor, pipeline.walker, pipeline.aggregator))
    baseline = BaselineRunner(llm_client=pipeline.llm_client)
    _emit(f"Pipeline built in {time.time() - t0:.1f}s.")

    # Incremental JSONL siblings of --output (one per runner), so a long live
    # run is monitorable and interruption-safe.
    out = args.output
    base_jsonl = aedos_jsonl = None
    if out is not None and args.track:
        base_jsonl = out.with_name(f"{out.stem}_baseline.jsonl")
        aedos_jsonl = out.with_name(f"{out.stem}_aedos.jsonl")

    if args.baseline_only:
        _emit(f"Running baseline only over {len(cases)} cases ...")
        _emit(compute_metrics(cases, _run_one(baseline, cases, "base", base_jsonl, args.track)).summary())
        return 0
    if args.aedos_only:
        _emit(f"Running Aedos only over {len(cases)} cases ...")
        metrics = compute_metrics(cases, _run_one(aedos, cases, "aedos", aedos_jsonl, args.track))
        _emit(metrics.summary())
        return 0 if all(soundness_gates(metrics).values()) else 1

    _emit(f"--- BASELINE over {len(cases)} cases ---")
    baseline_results = _run_one(baseline, cases, "base", base_jsonl, args.track)
    _emit(f"--- AEDOS over {len(cases)} cases ---")
    aedos_results = _run_one(aedos, cases, "aedos", aedos_jsonl, args.track)

    finished = datetime.now(timezone.utc).isoformat()
    aedos_metrics = compute_metrics(cases, aedos_results)
    baseline_metrics = compute_metrics(cases, baseline_results)
    report = generate_report(cases, aedos_results, baseline_results, output_path=out)

    # A per-case JSON sibling for the aggregator (medium_bar_aggregate.py).
    if out is not None:
        ar = {r.case_id: r for r in aedos_results}
        br = {r.case_id: r for r in baseline_results}
        per_case = [{
            "case_id": c.case_id, "statement": c.statement,
            "ground_truth": c.ground_truth, "failure_mode": c.failure_mode,
            "notes": c.notes,
            "aedos_verdict": ar[c.case_id].verdict,
            "aedos_latency_s": round(ar[c.case_id].latency_seconds, 2),
            "baseline_verdict": br[c.case_id].verdict,
            "baseline_latency_s": round(br[c.case_id].latency_seconds, 2),
        } for c in cases]
        out.with_suffix(".json").write_text(json.dumps({
            "tag": args.tag, "started_at": started, "finished_at": finished,
            "duration_s": round(time.time() - t0, 1), "case_count": len(cases),
            "db_path": db_path,
            "aedos_metrics": _metrics_to_dict(aedos_metrics),
            "baseline_metrics": _metrics_to_dict(baseline_metrics),
            "per_case": per_case,
        }, indent=2), encoding="utf-8")

    _emit("")
    _emit(report.replace("≤", "<=").replace("≥", ">="))
    if out is not None:
        _emit(f"\nResults written to {out}")
    gates = soundness_gates(aedos_metrics)
    _emit(f"\n*** SOUNDNESS GATES: {'PASS' if all(gates.values()) else 'FAIL'} "
          f"(false_verified={aedos_metrics.false_verified}, "
          f"false_contradicted={aedos_metrics.false_contradicted}) ***")
    _emit("RUN COMPLETE")
    return 0 if all(gates.values()) else 1


if __name__ == "__main__":
    import argparse
    # F-013: load `.env` so the live-mode env checks below (and any
    # downstream Config.from_env) pick up keys from the file without
    # requiring shell-sourced env vars. Idempotent (F3 §6); no-op when
    # no `.env` is present.
    from aedos.utils.env import load_dotenv_if_present
    load_dotenv_if_present()
    parser = argparse.ArgumentParser(description="Aedos standing medium-bar evaluation harness")
    parser.add_argument("--test-set", type=Path, default=_TEST_SET_PATH)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--tag", default="medium_bar",
                        help="Run tag, used to name the incremental JSONL siblings.")
    parser.add_argument("--track", action="store_true",
                        help="Per-instance supervision: watchdog + live "
                             "false-verified/false-contradicted counters + "
                             "incremental per-case JSONL (interruption-safe).")
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
