"""Calibration test for the predicate_equivalence oracle.

Gated behind ``RUN_API_TESTS=1``. One Anthropic API call per gold
entry (70 calls total per run); the oracle's memoization means
re-running on the same database doesn't re-hit the API.

The corpus lives in ``tests/v2/calibration/predicate_equivalence_gold.
jsonl``. Each line is a JSON object:

  {
    "id": "pe-anti-001",
    "category": "antonym_polarity_flip",
    "pattern": "preference",
    "predicate_a": "likes",
    "predicate_b": "dislikes",
    "expected_label": "contradictory",
    "expected_slot_reversal": "none",
    "notes": "..."
  }

Six categories with VARIED per-category accuracy floors. The
architectural commitment is that wrong-equivalent / wrong-
contradictory calls are direct contamination of the store while
wrong-distinct calls are just cache misses, so the floors weight
toward catching the high-cost mistakes:

  * antonym_polarity_flip      - 0.85 (stark contrasts; the LLM
                                       should nail these)
  * active_passive_slot_reversal - 0.75 (slot reversal is harder)
  * distinct_but_related       - 0.70 (distinct is the hardest
                                       label to predict consistently)
  * trivially_equivalent_surface - 0.85 (these should be easy)
  * over_merge_tempting        - 0.60 (the conservative-bias check;
                                       some over-merging is expected)
  * hard_cases                 - exempt (gold itself is contestable)

Aggregate floor: 0.90 across the non-exempt categories.

A run reports per-category accuracy AND the aggregate. A 91%
aggregate that hides 30% on slot_reversal is failing per the
varied-floor design.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.fact_store import FactStore
from src.layer3_substrate.predicate_equivalence import (
    PredicateEquivalence,
)
from src.llm_client import LLMClient


_GOLD_PATH = (
    Path(__file__).parent / "calibration" /
    "predicate_equivalence_gold.jsonl"
)


# Per-category accuracy floors. Aggregate floor is enforced
# separately in the test below.
_CATEGORY_FLOORS: dict[str, float] = {
    "antonym_polarity_flip":         0.85,
    "active_passive_slot_reversal":  0.75,
    "distinct_but_related":          0.70,
    "trivially_equivalent_surface":  0.85,
    "over_merge_tempting":           0.60,
    "hard_cases":                    0.0,  # exempt from per-category floor
}

_AGGREGATE_FLOOR = 0.90

# Categories that DO contribute to the aggregate accuracy. hard_cases
# is excluded both from per-category and from aggregate gates because
# the gold itself is contestable; a low score there is signal but
# not a regression.
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
    """Sanity check on the corpus shape — runs on every CI invocation
    (no RUN_API_TESTS gate). If someone removes a category by
    accident or the JSONL gets corrupted, we want to know fast."""
    gold = _load_gold()
    assert len(gold) == 70, (
        f"calibration corpus must be exactly 70 entries; got {len(gold)}"
    )
    by_cat: dict[str, int] = {}
    for entry in gold:
        by_cat[entry["category"]] = by_cat.get(entry["category"], 0) + 1
    expected_distribution = {
        "antonym_polarity_flip":         15,
        "active_passive_slot_reversal":  15,
        "distinct_but_related":          15,
        "trivially_equivalent_surface":  10,
        "hard_cases":                    10,
        "over_merge_tempting":            5,
    }
    assert by_cat == expected_distribution, (
        f"category distribution drift: got {by_cat}, expected "
        f"{expected_distribution}"
    )


def test_gold_corpus_entries_are_well_formed():
    """Schema check: every gold entry has the required fields and
    values are in the closed enums."""
    from src.layer3_substrate.predicate_equivalence import (
        LABELS, SLOT_REVERSALS,
    )
    from src.layer1_extraction.pattern_registry import (
        load_default_registry,
    )
    registry = load_default_registry()
    valid_patterns = set(registry.names())
    gold = _load_gold()
    for entry in gold:
        for field in ("id", "category", "pattern", "predicate_a",
                      "predicate_b", "expected_label",
                      "expected_slot_reversal"):
            assert field in entry, (
                f"entry {entry.get('id')!r} missing field {field!r}"
            )
        assert entry["pattern"] in valid_patterns, (
            f"entry {entry['id']!r} has unknown pattern {entry['pattern']!r}"
        )
        assert entry["expected_label"] in LABELS
        assert entry["expected_slot_reversal"] in SLOT_REVERSALS
        assert entry["category"] in _CATEGORY_FLOORS
        assert entry["predicate_a"] != entry["predicate_b"], (
            f"entry {entry['id']!r} is a self-pair"
        )


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="live LLM calibration; gated behind RUN_API_TESTS=1",
)
def test_predicate_equivalence_calibration(tmp_path):
    """Run every gold entry through the live LLM oracle and report
    per-category + aggregate accuracy.

    The test fails if:
      * The aggregate accuracy across non-exempt categories drops
        below 0.90.
      * Any non-exempt category drops below its per-category floor.

    Failure mode reporting (printed on assertion error):
      * Per-category accuracies, formatted alongside their floors.
      * The list of misclassified entries with expected vs actual
        labels.
    """
    # Load .env so the live API key is in os.environ. Tests
    # invoked via pytest don't go through src/app.py's load_dotenv().
    from dotenv import load_dotenv
    load_dotenv()

    gold = _load_gold()
    store = FactStore(tmp_path / "calibration.db")
    oracle = PredicateEquivalence(store)
    llm = LLMClient()

    results: list[dict] = []
    for entry in gold:
        verdict = oracle.consult(
            entry["pattern"],
            entry["predicate_a"],
            entry["predicate_b"],
            llm=llm,
        )
        # We treat slot_reversal correctness as part of the
        # classification. A "right label, wrong slot_reversal"
        # outcome counts as INCORRECT — that's the wrong-merge or
        # wrong-swap failure mode the oracle exists to prevent.
        correct = (
            not verdict.classification_failed
            and verdict.label == entry["expected_label"]
            and verdict.slot_reversal == entry["expected_slot_reversal"]
        )
        results.append({
            "id": entry["id"],
            "category": entry["category"],
            "expected_label": entry["expected_label"],
            "expected_slot_reversal": entry["expected_slot_reversal"],
            "actual_label": verdict.label,
            "actual_slot_reversal": verdict.slot_reversal,
            "classification_failed": verdict.classification_failed,
            "correct": correct,
            "reason": verdict.reason,
        })

    # ---- Per-category accuracy ------------------------------------------
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

    # ---- Print the report (visible in pytest -s) ------------------------
    report_lines = [
        "",
        "=" * 70,
        "predicate_equivalence calibration report",
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

    # ---- Misclassifications ---------------------------------------------
    misses = [r for r in results if not r["correct"]]
    if misses:
        report_lines.append(f"\n{len(misses)} misclassifications:")
        for m in misses:
            report_lines.append(
                f"  {m['id']:18s} [{m['category']:32s}] "
                f"expected ({m['expected_label']}, "
                f"{m['expected_slot_reversal']}) -> "
                f"actual ({m['actual_label']}, "
                f"{m['actual_slot_reversal']})"
            )

    print("\n".join(report_lines))

    # ---- Hard assertions ------------------------------------------------
    failed_categories = [
        cat for cat in _AGGREGATE_CATEGORIES
        if per_cat_acc[cat] < _CATEGORY_FLOORS[cat]
    ]
    assert not failed_categories, (
        "predicate_equivalence calibration failed per-category floors: "
        + ", ".join(
            f"{cat}={per_cat_acc[cat]:.3f}<{_CATEGORY_FLOORS[cat]:.2f}"
            for cat in failed_categories
        )
        + ". See full report above."
    )
    assert aggregate_acc >= _AGGREGATE_FLOOR, (
        f"predicate_equivalence calibration aggregate accuracy "
        f"{aggregate_acc:.3f} below floor {_AGGREGATE_FLOOR:.2f}. "
        f"See full report above."
    )
