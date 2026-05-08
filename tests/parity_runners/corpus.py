"""Corpus loading + the expected-divergence registry.

Loading. ``load_corpus()`` reads ``tests/v2/smoke_corpus.jsonl``,
validates each entry against the dispatcher's schema, and returns
the entries in file order. The schema validation is the same one
that ``tests/v2/test_smoke_dispatcher.py`` runs as a regression
check; any unexpected shape is caught before the runners see it.

Expected-divergence registry. ``EXPECTED_DIVERGENCE_BY_ENTRY_ID``
documents which entries are expected to land outside the
"both-stacks-agree" buckets, and the architectural reason. The
bucketer (``bucketer.py``) reads this map. None means "no expected
divergence — the bucketer should land BOTH_PASS or BOTH_FAIL based
on observed outcomes."

Substrate fixtures. ``SUBSTRATE_FIXTURE`` describes the substrate
rows that ASSISTANT_LOOKUP entries depend on. The v2 runner pre-
populates these before walking the claim. The map is hand-curated
from the corpus's ``expected_oracles_consulted`` and the canonical
labels documented in the corpus notes; any new ASSISTANT_LOOKUP
entry that consults oracles must add a fixture entry here. Without
this pre-population, the audit would either need live LLM calls
(non-deterministic, expensive) or the walker would always miss
through derivation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from tests.smoke_dispatcher import (
    SmokeEntryShape,
    detect_shape,
    validate_corpus,
)


CORPUS_PATH = Path(__file__).resolve().parents[1] / "smoke_corpus.jsonl"


def load_corpus() -> list[dict]:
    """Load the smoke corpus in file order.

    Validates each entry against the dispatcher schema; if any entry
    fails validation, raises ``ValueError`` so the audit can surface
    the schema bug rather than running against malformed input.
    """
    result = validate_corpus(CORPUS_PATH)
    if not result.ok:
        msgs: list[str] = []
        if result.duplicate_ids:
            msgs.append(f"duplicate ids: {result.duplicate_ids}")
        for r in result.failures:
            for e in r.errors:
                msgs.append(
                    f"{r.entry_id}: {e.path} expected {e.expected!r} "
                    f"got {e.actual!r}"
                )
        raise ValueError(
            "smoke corpus failed schema validation:\n  "
            + "\n  ".join(msgs)
        )
    rows: list[dict] = []
    with CORPUS_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def shape_of(entry: dict) -> SmokeEntryShape:
    """Detect the dispatcher shape for an entry. Raises if unknown."""
    s = detect_shape(entry)
    if s is None:
        raise ValueError(
            f"entry {entry.get('id')!r} has no detectable shape"
        )
    return s


_AUDIT_PLACEHOLDER = "_audit_placeholder"


def reconstruct_claim_slots(
    pattern_name: str,
    slots_subset: dict,
    *,
    required_slots: list[str],
) -> dict:
    """Build a slots dict from the corpus's ``slots_subset``, filling
    in missing required slots with a placeholder string so v2's
    Layer 2 validator (invariant 1: every required slot present and
    non-empty) accepts the claim.

    The corpus's ``slots_subset`` is by name a *contains-at-least*
    annotation: it pins the slots the entry author cares about, not
    the full claim. The real extractor would fill the unpinned slots
    too. The audit can't run the extractor (deterministic, no LLM),
    so it fills the gaps with ``_audit_placeholder`` — non-empty
    enough to pass validation, distinct enough to surface in any
    audit-side trace where it appears.

    The placeholder NEVER matches a real stored fact, so any
    ASSISTANT_LOOKUP entry whose expected outcome is MISS will still
    get MISS (which is what we want for entries like
    p7-derivation-miss-weighs-no-distribution). Entries whose
    expected outcome is MATCH must already pin the matching identity
    slots in ``slots_subset`` — otherwise the audit can't satisfy
    them, and the entry should be revised.
    """
    out = dict(slots_subset)
    for slot in required_slots:
        if slot not in out or out[slot] in (None, "", []):
            out[slot] = _AUDIT_PLACEHOLDER
    return out


# ============================================================================
# Expected-divergence registry
# ============================================================================
#
# Per-entry annotation of *whether* a divergence between v1 and v2 is
# architecturally justified, and *why*. The bucketer uses this:
#
#   * If both stacks pass     → BOTH_PASS (regardless of registry value)
#   * If both stacks fail     → BOTH_FAIL (regardless)
#   * If v2 only attempts     → V2_ONLY_BY_DESIGN (substrate_direct etc)
#   * If only one passes:
#       - registry value is non-None → EXPECTED_DIVERGENCE
#       - registry value is None     → UNEXPECTED_DIVERGENCE  ← gate
#
# Keep this map exhaustive over the corpus so a new entry without a
# registry entry surfaces as a schema error during audit setup.

EXPECTED_DIVERGENCE_BY_ENTRY_ID: dict[str, Optional[str]] = {
    # --- Phase 1: extraction-shape parity ---
    # Mereological is a v2-only pattern. v1's extractor + registry has
    # no `mereological` pattern, so v1 will either miss the fact entirely
    # or coerce into spatial_temporal. Either way: expected divergence.
    "p1-merco-clean": "mereological_pattern",
    "p1-loc-clean": None,                  # spatial_temporal, both stacks
    "p1-disambig-pair": "mereological_pattern",  # mixed: 1 mereological + 1 spatial
    # --- Phase 2: routing memo (ROUTING_MEMO shape; v2_only_by_design) ---
    "p2-routing-anomaly-preference": None,
    "p2-memo-write-mereological": None,
    "p2-memo-hit-mereological": None,
    # --- Phase 3: predicate equivalence ---
    "p3-cheetahs-storage": None,           # both stacks store user fact
    # cheetahs assertion: v2 walker MATCHES via predicate_equivalence
    # (likes/dislikes contradictory + polarity flip). v1 may or may not
    # match depending on store_lookup_verify's behavior on opposite-
    # polarity claims with a synonym predicate; for the audit we treat
    # this as an architecturally-expected v2 win.
    "p3-cheetahs-assertion": "predicate_equivalence",
    "p3-active-passive": None,             # TWO_TEXT_ORACLE shape; v2_only
    "p3-distinct-negative": None,          # TWO_TEXT_ORACLE shape; v2_only
    # --- Phase 4: entity equivalence ---
    "p4-alias-resolution-positive-storage": None,
    # NYC ↔ New York City: v2 matches via entity_equivalence; v1 does
    # not have an entity oracle.
    "p4-alias-resolution-positive-assertion": "entity_equivalence",
    "p4-case-disambiguation-negative-storage": None,
    # apple (fruit) vs Apple (company): v2 entity_equivalence says
    # different so Tier U misses (correct). v1 may also miss (correct)
    # or may match incorrectly via case-insensitive matching.
    "p4-case-disambiguation-negative-assertion": "entity_equivalence",
    "p4-over-merge-negative-storage": None,
    "p4-over-merge-negative-assertion": None,  # both should miss; correct
    # --- Phase 5: substrate (SUBSTRATE_DIRECT shape; v2_only_by_design) ---
    "p5-tax-isa-clear": None,
    "p5-tax-partof-clear": None,
    "p5-dist-likes-isa": None,
    # --- Phase 6: session model ---
    # v0.14's session model (is_session_local + session_ids JSON) has no
    # v1 equivalent — v1's microtheory lived in a session_id column
    # that didn't have the local/cross-session distinction.
    "p6-session-local-storage": "session_local",
    "p6-session-local-in-session-match": "session_local",
    "p6-session-local-cross-session-miss": "session_local",
    # --- Phase 7: derivation ---
    "p7-williamstown-deriv-storage": None,         # storage; both stacks
    "p7-williamstown-deriv-assertion": "derivation",
    "p7-cheetahs-deriv-storage": None,
    "p7-cheetahs-deriv-assertion": "derivation",
    "p7-derivation-miss-no-substrate": None,       # both should miss
    "p7-derivation-miss-weighs-no-distribution": None,  # both should miss
}


# ============================================================================
# Substrate fixtures
# ============================================================================
#
# ASSISTANT_LOOKUP entries that depend on substrate rows declare the
# minimal pre-population needed for the v2 walker to produce the
# expected outcome without consulting a live LLM.

# Each fixture is a list of (oracle_name, kwargs) tuples. The v2 runner
# applies them in order before the walk.


# Module-level sentinel: this fixture is consumed by ``v2_runner.populate_substrate``.
# The shape mirrors the consult() signatures so the runner can dispatch
# blindly.

SUBSTRATE_FIXTURE: dict[str, list[dict[str, Any]]] = {
    "p3-cheetahs-assertion": [
        {
            "oracle": "predicate_equivalence",
            "pattern": "preference",
            "predicate_a": "dislikes",
            "predicate_b": "likes",
            "label": "contradictory",
            "slot_reversal": "none",
            "reason": "antonym pair within preference",
        },
    ],
    "p4-alias-resolution-positive-assertion": [
        {
            "oracle": "entity_equivalence",
            "entity_a": "NYC",
            "entity_b": "New York City",
            "label": "same",
            "reason": "common abbreviation",
        },
    ],
    "p4-case-disambiguation-negative-assertion": [
        {
            "oracle": "entity_equivalence",
            "entity_a": "Apple",
            "entity_b": "apple",
            "label": "different",
            "reason": "case-sensitive: company vs fruit",
        },
    ],
    "p4-over-merge-negative-assertion": [
        {
            "oracle": "entity_equivalence",
            "entity_a": "Japan",
            "entity_b": "Tokyo",
            "label": "different",
            "reason": "containment, not equivalence",
        },
    ],
    "p7-williamstown-deriv-assertion": [
        {
            "oracle": "entity_taxonomy",
            "child": "Williamstown",
            "parent": "Massachusetts",
            "relation_type": "part_of",
            "label": "child_subsumed_by_parent",
            "reason": "Williamstown is a town within Massachusetts",
        },
        {
            "oracle": "predicate_distribution",
            "pattern": "spatial_temporal",
            "predicate": "lives_in",
            "polarity": 1,
            "taxonomy_relation_type": "part_of",
            "label": "distributes_up",
            "reason": "lives_in propagates up part_of chains",
        },
    ],
    "p7-cheetahs-deriv-assertion": [
        {
            "oracle": "predicate_equivalence",
            "pattern": "preference",
            "predicate_a": "dislikes",
            "predicate_b": "likes",
            "label": "contradictory",
            "slot_reversal": "none",
            "reason": "antonym pair within preference",
        },
        {
            "oracle": "entity_taxonomy",
            "child": "cheetahs",
            "parent": "animals",
            "relation_type": "is_a",
            "label": "child_subsumed_by_parent",
            "reason": "cheetahs are a kind of animal",
        },
        {
            "oracle": "predicate_distribution",
            "pattern": "preference",
            "predicate": "dislikes",
            "polarity": 1,
            "taxonomy_relation_type": "is_a",
            "label": "distributes_down",
            "reason": "disliking a category implies disliking instances",
        },
    ],
}


def fixture_for(entry_id: str) -> list[dict[str, Any]]:
    """Return the substrate-row fixture for an entry. Empty list when
    no fixture is needed (literal-match cases, miss cases, storage
    cases)."""
    return SUBSTRATE_FIXTURE.get(entry_id, [])
