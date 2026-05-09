"""Live calibration test for the v0.14 claim extractor (pattern accuracy).

Runs ``ClaimExtractor.extract`` against the curated corpus at
``tests/calibration/extraction_corpus.jsonl`` and asserts the
extractor picks the expected pattern for each entry. Gated behind
``RUN_API_TESTS=1`` — fires the real LLM (one call per corpus entry).

Floor: 0.90 for pattern accuracy. Below this floor pattern errors
cascade into wrong routing → wrong verifier → poisoned cache. The
corpus is heavy on the disambiguation boundaries (role_assignment vs
categorical, relational vs quantitative, mereological vs
spatial_temporal, propositional_attitude vs preference) where the
extractor is most prone to misclassify.

Abstain entries (expected_pattern == null) require the extractor to
return zero facts — those test the disciplined abstention principle
(aesthetic / evaluative claims, questions).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.layer1_extraction.extractor import ClaimExtractor
from src.layer1_extraction.pattern_registry import (
    load_default_registry,
    reset_cache,
)
from src.llm_client import LLMClient


CORPUS_PATH = Path(__file__).parent / "calibration" / "extraction_corpus.jsonl"
ACCURACY_FLOOR = 0.90


def _load_corpus() -> list[dict]:
    out: list[dict] = []
    for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_cache()
    yield
    reset_cache()


@pytest.mark.skipif(
    not os.getenv("RUN_API_TESTS"),
    reason="live LLM extraction calibration; gated behind RUN_API_TESTS=1",
)
def test_extraction_pattern_accuracy_meets_floor():
    corpus = _load_corpus()
    assert corpus, f"empty corpus at {CORPUS_PATH}"

    llm = LLMClient()
    extractor = ClaimExtractor(llm, load_default_registry())

    correct = 0
    misses: list[tuple[str, str | None, list[str]]] = []

    for entry in corpus:
        result = extractor.extract(entry["text"], role="user")
        expected = entry["expected_pattern"]
        actual_patterns = [f["pattern"] for f in result.valid_facts]
        if expected is None:
            # Abstain case: extractor must produce zero facts.
            if not result.valid_facts:
                correct += 1
            else:
                misses.append((entry["id"], "<abstain>", actual_patterns))
        else:
            # The expected pattern must appear in the extracted set
            # (the corpus mostly targets single-fact inputs, but the
            # disambig pair test may produce two facts; we only check
            # presence of the labeled pattern).
            if expected in actual_patterns:
                correct += 1
            else:
                misses.append((entry["id"], expected, actual_patterns))

    accuracy = correct / len(corpus)
    miss_lines = "\n".join(
        f"  {mid}: expected {exp!r}, got {got!r}"
        for mid, exp, got in misses
    )
    assert accuracy >= ACCURACY_FLOOR, (
        f"pattern accuracy {accuracy:.3f} below floor {ACCURACY_FLOOR}\n"
        f"correct: {correct}/{len(corpus)}\n"
        f"misses ({len(misses)}):\n{miss_lines}"
    )
