"""Thin operator shim over the standing medium-bar harness.

v0.16.1 WS7 folded the per-instance supervision this script used to carry — the
watchdog, the LIVE false-verified AND false-contradicted counters, and the
incremental per-case JSONL — INTO `tests/evaluation/benchmark.py` (`run_tracked`
+ the `--track` live entry point). This script is now a thin shim that invokes
that one committed entry point with tracking on, so the operator's final
medium-bar run still works while the logic lives in one place under test.

Usage:  py -3 scripts/medium_bar_step1_run.py [--tag v016_step1]
                                              [--db-path aedos_phase10_5.db]
                                              [--out-dir docs/phase_10_5/medium_bar]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from aedos.utils.env import load_dotenv_if_present  # noqa: E402

load_dotenv_if_present(_ROOT / ".env")
os.environ["RUN_LIVE_KB"] = "1"
os.environ["RUN_LIVE_TESTS"] = "1"

from tests.evaluation import benchmark  # noqa: E402


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag", default="v016_step1")
    ap.add_argument("--db-path", default="aedos_phase10_5.db")
    ap.add_argument("--out-dir", type=Path,
                    default=_ROOT / "docs" / "phase_10_5" / "medium_bar")
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    os.environ["AEDOS_DB_PATH"] = args.db_path

    # Delegate to the standing harness entry point with per-instance tracking.
    # benchmark._run_live writes the markdown report at --output, the per-case
    # JSON at <output>.json, and the incremental JSONL siblings (one per runner).
    live_args = argparse.Namespace(
        test_set=benchmark._TEST_SET_PATH,
        output=args.out_dir / f"medium_bar_{args.tag}.md",
        tag=args.tag,
        track=True,
        baseline_only=False,
        aedos_only=False,
    )
    return benchmark._run_live(live_args)


if __name__ == "__main__":
    raise SystemExit(main())
