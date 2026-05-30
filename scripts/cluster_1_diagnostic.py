"""Phase H Cluster 1 — Stage 2 audit log analysis for D47 failure cases.

For each of the six derivation_corpus cases where the D51 diagnostic
showed an entity-resolution-driven abstention (verdict=no_grounding_found
with 0 edges or near-zero edges), capture:

  1. Whether Stage 2 was invoked (the entity_normalization audit event).
  2. The surface form being normalized.
  3. The structured claim context plumbed into Stage 2.
  4. The candidate list shown to Stage 2.
  5. Stage 2's actual output (selection, reasoning, abstain/pick).
  6. The full user_message string the LLM saw (patched in to confirm
     source_text reached the prompt and what it looked like).

Output: docs/phase_H/cluster_1_stage_2_audit.json + a human-readable
summary printed to stdout.

This is a free, read-only investigation — it runs the corpus cases live
because Stage 2 calls Haiku, but it does not modify any code. Total
cost: ~$0.10-0.30 for six Haiku calls.
"""

from __future__ import annotations

import contextlib
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


TARGET_CASE_IDS = [
    "der_cross_001",
    "der_cross_008",
    "der_predicate_translation_001",
    "der_disambiguation_003",
    "der_disambiguation_004",
    "der_disambiguation_006",
]


@contextlib.contextmanager
def _capture_stage_2_prompts():
    """Patch WikipediaNormalizer._stage_2_llm_select to record the
    constructed user_message and the raw LLM output before returning."""
    from aedos.layer1_extraction import wikipedia_normalizer as wn

    captured: list[dict] = []
    orig = wn.WikipediaNormalizer._stage_2_llm_select

    def patched(self, *, surface_form, claim_subject, claim_predicate,
                claim_object, source_text, candidates):
        candidate_lines = "\n".join(f"  - {c}" for c in candidates)
        user_message = (
            f"surface form : {surface_form}\n"
            f"claim        : "
            f"{claim_subject or '(unknown)'} → "
            f"{claim_predicate or '(unknown)'} → "
            f"{claim_object or '(unknown)'}\n"
            f"source text  :\n"
            f"---\n"
            f"{source_text or '(no surrounding text)'}\n"
            f"---\n"
            f"candidates   :\n"
            f"{candidate_lines}\n"
        )
        selection, reasoning, error = orig(
            self,
            surface_form=surface_form,
            claim_subject=claim_subject,
            claim_predicate=claim_predicate,
            claim_object=claim_object,
            source_text=source_text,
            candidates=candidates,
        )
        captured.append({
            "surface_form": surface_form,
            "claim_subject": claim_subject,
            "claim_predicate": claim_predicate,
            "claim_object": claim_object,
            "source_text": source_text,
            "candidates": list(candidates),
            "selection": selection,
            "reasoning": reasoning,
            "error": error,
            "user_message": user_message,
        })
        return selection, reasoning, error

    wn.WikipediaNormalizer._stage_2_llm_select = patched
    try:
        yield captured
    finally:
        wn.WikipediaNormalizer._stage_2_llm_select = orig


