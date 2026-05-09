"""Live calibration test for the cache stability classifier (v0.14.1).

Runs ``classify_stability`` against the curated corpus at
``tests/calibration/stability_corpus.jsonl`` and asserts the
classifier picks the expected stability bin for each entry. Gated
behind ``RUN_API_TESTS=1``: the test fires the real LLM (one call
per non-shortcut entry, plus the deterministic shortcut path for the
rest).

Floor: 0.85. Below this floor the bin-picking errors compound into
TTL drift — months_stable claims getting cached for a year, etc.
The corpus is heavy on the borderline cases (biological facts,
changing corporate metrics, current flagship products) where nano
struggled.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.cache.stability_classifier import (
    STABILITY_CLASSES,
    classify_stability,
)
from src.llm_client import LLMClient


CORPUS_PATH = Path(__file__).parent / "calibration" / "stability_corpus.jsonl"
ACCURACY_FLOOR = 0.85


def _load_corpus() -> list[dict]:
    out: list[dict] = []
    for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


@pytest.mark.skipif(
    not os.getenv("RUN_API_TESTS"),
    reason="live LLM stability calibration; gated behind RUN_API_TESTS=1",
)
def test_stability_classifier_meets_calibration_floor():
    corpus = _load_corpus()
    assert corpus, f"empty corpus at {CORPUS_PATH}"

    llm = LLMClient()
    correct = 0
    misses: list[tuple[str, str, str]] = []

    for entry in corpus:
        decision = classify_stability(entry["claim"], llm)
        assert decision.stability_class in STABILITY_CLASSES
        if decision.stability_class == entry["expected"]:
            correct += 1
        else:
            misses.append((entry["id"],
                           entry["expected"],
                           decision.stability_class))

    accuracy = correct / len(corpus)
    miss_lines = "\n".join(
        f"  {mid}: expected {exp}, got {got}" for mid, exp, got in misses
    )
    assert accuracy >= ACCURACY_FLOOR, (
        f"accuracy {accuracy:.3f} below floor {ACCURACY_FLOOR}\n"
        f"correct: {correct}/{len(corpus)}\n"
        f"misses ({len(misses)}):\n{miss_lines}"
    )
