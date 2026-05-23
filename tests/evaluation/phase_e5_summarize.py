"""Phase E5 — collect per-component results and emit a comparison table.

Reads every `*.json` under `docs/phase_E/results/phase_e5_per_component/` and
groups by component (the corpus's underlying calibration target) to produce
the table the synthesis report consumes:

    component (corpus)         candidate                    acc    n   cost   false_v   notes
    --------------------------- ----------------------------- ----   --   -----  -------
    predicate_translation        claude-haiku-4-5             78%   80   $0.32   n/a
    predicate_translation        qwen-3-next-...              64%   80   $0.01   n/a
    ...

Used by the per-component check-ins (after each corpus's three candidates
finish) and by the final synthesis (after all corpora finish).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_RESULTS = Path(__file__).resolve().parents[2] / "docs" / "phase_E" / "results" / "phase_e5_per_component"

_COMPONENT_BY_CORPUS = {
    "predicate_metadata_corpus": "predicate_translation",
    "subsumption_corpus": "subsumption",
    "predicate_distribution_corpus": "predicate_distribution",
    "entity_resolution_corpus": "entity_resolution",
    "derivation_corpus": "walker (derivation)",
    "extraction_corpus": "(smoke)",
}

# Threshold map mirrored from THRESHOLDS in tests/calibration/test_corpus_runner.py
# so the summary can flag below-threshold results at a glance.
_THRESHOLDS = {
    "predicate_metadata_corpus": 0.85,
    "subsumption_corpus": 0.80,
    "predicate_distribution_corpus": 0.85,
    "entity_resolution_corpus": 0.90,
    "derivation_corpus": 0.80,
}


def collect() -> list[dict]:
    out = []
    if not _RESULTS.exists():
        return out
    for p in sorted(_RESULTS.glob("*.json")):
        if p.name.endswith(".transcript.json"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "candidate": data.get("candidate", "?"),
            "model": data.get("model", "?"),
            "corpus": data.get("corpus", "?"),
            "component": _COMPONENT_BY_CORPUS.get(data.get("corpus", ""), "?"),
            "n": data.get("total_cases", 0),
            "passed": data.get("passed", 0),
            "accuracy": data.get("accuracy", 0.0),
            "false_verifieds": data.get("false_verifieds"),
            "abstentions_on_positive": data.get("abstentions_on_positive"),
            "runner_errors": data.get("runner_errors", 0),
            "total_calls": data.get("total_calls", 0),
            "total_cost_usd": data.get("total_cost_usd"),
            "elapsed_seconds": data.get("elapsed_seconds", 0.0),
        })
    return out


def emit_table(rows: list[dict]) -> str:
    # Group by component, within group sort by accuracy desc.
    by_comp: dict[str, list[dict]] = {}
    for r in rows:
        by_comp.setdefault(r["component"], []).append(r)
    lines = []
    for comp, comp_rows in sorted(by_comp.items()):
        comp_rows.sort(key=lambda r: -r["accuracy"])
        threshold = next(
            (_THRESHOLDS[r["corpus"]] for r in comp_rows if r["corpus"] in _THRESHOLDS),
            None,
        )
        thresh_str = f"(threshold {threshold:.0%})" if threshold else ""
        lines.append(f"\n## {comp}  {thresh_str}")
        lines.append(
            f"{'candidate':32s} {'acc':>6s} {'n':>4s} {'cost':>8s} {'errs':>5s} "
            f"{'fv':>4s} {'fa+':>4s} {'calls':>6s} {'elapsed':>8s}"
        )
        for r in comp_rows:
            cost = f"${r['total_cost_usd']:.4f}" if r['total_cost_usd'] is not None else "n/a"
            fv = "-" if r["false_verifieds"] is None else str(r["false_verifieds"])
            fa = "-" if r["abstentions_on_positive"] is None else str(r["abstentions_on_positive"])
            lines.append(
                f"{r['candidate']:32s} {r['accuracy']:6.1%} {r['n']:>4d} {cost:>8s} "
                f"{r['runner_errors']:>5d} {fv:>4s} {fa:>4s} "
                f"{r['total_calls']:>6d} {r['elapsed_seconds']:>7.1f}s"
            )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    rows = collect()
    if not rows:
        print(f"no results found under {_RESULTS}", file=sys.stderr)
        return 1
    if argv and argv[0] == "--json":
        print(json.dumps(rows, indent=2))
        return 0
    print(emit_table(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
