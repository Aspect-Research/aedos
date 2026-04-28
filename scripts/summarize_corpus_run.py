"""Summarize a hallucination-corpus run — catches, hedges, failures.

Walks diagnostic_output/<prefix>_*.json and produces a digest:

  * Catches (verdict=contradicted): the assistant said something
    factually wrong that AEDOS caught and the corrector replaced.
  * Hedges (retrieval_inconclusive / retrieval_failed → corrector
    softened): cases where the verifier was uncertain.
  * Verifier failures (no signal): retrieval got nothing — known
    DDG flakiness / endpoint outage.
  * Pipeline errors: chat backend timeouts, content=null, etc.

Each catch/hedge prints the prompt, the model's draft, the corrected
final, and the routing decision. Designed to make the run-results
easy to scan for the operator — the JSON dumps are detailed but
unfriendly.

Usage:
    python scripts/summarize_corpus_run.py
    python scripts/summarize_corpus_run.py --prefix hallu
    python scripts/summarize_corpus_run.py --prefix dogfood
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _diag_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "diagnostic_output"


def main(argv: list[str]) -> int:
    # UTF-8 stdout for non-ASCII chat content.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", default="hallu",
                        help="filename prefix (default: hallu)")
    args = parser.parse_args(argv[1:])

    files = sorted(_diag_dir().glob(f"{args.prefix}_*.json"))
    if not files:
        print(f"no files matching {args.prefix}_*.json in {_diag_dir()}",
              file=sys.stderr)
        return 1

    catches = []      # contradicted + intervention
    hedges = []       # retrieval_inconclusive / retrieval_failed
    failures = []     # pipeline error
    clean = 0         # all-verified turns
    routing_method_counts: dict[str, int] = {}
    verdict_counts: dict[str, int] = {}

    for f in files:
        d = json.load(f.open(encoding="utf-8"))
        if "summary" not in d:
            failures.append({
                "id": d.get("id", f.stem),
                "category": d.get("category", "?"),
                "prompt": d.get("prompt", ""),
                "expected": d.get("expected", ""),
                "error": d.get("error", "(unknown)"),
            })
            continue

        s = d["summary"]
        verdicts = s.get("verdicts", [])
        contradicted = "contradicted" in verdicts
        any_inconclusive = any(
            v in {"retrieval_inconclusive", "retrieval_failed"}
            for v in verdicts
        )
        for v in verdicts:
            verdict_counts[v] = verdict_counts.get(v, 0) + 1
        for r in s.get("routings", []):
            method = r.get("method", "?")
            routing_method_counts[method] = routing_method_counts.get(method, 0) + 1

        # Pull the trace for richer context.
        trace = d.get("trace", {})
        decisions = trace.get("verification_decisions", [])

        if contradicted:
            wrong_decision = next(
                (de for de in decisions
                 if de.get("verification_status") == "contradicted"), None,
            )
            catches.append({
                "id": s["id"], "category": s["category"],
                "prompt": s["prompt"], "expected": s["expected"],
                "draft": s["final_content_first_240"],
                "wrong_claim": (wrong_decision or {}).get("claim"),
                "correction": (wrong_decision or {}).get("correction"),
            })
        elif any_inconclusive:
            inc_decision = next(
                (de for de in decisions
                 if de.get("verification_status") in {
                    "retrieval_inconclusive", "retrieval_failed"}), None,
            )
            hedges.append({
                "id": s["id"], "category": s["category"],
                "prompt": s["prompt"], "expected": s["expected"],
                "draft": s["final_content_first_240"],
                "hedged_claim": (inc_decision or {}).get("claim"),
                "verdict": (inc_decision or {}).get("verification_status"),
            })
        else:
            clean += 1

    # ---- print ----
    print(f"=== {args.prefix} corpus summary ({len(files)} turns) ===\n")
    print(f"  catches:           {len(catches)}")
    print(f"  hedges:            {len(hedges)}")
    print(f"  pipeline failures: {len(failures)}")
    print(f"  clean (verified):  {clean}")
    if routing_method_counts:
        print(f"\n  routing methods (across all claims):")
        for m, n in sorted(routing_method_counts.items(),
                           key=lambda x: -x[1]):
            print(f"    {m}: {n}")
    if verdict_counts:
        print(f"\n  verdicts:")
        for v, n in sorted(verdict_counts.items(), key=lambda x: -x[1]):
            print(f"    {v}: {n}")
    print()

    if catches:
        print("\n=== CATCHES (verifier said contradicted) ===\n")
        for c in catches:
            print(f"--- {c['id']} [{c['category']}] ---")
            print(f"  prompt: {c['prompt']}")
            print(f"  expected: {c['expected']}")
            print(f"  model draft: {c['draft']}")
            wc = c.get("wrong_claim") or {}
            print(f"  wrong claim: {wc.get('predicate')}({wc.get('slots')})")
            corr = c.get("correction") or {}
            if corr:
                print(f"  corrected to: {corr.get('corrected_object')}")
                print(f"  reason: {(corr.get('explanation', '') or '')[:200]}")
            print()

    if hedges:
        print(f"\n=== HEDGES ({len(hedges)} — verifier was uncertain) ===\n")
        for h in hedges:
            print(f"--- {h['id']} [{h['category']}] ({h.get('verdict')}) ---")
            print(f"  prompt: {h['prompt']}")
            print(f"  expected: {h['expected']}")
            print(f"  draft: {h['draft']}")
            hc = h.get("hedged_claim") or {}
            print(f"  hedged claim: {hc.get('predicate')}({hc.get('slots')})")
            print()

    if failures:
        print(f"\n=== PIPELINE FAILURES ({len(failures)}) ===\n")
        for f in failures:
            print(f"  [{f['category']}] {f['id']}: {f['error']}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