def main() -> int:
    all_cases = _load_corpus("derivation_corpus")
    by_id = {c["id"]: c for c in all_cases}
    cases = [by_id[i] for i in TARGET_CASE_IDS if i in by_id]
    missing = [i for i in TARGET_CASE_IDS if i not in by_id]
    if missing:
        print(f"WARN: missing case ids in corpus: {missing}")

    harness = _Harness()
    runner = _RUNNERS["derivation_corpus"]

    per_case: list[dict] = []
    for i, case in enumerate(cases, 1):
        case_id = case["id"]
        print(f"\n=== [{i}/{len(cases)}] {case_id} ===")
        print(f"    text: {case['input'].get('text')!r}")

        ev_before = len(query_events(
            harness.db, event_type="entity_normalization", limit=100000))
        with _capture_stage_2_prompts() as stage_2_calls:
            try:
                passed = bool(runner(harness, case))
                error = None
            except Exception as exc:
                passed = False
                error = f"{type(exc).__name__}: {exc}"
        ev_after_rows = query_events(
            harness.db, event_type="entity_normalization", limit=100000)
        new_events = [
            e for e in ev_after_rows
            if e["id"] > 0  # placeholder; we use the count for slicing
        ]
        # Take only the newest N events where N = (after - before).
        n_new = len(ev_after_rows) - ev_before
        new_events = ev_after_rows[:n_new] if n_new > 0 else []

        # Pretty-print captured Stage 2 calls.
        for j, call in enumerate(stage_2_calls, 1):
            print(f"    --- stage 2 call {j} ---")
            print(f"        surface form  : {call['surface_form']!r}")
            print(f"        claim         : {call['claim_subject']} -> "
                  f"{call['claim_predicate']} -> {call['claim_object']}")
            print(f"        source_text   : {call['source_text']!r}")
            print(f"        candidates    : {call['candidates']}")
            print(f"        selection     : {call['selection']!r}")
            print(f"        reasoning     : {call['reasoning']!r}")
            print(f"        error         : {call['error']!r}")
        if not stage_2_calls:
            print("    (no Stage 2 invocations during this case)")

        # Pretty-print entity_normalization events to surface
        # disambiguation_page outcomes and the audit shape.
        if new_events:
            print(f"    --- audit log: {len(new_events)} entity_normalization events ---")
            for ev in new_events:
                d = ev["event_data"] if isinstance(ev["event_data"], dict) else {}
                print(f"        surface={ev['event_subject']!r} "
                      f"stage1={d.get('stage_1_outcome')} "
                      f"stage2_invoked={d.get('stage_2_invoked')} "
                      f"source_text_present={d.get('source_text_present')} "
                      f"selection={d.get('stage_2_selection')!r} "
                      f"normalized={d.get('normalized_form')!r}")
        else:
            print("    (no entity_normalization audit events for this case)")

        print(f"    -> passed={passed} error={error}")

        per_case.append({
            "case_id": case_id,
            "text": case["input"].get("text"),
            "expected_verdict": case.get("expected_output", {}).get("verdict"),
            "runner_passed": passed,
            "runner_error": error,
            "stage_2_calls": stage_2_calls,
            "audit_events": [
                {
                    "event_subject": e["event_subject"],
                    "event_data": e["event_data"],
                }
                for e in new_events
            ],
        })

    out = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "target_case_ids": TARGET_CASE_IDS,
        "per_case": per_case,
    }
    out_path = (
        Path(__file__).resolve().parent.parent
        / "docs" / "phase_H" / "cluster_1_stage_2_audit.json"
    )
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")

    # Summary table: per-case mechanism classification.
    print("\n\n=== Summary ===")
    print(f"{'case_id':<35} {'stage2_calls':<14} {'classification'}")
    for c in per_case:
        n_calls = len(c["stage_2_calls"])
        events = c["audit_events"]
        # Quick classification heuristic for the printed summary; the
        # real classification happens in human review of the JSON.
        if n_calls == 0:
            stages = {
                e["event_data"].get("stage_1_outcome")
                for e in events
                if isinstance(e["event_data"], dict)
            }
            if not events:
                klass = "no_normalization_attempted"
            elif stages.issubset({"canonical_no_redirect", "clean_redirect"}):
                klass = "stage_1_resolved (not a Stage 2 case)"
            elif "not_found" in stages:
                klass = "stage_1_not_found"
            elif "disambiguation_page" in stages:
                klass = "Stage 1 saw disambig but Stage 2 not invoked (B/wiring)"
            else:
                klass = f"other ({stages})"
        else:
            picks = [c0 for c0 in c["stage_2_calls"] if c0.get("selection") and c0["selection"] != "ABSTAIN"]
            abstains = [c0 for c0 in c["stage_2_calls"] if not c0.get("selection") or c0["selection"] == "ABSTAIN"]
            if abstains and not picks:
                klass = "Stage 2 ABSTAINED (Mech A candidate)"
            elif picks and not abstains:
                klass = "Stage 2 picked (check correctness — Mech D candidate?)"
            else:
                klass = "mixed: some picks, some abstains"
        print(f"{c['case_id']:<35} {n_calls:<14} {klass}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
