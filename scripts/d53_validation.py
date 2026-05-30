"""Phase H D53 step 4 — focused validation.

For each of the six Cluster 1 problem cases, runs the case live
through the normalizer + walker and captures:

  - Stage A outcome
  - Stage B query
  - Stage C selected_qid (or abstain)
  - Final walker verdict + edges

Compares against the operator's expected canonical Q-ids:

  der_cross_001                 (Obama)       → Q76
  der_cross_008                 (Obama)       → Q76
  der_predicate_translation_001 (Obama)       → Q76
  der_disambiguation_003        (Apple)       → Q312 (Apple Inc.)
  der_disambiguation_004        (Einstein)    → Q937 (Albert Einstein)
  der_disambiguation_006        (Amazon)      → Q3783 (Amazon River, given river context)

Output: docs/phase_H/d53_validation.json + human-readable summary.

This is targeted at the entity normalization layer — downstream walker
verdicts may still hit Cluster 2 / Cluster 3 gaps and abstain. The
validation question is whether Stage C now selects the correct Q-id,
not whether the case ultimately verifies.
"""

from __future__ import annotations

import json
import os
import sys
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


TARGETS = [
    # (case_id, surface_form, expected_qid, description)
    ("der_cross_001", "Obama", "Q76", "Barack Obama"),
    ("der_cross_008", "Obama", "Q76", "Barack Obama"),
    ("der_predicate_translation_001", "Obama", "Q76", "Barack Obama"),
    ("der_disambiguation_003", "Apple", "Q312", "Apple Inc."),
    ("der_disambiguation_004", "Einstein", "Q937", "Albert Einstein"),
    ("der_disambiguation_006", "Amazon", "Q3783", "Amazon River (with river context)"),
]


def main() -> int:
    cases_all = _load_corpus("derivation_corpus")
    by_id = {c["id"]: c for c in cases_all}

    harness = _Harness()
    runner = _RUNNERS["derivation_corpus"]

    per_case: list[dict] = []
    print(f"{'='*72}")
    print(f"D53 step 4 — focused entity-normalization validation")
    print(f"{'='*72}\n")

    for case_id, surface, expected_qid, desc in TARGETS:
        case = by_id.get(case_id)
        if case is None:
            print(f"  SKIP {case_id}: not in corpus")
            continue

        ev_before = len(query_events(
            harness.db, event_type="entity_normalization", limit=100000))

        try:
            passed = bool(runner(harness, case))
            err = None
        except Exception as exc:
            passed = False
            err = f"{type(exc).__name__}: {exc}"

        # Pull events written during this case (newest-first).
        all_events = query_events(
            harness.db, event_type="entity_normalization", limit=100000)
        n_new = len(all_events) - ev_before
        new_events = all_events[:n_new] if n_new > 0 else []

        # Find the event(s) for the target surface form.
        target_events = [
            e for e in new_events
            if e["event_subject"] == surface
        ]
        # Pick the most recent (newest first).
        primary = target_events[0] if target_events else None
        if primary is None:
            print(f"  [{case_id:35}] no normalization event for {surface!r}")
            continue

        d = primary["event_data"]
        got_qid = d.get("selected_qid")
        match = "✓" if got_qid == expected_qid else "✗"

        print(f"  [{case_id:35}] {surface!r:12} expected={expected_qid:7} got={str(got_qid):10} {match}")
        print(f"     stage_a={d.get('stage_a_outcome'):25}  stage_b_query={d.get('stage_b_query')!r}")
        print(f"     stage_b_count={d.get('stage_b_candidate_count')}  "
              f"stage_c_shortcut={d.get('stage_c_shortcut_fired')}  "
              f"stage_c_llm={d.get('stage_c_llm_invoked')}")
        if d.get("stage_c_reasoning"):
            r = d["stage_c_reasoning"]
            print(f"     reasoning: {r[:140]!r}")
        print(f"     runner passed={passed} err={err}")
        print()

        per_case.append({
            "case_id": case_id,
            "surface_form": surface,
            "expected_qid": expected_qid,
            "selected_qid": got_qid,
            "qid_matches": got_qid == expected_qid,
            "stage_a_outcome": d.get("stage_a_outcome"),
            "stage_b_query": d.get("stage_b_query"),
            "stage_b_candidate_count": d.get("stage_b_candidate_count"),
            "stage_c_shortcut_fired": d.get("stage_c_shortcut_fired"),
            "stage_c_llm_invoked": d.get("stage_c_llm_invoked"),
            "stage_c_reasoning": d.get("stage_c_reasoning"),
            "runner_passed": passed,
            "runner_error": err,
        })

    # Summary
    print(f"\n{'='*72}")
    print(f"Summary: {sum(1 for c in per_case if c['qid_matches'])}/{len(per_case)} "
          f"selected_qid matches expected; "
          f"{sum(1 for c in per_case if c['runner_passed'])}/{len(per_case)} "
          f"runner-passed (verdict matches expected_output)")
    print(f"{'='*72}")

    out = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "per_case": per_case,
    }
    out_path = Path(__file__).resolve().parent.parent / "docs" / "phase_H" / "d53_validation.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
