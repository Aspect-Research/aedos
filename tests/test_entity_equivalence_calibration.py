"""Calibration test for the entity_equivalence oracle.

Gated behind ``RUN_API_TESTS=1``. One Anthropic API call per gold
entry (50 calls per cold-cache run); memoization makes re-runs cheap.

The corpus lives in ``tests/v2/calibration/entity_equivalence_gold.
jsonl``. Distribution: 28 same + 17 different + 5 hard. The
``different`` cases are the high-stakes ones — wrong-same calls
admit false equivalences in the verified store, while wrong-
different calls just cost a cache miss. The corpus is weighted
toward stress-testing the prompt's conservative bias on
over-merge-tempting cases (Tokyo/Japan, Apple/iPhone, NSA/NASA).

Per-category accuracy floors. Aggregate floor: 0.85.

  * alias_resolution         - 0.85 (well-known aliases)
  * abbreviation             - 0.90 (highest floor — easiest signal)
  * alternate_spelling       - 0.75 (world-knowledge dependent)
  * case_disambiguation      - 0.80 (case-sensitivity discipline)
  * person_vs_place          - 0.80 (world knowledge)
  * over_merge_tempting      - 0.70 (conservative-bias check)
  * hard_cases               - exempt (gold contestable)

The over_merge_tempting category is the watch-this-one. Landing at
0.70 means the model is doing what the prompt instructs (prefer
different on uncertainty). 0.90+ means the prompt's conservative
bias is stronger than expected; below 0.50 means the model can't
resist over-merging despite the prompt's guidance.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.fact_store import FactStore
from src.layer3_substrate.entity_equivalence import (
    EntityEquivalence,
)
from src.llm_client import LLMClient


_GOLD_PATH = (
    Path(__file__).parent / "calibration" /
    "entity_equivalence_gold.jsonl"
)


_CATEGORY_FLOORS: dict[str, float] = {
    "alias_resolution":     0.85,
    "abbreviation":         0.90,
    "alternate_spelling":   0.75,
    "case_disambiguation":  0.80,
    "person_vs_place":      0.80,
    "over_merge_tempting":  0.70,
    "hard_cases":           0.0,  # exempt
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
    """Sanity check on corpus shape — runs on every CI invocation."""
    gold = _load_gold()
    assert len(gold) == 50, (
        f"calibration corpus must be exactly 50 entries; got {len(gold)}"
    )
    by_cat: dict[str, int] = {}
    for entry in gold:
        by_cat[entry["category"]] = by_cat.get(entry["category"], 0) + 1
    expected_distribution = {
        "alias_resolution":     12,
        "abbreviation":          8,
        "alternate_spelling":    8,
        "case_disambiguation":   5,
        "person_vs_place":       5,
        "over_merge_tempting":   7,
        "hard_cases":            5,
    }
    assert by_cat == expected_distribution, (
        f"category distribution drift: got {by_cat}, expected "
        f"{expected_distribution}"
    )


def test_gold_corpus_entries_are_well_formed():
    """Schema check: every gold entry has required fields and values
    are in the closed enums."""
    from src.layer3_substrate.entity_equivalence import LABELS
    gold = _load_gold()
    for entry in gold:
        for field in ("id", "category", "entity_a", "entity_b",
                      "expected_label"):
            assert field in entry, (
                f"entry {entry.get('id')!r} missing field {field!r}"
            )
        assert entry["expected_label"] in LABELS
        assert entry["category"] in _CATEGORY_FLOORS
        assert entry["entity_a"] != entry["entity_b"], (
            f"entry {entry['id']!r} is a self-pair"
        )


def test_gold_corpus_future_match_via_only_on_overmerge():
    """The future_match_via annotation is only meaningful for cases
    where Phase 5/7's entity_taxonomy + derivation will eventually
    MATCH (containment cases like Tokyo/Japan). It must NOT appear
    on case_disambiguation or person_vs_place entries — those are
    genuinely different entities, not part-of relationships."""
    gold = _load_gold()
    for entry in gold:
        if "future_match_via" in entry:
            assert entry["category"] == "over_merge_tempting", (
                f"entry {entry['id']!r}: future_match_via only "
                f"applies to over_merge_tempting (containment); "
                f"got category={entry['category']!r}"
            )


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="live LLM calibration; gated behind RUN_API_TESTS=1",
)
def test_entity_equivalence_calibration(tmp_path):
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
    oracle = EntityEquivalence(store)
    llm = LLMClient()

    results: list[dict] = []
    for entry in gold:
        verdict = oracle.consult(
            entry["entity_a"], entry["entity_b"], llm=llm,
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
        "entity_equivalence calibration report",
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
                f"  {m['id']:24s} [{m['category']:24s}] "
                f"expected {m['expected_label']!r:14s} -> "
                f"actual {m['actual_label']!r}"
            )

    print("\n".join(report_lines))

    failed_categories = [
        cat for cat in _AGGREGATE_CATEGORIES
        if per_cat_acc[cat] < _CATEGORY_FLOORS[cat]
    ]
    assert not failed_categories, (
        "entity_equivalence calibration failed per-category floors: "
        + ", ".join(
            f"{cat}={per_cat_acc[cat]:.3f}<{_CATEGORY_FLOORS[cat]:.2f}"
            for cat in failed_categories
        )
        + ". See full report above."
    )
    assert aggregate_acc >= _AGGREGATE_FLOOR, (
        f"entity_equivalence calibration aggregate accuracy "
        f"{aggregate_acc:.3f} below floor {_AGGREGATE_FLOOR:.2f}. "
        f"See full report above."
    )
