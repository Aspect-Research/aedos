"""Calibration test for the entity_taxonomy oracle.

Gated behind ``RUN_API_TESTS=1``. One Anthropic API call per gold
entry (62 calls per cold-cache run); memoization makes re-runs cheap.

The corpus lives in ``tests/v2/calibration/entity_taxonomy_gold.
jsonl``. 62 entries across seven categories. Per-category accuracy
floors weighted by structural difficulty:

  * is_a_clear                   - 0.85 (clean categorical chains)
  * part_of_clear                - 0.85 (clean compositional chains)
  * cross_relation_distractor    - 0.75 (relation_type mismatch)
  * over_subsumption_tempting    - 0.70 (case + ambiguity stress)
  * reverse_direction            - 0.80 (inversion-label correctness)
  * equivalent_level             - 0.60 (small-N; effectively
                                          "1 of 3 right is OK")
  * hard_cases                   - exempt (gold itself contestable)

Aggregate floor: 0.85.

The over_subsumption_tempting category is the watch-this-one. Wrong-
subsumption calls in this category would let Phase 7's derivation
walker propagate facts across links it shouldn't propagate through.
Landing at 0.70 is acceptable; 0.50 means the prompt's case-
disambiguation guidance isn't holding.

Per the iteration budget locked in the Phase 5 plan: if the first
calibration run lands below the aggregate floor, prompt revisions
are capped at 3. After that, surface the result rather than over-
iterate.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.fact_store import FactStore
from src.layer3_substrate.entity_taxonomy import (
    EntityTaxonomy,
)
from src.llm_client import LLMClient


_GOLD_PATH = (
    Path(__file__).parent / "calibration" /
    "entity_taxonomy_gold.jsonl"
)


_CATEGORY_FLOORS: dict[str, float] = {
    "is_a_clear":                  0.85,
    "part_of_clear":               0.85,
    "cross_relation_distractor":   0.75,
    "over_subsumption_tempting":   0.70,
    "reverse_direction":           0.80,
    "equivalent_level":            0.60,
    "hard_cases":                  0.0,  # exempt
}

_AGGREGATE_FLOOR = 0.85

_AGGREGATE_CATEGORIES = {
    cat for cat in _CATEGORY_FLOORS if cat != "hard_cases"
}


def _load_gold() -> list[dict]:
    rows: list[dict] = []
    with _GOLD_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def test_gold_corpus_loads_and_distribution_matches_plan():
    """Sanity check on corpus shape — runs unconditionally."""
    gold = _load_gold()
    assert len(gold) == 62, (
        f"calibration corpus must be exactly 62 entries; got {len(gold)}"
    )
    by_cat: dict[str, int] = {}
    for entry in gold:
        by_cat[entry["category"]] = by_cat.get(entry["category"], 0) + 1
    expected_distribution = {
        "is_a_clear":                 18,
        "part_of_clear":              14,
        "cross_relation_distractor":   9,
        "over_subsumption_tempting":   8,
        "reverse_direction":           6,
        "equivalent_level":            3,
        "hard_cases":                  4,
    }
    assert by_cat == expected_distribution, (
        f"category distribution drift: got {by_cat}, expected "
        f"{expected_distribution}"
    )


def test_gold_corpus_entries_are_well_formed():
    """Schema check: every gold entry has required fields and values
    are in the closed enums."""
    from src.layer3_substrate.entity_taxonomy import (
        LABELS, RELATION_TYPES,
    )
    gold = _load_gold()
    for entry in gold:
        for field in (
            "id", "category", "child", "parent", "relation_type",
            "expected_label",
        ):
            assert field in entry, (
                f"entry {entry.get('id')!r} missing field {field!r}"
            )
        assert entry["expected_label"] in LABELS, (
            f"entry {entry['id']!r} expected_label "
            f"{entry['expected_label']!r} not in {LABELS}"
        )
        assert entry["relation_type"] in RELATION_TYPES, (
            f"entry {entry['id']!r} relation_type "
            f"{entry['relation_type']!r} not in {RELATION_TYPES}"
        )
        assert entry["category"] in _CATEGORY_FLOORS, (
            f"entry {entry['id']!r} unknown category "
            f"{entry['category']!r}"
        )
        assert entry["child"] != entry["parent"], (
            f"entry {entry['id']!r} is a self-pair"
        )


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="live LLM calibration; gated behind RUN_API_TESTS=1",
)
def test_entity_taxonomy_calibration(tmp_path):
    """Run every gold entry through the live LLM oracle and report
    per-category + aggregate accuracy.

    Fails if:
      * Aggregate accuracy across non-exempt categories < 0.85.
      * Any non-exempt category drops below its per-category floor.

    Failure reporting prints per-category accuracies vs floors and
    the list of misclassified entries.
    """
    from dotenv import load_dotenv
    load_dotenv()

    gold = _load_gold()
    store = FactStore(tmp_path / "calibration.db")
    oracle = EntityTaxonomy(store)
    llm = LLMClient()

    results: list[dict] = []
    for entry in gold:
        verdict = oracle.consult(
            entry["child"], entry["parent"], entry["relation_type"],
            llm=llm,
        )
        correct = (
            not verdict.classification_failed
            and verdict.label == entry["expected_label"]
        )
        results.append({
            "id": entry["id"],
            "category": entry["category"],
            "expected_label": entry["expected_label"],
            "actual_label": verdict.label,
            "classification_failed": verdict.classification_failed,
            "correct": correct,
            "reason": verdict.reason,
        })

    by_cat: dict[str, list[dict]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)

    per_cat_acc: dict[str, float] = {}
    for cat, rs in by_cat.items():
        per_cat_acc[cat] = sum(1 for r in rs if r["correct"]) / len(rs)

    aggregate_correct = sum(
        1 for r in results
        if r["category"] in _AGGREGATE_CATEGORIES and r["correct"]
    )
    aggregate_total = sum(
        1 for r in results if r["category"] in _AGGREGATE_CATEGORIES
    )
    aggregate_acc = aggregate_correct / aggregate_total

    report_lines = [
        "",
        "=" * 70,
        "entity_taxonomy calibration report",
        "=" * 70,
    ]
    for cat in sorted(by_cat.keys()):
        floor = _CATEGORY_FLOORS[cat]
        acc = per_cat_acc[cat]
        flag = "OK " if (cat == "hard_cases" or acc >= floor) else "FAIL"
        report_lines.append(
            f"  [{flag}] {cat:32s} "
            f"acc={acc:.3f}  floor={floor:.2f}  "
            f"({sum(1 for r in by_cat[cat] if r['correct'])}/"
            f"{len(by_cat[cat])})"
        )
    report_lines.append("-" * 70)
    flag = "OK " if aggregate_acc >= _AGGREGATE_FLOOR else "FAIL"
    report_lines.append(
        f"  [{flag}] AGGREGATE (excl. hard_cases)    "
        f"acc={aggregate_acc:.3f}  floor={_AGGREGATE_FLOOR:.2f}  "
        f"({aggregate_correct}/{aggregate_total})"
    )
    report_lines.append("=" * 70)

    misses = [r for r in results if not r["correct"]]
    if misses:
        report_lines.append(f"\n{len(misses)} misclassifications:")
        for m in misses:
            report_lines.append(
                f"  {m['id']:20s} [{m['category']:28s}] "
                f"expected {m['expected_label']!r:28s} -> "
                f"actual {m['actual_label']!r}"
            )

    print("\n".join(report_lines))

    failed_categories = [
        cat for cat in _AGGREGATE_CATEGORIES
        if per_cat_acc[cat] < _CATEGORY_FLOORS[cat]
    ]
    assert not failed_categories, (
        "entity_taxonomy calibration failed per-category floors: "
        + ", ".join(
            f"{cat}={per_cat_acc[cat]:.3f}<{_CATEGORY_FLOORS[cat]:.2f}"
            for cat in failed_categories
        )
        + ". See full report above."
    )
    assert aggregate_acc >= _AGGREGATE_FLOOR, (
        f"entity_taxonomy calibration aggregate accuracy "
        f"{aggregate_acc:.3f} below floor {_AGGREGATE_FLOOR:.2f}. "
        f"See full report above."
    )
