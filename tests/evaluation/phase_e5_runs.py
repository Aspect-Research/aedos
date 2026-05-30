"""Phase E5 — per-component model-selection matrix.

Defines the candidate × corpus matrix this session runs and provides a single
named-cell entry point so each run is reproducible from one CLI invocation.
Each cell knows which purposes to override and (for derivation_corpus) which
purposes to pin to the already-decided Phase E winners — see the session brief.

Routing decisions baked in here:

- Substrate cells (predicate_translation / subsumption / predicate_distribution
  / entity_resolution) override ONLY the matching `substrate:*` purpose. Every
  other purpose stays on its rc.9 DEFAULT_MODEL_BY_PURPOSE value. This isolates
  the per-component signal so a low substrate-oracle accuracy can't be blamed
  on whatever model the rest of the pipeline runs.
- Derivation cells override all four substrate purposes to the candidate AND
  pin `python_verifier` to Devstral Small 1.1 (the Phase E python_verifier
  winner). Extractor stays on its rc.9 Haiku 4.5 default (calibrated in
  Phase E3). This composes the candidate-under-test (substrate) with the
  already-decided python_verifier choice, matching the post-Phase-E5
  configuration's expected walker behavior.

Results write to `docs/phase_E/results/phase_e5_per_component/`, keeping the
per-component runs separate from the original Phase E `{"*": ...}` whole-run
results.

Usage:
    py -m tests.evaluation.phase_e5_runs --list
    py -m tests.evaluation.phase_e5_runs <cell>

A cell is `<candidate>__<short_component>`, e.g.:
    qwen-3-next-80b-a3b-instruct__predicate_translation
    claude-haiku-4-5__walker
    gpt-4.1-mini__entity_resolution
"""

from __future__ import annotations

import json
import sys
from typing import Optional

from tests.evaluation.phase_e_comparison import run_comparison

_RESULT_SUBDIR = "phase_e5_per_component"

# Corpora that exercise the KB path under the calibration runner. `RUN_LIVE_KB=1`
# is set for these so the WikidataAdapter switches from fixture-only to live
# mode (`kb_wikidata.py:326`). Without it, entity_resolution returns
# fixture-driven candidate pools (which D33 surfaced as divergent from live
# Wikidata) and the derivation walker abstains whenever its KB lookup misses
# the fixture set.
_LIVE_KB_CORPORA = frozenset({"entity_resolution_corpus", "derivation_corpus"})

# Substrate components: each maps to a single purpose override + one corpus.
_SUBSTRATE_COMPONENTS: dict[str, dict] = {
    "predicate_translation": {
        "corpus": "predicate_metadata_corpus",
        "purposes": ["substrate:predicate_translation"],
    },
    "subsumption": {
        "corpus": "subsumption_corpus",
        "purposes": ["substrate:subsumption"],
    },
    "predicate_distribution": {
        "corpus": "predicate_distribution_corpus",
        "purposes": ["substrate:predicate_distribution"],
    },
    "entity_resolution": {
        "corpus": "entity_resolution_corpus",
        "purposes": ["substrate:entity_resolution"],
    },
}

# Walker component: substrate purposes routed to the candidate, python_verifier
# pinned to Devstral (the Phase E python_verifier winner). Extractor stays on
# its rc.9 Haiku 4.5 default.
_WALKER_COMPONENT = {
    "corpus": "derivation_corpus",
    "purposes": [
        "substrate:predicate_translation",
        "substrate:subsumption",
        "substrate:predicate_distribution",
        "substrate:entity_resolution",
    ],
    "pin_purposes": {"python_verifier": "devstral-small-2"},
}

# Candidates this session evaluates.
_PRIMARY_CANDIDATES = (
    "qwen-3-next-80b-a3b-instruct",
    "claude-haiku-4-5",
    "gpt-4.1-mini",
)
# Sonnet 4.6 runs derivation initially; expanded only if derivation result
# warrants. The session brief: "Run only on derivation_corpus initially."
_SONNET_DERIVATION_ONLY = "claude-sonnet-4-6"


def all_cells() -> dict[str, dict]:
    """Return the full cell table: cell_name → {candidate, corpus, purposes, ...}."""
    cells: dict[str, dict] = {}
    for cand in _PRIMARY_CANDIDATES:
        for comp_name, comp in _SUBSTRATE_COMPONENTS.items():
            cells[f"{cand}__{comp_name}"] = {
                "candidate": cand,
                "corpus": comp["corpus"],
                "purposes": comp["purposes"],
            }
        cells[f"{cand}__walker"] = {
            "candidate": cand,
            "corpus": _WALKER_COMPONENT["corpus"],
            "purposes": _WALKER_COMPONENT["purposes"],
            "pin_purposes": _WALKER_COMPONENT["pin_purposes"],
        }
    # Sonnet — derivation only initially.
    cells[f"{_SONNET_DERIVATION_ONLY}__walker"] = {
        "candidate": _SONNET_DERIVATION_ONLY,
        "corpus": _WALKER_COMPONENT["corpus"],
        "purposes": _WALKER_COMPONENT["purposes"],
        "pin_purposes": _WALKER_COMPONENT["pin_purposes"],
    }
    return cells


