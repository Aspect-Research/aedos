"""Doc-test (N7): the Phase 10.5 runbook's Step 4 threshold table must agree
with the calibration runner's THRESHOLDS dict — the single source of truth.

The runner dict is executable (asserted in CI under RUN_CALIBRATION); the
runbook table is operator-facing. This test fails if the two ever diverge, so a
threshold changed in one place is caught here rather than silently shipped —
the exact failure mode the original audit's M5 named (a silent threshold
divergence). This test is unmarked, so it runs in the default `make test`.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.calibration.test_corpus_runner import THRESHOLDS

_RUNBOOK = Path(__file__).parents[2] / "docs" / "v0_15" / "phase_10_5_runbook.md"

# A threshold-table row:  | `corpus_name` | NN% | plan bar |
_ROW = re.compile(r"^\|\s*`([a-z_]+)`\s*\|\s*(\d+)%\s*\|", re.MULTILINE)


def _parse_runbook_thresholds() -> dict[str, float]:
    text = _RUNBOOK.read_text(encoding="utf-8")
    return {m.group(1): int(m.group(2)) / 100 for m in _ROW.finditer(text)}


def test_runbook_table_parses():
    parsed = _parse_runbook_thresholds()
    assert parsed, "no threshold rows parsed from the runbook Step 4 table"


def test_runbook_table_covers_every_corpus():
    parsed = _parse_runbook_thresholds()
    assert set(parsed) == set(THRESHOLDS), (
        f"runbook table corpora {sorted(parsed)} != "
        f"runner THRESHOLDS {sorted(THRESHOLDS)}"
    )


def test_runbook_thresholds_match_runner():
    parsed = _parse_runbook_thresholds()
    for corpus, runner_value in sorted(THRESHOLDS.items()):
        assert corpus in parsed, f"{corpus} missing from the runbook Step 4 table"
        assert abs(parsed[corpus] - runner_value) < 1e-9, (
            f"{corpus}: runbook table says {parsed[corpus]:.0%} but the runner's "
            f"THRESHOLDS says {runner_value:.0%} — update one to match the other"
        )
