"""Phase 10.5 Step 6 medium-bar evaluation runner.

Wraps `tests/evaluation/benchmark.py` to (a) emit a per-case JSON alongside
the markdown report so the soundness analysis can extract false-positive
correction counts and other derived metrics, and (b) avoid the Windows
cp1252 console encode issue on the `<=` character in the report's print
(the file write happens before the print, but the non-zero exit code masks
success for orchestration).

Usage:
    py scripts/medium_bar_run.py --run 1
    py scripts/medium_bar_run.py --run 2
    py scripts/medium_bar_run.py --run 3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from aedos.utils.env import load_dotenv_if_present  # noqa: E402

load_dotenv_if_present(_ROOT / ".env")
os.environ.setdefault("RUN_LIVE_KB", "1")
os.environ.setdefault("RUN_LIVE_TESTS", "1")

from tests.evaluation.benchmark import (  # noqa: E402
    AedosRunner, BaselineRunner, compute_metrics, generate_report,
    load_test_set, _TEST_SET_PATH,
)
from aedos.database import open_db  # noqa: E402
from aedos.pipeline import build_pipeline  # noqa: E402


def _utf8_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def run_once(run_id: int, test_set_path: Path, out_dir: Path, db_path: str) -> dict:
    cases = load_test_set(test_set_path)
    # Explicit override — the chat-deployment .env defaults AEDOS_DB_PATH to
    # `aedos.db`, but the medium-bar benchmark runs against the seeded
    # `aedos_phase10_5.db` per runbook Steps 2-3.
    os.environ["AEDOS_DB_PATH"] = db_path
    print(f"Loaded {len(cases)} cases from {test_set_path}")
    print(f"Database: {db_path}")

    pipeline = build_pipeline(open_db(db_path))
    aedos = AedosRunner(pipeline=(pipeline.extractor, pipeline.walker, pipeline.aggregator))
    baseline = BaselineRunner(llm_client=pipeline.llm_client)

    started = datetime.now(timezone.utc).isoformat()
    t0 = time.time()

    print(f"\nRun {run_id} — baseline over {len(cases)} cases ...")
    baseline_results = []
    for i, c in enumerate(cases, 1):
        r = baseline.run_case(c)
        baseline_results.append(r)
        if i % 10 == 0 or i == len(cases):
            print(f"  baseline [{i:3}/{len(cases)}] last={c.case_id} verdict={r.verdict}")

    print(f"\nRun {run_id} — Aedos pipeline over {len(cases)} cases ...")
    aedos_results = []
    for i, c in enumerate(cases, 1):
        r = aedos.run_case(c)
        aedos_results.append(r)
        if i % 10 == 0 or i == len(cases):
            print(f"  aedos    [{i:3}/{len(cases)}] last={c.case_id} verdict={r.verdict}")

    finished = datetime.now(timezone.utc).isoformat()
    duration = round(time.time() - t0, 1)

    # Per-case structured record
    per_case = []
    ar_map = {r.case_id: r for r in aedos_results}
    br_map = {r.case_id: r for r in baseline_results}
    for c in cases:
        a = ar_map.get(c.case_id)
        b = br_map.get(c.case_id)
        per_case.append({
            "case_id": c.case_id,
            "statement": c.statement,
            "ground_truth": c.ground_truth,
            "failure_mode": c.failure_mode,
            "notes": c.notes,
            "aedos_verdict": a.verdict if a else None,
            "aedos_latency_s": a.latency_seconds if a else None,
            "baseline_verdict": b.verdict if b else None,
            "baseline_latency_s": b.latency_seconds if b else None,
        })

    aedos_metrics = compute_metrics(cases, aedos_results)
    baseline_metrics = compute_metrics(cases, baseline_results)

    # Report (markdown) + JSON (per-case + aggregates)
    report_path = out_dir / f"medium_bar_run_{run_id:02d}.md"
    json_path = out_dir / f"medium_bar_run_{run_id:02d}.json"
    report = generate_report(cases, aedos_results, baseline_results, output_path=report_path)

    json_path.write_text(json.dumps({
        "run_id": run_id,
        "started_at": started,
        "finished_at": finished,
        "duration_s": duration,
        "case_count": len(cases),
        "db_path": db_path,
        "aedos_metrics": _metrics_to_dict(aedos_metrics),
        "baseline_metrics": _metrics_to_dict(baseline_metrics),
        "per_case": per_case,
    }, indent=2), encoding="utf-8")

    print(f"\nRun {run_id} done in {duration}s.")
    print(f"  report: {report_path}")
    print(f"  json:   {json_path}")
    print()
    print(report.replace("≤", "<=").replace("≥", ">="))
    return {"report_path": str(report_path), "json_path": str(json_path)}


def _metrics_to_dict(m) -> dict:
    return {
        "total": m.total,
        "correct": m.correct,
        "accuracy": m.accuracy,
        "false_verified": m.false_verified,
        "false_verified_rate": m.false_verified_rate,
        "false_abstain": m.false_abstain,
        "false_abstain_rate": m.false_abstain_rate,
        "per_failure_mode": m.per_failure_mode,
    }


def main() -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", type=int, required=True, help="run number (1-3)")
    ap.add_argument("--test-set", type=Path, default=_TEST_SET_PATH)
    ap.add_argument("--out-dir", type=Path,
                    default=_ROOT / "docs" / "phase_10_5" / "medium_bar")
    ap.add_argument("--db-path", type=str, default="aedos_phase10_5.db",
                    help="benchmark substrate (seeded via runbook Steps 2-3)")
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    run_once(args.run, args.test_set, args.out_dir, args.db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
