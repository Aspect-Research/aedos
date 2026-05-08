"""Calibration test for the predicate_distribution oracle.

Gated behind ``RUN_API_TESTS=1``. One Anthropic API call per gold
entry (50 calls per cold-cache run); memoization makes re-runs cheap.

The corpus lives in ``tests/v2/calibration/predicate_distribution_
gold.jsonl``. 50 entries across six categories with varied per-
category accuracy floors:

  * distributes_down_is_a    - 0.80 (categorical attitudes)
  * distributes_up_part_of   - 0.80 (compositional residence)
  * neither_property         - 0.75 (individual properties)
  * directional_asymmetry    - 0.75 (paired entries — same predicate
                                     under different relation_types
                                     must yield different labels)
  * polarity_sensitive       - 0.65 (polarity reasoning is the
                                     hardest category)
  * hard_cases               - exempt (causal/modal predicates;
                                     gold contestable)

Aggregate floor: 0.85.

The labels here are inferential, not lexical (Phase 3-4's oracles
ask paraphrase questions; this oracle asks logical-propagation
questions). The first calibration run may need 1-3 prompt iterations
to clear 0.85. Per the iteration budget locked in the Phase 5 plan,
revisions are capped at 3 — surface honest results rather than
prompt-engineer through the floor.

The directional_asymmetry category is paired by design — each "pair"
is two corpus rows ((same predicate, different relation_type),
expected to yield different labels). The scorer counts each row
independently, but a misclassification on either half of a pair
indicates the prompt's is_a-vs-part_of contrast isn't holding.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.fact_store import FactStore
from src.layer3_substrate.predicate_distribution import (
    PredicateDistribution,
)
from src.llm_client import LLMClient


_GOLD_PATH = (
    Path(__file__).parent / "calibration" /
    "predicate_distribution_gold.jsonl"
)


_CATEGORY_FLOORS: dict[str, float] = {
    "distributes_down_is_a":    0.80,
    "distributes_up_part_of":   0.80,
    "neither_property":         0.75,
    "directional_asymmetry":    0.75,
    "polarity_sensitive":       0.65,
    "hard_cases":               0.0,  # exempt
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
    assert len(gold) == 50, (
        f"calibration corpus must be exactly 50 entries; got {len(gold)}"
    )
    by_cat: dict[str, int] = {}
    for entry in gold:
        by_cat[entry["category"]] = by_cat.get(entry["category"], 0) + 1
    expected_distribution = {
        "distributes_down_is_a":    11,
        "distributes_up_part_of":    9,
        "neither_property":          8,
        "directional_asymmetry":     8,
        "polarity_sensitive":        8,
        "hard_cases":                6,
    }
    assert by_cat == expected_distribution, (
        f"category distribution drift: got {by_cat}, expected "
        f"{expected_distribution}"
    )


def test_gold_corpus_entries_are_well_formed():
    """Schema check: every gold entry has required fields and values
    are in the closed enums; pattern is in the registry."""
    from src.layer3_substrate.predicate_distribution import (
        LABELS, POLARITIES, RELATION_TYPES,
    )
    from src.layer1_extraction.pattern_registry import (
        load_default_registry,
    )
    registry = load_default_registry()
    valid_patterns = set(registry.names())
    gold = _load_gold()
    for entry in gold:
        for field in (
            "id", "category", "pattern", "predicate", "polarity",
            "taxonomy_relation_type", "expected_label",
        ):
            assert field in entry, (
                f"entry {entry.get('id')!r} missing field {field!r}"
            )
        assert entry["pattern"] in valid_patterns, (
            f"entry {entry['id']!r} unknown pattern "
            f"{entry['pattern']!r}"
        )
        assert entry["polarity"] in POLARITIES, (
            f"entry {entry['id']!r} polarity {entry['polarity']!r} "
            f"not in {POLARITIES}"
        )
        assert entry["taxonomy_relation_type"] in RELATION_TYPES, (
            f"entry {entry['id']!r} taxonomy_relation_type "
            f"{entry['taxonomy_relation_type']!r} not in "
            f"{RELATION_TYPES}"
        )
        assert entry["expected_label"] in LABELS, (
            f"entry {entry['id']!r} expected_label "
            f"{entry['expected_label']!r} not in {LABELS}"
        )
        assert entry["category"] in _CATEGORY_FLOORS, (
            f"entry {entry['id']!r} unknown category "
            f"{entry['category']!r}"
        )


def test_directional_asymmetry_entries_are_paired():
    """Sanity check on the corpus structure: every directional_
    asymmetry entry whose ID ends in 'a' must have a matching 'b'
    entry with the same prefix, same predicate, same polarity, and
    a different relation_type."""
    gold = _load_gold()
    asym = [e for e in gold if e["category"] == "directional_asymmetry"]
    a_entries = [e for e in asym if e["id"].endswith("a")]
    b_entries = [e for e in asym if e["id"].endswith("b")]
    assert len(a_entries) == len(b_entries), (
        f"directional_asymmetry pairing broken: "
        f"{len(a_entries)} a-entries vs {len(b_entries)} b-entries"
    )
    a_by_prefix = {e["id"][:-1]: e for e in a_entries}
    for b in b_entries:
        prefix = b["id"][:-1]
        assert prefix in a_by_prefix, (
            f"b-entry {b['id']!r} has no matching a-entry"
        )
        a = a_by_prefix[prefix]
        assert a["predicate"] == b["predicate"], (
            f"pair {prefix!r} predicate mismatch: "
            f"{a['predicate']!r} vs {b['predicate']!r}"
        )
        assert a["polarity"] == b["polarity"], (
            f"pair {prefix!r} polarity mismatch"
        )
        assert (
            a["taxonomy_relation_type"]
            != b["taxonomy_relation_type"]
        ), (
            f"pair {prefix!r} should test DIFFERENT relation_types"
        )


def test_polarity_sensitive_entries_are_paired():
    """Sanity check: every polarity_sensitive entry whose ID ends
    in 'a' must have a matching 'b' entry with the same prefix,
    same predicate, same relation_type, and a different polarity."""
    gold = _load_gold()
    pol = [e for e in gold if e["category"] == "polarity_sensitive"]
    a_entries = [e for e in pol if e["id"].endswith("a")]
    b_entries = [e for e in pol if e["id"].endswith("b")]
    assert len(a_entries) == len(b_entries)
    a_by_prefix = {e["id"][:-1]: e for e in a_entries}
    for b in b_entries:
        prefix = b["id"][:-1]
        assert prefix in a_by_prefix
        a = a_by_prefix[prefix]
        assert a["predicate"] == b["predicate"]
        assert (
            a["taxonomy_relation_type"]
            == b["taxonomy_relation_type"]
        )
        assert a["polarity"] != b["polarity"], (
            f"pair {prefix!r} should test DIFFERENT polarities"
        )


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="live LLM calibration; gated behind RUN_API_TESTS=1",
)
def test_predicate_distribution_calibration(tmp_path):
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
    oracle = PredicateDistribution(store)
    llm = LLMClient()

    results: list[dict] = []
    for entry in gold:
        verdict = oracle.consult(
            entry["pattern"],
            entry["predicate"],
            entry["polarity"],
            entry["taxonomy_relation_type"],
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
        "predicate_distribution calibration report",
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
                f"expected {m['expected_label']!r:20s} -> "
                f"actual {m['actual_label']!r}"
            )

    print("\n".join(report_lines))

    failed_categories = [
        cat for cat in _AGGREGATE_CATEGORIES
        if per_cat_acc[cat] < _CATEGORY_FLOORS[cat]
    ]
    assert not failed_categories, (
        "predicate_distribution calibration failed per-category "
        "floors: "
        + ", ".join(
            f"{cat}={per_cat_acc[cat]:.3f}<{_CATEGORY_FLOORS[cat]:.2f}"
            for cat in failed_categories
        )
        + ". See full report above."
    )
    assert aggregate_acc >= _AGGREGATE_FLOOR, (
        f"predicate_distribution calibration aggregate accuracy "
        f"{aggregate_acc:.3f} below floor {_AGGREGATE_FLOOR:.2f}. "
        f"See full report above."
    )
