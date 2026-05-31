"""Step 1 cold-start smoke: run 3 Medium-Bar instances live against a FRESH
(empty) substrate, so the full discover-from-Wikidata path is exercised from
scratch. A pre-flight before the full 122-case run — confirms the live pipeline
runs end-to-end without hanging.

Selects the first case of three failure modes (a grounding path, the discovery
path, and the abstention path) so the smoke covers verify + abstain.

Usage:  py -3 scripts/medium_bar_step1_smoke.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from aedos.utils.env import load_dotenv_if_present  # noqa: E402

load_dotenv_if_present(_ROOT / ".env")
os.environ["RUN_LIVE_KB"] = "1"
os.environ["RUN_LIVE_TESTS"] = "1"

from tests.evaluation.benchmark import AedosRunner, load_test_set  # noqa: E402
from aedos.database import open_db  # noqa: E402
from aedos.pipeline import build_pipeline  # noqa: E402


def _p(msg: str) -> None:
    print(msg, flush=True)


def _watchdog(case_id: str, stop: threading.Event) -> None:
    t0 = time.time()
    for mark in (60, 120, 240, 480):
        if stop.wait(timeout=mark - (time.time() - t0)):
            return
        if not stop.is_set():
            _p(f"    [WARN] {case_id} still running at {int(time.time() - t0)}s")


def main() -> int:
    cases = load_test_set()
    want_modes = ["multi_hop_distribution", "predicate_translation", "principled_abstention"]
    picked = []
    for m in want_modes:
        for c in cases:
            if c.failure_mode == m:
                picked.append(c)
                break

    cold_db = str(_ROOT / "aedos_step1_smoke_cold.db")
    if os.path.exists(cold_db):
        os.remove(cold_db)
    _p(f"Cold-start smoke: fresh empty substrate at {cold_db}")
    _p(f"Building live pipeline (RUN_LIVE_KB={os.environ.get('RUN_LIVE_KB')}) ...")
    t0 = time.time()
    pipeline = build_pipeline(open_db(cold_db))
    aedos = AedosRunner(pipeline=(pipeline.extractor, pipeline.walker, pipeline.aggregator))
    _p(f"Pipeline built in {time.time() - t0:.1f}s. Running {len(picked)} cold cases ...\n")

    rows = []
    for i, c in enumerate(picked, 1):
        _p(f"[{i}/{len(picked)}] {c.case_id} ({c.failure_mode}) gt={c.ground_truth}")
        _p(f"    statement: {c.statement}")
        stop = threading.Event()
        wd = threading.Thread(target=_watchdog, args=(c.case_id, stop), daemon=True)
        wd.start()
        cstart = time.time()
        r = aedos.run_case(c)
        stop.set()
        elapsed = time.time() - cstart
        # benchmark verdict -> ground-truth bucket
        bucket = ("verified" if r.verdict == "verified"
                  else "contradicted" if r.verdict == "contradicted"
                  else "abstain")
        match = "OK " if bucket == c.ground_truth else "MISS"
        flag = ""
        if bucket == "verified" and c.ground_truth != "verified":
            flag = "  <<< FALSE-VERIFIED (soundness!)"
        _p(f"    -> verdict={r.verdict} bucket={bucket} expected={c.ground_truth} [{match}] {elapsed:.1f}s{flag}\n")
        rows.append((c.case_id, c.failure_mode, c.ground_truth, r.verdict, bucket, match, round(elapsed, 1)))

    _p("=== SMOKE SUMMARY ===")
    for cid, mode, gt, v, bucket, match, el in rows:
        _p(f"  {match} {cid:20} {mode:24} gt={gt:12} verdict={v:20} {el}s")
    n_ok = sum(1 for r in rows if r[5] == "OK ")
    n_fv = sum(1 for r in rows if r[4] == "verified" and r[2] != "verified")
    _p(f"\n{n_ok}/{len(rows)} match ground truth | false-verified={n_fv} | total {time.time() - t0:.0f}s")
    _p("SMOKE COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