def run_cell(cell_name: str, *, case_ids: Optional[list[str]] = None,
             write: bool = True) -> dict:
    import os
    cells = all_cells()
    if cell_name not in cells:
        raise KeyError(f"unknown cell {cell_name!r}; known: {sorted(cells)}")
    cell = cells[cell_name]
    # Live-KB cells: switch the WikidataAdapter to live mode for the duration
    # of the run. Restored afterward so a later non-live cell isn't poisoned.
    prev_live = os.environ.get("RUN_LIVE_KB")
    if cell["corpus"] in _LIVE_KB_CORPORA:
        os.environ["RUN_LIVE_KB"] = "1"
    try:
        return run_comparison(
            cell["candidate"], cell["corpus"],
            purposes=cell["purposes"],
            pin_purposes=cell.get("pin_purposes"),
            case_ids=case_ids,
            write=write,
            write_subdir=_RESULT_SUBDIR,
        )
    finally:
        if prev_live is None:
            os.environ.pop("RUN_LIVE_KB", None)
        else:
            os.environ["RUN_LIVE_KB"] = prev_live


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--list":
        cells = all_cells()
        print(f"Cells ({len(cells)} total):")
        for name, cell in sorted(cells.items()):
            extra = ""
            if "pin_purposes" in cell:
                extra = f"  +pins={cell['pin_purposes']}"
            print(f"  {name:60s} corpus={cell['corpus']:32s} "
                  f"purposes={cell['purposes']}{extra}")
        return 0
    # `--smoke` runs each unique candidate against extraction_corpus on one
    # case (norm_001), exercising the per-purpose override path live. Cheap
    # (~$0.003 total at session prices).
    if argv[0] == "--smoke":
        from tests.evaluation.phase_e_comparison import run_comparison as rc
        smoked = list(_PRIMARY_CANDIDATES) + ["devstral-small-2", _SONNET_DERIVATION_ONLY]
        results = []
        for cand in smoked:
            print(f"\n=== smoke: {cand} ===", file=sys.stderr, flush=True)
            r = rc(cand, "extraction_corpus",
                   purposes=["extractor:user"],
                   case_ids=["norm_001"], write=False)
            results.append({
                "candidate": cand, "model": r["model"],
                "total_cases": r["total_cases"], "passed": r["passed"],
                "runner_errors": r["runner_errors"],
                "cost_usd": r["total_cost_usd"],
                "elapsed_seconds": r["elapsed_seconds"],
            })
        print("\n=== smoke summary ===")
        print(json.dumps(results, indent=2))
        return 0
    # `--batch <component>` runs all candidates for one component (3 primary +
    # walker also gets Sonnet) in a single process. The shared LRUHTTPCache
    # benefits the second and third candidates: live KB lookups for the same
    # entities are served from cache. For subsumption / entity_resolution /
    # derivation this cuts wall-clock by ~3x; for predicate_distribution /
    # predicate_metadata (no KB calls) it's neutral but still convenient.
    if argv[0] == "--batch":
        if len(argv) < 2:
            print("usage: phase_e5_runs --batch <component>", file=sys.stderr)
            return 2
        comp = argv[1]
        cells = all_cells()
        cell_names = [n for n in cells if n.endswith(f"__{comp}")]
        if not cell_names:
            print(f"no cells for component {comp!r}; known components: "
                  f"{sorted({n.split('__', 1)[1] for n in cells})}", file=sys.stderr)
            return 2
        for name in sorted(cell_names):
            print(f"\n=== {name} ===", file=sys.stderr, flush=True)
            r = run_cell(name)
            print(json.dumps(
                {k: v for k, v in r.items()
                 if k not in ("per_case_outcomes", "routing_override",
                              "pricing_verification", "pin_pricing_verifications")},
                indent=2, default=str,
            ))
        return 0
    cell = argv[0]
    case_ids = None
    if len(argv) > 1 and argv[1].startswith("--case-ids="):
        case_ids = argv[1].split("=", 1)[1].split(",")
    result = run_cell(cell, case_ids=case_ids)
    print(json.dumps(
        {k: v for k, v in result.items() if k != "per_case_outcomes"},
        indent=2, default=str,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
