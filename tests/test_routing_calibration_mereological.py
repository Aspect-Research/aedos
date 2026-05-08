"""Calibration test for the v0.14 extractor's mereological routing.

Runs the live extractor (Sonnet 4.6, via ``LLMClient.extract_with_tool``)
against ~25 hand-labeled cases covering:
  * clean constitutive parthood (part_of, member_of, composed_of,
    constitutes, subregion_of) — must extract as ``mereological``.
  * locational containment lookalikes (lives_in, located_in, "in"
    surface forms) — must NOT extract as mereological.
  * disambiguation pairs in a single sentence — must extract BOTH the
    mereological clause AND the spatial_temporal clause, in the right
    patterns.
  * negation, categorical-vs-mereological, person-vs-place edges.

Threshold: 85% per-case pass rate. Below that, the few-shot in
``src/layer1_extraction/extractor.py``'s SYSTEM_PROMPT_TEMPLATE
needs attention. The threshold is lower than predicate_equivalence's
90% because extraction errors are upstream of verification — a
misrouted mereological claim still gets verified, just under the
wrong pattern. Substrate oracles operate on stored facts where a
wrong call directly contaminates the store; extraction is a softer
boundary.

Gated behind ``RUN_API_TESTS=1``. Each case is one Anthropic API call.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

import pytest

from src.layer1_extraction.extractor import ClaimExtractor
from src.layer1_extraction.pattern_registry import (
    load_default_registry,
    reset_cache,
)


# Each case: text + expected pattern set (as (pattern, slots_subset)
# tuples). A case PASSES if every expected (pattern, slots_subset)
# is matched by some extracted fact, AND no extracted fact has a
# disallowed pattern.
@dataclass
class CalibrationCase:
    label: str
    text: str
    expected: list[tuple[str, dict]]
    # Patterns that, if extracted, fail the case immediately. Used to
    # catch mereological-misclassified-as-spatial_temporal and vice
    # versa.
    forbidden_patterns: list[str] | None = None


CALIBRATION_CASES: list[CalibrationCase] = [
    # ---- clean mereological (8) ----
    CalibrationCase(
        "part_of state — canonical",
        "Williamstown is part of Massachusetts.",
        [("mereological", {"part": "Williamstown", "whole": "Massachusetts"})],
        forbidden_patterns=["spatial_temporal"],
    ),
    CalibrationCase(
        "part_of country (constitutive)",
        "Tokyo is part of Japan.",
        [("mereological", {"part": "Tokyo", "whole": "Japan"})],
        forbidden_patterns=["spatial_temporal"],
    ),
    CalibrationCase(
        "part_of mechanical assembly",
        "The engine is part of the car.",
        [("mereological", {"part": "engine", "whole": "car"})],
        forbidden_patterns=["spatial_temporal"],
    ),
    CalibrationCase(
        "member_of named group",
        "Massachusetts is one of the New England states.",
        [("mereological", {"part": "Massachusetts"})],
        forbidden_patterns=["categorical"],
    ),
    CalibrationCase(
        "composed_of chemistry",
        "Water is composed of hydrogen and oxygen.",
        [("mereological", {"whole": "water"})],
    ),
    CalibrationCase(
        "subregion_of administrative division",
        "Berkshire County is a subregion of Massachusetts.",
        [("mereological", {"part": "Berkshire County",
                           "whole": "Massachusetts"})],
        forbidden_patterns=["spatial_temporal"],
    ),
    CalibrationCase(
        "constitutes biological",
        "Alveoli are part of the lungs.",
        [("mereological", {"part": "alveoli", "whole": "lungs"})],
        forbidden_patterns=["spatial_temporal"],
    ),
    CalibrationCase(
        "part_of historical territorial",
        "Crimea was part of Ukraine until 2014.",
        [("mereological", {"part": "Crimea", "whole": "Ukraine"})],
        forbidden_patterns=["spatial_temporal"],
    ),

    # ---- locational containment (must NOT be mereological) (8) ----
    CalibrationCase(
        "user lives_in (locational)",
        "Asa lives in Williamstown.",
        [("spatial_temporal", {"location": "Williamstown"})],
        forbidden_patterns=["mereological"],
    ),
    CalibrationCase(
        "tokyo IN japan (locational)",
        "Tokyo is in Japan.",
        [("spatial_temporal", {"entity": "Tokyo", "location": "Japan"})],
        forbidden_patterns=["mereological"],
    ),
    CalibrationCase(
        "engine IN car (placement)",
        "The engine is in the car.",
        [("spatial_temporal", {"entity": "engine", "location": "car"})],
        forbidden_patterns=["mereological"],
    ),
    CalibrationCase(
        "marie curie worked_at",
        "Marie Curie worked at the Sorbonne.",
        [("spatial_temporal", {"entity": "Marie Curie",
                               "location": "Sorbonne"})],
        forbidden_patterns=["mereological"],
    ),
    CalibrationCase(
        "located_in city",
        "The Eiffel Tower is located in Paris.",
        [("spatial_temporal", {"entity": "Eiffel Tower",
                               "location": "Paris"})],
        forbidden_patterns=["mereological"],
    ),
    CalibrationCase(
        "born_in",
        "Einstein was born in Ulm.",
        [],  # accept any reasonable extraction; main check is the
             # forbidden_patterns line below
        forbidden_patterns=["mereological"],
    ),
    CalibrationCase(
        "user lives_in (first person)",
        "I live in Boston.",
        [("spatial_temporal", {"location": "Boston"})],
        forbidden_patterns=["mereological"],
    ),
    CalibrationCase(
        "located_in country (locational)",
        "Paris is in France.",
        [("spatial_temporal", {"entity": "Paris", "location": "France"})],
        forbidden_patterns=["mereological"],
    ),

    # ---- disambiguation pairs (single sentence, both patterns) (4) ----
    CalibrationCase(
        "canonical disambiguation pair",
        "Williamstown is part of Massachusetts and Asa lives in Williamstown.",
        [
            ("mereological", {"part": "Williamstown",
                              "whole": "Massachusetts"}),
            ("spatial_temporal", {"location": "Williamstown"}),
        ],
    ),
    CalibrationCase(
        "categorical + mereological in one sentence",
        "Tokyo is a city that is part of Japan.",
        [
            ("categorical", {"entity": "Tokyo", "category": "city"}),
            ("mereological", {"part": "Tokyo", "whole": "Japan"}),
        ],
    ),
    CalibrationCase(
        "two mereological clauses",
        "Berkshire County is part of Massachusetts and Williamstown is part of Berkshire County.",
        [
            ("mereological", {"part": "Berkshire County",
                              "whole": "Massachusetts"}),
            ("mereological", {"part": "Williamstown",
                              "whole": "Berkshire County"}),
        ],
    ),
    CalibrationCase(
        "spatial_temporal + categorical (no mereological)",
        "Paris is a beautiful city in France.",
        [
            ("categorical", {"entity": "Paris", "category": "city"}),
            ("spatial_temporal", {"entity": "Paris", "location": "France"}),
        ],
        forbidden_patterns=["mereological"],
    ),

    # ---- negation, edge cases (5) ----
    CalibrationCase(
        "negated mereological",
        "Hawaii is not part of the contiguous United States.",
        [("mereological", {"part": "Hawaii"})],
    ),
    CalibrationCase(
        "negated member_of",
        "Switzerland is not a member of the European Union.",
        [("mereological", {"part": "Switzerland",
                           "whole": "European Union"})],
    ),
    CalibrationCase(
        "kind membership not mereological",
        "Tokyo is a city.",
        [("categorical", {"entity": "Tokyo", "category": "city"})],
        forbidden_patterns=["mereological"],
    ),
    CalibrationCase(
        "person 'lives in' boundary",
        "Carlos lives in Mexico City.",
        [("spatial_temporal", {"location": "Mexico City"})],
        forbidden_patterns=["mereological"],
    ),
    CalibrationCase(
        "abstain on aesthetic — no mereological invented",
        "The sunset was beautiful.",
        [],
        forbidden_patterns=["mereological"],
    ),
]


def _slots_subset_match(extracted_slots: dict, expected_subset: dict) -> bool:
    """expected_subset ⊆ extracted_slots, with case-insensitive string
    comparison so 'Massachusetts' matches 'massachusetts'."""
    for k, v in expected_subset.items():
        if k not in extracted_slots:
            return False
        ex = extracted_slots[k]
        if isinstance(v, str) and isinstance(ex, str):
            if v.lower() != ex.lower():
                return False
        else:
            if v != ex:
                return False
    return True


def _case_passes(case: CalibrationCase, valid_facts: list[dict]) -> tuple[bool, str]:
    """Return (passed, reason). Reason is empty when passed."""
    extracted_patterns = [f.get("pattern") for f in valid_facts]

    # Forbidden-pattern check first.
    for p in case.forbidden_patterns or []:
        if p in extracted_patterns:
            return (
                False,
                f"forbidden pattern {p!r} appeared in extraction "
                f"(extracted: {extracted_patterns})",
            )

    # Each expected (pattern, slots_subset) must be matched by some fact.
    for exp_pattern, exp_slots in case.expected:
        matched = any(
            f.get("pattern") == exp_pattern
            and _slots_subset_match(f.get("slots") or {}, exp_slots)
            for f in valid_facts
        )
        if not matched:
            return (
                False,
                f"expected ({exp_pattern!r}, {exp_slots}) not matched. "
                f"Extracted: {[(f.get('pattern'), f.get('slots')) for f in valid_facts]}",
            )

    return (True, "")


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="real API test gated behind RUN_API_TESTS=1",
)
def test_mereological_extraction_calibration():
    """≥85% of mereological calibration cases must extract correctly.

    Each failure is reported in the assertion message so a drift run
    surfaces all the regressions at once, not one at a time.
    """
    reset_cache()
    from src.llm_client import LLMClient

    extractor = ClaimExtractor(LLMClient(), load_default_registry())

    failures: list[tuple[str, str]] = []
    for case in CALIBRATION_CASES:
        result = extractor.extract(case.text, role="user")
        passed, reason = _case_passes(case, result.valid_facts)
        if not passed:
            failures.append((case.label, reason))

    total = len(CALIBRATION_CASES)
    passed = total - len(failures)
    threshold = math.ceil(0.85 * total)
    assert passed >= threshold, (
        f"mereological calibration regression: {passed}/{total} passed "
        f"(threshold {threshold} for 85%). Failures:\n  "
        + "\n  ".join(f"{label}: {reason}" for label, reason in failures)
    )
