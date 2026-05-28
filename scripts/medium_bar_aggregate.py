"""Phase 10.5 Step 6 medium-bar aggregation.

Loads the per-run JSON files produced by `scripts/medium_bar_run.py` and
computes the across-runs aggregates needed for `docs/phase_10_5/medium_bar_results.md`:

  - Per-run accuracy, false-verified rate, false-abstain rate (Aedos + baseline)
  - Median + range across runs (D49 variance discipline)
  - Per-failure-mode medians and ranges
  - Derived soundness metrics:
      * False-positive correction count
        (predicted=contradicted ∧ ground_truth=verified) — the session-prompt
        "most harmful outcome", not surfaced separately by the harness.
      * False-positive abstention count
        (predicted=abstain ∧ ground_truth=verified) — IS the `false_abstain`
        metric; surfaced here for clarity alongside false-positive correction.
      * False-negative miss count
        (predicted=verified ∧ ground_truth=contradicted) — subset of
        `false_verified` that's symmetric to the LLM-only baseline's failure.
  - Per-case agreement breakdown:
      * Aedos correct, baseline wrong (Aedos-wins)
      * Aedos wrong, baseline correct (Aedos-hurts)
      * Both correct / both wrong
  - Acceptance-threshold result per run + median

Usage:
    py scripts/medium_bar_aggregate.py docs/phase_10_5/medium_bar/medium_bar_run_*.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _normalize_verdict(verdict: str) -> str:
    if verdict == "verified":
        return "verified"
    if verdict == "contradicted":
        return "contradicted"
    return "abstain"


def _derived_metrics(per_case: list[dict]) -> dict:
    """Compute the session-prompt derived metrics from a single run's
    per-case records."""
    aedos_fp_correction = 0  # predicted=contradicted, gt=verified — most harmful
    aedos_fp_abstention = 0  # predicted=abstain, gt=verified — over-caution
    aedos_fn_miss = 0        # predicted=verified, gt=contradicted — same as baseline
    aedos_fv_other = 0       # predicted=verified, gt=abstain — false-verified non-miss

    baseline_fp_correction = 0
    baseline_fp_abstention = 0
    baseline_fn_miss = 0
    baseline_fv_other = 0

    aedos_wins = 0
    aedos_hurts = 0
    both_correct = 0
    both_wrong = 0

    for c in per_case:
        gt = c["ground_truth"]
        a = _normalize_verdict(c.get("aedos_verdict") or "")
        b = _normalize_verdict(c.get("baseline_verdict") or "")

        a_correct = a == gt
        b_correct = b == gt
        if a_correct and not b_correct:
            aedos_wins += 1
        elif (not a_correct) and b_correct:
            aedos_hurts += 1
        elif a_correct and b_correct:
            both_correct += 1
        else:
            both_wrong += 1

        if a == "contradicted" and gt == "verified":
            aedos_fp_correction += 1
        if a == "abstain" and gt == "verified":
            aedos_fp_abstention += 1
        if a == "verified" and gt == "contradicted":
            aedos_fn_miss += 1
        if a == "verified" and gt == "abstain":
            aedos_fv_other += 1

        if b == "contradicted" and gt == "verified":
            baseline_fp_correction += 1
        if b == "abstain" and gt == "verified":
            baseline_fp_abstention += 1
        if b == "verified" and gt == "contradicted":
            baseline_fn_miss += 1
        if b == "verified" and gt == "abstain":
            baseline_fv_other += 1

    return {
        "aedos": {
            "fp_correction": aedos_fp_correction,
            "fp_abstention": aedos_fp_abstention,
            "fn_miss": aedos_fn_miss,
            "fv_on_abstain": aedos_fv_other,
        },
        "baseline": {
            "fp_correction": baseline_fp_correction,
            "fp_abstention": baseline_fp_abstention,
            "fn_miss": baseline_fn_miss,
            "fv_on_abstain": baseline_fv_other,
        },
        "case_agreement": {
            "aedos_wins": aedos_wins,
            "aedos_hurts": aedos_hurts,
            "both_correct": both_correct,
            "both_wrong": both_wrong,
            "total": len(per_case),
        },
    }


def _latency_stats(per_case: list[dict], runner: str) -> dict:
    key = f"{runner}_latency_s"
    values = [c[key] for c in per_case if c.get(key) is not None]
    if not values:
        return {"count": 0, "median": None, "p95": None, "max": None, "sum": None}
    values_sorted = sorted(values)
    n = len(values_sorted)
    p95_idx = max(0, min(n - 1, int(0.95 * n) - 1))
    return {
        "count": n,
        "median": statistics.median(values_sorted),
        "p95": values_sorted[p95_idx],
        "max": max(values_sorted),
        "sum": sum(values_sorted),
    }


def _median_with_range(values: list[float]) -> dict:
    if not values:
        return {"median": None, "min": None, "max": None, "n": 0}
    return {
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "n": len(values),
    }


def aggregate(run_files: list[Path]) -> dict:
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in run_files]

    enriched = []
    for r in runs:
        derived = _derived_metrics(r["per_case"])
        r_enriched = dict(r)
        r_enriched["derived"] = derived
        r_enriched["aedos_latency"] = _latency_stats(r["per_case"], "aedos")
        r_enriched["baseline_latency"] = _latency_stats(r["per_case"], "baseline")
        enriched.append(r_enriched)

    # Per-mode accuracy across runs
    modes = set()
    for r in runs:
        modes.update(r["aedos_metrics"]["per_failure_mode"].keys())

    aggregates = {
        "aedos": {
            "accuracy": _median_with_range([r["aedos_metrics"]["accuracy"] for r in runs]),
            "false_verified_rate": _median_with_range(
                [r["aedos_metrics"]["false_verified_rate"] for r in runs]),
            "false_abstain_rate": _median_with_range(
                [r["aedos_metrics"]["false_abstain_rate"] for r in runs]),
            "per_failure_mode": {
                m: _median_with_range(
                    [r["aedos_metrics"]["per_failure_mode"].get(m, {}).get("accuracy", 0.0)
                     for r in runs]
                ) for m in sorted(modes)
            },
            "fp_correction": _median_with_range(
                [e["derived"]["aedos"]["fp_correction"] for e in enriched]),
            "fp_abstention": _median_with_range(
                [e["derived"]["aedos"]["fp_abstention"] for e in enriched]),
            "fn_miss": _median_with_range(
                [e["derived"]["aedos"]["fn_miss"] for e in enriched]),
            "fv_on_abstain": _median_with_range(
                [e["derived"]["aedos"]["fv_on_abstain"] for e in enriched]),
        },
        "baseline": {
            "accuracy": _median_with_range([r["baseline_metrics"]["accuracy"] for r in runs]),
            "false_verified_rate": _median_with_range(
                [r["baseline_metrics"]["false_verified_rate"] for r in runs]),
            "false_abstain_rate": _median_with_range(
                [r["baseline_metrics"]["false_abstain_rate"] for r in runs]),
            "per_failure_mode": {
                m: _median_with_range(
                    [r["baseline_metrics"]["per_failure_mode"].get(m, {}).get("accuracy", 0.0)
                     for r in runs]
                ) for m in sorted(modes)
            },
            "fp_correction": _median_with_range(
                [e["derived"]["baseline"]["fp_correction"] for e in enriched]),
            "fp_abstention": _median_with_range(
                [e["derived"]["baseline"]["fp_abstention"] for e in enriched]),
            "fn_miss": _median_with_range(
                [e["derived"]["baseline"]["fn_miss"] for e in enriched]),
            "fv_on_abstain": _median_with_range(
                [e["derived"]["baseline"]["fv_on_abstain"] for e in enriched]),
        },
        "case_agreement": {
            "aedos_wins": _median_with_range(
                [e["derived"]["case_agreement"]["aedos_wins"] for e in enriched]),
            "aedos_hurts": _median_with_range(
                [e["derived"]["case_agreement"]["aedos_hurts"] for e in enriched]),
            "both_correct": _median_with_range(
                [e["derived"]["case_agreement"]["both_correct"] for e in enriched]),
            "both_wrong": _median_with_range(
                [e["derived"]["case_agreement"]["both_wrong"] for e in enriched]),
        },
        "duration_s": _median_with_range([r["duration_s"] for r in runs]),
    }

    return {
        "n_runs": len(runs),
        "run_files": [str(p) for p in run_files],
        "per_run": enriched,
        "across_runs": aggregates,
    }


def acceptance_check(across: dict, per_run: list[dict]) -> dict:
    """Apply the 4 Phase 10.5 acceptance thresholds (per benchmark.py
    generate_report) at the median across runs.

    1. Aedos false-verified rate <= 5%
    2. Aedos accuracy >= baseline + 15pp
    3. No-regression: Aedos accuracy >= baseline on every failure mode
    4. >= 4 of 6 modes with Aedos >= baseline + 20pp
    """
    a = across["aedos"]
    b = across["baseline"]
    fv = a["false_verified_rate"]["median"]
    acc_delta = a["accuracy"]["median"] - b["accuracy"]["median"]

    per_mode_status = {}
    n_regressions = 0
    n_big_gains = 0
    for mode in a["per_failure_mode"]:
        am = a["per_failure_mode"][mode]["median"]
        bm = b["per_failure_mode"].get(mode, {}).get("median", 0.0) or 0.0
        regressed = am < bm
        big_gain = (am - bm) >= 0.20
        if regressed:
            n_regressions += 1
        if big_gain:
            n_big_gains += 1
        per_mode_status[mode] = {
            "aedos": am, "baseline": bm,
            "delta": am - bm,
            "regressed": regressed,
            "big_gain": big_gain,
        }

    return {
        "criterion_1_fv_le_5": {
            "pass": fv <= 0.05, "value": fv, "threshold": 0.05,
        },
        "criterion_2_acc_ge_baseline_plus_15": {
            "pass": acc_delta >= 0.15, "value": acc_delta, "threshold": 0.15,
        },
        "criterion_3_no_regression": {
            "pass": n_regressions == 0,
            "regressions": n_regressions,
            "per_mode": per_mode_status,
        },
        "criterion_4_big_gain_4_of_6": {
            "pass": n_big_gains >= 4,
            "big_gains": n_big_gains,
            "needed": 4,
            "total_modes": len(per_mode_status),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_files", nargs="+", type=Path,
                    help="per-run JSON files (output of medium_bar_run.py)")
    ap.add_argument("--out", type=Path,
                    default=_ROOT / "docs" / "phase_10_5" / "medium_bar" / "aggregate.json")
    args = ap.parse_args()
    if any(not p.exists() for p in args.run_files):
        missing = [str(p) for p in args.run_files if not p.exists()]
        print(f"ERROR: missing files: {missing}", file=sys.stderr)
        return 1
    result = aggregate(args.run_files)
    result["acceptance"] = acceptance_check(result["across_runs"], result["per_run"])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Wrote {args.out}")

    # Print human-readable summary
    a = result["across_runs"]["aedos"]
    b = result["across_runs"]["baseline"]
    ag = result["across_runs"]["case_agreement"]
    ac = result["acceptance"]
    print(f"\n# Across {result['n_runs']} runs (median [min..max])\n")
    print(f"Aedos accuracy:          {a['accuracy']['median']:.1%} "
          f"[{a['accuracy']['min']:.1%}..{a['accuracy']['max']:.1%}]")
    print(f"Baseline accuracy:       {b['accuracy']['median']:.1%} "
          f"[{b['accuracy']['min']:.1%}..{b['accuracy']['max']:.1%}]")
    print(f"Aedos false-verified:    {a['false_verified_rate']['median']:.1%} "
          f"[{a['false_verified_rate']['min']:.1%}..{a['false_verified_rate']['max']:.1%}]")
    print(f"Baseline false-verified: {b['false_verified_rate']['median']:.1%} "
          f"[{b['false_verified_rate']['min']:.1%}..{b['false_verified_rate']['max']:.1%}]")
    print(f"Aedos FP-correction:     median {a['fp_correction']['median']} "
          f"[{a['fp_correction']['min']}..{a['fp_correction']['max']}]  (the harmful one)")
    print(f"Baseline FP-correction:  median {b['fp_correction']['median']} "
          f"[{b['fp_correction']['min']}..{b['fp_correction']['max']}]")
    print(f"\nCase agreement (median): "
          f"Aedos-wins {ag['aedos_wins']['median']}, "
          f"Aedos-hurts {ag['aedos_hurts']['median']}, "
          f"both-correct {ag['both_correct']['median']}, "
          f"both-wrong {ag['both_wrong']['median']}")
    print(f"\nAcceptance:")
    print(f"  1) FV <= 5%:                {'PASS' if ac['criterion_1_fv_le_5']['pass'] else 'FAIL'}")
    print(f"  2) Acc >= baseline+15pp:    {'PASS' if ac['criterion_2_acc_ge_baseline_plus_15']['pass'] else 'FAIL'}")
    print(f"  3) No per-mode regression:  {'PASS' if ac['criterion_3_no_regression']['pass'] else 'FAIL'}")
    print(f"  4) >=4 of 6 modes +20pp:    {'PASS' if ac['criterion_4_big_gain_4_of_6']['pass'] else 'FAIL'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
