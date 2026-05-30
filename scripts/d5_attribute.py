"""Phase H D5: attribution check for cases that moved fail→pass.

Runs the two derivation_corpus cases (der_predicate_translation_004,
_008) with trace and audit-log capture, then reports whether
kb_neighbor_enumeration edges fired. If they did, the post-D5 lift on
these cases is attributable to D5 itself rather than run-to-run
variance.
"""

from __future__ import annotations

import contextlib
import os
import sys
import json
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


TARGET_IDS = {"der_predicate_translation_004", "der_predicate_translation_008"}


@contextlib.contextmanager
def _walk_capture():
    from aedos.layer4_sources import walker as walker_mod
    captured: list[dict] = []
    orig = walker_mod.Walker.walk

    def patched(self, *a, **k):
        result = orig(self, *a, **k)
        captured.append({
            "verdict": result.verdict,
            "abstention_reason": result.abstention_reason,
            "edges": [
                {"edge_type": e.edge_type, "metadata": dict(e.metadata or {})}
                for e in result.trace.edges
            ],
        })
        return result

    walker_mod.Walker.walk = patched
    try:
        yield captured
    finally:
        walker_mod.Walker.walk = orig


def main() -> int:
    cases = _load_corpus("derivation_corpus")
    harness = _Harness()
    runner = _RUNNERS["derivation_corpus"]

    per_case_results: dict[str, dict] = {}
    # Run cases in their corpus order so substrate state matches the
    # post-fix harness's per-case isolation invariant.
    for case in cases:
        case_id = case.get("id", "?")
        # Run only target cases under the capture; non-target cases
        # still run (substrate-state accumulation should match the
        # full-corpus measurement, even if we don't keep their traces).
        if case_id in TARGET_IDS:
            with _walk_capture() as walks:
                try:
                    passed = bool(runner(harness, case))
                except Exception as exc:
                    passed = False
                    walks = [{"error": f"{type(exc).__name__}: {exc}", "edges": []}]
            walk = walks[-1] if walks else {"edges": []}
            kb_neighbor_edges = [
                e for e in walk.get("edges", [])
                if e["edge_type"] == "kb_neighbor_enumeration"
            ]
            per_case_results[case_id] = {
                "passed": passed,
                "verdict": walk.get("verdict"),
                "kb_neighbor_edges_count": len(kb_neighbor_edges),
                "kb_neighbor_edges": kb_neighbor_edges[:5],
                "all_edge_types": [e["edge_type"] for e in walk.get("edges", [])],
            }
        else:
            try:
                runner(harness, case)
            except Exception:
                pass

    # Also query the audit log for kb_live_neighbors events written
    # specifically during the target cases. The harness shares one DB
    # across all cases, so we can only see the global event list.
    events = query_events(harness.db, event_type="kb_live_neighbors", limit=200)

    print(json.dumps({
        "target_cases": per_case_results,
        "total_kb_live_neighbors_audit_events": len(events),
        "audit_event_sample_entities": [e["event_subject"] for e in events[:10]],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
