"""Step 1 full Medium-Bar run with per-instance tracking.

Wraps tests/evaluation/benchmark over all 122 cases against the seeded
aedos_phase10_5.db, adding what the plain runner lacks for careful operator
supervision:
  - per-case immediate (flushed) logging: case_id, verdict, latency, and a
    running verified/contradicted/abstain/error tally + a LIVE false-verified
    counter (the soundness metric — any nonzero is surfaced immediately);
  - an incremental JSONL written per case so progress is monitorable live and
    no data is lost if the run is interrupted;
  - a per-case watchdog that flags a case still running at 120/300/600s
    (does not kill — the KB/LLM clients have their own timeouts — just records
    a possible hang so it can be investigated).

Metrics + report reuse benchmark.compute_metrics / generate_report unchanged,
so the output is directly comparable to the prior runs.

Usage:  py -3 scripts/medium_bar_step1_run.py [--tag v016_step1]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from aedos.utils.env import load_dotenv_if_present  # noqa: E402

load_dotenv_if_present(_ROOT / ".env")
os.environ["RUN_LIVE_KB"] = "1"
os.environ["RUN_LIVE_TESTS"] = "1"

from tests.evaluation.benchmark import (  # noqa: E402
    AedosRunner, BaselineRunner, compute_metrics, generate_report, load_test_set,
)
from aedos.database import open_db  # noqa: E402
from aedos.pipeline import build_pipeline  # noqa: E402


def _p(msg: str) -> None:
    print(msg, flush=True)


def _watchdog(label: str, stop: threading.Event) -> None:
    t0 = time.time()
    for mark in (120, 300, 600):
        remaining = mark - (time.time() - t0)
        if remaining > 0 and stop.wait(timeout=remaining):
            return
        if not stop.is_set():
            _p(f"    [WATCHDOG] {label} still running at {int(time.time() - t0)}s — possible hang")


def _run_tracked(runner, cases, kind, jsonl_path):
    results = []
    tally = {"verified": 0, "contradicted": 0, "no_grounding_found": 0, "error": 0}
    false_verified = 0
    gt = {c.case_id: c.ground_truth for c in cases}
    with open(jsonl_path, "w", encoding="utf-8") as jf:
        for i, c in enumerate(cases, 1):
            stop = threading.Event()
            wd = threading.Thread(target=_watchdog, args=(f"{kind} {c.case_id}", stop), daemon=True)
            wd.start()
            t0 = time.time()
            r = runner.run_case(c)
            stop.set()
            results.append(r)
            tally[r.verdict] = tally.get(r.verdict, 0) + 1
            bucket = ("verified" if r.verdict == "verified"
                      else "contradicted" if r.verdict == "contradicted" else "abstain")
            fv = bucket == "verified" and gt[c.case_id] != "verified"
            if fv:
                false_verified += 1
            jf.write(json.dumps({
                "i": i, "case_id": c.case_id, "failure_mode": c.failure_mode,
                "ground_truth": gt[c.case_id], "verdict": r.verdict, "bucket": bucket,
                "false_verified": fv, "latency_s": round(r.latency_seconds, 1),
            }) + "\n")
            jf.flush()
            flag = "  <<< FALSE-VERIFIED" if fv else ("  <<ERROR>>" if r.verdict == "error" else "")
            _p(f"  {kind} [{i:3}/{len(cases)}] {c.case_id:20} gt={gt[c.case_id]:11} "
               f"-> {r.verdict:20} {r.latency_seconds:6.1f}s | FV={false_verified} "
               f"V={tally['verified']} C={tally['contradicted']} A={tally['no_grounding_found']} "
               f"E={tally['error']}{flag}")
    return results, false_verified


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag", default="v016_step1")
    ap.add_argument("--db-path", default="aedos_phase10_5.db")
    ap.add_argument("--out-dir", type=Path, default=_ROOT / "docs" / "phase_10_5" / "medium_bar")
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    os.environ["AEDOS_DB_PATH"] = args.db_path
    cases = load_test_set()
    _p(f"Loaded {len(cases)} cases | DB={args.db_path} | tag={args.tag}")
    t0 = time.time()
    pipeline = build_pipeline(open_db(args.db_path))
    aedos = AedosRunner(pipeline=(pipeline.extractor, pipeline.walker, pipeline.aggregator))
    baseline = BaselineRunner(llm_client=pipeline.llm_client)
    _p(f"Pipeline built in {time.time() - t0:.1f}s.\n")

    started = datetime.now(timezone.utc).isoformat()

    _p(f"--- BASELINE over {len(cases)} cases ---")
    baseline_results, _ = _run_tracked(
        baseline, cases, "base", args.out_dir / f"medium_bar_{args.tag}_baseline.jsonl")

    _p(f"\n--- AEDOS over {len(cases)} cases ---")
    aedos_results, aedos_fv = _run_tracked(
        aedos, cases, "aedos", args.out_dir / f"medium_bar_{args.tag}_aedos.jsonl")

    finished = datetime.now(timezone.utc).isoformat()
    duration = round(time.time() - t0, 1)

    aedos_metrics = compute_metrics(cases, aedos_results)
    baseline_metrics = compute_metrics(cases, baseline_results)

    ar = {r.case_id: r for r in aedos_results}
    br = {r.case_id: r for r in baseline_results}
    per_case = [{
        "case_id": c.case_id, "statement": c.statement, "ground_truth": c.ground_truth,
        "failure_mode": c.failure_mode, "notes": c.notes,
        "aedos_verdict": ar[c.case_id].verdict, "aedos_latency_s": round(ar[c.case_id].latency_seconds, 2),
        "baseline_verdict": br[c.case_id].verdict, "baseline_latency_s": round(br[c.case_id].latency_seconds, 2),
    } for c in cases]

    report_path = args.out_dir / f"medium_bar_{args.tag}.md"
    json_path = args.out_dir / f"medium_bar_{args.tag}.json"
    report = generate_report(cases, aedos_results, baseline_results, output_path=report_path)

    def _md(m):
        return {"total": m.total, "correct": m.correct, "accuracy": m.accuracy,
                "false_verified": m.false_verified, "false_verified_rate": m.false_verified_rate,
                "false_abstain": m.false_abstain, "false_abstain_rate": m.false_abstain_rate,
                "per_failure_mode": m.per_failure_mode}
    json_path.write_text(json.dumps({
        "tag": args.tag, "started_at": started, "finished_at": finished, "duration_s": duration,
        "case_count": len(cases), "db_path": args.db_path,
        "aedos_metrics": _md(aedos_metrics), "baseline_metrics": _md(baseline_metrics),
        "per_case": per_case,
    }, indent=2), encoding="utf-8")

    _p(f"\nDone in {duration}s. report={report_path} json={json_path}")
    _p("\n" + report.replace("≤", "<=").replace("≥", ">="))
    _p(f"\n*** AEDOS false-verified = {aedos_fv} ({aedos_metrics.false_verified_rate:.1%}) ***")
    _p("RUN COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
