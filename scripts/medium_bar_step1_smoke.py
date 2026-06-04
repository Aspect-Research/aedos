"""Cold-start smoke: run 3 Medium-Bar instances live against a FRESH (empty)
substrate, so the full discover-from-Wikidata path is exercised from scratch.
A pre-flight before the full 122-case run — confirms the live pipeline runs
end-to-end without hanging.

Selects the first case of three failure modes (a grounding path, the discovery
path, and the abstention path) so the smoke covers verify + abstain.

v0.16.1 WS7: the per-instance supervision (watchdog + live false-verified AND
false-contradicted counters) is now the standing harness's `run_tracked`; this
smoke is a thin wrapper that builds a cold pipeline and calls it on a 3-case
slice. No bespoke watchdog/counter code lives here anymore.

Usage:  py -3 scripts/medium_bar_step1_smoke.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

from aedos.utils.env import load_dotenv_if_present  # noqa: E402

load_dotenv_if_present(_ROOT / ".env")
os.environ["RUN_LIVE_KB"] = "1"
os.environ["RUN_LIVE_TESTS"] = "1"

from tests.evaluation.benchmark import AedosRunner, load_test_set, run_tracked  # noqa: E402
from aedos.database import open_db  # noqa: E402
from aedos.pipeline import build_pipeline  # noqa: E402


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
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
    print(f"Cold-start smoke: fresh empty substrate at {cold_db}", flush=True)
    print(f"Building live pipeline (RUN_LIVE_KB={os.environ.get('RUN_LIVE_KB')}) ...", flush=True)
    t0 = time.time()
    pipeline = build_pipeline(open_db(cold_db))
    aedos = AedosRunner(pipeline=(pipeline.extractor, pipeline.walker, pipeline.aggregator))
    print(f"Pipeline built in {time.time() - t0:.1f}s. Running {len(picked)} cold cases ...\n", flush=True)

    # The standing harness's per-instance supervision: watchdog + live FV/FC
    # counters. JSONL goes to the smoke file so progress is monitorable.
    results = run_tracked(
        aedos, picked, "smoke", _ROOT / "aedos_step1_smoke.jsonl"
    )

    gt = {c.case_id: c.ground_truth for c in picked}
    n_ok = sum(1 for c, r in zip(picked, results)
               if (r.verdict if r.verdict in ("verified", "contradicted") else "abstain") == gt[c.case_id])
    n_fv = sum(1 for c, r in zip(picked, results)
               if r.verdict == "verified" and gt[c.case_id] != "verified")
    print(f"\n{n_ok}/{len(results)} match ground truth | false-verified={n_fv} | "
          f"total {time.time() - t0:.0f}s", flush=True)
    print("SMOKE COMPLETE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
