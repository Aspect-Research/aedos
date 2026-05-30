"""Phase H Cluster 2 step 6 — cross-run aggregation.

Takes 2+ run JSON files from `cluster_2_validation.py` and produces:

  - Per-case verdict across runs (consistency check)
  - Per-rule PASS/MISS counts
  - Cases that flipped verdict across runs (KB nondeterminism or bug)
  - Aggregate accuracy + variance

Output: markdown table snippets ready to paste into
docs/phase_H/cluster_2_validation.md, plus a summary JSON.

Usage:
  py scripts/cluster_2_validation_aggregate.py docs/phase_H/cluster_2_validation_run_*.json
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import Counter
from pathlib import Path


def load_runs(paths: list[str]) -> list[dict]:
    """Each path may contain multiple runs (one JSON file from --runs N).
    Flatten into a list of per-run dicts."""
    all_runs = []
    for p in paths:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        all_runs.extend(data["runs"])
    return all_runs


def per_case_across_runs(runs: list[dict]) -> dict[str, list[dict]]:
    """Group per-case results across runs. Keys are case_ids; values
    are lists of per-case dicts (one per run, in run order)."""
    by_id: dict[str, list[dict]] = {}
    for run in runs:
        for c in run["per_case"]:
            by_id.setdefault(c["case_id"], []).append(c)
    return by_id


def per_case_consistency(by_id: dict[str, list[dict]]) -> dict[str, dict]:
    """Per-case consistency summary across runs."""
    out: dict[str, dict] = {}
    for cid, cases in by_id.items():
        verdicts = [c["actual_verdict"] for c in cases]
        passed_per_run = [c["runner_passed"] for c in cases]
        unique_verdicts = set(verdicts)
        out[cid] = {
            "verdicts": verdicts,
            "passed_per_run": passed_per_run,
            "consistent_verdict": len(unique_verdicts) == 1,
            "consistent_pass": len(set(passed_per_run)) == 1,
            "rule": cases[0]["rule"],
            "category": cases[0]["category"],
            "expected_verdict": cases[0]["expected_verdict"],
            "judgment": cases[0]["judgment"],
        }
    return out


def aggregate(runs: list[dict]) -> dict:
    by_id = per_case_across_runs(runs)
    consistency = per_case_consistency(by_id)

    n_runs = len(runs)
    n_cases = len(by_id)
    accuracies = [r["accuracy_pct"] for r in runs]
    avg_acc = sum(accuracies) / n_runs

    inconsistent = [cid for cid, c in consistency.items() if not c["consistent_verdict"]]
    pass_inconsistent = [cid for cid, c in consistency.items() if not c["consistent_pass"]]

    # Per-rule PASS/MISS counts across runs (averaged).
    rule_pass_per_run: list[Counter] = []
    for run in runs:
        bucket: Counter = Counter()
        for c in run["per_case"]:
            status = "PASS" if c["runner_passed"] else "MISS"
            bucket[(c["rule"], status)] += 1
        rule_pass_per_run.append(bucket)

    # Serialize tuple keys as 'rule:status' strings for JSON-friendliness.
    rule_pass_serializable = [
        {f"{rule}:{status}": n for (rule, status), n in c.items()}
        for c in rule_pass_per_run
    ]
    return {
        "n_runs": n_runs,
        "n_cases": n_cases,
        "per_run_accuracy": accuracies,
        "avg_accuracy_pct": avg_acc,
        "inconsistent_verdict_count": len(inconsistent),
        "inconsistent_verdict_cases": inconsistent,
        "inconsistent_pass_count": len(pass_inconsistent),
        "inconsistent_pass_cases": pass_inconsistent,
        "per_case_consistency": consistency,
        "rule_pass_per_run": rule_pass_serializable,
    }


def _print_variance(agg: dict, runs: list[dict]) -> None:
    """Pulled out for testing; the in-process rule bucket retains tuple keys."""
    pass


def print_markdown_tables(agg: dict) -> None:
    n_runs = agg["n_runs"]
    print("## Per-run accuracy")
    print()
    print("| Run | Accuracy | vs baseline (22/50, 44%) |")
    print("|---|---|---|")
    for i, acc in enumerate(agg["per_run_accuracy"]):
        passed = int(round(acc * 50 / 100))
        delta = acc - 44.0
        print(f"| {i+1} | {passed}/50 ({acc:.1f}%) | {delta:+.1f} pp |")
    print(f"| **avg** | — | **{agg['avg_accuracy_pct'] - 44.0:+.1f} pp** |")
    print()

    print("## Per-rule pass/miss across runs")
    print()
    # Keys are 'rule:status' strings post-serialization fix.
    rules = sorted({
        key.split(":")[0]
        for bucket in agg["rule_pass_per_run"]
        for key in bucket
    })
    header = "| rule | " + " | ".join(f"run {i+1} P/M" for i in range(n_runs)) + " |"
    sep = "|---|" + "---|" * n_runs
    print(header)
    print(sep)
    for rule in rules:
        cells = []
        for bucket in agg["rule_pass_per_run"]:
            p = bucket.get(f"{rule}:PASS", 0)
            m = bucket.get(f"{rule}:MISS", 0)
            cells.append(f"{p}/{p+m}")
        print(f"| {rule} | " + " | ".join(cells) + " |")
    print()

    print("## Per-case verdict consistency")
    print()
    print("| case_id | rule | expected | " +
          " | ".join(f"r{i+1}" for i in range(n_runs)) +
          " | consistent | passed |")
    sep_h = "|---|---|---|" + "---|" * n_runs + "---|---|"
    print(sep_h)
    for cid in sorted(agg["per_case_consistency"]):
        c = agg["per_case_consistency"][cid]
        verdicts = " | ".join(str(v) for v in c["verdicts"])
        passed = "yes" if all(c["passed_per_run"]) else (
            "no" if not any(c["passed_per_run"]) else "MIXED"
        )
        consist = "yes" if c["consistent_verdict"] else "**NO**"
        print(f"| {cid} | {c['rule']} | {c['expected_verdict']} | "
              f"{verdicts} | {consist} | {passed} |")
    print()

    if agg["inconsistent_verdict_cases"]:
        print(f"## Cases with cross-run verdict variance ({agg['inconsistent_verdict_count']})")
        print()
        print("These cases produced different verdicts across runs — either "
              "legitimate KB/extractor nondeterminism or a bug worth investigating.")
        print()
        for cid in agg["inconsistent_verdict_cases"]:
            c = agg["per_case_consistency"][cid]
            print(f"- **{cid}** ({c['rule']}): verdicts = {c['verdicts']}")
        print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", help="run JSON paths (globs ok)")
    ap.add_argument("--out", help="write aggregate JSON to this path")
    args = ap.parse_args()

    # Expand globs.
    expanded: list[str] = []
    for p in args.paths:
        matched = glob.glob(p)
        expanded.extend(matched or [p])

    runs = load_runs(expanded)
    if not runs:
        print(f"No runs loaded from {expanded}", file=sys.stderr)
        return 1

    agg = aggregate(runs)
    print_markdown_tables(agg)

    if args.out:
        Path(args.out).write_text(json.dumps(agg, indent=2), encoding="utf-8")
        print(f"\nWrote {args.out}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
