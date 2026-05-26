"""Phase H Cluster 3 step 5 — dual-measurement validation harness.

Runs `derivation_corpus` against the post-Cluster-3 build in BOTH
measurement modes:

  - **Seeded** (`--mode seeded`, default): the corpus runner's _Harness
    constructs its in-memory DB with `load_seeds=True`, so the seed pack
    populates `predicate_translation` before any case runs. This measures
    the in-vocabulary path production deployments use when the extractor
    produces a known predicate; per-case latency is bounded by the LLM-
    LLM walk, not by cold-start predicate metadata generation.

  - **Cold-start** (`--mode cold-start`): _Harness constructs the DB
    empty (matches pre-Cluster-3 behavior). Every predicate consultation
    triggers an LLM oracle call. This measures the system's robustness
    on novel vocabulary not anticipated by the seed pack.

The harness writes a per-mode JSON (one per run) and prints aggregate
accuracy + audit-event counts. The validation doc
(`docs/phase_H/cluster_3_validation.md`) reports both modes side by
side as the precedent for Phase 10.5's release-decision data.

Cost: ~$2-3 per seeded run, $2-3 per cold-start run (the cold-start
mode has more LLM calls per case but the corpus is small). Total for
3 runs × 2 modes ≈ $12-18.

Usage:
  py scripts/cluster_3_validation.py --mode seeded --runs 3
  py scripts/cluster_3_validation.py --mode cold-start --runs 3
  py scripts/cluster_3_validation.py --mode both --runs 3   # convenience
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
from tests.calibration.test_corpus_runner import _Harness, _load_corpus, _RUNNERS  # noqa: E402

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

    walk = walks[0] if walks else None

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


def run_once(run_index: int, mode: str) -> dict:
    """Run the entire derivation_corpus once in the given mode."""
    cases = _load_corpus("derivation_corpus")
    runner = _RUNNERS["derivation_corpus"]
    seeded = mode == "seeded"
    harness = _Harness(seeded=seeded)

    started = datetime.now(timezone.utc).isoformat()
    print(f"\n{'='*72}")
    print(f"Cluster 3 validation — mode={mode!r} — run {run_index + 1} — "
          f"started {started}")
    print(f"{'='*72}\n")

    # Pin the substrate state at harness construction. For seeded mode,
    # `predicate_translation` should already carry the seed pack (load
    # was triggered by the first .db access during harness setup). For
    # cold-start mode, it should be empty.
    seed_count = harness.db.execute(
        "SELECT COUNT(*) FROM predicate_translation"
    ).fetchone()[0]
    print(f"  predicate_translation row count at start: {seed_count} "
          f"(expected {'>=64' if seeded else '0'})")

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
    print(f"\n  Mode: {mode}  Accuracy: {passed}/{len(per_case)} "
          f"({100.0 * passed / len(per_case):.1f}%)")
    print(f"  Run finished at {finished}")

    return {
        "mode": mode,
        "seeded": seeded,
        "predicate_translation_row_count_at_start": seed_count,
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
    ap.add_argument(
        "--mode", choices=("seeded", "cold-start", "both"), default="seeded",
        help="Measurement mode (default: seeded). 'both' runs each mode "
             "back-to-back; not the same statistical question as separate "
             "invocations but cheaper for the typical 3-runs-each case.",
    )
    ap.add_argument(
        "--runs", type=int, default=1,
        help="number of runs per mode (default 1; per D49 use 2-3)",
    )
    args = ap.parse_args()

    out_dir = Path(__file__).resolve().parent.parent / "docs" / "phase_H"
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    modes = ("seeded", "cold-start") if args.mode == "both" else (args.mode,)

    all_runs = []
    for mode in modes:
        for i in range(args.runs):
            run_result = run_once(i, mode)
            all_runs.append(run_result)

    out = {
        "timestamp": timestamp,
        "modes": list(modes),
        "runs_per_mode": args.runs,
        "runs_total": len(all_runs),
        "runs": all_runs,
    }
    out_path = out_dir / f"cluster_3_validation_run_{timestamp}.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
