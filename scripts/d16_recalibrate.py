"""Phase H D16 re-baselining script.

Runs derivation_corpus, predicate_metadata_corpus, entity_resolution_corpus
through the (post-fix) calibration harness with the production model
configuration. Captures per-case pass/fail and produced verdict (for
verdict-bearing corpora), dumps to docs/phase_H/d16_rebaseline_<corpus>.json
for diff against Phase E5's per-component data.

Usage: py scripts/d16_recalibrate.py [<corpus> ...]
  Default: runs all three corpora.

Needs RUN_LIVE_KB=1 in environment for entity_resolution + derivation to
hit live Wikidata. Loads .env automatically via aedos.utils.env.
Production-config models cost ~$0.10-1.00 per corpus run; expected total
$1-3.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Load .env and set live-mode flags before any aedos imports.
from aedos.utils.env import load_dotenv_if_present  # noqa: E402

load_dotenv_if_present(Path(__file__).resolve().parent.parent / ".env")
os.environ.setdefault("RUN_LIVE_KB", "1")
os.environ.setdefault("RUN_LIVE_TESTS", "1")
os.environ.setdefault("RUN_CALIBRATION", "1")

from tests.calibration.test_corpus_runner import _Harness, _RUNNERS, _load_corpus  # noqa: E402


_VERDICT_CORPORA = {"derivation_corpus", "python_verification_corpus"}
_OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "phase_H"


@contextlib.contextmanager
def _verdict_recorder():
    """Monkey-patch Walker.walk so we can record the actual verdict for
    verdict-bearing corpora. Mirrors phase_e_comparison's pattern."""
    from aedos.layer4_sources import walker as walker_mod

    recorded: list[str] = []
    orig = walker_mod.Walker.walk

    def patched(self, *a, **k):
        result = orig(self, *a, **k)
        recorded.append(getattr(result, "verdict", None))
        return result

    walker_mod.Walker.walk = patched
    try:
        yield recorded
    finally:
        walker_mod.Walker.walk = orig


def run_one_corpus(corpus: str) -> dict:
    cases = _load_corpus(corpus)
    runner = _RUNNERS[corpus]
    harness = _Harness()

    is_verdict = corpus in _VERDICT_CORPORA
    outcomes: list[dict] = []
    passed = 0
    started = time.monotonic()

    for i, case in enumerate(cases, 1):
        case_id = case.get("id", f"case_{i}")
        case_started = time.monotonic()
        recorded_verdicts: list[str] = []
        produced_verdict = None
        error = None
        try:
            with _verdict_recorder() as rec:
                ok = runner(harness, case)
            recorded_verdicts = list(rec)
            if is_verdict and recorded_verdicts:
                produced_verdict = recorded_verdicts[-1]
        except Exception as exc:  # noqa: BLE001
            ok = False
            error = f"{type(exc).__name__}: {exc}"

        if ok:
            passed += 1

        expected_verdict = None
        if is_verdict:
            expected_verdict = (case.get("expected_output") or {}).get("verdict")

        elapsed_ms = round((time.monotonic() - case_started) * 1000, 1)
        outcomes.append({
            "case_id": case_id,
            "passed": ok,
            "produced_verdict": produced_verdict,
            "expected_verdict": expected_verdict,
            "error": error,
            "elapsed_ms": elapsed_ms,
        })
        verdict_str = f" verdict={produced_verdict}" if is_verdict else ""
        status = "PASS" if ok else "FAIL"
        print(f"  [{i:2}/{len(cases)}] {case_id:35s} {status}{verdict_str}"
              + (f"  ERR: {error}" if error else ""))

    total_elapsed = round(time.monotonic() - started, 1)
    accuracy = passed / len(cases) if cases else 0.0
    summary = {
        "corpus": corpus,
        "total_cases": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "accuracy": round(accuracy, 4),
        "wall_clock_seconds": total_elapsed,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "config_note": (
            "post-D16 harness fix (per-case Tier U + calib subsumption isolation); "
            "production model config (DEFAULT_MODEL_BY_PURPOSE rc.10)"
        ),
        "outcomes": outcomes,
    }
    return summary


def main(argv: list[str]) -> int:
    requested = argv if argv else [
        "derivation_corpus",
        "predicate_metadata_corpus",
        "entity_resolution_corpus",
    ]

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    summaries: list[dict] = []
    for corpus in requested:
        print(f"\n=== {corpus} ===")
        summary = run_one_corpus(corpus)
        out_path = _OUT_DIR / f"d16_rebaseline_{corpus}.json"
        out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"  accuracy: {summary['accuracy']:.1%} "
              f"({summary['passed']}/{summary['total_cases']}) "
              f"in {summary['wall_clock_seconds']}s")
        print(f"  -> {out_path}")
        summaries.append({
            "corpus": corpus,
            "accuracy": summary["accuracy"],
            "passed": summary["passed"],
            "total": summary["total_cases"],
        })

    print("\n=== Summary ===")
    for s in summaries:
        print(f"  {s['corpus']:32s} {s['accuracy']:.1%} ({s['passed']}/{s['total']})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
