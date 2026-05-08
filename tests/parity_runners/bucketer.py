"""Bucket assignment for the Phase 9 parity audit.

Five-bucket scheme (per the 9a planning conversation):

  V2_ONLY_BY_DESIGN    — shape has no v1 equivalent path; v2 attempts.
                          v2 PASS → bucket. v2 FAIL → still v2-only by
                          design *plus* a v2 bug, but the bucketer
                          places it here and the report's per-entry
                          status flags the v2 fail.

  EXPECTED_DIVERGENCE  — v1 and v2 disagree, the entry is annotated
                          in the registry with a non-None divergence
                          kind (mereological_pattern, predicate_
                          equivalence, entity_equivalence,
                          session_local, derivation, ...). v2 should
                          be the architecturally favored side.

  UNEXPECTED_DIVERGENCE — v1 and v2 disagree, the registry says no
                          divergence is expected for this entry.
                          ↑↑↑ THE CUTOVER GATE ↑↑↑
                          A non-zero count blocks the rename surgery.

  BOTH_PASS            — v1 and v2 both produce the corpus's expected
                          outcome. Confirms parity on shared
                          functionality.

  BOTH_FAIL            — both stacks fail the same entry. Pre-existing
                          limitations (or a corpus annotation that
                          doesn't reflect either stack's reality);
                          surfaces for visibility but doesn't gate the
                          cutover.

The function is deliberately small and table-driven so the policy
is auditable from one place.
"""

from __future__ import annotations

from typing import Optional

from tests.parity_runners.types import (
    Bucket,
    EntryOutcome,
    StackResult,
    StackVerdict,
)
from tests.smoke_dispatcher import SmokeEntryShape, detect_shape


_V2_ONLY_SHAPES = {
    SmokeEntryShape.SUBSTRATE_DIRECT,
    SmokeEntryShape.TWO_TEXT_ORACLE,
    SmokeEntryShape.ROUTING_MEMO,
}


def _is_pass(verdict: StackVerdict) -> bool:
    return verdict.result is StackResult.PASS


def _is_attempt(verdict: StackVerdict) -> bool:
    """An attempt = the stack tried to handle the entry. NOT_APPLICABLE
    means it didn't try at all (that's expected for v2-only shapes).
    PASS / FAIL / ERROR all count as attempts."""
    return verdict.result is not StackResult.NOT_APPLICABLE


def assign_bucket(
    entry: dict,
    v1: StackVerdict,
    v2: StackVerdict,
    expected_divergence_kind: Optional[str],
) -> Bucket:
    """Compute the bucket for one entry given both stacks' verdicts."""
    shape = detect_shape(entry)
    if shape in _V2_ONLY_SHAPES and not _is_attempt(v1):
        # v1 correctly declined to attempt; bucket on v2's behavior.
        return Bucket.V2_ONLY_BY_DESIGN

    v1_pass = _is_pass(v1)
    v2_pass = _is_pass(v2)
    if v1_pass and v2_pass:
        return Bucket.BOTH_PASS
    if (not v1_pass) and (not v2_pass):
        return Bucket.BOTH_FAIL
    # Mixed — exactly one passed.
    if expected_divergence_kind is not None:
        return Bucket.EXPECTED_DIVERGENCE
    return Bucket.UNEXPECTED_DIVERGENCE


def make_outcome(
    entry: dict,
    v1: StackVerdict,
    v2: StackVerdict,
    expected_divergence_kind: Optional[str],
) -> EntryOutcome:
    bucket = assign_bucket(entry, v1, v2, expected_divergence_kind)
    shape = detect_shape(entry)
    return EntryOutcome(
        entry_id=entry["id"],
        shape=shape.value if shape else "unknown",
        expected_divergence_kind=expected_divergence_kind,
        v1=v1,
        v2=v2,
        bucket=bucket,
    )
