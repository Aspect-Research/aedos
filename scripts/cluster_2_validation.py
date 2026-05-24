"""Phase H Cluster 2 step 6 — validation harness.

Runs the entire derivation_corpus (50 cases) under the live
RUN_CALIBRATION + RUN_LIVE_KB + RUN_LIVE_TESTS path and captures per
case:

  - case_id, category, rule (from cluster_2_corpus_align.py)
  - expected_verdict (from corpus)
  - actual_verdict (walker output)
  - matched (verdict equality)
  - chain_includes_assertion (trace flag)
  - premise_status edge metadata (which Tier U row status fed the
    walker's first matching premise — sentinel if no Tier U edge)
  - tier_u_status_upgraded event count (did Q-Lookup α fire?)
  - cross_source_contradiction event count
  - walker_skipped_due_to_pre_verdict event count
  - any error

Output: docs/phase_H/cluster_2_validation_run_<timestamp>.json. Run
2-3 times per D49; the validation doc aggregates across runs and
flags cross-run inconsistencies.

Cost: ~$1-3 per run per D49 budget; total $2-9 for 2-3 runs.

Usage:
  py scripts/cluster_2_validation.py            # one run
  py scripts/cluster_2_validation.py --runs 3   # three runs
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aedos.utils.env import load_dotenv_if_present  # noqa: E402

load_dotenv_if_present(Path(__file__).resolve().parent.parent / ".env")
os.environ.setdefault("RUN_LIVE_KB", "1")
os.environ.setdefault("RUN_LIVE_TESTS", "1")
os.environ.setdefault("RUN_CALIBRATION", "1")

from aedos.audit.log import query_events  # noqa: E402
from tests.calibration.test_corpus_runner import _Harness, _load_corpus  # noqa: E402

# Reuse the corpus-align categorization for per-case rule labels.
from scripts.cluster_2_corpus_align import categorize  # noqa: E402


@contextlib.contextmanager
def _walk_capture():
    """Patch Walker.walk to capture per-call verdict + trace metadata."""
    from aedos.layer4_sources import walker as walker_mod
    captured: list[dict] = []
    orig = walker_mod.Walker.walk

    def patched(self, *a, **k):
        result = orig(self, *a, **k)
        captured.append({
            "verdict": result.verdict,
            "abstention_reason": result.abstention_reason,
            "chain_includes_assertion": result.trace.chain_includes_assertion,
            "edges": [
                {
                    "edge_type": e.edge_type,
                    "source": e.metadata.get("source"),
                    "premise_status": e.metadata.get("premise_status"),
                    "verdict": e.metadata.get("verdict"),
                    "belief_revision": e.metadata.get("belief_revision"),
                }
                for e in result.trace.edges
            ],
        })
        return result

    walker_mod.Walker.walk = patched
    try:
        yield captured
    finally:
        walker_mod.Walker.walk = orig


def _run_case(harness, case: dict, runner) -> dict:
    """Run one case live, capturing verdict + audit signals."""
    case_id = case["id"]
    rule_info = categorize(case)
    expected = case["expected_output"].get("verdict") or "<non-standard>"

    # Counts of cluster-2 audit events BEFORE this case fires, so the
    # diff is the case's own contribution.
    def _count(event_type: str) -> int:
        return len(query_events(harness.db, event_type=event_type, limit=100000))

    before = {
        "upgrade": _count("tier_u_status_upgraded"),
        "cross_source": _count("cross_source_contradiction"),
        "walker_skipped": _count("walker_skipped_due_to_pre_verdict"),
    }

    with _walk_capture() as walks:
        try:
            passed = bool(runner(harness, case))
            err = None
        except Exception:
            passed = False
            err = traceback.format_exc(limit=4)

    after = {
        "upgrade": _count("tier_u_status_upgraded"),
        "cross_source": _count("cross_source_contradiction"),
        "walker_skipped": _count("walker_skipped_due_to_pre_verdict"),
    }
    deltas = {k: after[k] - before[k] for k in before}

    # The runner walks claims[0]; if walks is non-empty, use the first.
    walk = walks[0] if walks else None

    # Identify the first tier_u edge's premise_status — the high-signal
    # field for cluster 2 validation.
    first_tier_u_status = None
    if walk:
        for e in walk["edges"]:
            if e.get("source") == "tier_u" and e.get("premise_status"):
                first_tier_u_status = e["premise_status"]
                break

    return {
        "case_id": case_id,
        "category": case.get("category"),
        "rule": rule_info["rule"],
        "judgment": rule_info["judgment"],
        "expected_verdict": expected,
        "actual_verdict": walk["verdict"] if walk else None,
        "runner_passed": passed,
        "chain_includes_assertion": walk["chain_includes_assertion"] if walk else None,
        "first_tier_u_premise_status": first_tier_u_status,
        "audit_event_deltas": deltas,
        "error": err,
        "walk_edges_count": len(walk["edges"]) if walk else 0,
    }


def run_once(run_index: int) -> dict:
    """Run the entire derivation_corpus once. Returns a structured
    result dict suitable for JSON serialization."""
    cases = _load_corpus("derivation_corpus")
    from tests.calibration.test_corpus_runner import _RUNNERS
    runner = _RUNNERS["derivation_corpus"]
    harness = _Harness()

    started = datetime.now(timezone.utc).isoformat()
    print(f"\n{'='*72}")
    print(f"Cluster 2 validation — run {run_index + 1} — started {started}")
    print(f"{'='*72}\n")

    per_case: list[dict] = []
    for i, case in enumerate(cases, 1):
        result = _run_case(harness, case, runner)
        match_glyph = "OK " if (
            result["actual_verdict"] == result["expected_verdict"]
            or result["runner_passed"]
        ) else "MISS"
        print(
            f"  [{i:2}/50] {result['case_id']:35} {result['rule']:14} "
            f"{match_glyph}  exp={result['expected_verdict']:35} "
            f"got={str(result['actual_verdict']):35}"
        )
        per_case.append(result)

    finished = datetime.now(timezone.utc).isoformat()
    passed = sum(1 for r in per_case if r["runner_passed"])
    print(f"\n  Accuracy: {passed}/{len(per_case)} runner-passed "
          f"({100.0 * passed / len(per_case):.1f}%)")
    print(f"  Run finished at {finished}")

    return {
        "run_index": run_index,
        "started_at": started,
        "finished_at": finished,
        "case_count": len(per_case),
        "runner_passed": passed,
        "accuracy_pct": 100.0 * passed / len(per_case),
        "per_case": per_case,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=1,
                    help="number of validation runs (default 1)")
    args = ap.parse_args()

    out_dir = Path(__file__).resolve().parent.parent / "docs" / "phase_H"
    out_dir.mkdir(parents=True, exist_ok=True)

    all_runs = []
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for i in range(args.runs):
        run_result = run_once(i)
        all_runs.append(run_result)

    out = {
        "timestamp": timestamp,
        "runs_requested": args.runs,
        "runs_completed": len(all_runs),
        "runs": all_runs,
    }
    out_path = out_dir / f"cluster_2_validation_run_{timestamp}.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
