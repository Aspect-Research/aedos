"""Phase H D5: full-corpus diagnostic to discriminate D5's three
possible outcomes per the operator's check-in.

Captures, for every derivation_corpus case:
  1. The walker's final verdict.
  2. The full list of trace edges (with metadata).
  3. The kb_live_neighbors audit events written during that case.
  4. The kb_neighbor_enumeration trace edges (if any).

Then aggregates to answer:
  A. What entities/properties did D5 query, and what came back?
  B. When neighbors were returned, did the walker use them as premises?
  C. For failing cases, did the walker reach enumeration at all?

Output: docs/phase_H/d5_diagnostic.json (structured per-case + aggregates).
"""

from __future__ import annotations

import contextlib
import os
import sys
import json
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aedos.utils.env import load_dotenv_if_present  # noqa: E402
load_dotenv_if_present(Path(__file__).resolve().parent.parent / ".env")
os.environ.setdefault("RUN_LIVE_KB", "1")
os.environ.setdefault("RUN_LIVE_TESTS", "1")
os.environ.setdefault("RUN_CALIBRATION", "1")

from aedos.audit.log import query_events  # noqa: E402
from tests.calibration.test_corpus_runner import _Harness, _load_corpus, _RUNNERS  # noqa: E402


@contextlib.contextmanager
def _walk_capture():
    """Patch Walker.walk to capture per-call verdict + full trace edges."""
    from aedos.layer4_sources import walker as walker_mod
    captured: list[dict] = []
    orig = walker_mod.Walker.walk

    def patched(self, *a, **k):
        result = orig(self, *a, **k)
        captured.append({
            "verdict": result.verdict,
            "abstention_reason": result.abstention_reason,
            "edges": [
                {
                    "edge_type": e.edge_type,
                    "metadata": dict(e.metadata or {}),
                    "source_content": dict(e.source.content or {}) if e.source else {},
                    "target_content": dict(e.target.content or {}) if e.target else {},
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


def main() -> int:
    cases = _load_corpus("derivation_corpus")
    harness = _Harness()
    runner = _RUNNERS["derivation_corpus"]

    per_case: list[dict] = []
    audit_event_count_before = len(query_events(harness.db, event_type="kb_live_neighbors", limit=100000))

    for i, case in enumerate(cases, 1):
        case_id = case.get("id", f"?{i}")
        case_audit_before = len(query_events(harness.db, event_type="kb_live_neighbors", limit=100000))
        with _walk_capture() as walks:
            try:
                passed = bool(runner(harness, case))
                error = None
            except Exception as exc:
                passed = False
                error = f"{type(exc).__name__}: {exc}"
        walk = walks[-1] if walks else {"verdict": None, "edges": []}
        case_audit_after = len(query_events(harness.db, event_type="kb_live_neighbors", limit=100000))
        per_case.append({
            "case_id": case_id,
            "category": case.get("category"),
            "passed": passed,
            "verdict": walk.get("verdict"),
            "abstention_reason": walk.get("abstention_reason"),
            "edges": walk.get("edges", []),
            "kb_live_neighbors_calls_this_case": case_audit_after - case_audit_before,
            "error": error,
        })
        print(f"  [{i:2}/{len(cases)}] {case_id}: {walk.get('verdict')} "
              f"({len(walk.get('edges', []))} edges, "
              f"{case_audit_after - case_audit_before} kb_live_neighbors calls)")

    # Pull all kb_live_neighbors audit events with full event_data.
    all_events = query_events(harness.db, event_type="kb_live_neighbors", limit=100000)
    # Strip the auto-added id/occurred_at to keep file size manageable;
    # keep event_subject (entity) and event_data (the structured payload).
    audit_log = [
        {"event_subject": e["event_subject"], "event_data": e["event_data"]}
        for e in all_events
    ]

    # Aggregates
    by_verdict = Counter(c["verdict"] for c in per_case)
    pass_rate = sum(1 for c in per_case if c["passed"]) / len(per_case) if per_case else 0
    kb_neighbor_edge_count = sum(
        sum(1 for e in c["edges"] if e["edge_type"] == "kb_neighbor_enumeration")
        for c in per_case
    )
    cases_with_d5_edge = sum(
        1 for c in per_case
        if any(e["edge_type"] == "kb_neighbor_enumeration" for e in c["edges"])
    )
    cases_with_kb_call = sum(
        1 for c in per_case if c["kb_live_neighbors_calls_this_case"] > 0
    )
    cases_with_neighbors_returned = sum(
        1 for ev in audit_log
        if isinstance(ev["event_data"], dict)
        and ev["event_data"].get("total_neighbors_returned", 0) > 0
    )

    # Per-property aggregate: when D5 fires, what neighbor counts come back?
    prop_returned = Counter()
    prop_total_returned = Counter()
    for ev in audit_log:
        d = ev["event_data"] if isinstance(ev["event_data"], dict) else {}
        per_prop = d.get("per_property_counts", {}) or {}
        for prop, count in per_prop.items():
            if count > 0:
                prop_returned[prop] += 1
            prop_total_returned[prop] += count

    out = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_cases": len(per_case),
        "passed": sum(1 for c in per_case if c["passed"]),
        "accuracy": round(pass_rate, 4),
        "verdict_distribution": dict(by_verdict),
        "total_kb_live_neighbors_events": len(audit_log),
        "cases_where_kb_was_called": cases_with_kb_call,
        "cases_with_d5_edge_in_trace": cases_with_d5_edge,
        "total_kb_neighbor_edges": kb_neighbor_edge_count,
        "audit_events_with_neighbors_returned": cases_with_neighbors_returned,
        "per_property_calls_returning_neighbors": dict(prop_returned),
        "per_property_total_neighbors_returned": dict(prop_total_returned),
        "per_case": per_case,
        "audit_log_kb_live_neighbors": audit_log,
    }

    out_path = Path(__file__).resolve().parent.parent / "docs" / "phase_H" / "d5_diagnostic.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")
    print(f"Accuracy: {pass_rate:.1%}  ({sum(1 for c in per_case if c['passed'])}/{len(per_case)})")
    print(f"Total kb_live_neighbors events: {len(audit_log)}")
    print(f"Cases where _live_neighbors was called: {cases_with_kb_call}")
    print(f"Cases with kb_neighbor_enumeration edge in trace: {cases_with_d5_edge}")
    print(f"Total kb_neighbor_enumeration edges across all walks: {kb_neighbor_edge_count}")
    print(f"Audit events that returned >0 neighbors: {cases_with_neighbors_returned} / {len(audit_log)}")
    print(f"Per-property calls returning neighbors: {dict(prop_returned)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
