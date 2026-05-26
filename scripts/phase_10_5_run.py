"""Phase 10.5 single-corpus measurement driver.

Runs one calibration corpus in one mode (seeded or cold-start) and emits a
per-run JSON for the release-decision data. Modeled on
`scripts/cluster_3_validation.py` but generalized across all 11 corpora;
Phase 10.5 invokes it once per (corpus, mode) pair per the operator's
single-run scope decision (2026-05-26, Session 1).

Outputs JSON to `docs/phase_10_5/runs/<corpus>__<mode>__<timestamp>.json`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from aedos.utils.env import load_dotenv_if_present  # noqa: E402

load_dotenv_if_present(_ROOT / ".env")
os.environ.setdefault("RUN_LIVE_KB", "1")
os.environ.setdefault("RUN_LIVE_TESTS", "1")
os.environ.setdefault("RUN_CALIBRATION", "1")

from tests.calibration.test_corpus_runner import (  # noqa: E402
    _Harness, _RUNNERS, _load_corpus, THRESHOLDS,
)


def run_once(corpus: str, mode: str) -> dict:
    cases = _load_corpus(corpus)
    runner = _RUNNERS[corpus]
    seeded = mode == "seeded"
    harness = _Harness(seeded=seeded)

    started = datetime.now(timezone.utc).isoformat()
    t0 = time.time()
    print(f"\n{'='*72}")
    print(f"Phase 10.5 — corpus={corpus} mode={mode} — started {started}")
    print(f"{'='*72}")

    seed_count = harness.db.execute(
        "SELECT COUNT(*) FROM predicate_translation"
    ).fetchone()[0]
    print(f"  predicate_translation rows at start: {seed_count} "
          f"(expected {'>=64' if seeded else '0'})")

    per_case: list[dict] = []
    passed = 0
    for i, case in enumerate(cases, 1):
        case_id = case.get("id", f"<#{i}>")
        t_case = time.time()
        try:
            ok = bool(runner(harness, case))
            err = None
        except Exception:
            ok = False
            err = traceback.format_exc(limit=4)
        per_case.append({
            "case_id": case_id,
            "category": case.get("category"),
            "passed": ok,
            "error": err,
            "duration_s": round(time.time() - t_case, 3),
        })
        if ok:
            passed += 1
        glyph = "OK " if ok else "MISS"
        cat = case.get("category", "")
        print(f"  [{i:3}/{len(cases)}] {case_id:35} {cat:24} {glyph}")

    finished = datetime.now(timezone.utc).isoformat()
    duration = round(time.time() - t0, 1)
    accuracy = passed / len(cases) if cases else 0.0
    threshold = THRESHOLDS.get(corpus)
    print(f"\n  {corpus} {mode}: {passed}/{len(cases)} = {accuracy:.1%} "
          f"(threshold {threshold:.0%})  duration {duration}s")
    return {
        "corpus": corpus,
        "mode": mode,
        "seeded": seeded,
        "predicate_translation_row_count_at_start": seed_count,
        "started_at": started,
        "finished_at": finished,
        "duration_s": duration,
        "case_count": len(cases),
        "passed": passed,
        "accuracy": accuracy,
        "threshold": threshold,
        "per_case": per_case,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("corpus", choices=sorted(_RUNNERS),
                    help="corpus name (without .jsonl)")
    ap.add_argument("--mode", choices=("seeded", "cold-start"),
                    default="seeded")
    args = ap.parse_args()

    out_dir = _ROOT / "docs" / "phase_10_5" / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)

    result = run_once(args.corpus, args.mode)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fname = f"{args.corpus}__{args.mode}__{ts}.json"
    out_path = out_dir / fname
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path.relative_to(_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
