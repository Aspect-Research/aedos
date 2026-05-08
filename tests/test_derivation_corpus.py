"""25-scenario derivation calibration corpus runner (v0.14 Phase 7g).

Each entry in ``calibration/derivation_corpus.jsonl`` specifies:
  * starting facts in U (with verification_status, asserted_by)
  * substrate row preconditions (oracle, key columns, expected label,
    optional contradicted_count_override for the floor scenario)
  * the query claim
  * the expected derivation outcome (match / miss)
  * the expected chain (list of oracle names) on match
  * the expected chain_reliability lower bound

The runner:
  1. Loads the corpus, sanity-checks shape + distribution.
  2. For each entry: fresh in-memory store, pre-store facts, pre-warm
     substrate, take three snapshots (pre / mid / post), run the
     derivation walker, assert outcome + chain shape + no-persistence
     (the three-snapshot gate per Ambiguity #7).
  3. Aggregate floor assertion: ≥ 0.80 entries match expectation.

Live calibration (RUN_API_TESTS=1): same 25 scenarios with substrate
NOT pre-warmed; LLM classifies cold-start cells. Verifies that the
derivation walker resolves correctly when the substrate composes
real LLM classifications. Floor: 0.80 (lower than per-oracle floors
because chains compound oracle-level error).

The three-snapshot gate
=======================

  * snap_pre  — empty store
  * snap_mid  — after fact storage + substrate pre-warming. Counts:
                facts increased by len(facts_to_store); substrate
                rows increased by len(substrate_to_pre_warm).
                verification_cache unchanged (Tier W not populated).
  * snap_post — after derivation.walk(). Counts:
                facts UNCHANGED from mid (derivation never persists
                facts); verification_cache UNCHANGED from mid
                (derivation never writes Tier W). Substrate counts
                may grow IFF cold-start LLM ran (live path); on
                warm path, substrate is unchanged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.fact_store import Fact, FactStore
from src.layer1_extraction.pattern_registry import (
    PatternRegistry,
)
from src.layer2_routing.constants import KEY_SLOTS_BY_PATTERN
from src.layer3_substrate.entity_equivalence import (
    EntityEquivalence,
)
from src.layer3_substrate.entity_taxonomy import (
    EntityTaxonomy,
)
from src.layer3_substrate.predicate_distribution import (
    PredicateDistribution,
)
from src.layer3_substrate.predicate_equivalence import (
    PredicateEquivalence,
)
from src.layer4_lookup import derivation
from src.layer4_lookup.types import LookupOutcome


_CORPUS_PATH = (
    Path(__file__).parent / "calibration" / "derivation_corpus.jsonl"
)
_PATTERNS_PATH = (
    Path(__file__).parent.parent
    / "src" / "layer1_extraction" / "patterns.yaml"
)

_AGGREGATE_FLOOR = 0.80

_EXPECTED_DISTRIBUTION = {
    "single_hop_partof_up":              4,
    "single_hop_isa_down":               5,
    "multi_hop_partof_up":               4,
    "multi_hop_isa_down":                3,
    "mixed_partof_isa":                  2,
    "polarity_flip_mid_chain":           2,
    "cycle_detection":                   2,
    "predicate_distribution_neither_miss": 2,
    "chain_reliability_floor":           1,
}


# ============================================================================
# Corpus loading + sanity
# ============================================================================


def _load_corpus() -> list[dict]:
    rows: list[dict] = []
    with _CORPUS_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            rows.append(json.loads(stripped))
    return rows


def test_corpus_loads_and_distribution_matches_plan():
    """Sanity check on corpus shape — runs unconditionally."""
    rows = _load_corpus()
    assert len(rows) == 25, (
        f"expected 25 entries; got {len(rows)}"
    )
    by_cat: dict[str, int] = {}
    for entry in rows:
        cat = entry.get("category")
        assert cat in _EXPECTED_DISTRIBUTION, (
            f"unknown category {cat!r} in entry {entry.get('id')!r}"
        )
        by_cat[cat] = by_cat.get(cat, 0) + 1
    assert by_cat == _EXPECTED_DISTRIBUTION, (
        f"distribution mismatch:\nactual={by_cat}\n"
        f"expected={_EXPECTED_DISTRIBUTION}"
    )


def test_corpus_entries_have_required_fields():
    """Every entry must specify the required fields."""
    rows = _load_corpus()
    required = {
        "id", "category", "facts_to_store", "substrate_to_pre_warm",
        "query_claim", "expected_outcome",
    }
    for entry in rows:
        missing = required - set(entry.keys())
        assert not missing, (
            f"entry {entry.get('id')!r} missing required fields: {missing}"
        )


def test_corpus_ids_unique():
    rows = _load_corpus()
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), (
        f"duplicate entry ids: {[i for i in ids if ids.count(i) > 1]}"
    )


# ============================================================================
# Test fixtures
# ============================================================================


@pytest.fixture(scope="module")
def registry():
    return PatternRegistry.from_yaml(_PATTERNS_PATH)


# ============================================================================
# Three-snapshot helper
# ============================================================================


@dataclass(frozen=True)
class _Snapshot:
    facts: int
    cache: int
    et: int
    pd: int
    pe: int
    ee: int


def _take_snapshot(store: FactStore) -> _Snapshot:
    return _Snapshot(
        facts=int(store._conn.execute(
            "SELECT COUNT(*) FROM facts").fetchone()[0] or 0),
        cache=int(store._conn.execute(
            "SELECT COUNT(*) FROM verification_cache").fetchone()[0] or 0),
        et=int(store._conn.execute(
            "SELECT COUNT(*) FROM entity_taxonomy").fetchone()[0] or 0),
        pd=int(store._conn.execute(
            "SELECT COUNT(*) FROM predicate_distribution").fetchone()[0] or 0),
        pe=int(store._conn.execute(
            "SELECT COUNT(*) FROM predicate_equivalence").fetchone()[0] or 0),
        ee=int(store._conn.execute(
            "SELECT COUNT(*) FROM entity_equivalence").fetchone()[0] or 0),
    )


# ============================================================================
# Setup helpers (per-entry)
# ============================================================================


def _store_facts(store: FactStore, facts_spec: list[dict]) -> None:
    for spec in facts_spec:
        if spec.get("tier") != "u":
            # Tier W population in this corpus is via direct fact inserts
            # would skip the tier_w write path; we don't currently exercise
            # that here. Phase 8 may extend.
            continue
        store.insert_fact(Fact(
            pattern=spec["pattern"],
            predicate=spec["predicate"],
            slots=dict(spec["slots"]),
            polarity=int(spec["polarity"]),
            asserted_by=spec.get("asserted_by", "user"),
            verification_status=spec.get(
                "verification_status", "user_asserted",
            ),
        ))


def _prewarm_substrate(
    store: FactStore,
    substrate_spec: list[dict],
    *,
    predicate_oracle: PredicateEquivalence,
    entity_oracle: EntityEquivalence,
    taxonomy_oracle: EntityTaxonomy,
    distribution_oracle: PredicateDistribution,
) -> None:
    for row_spec in substrate_spec:
        oracle_name = row_spec["oracle"]
        contradicted_override = row_spec.get("contradicted_count_override")
        if oracle_name == "entity_taxonomy":
            row = taxonomy_oracle.record(
                row_spec["child"], row_spec["parent"],
                row_spec["relation_type"],
                label=row_spec["label"],
                reason=row_spec.get("reason", "calibration prewarm"),
            )
            if contradicted_override is not None:
                store._conn.execute(
                    "UPDATE entity_taxonomy SET contradicted_count = ? "
                    "WHERE id = ?",
                    (int(contradicted_override), row.id),
                )
                store._conn.commit()
        elif oracle_name == "predicate_distribution":
            distribution_oracle.record(
                row_spec["pattern"], row_spec["predicate"],
                int(row_spec["polarity"]),
                row_spec["taxonomy_relation_type"],
                label=row_spec["label"],
                reason=row_spec.get("reason", "calibration prewarm"),
            )
        elif oracle_name == "predicate_equivalence":
            predicate_oracle.record(
                row_spec["pattern"],
                row_spec["predicate_a"], row_spec["predicate_b"],
                label=row_spec["label"],
                slot_reversal=row_spec.get("slot_reversal", "none"),
                reason=row_spec.get("reason", "calibration prewarm"),
            )
        elif oracle_name == "entity_equivalence":
            entity_oracle.record(
                row_spec["entity_a"], row_spec["entity_b"],
                label=row_spec["label"],
                reason=row_spec.get("reason", "calibration prewarm"),
            )
        else:
            raise ValueError(
                f"unknown oracle in substrate spec: {oracle_name!r}"
            )


# ============================================================================
# Per-entry runner (warm path, default)
# ============================================================================


@pytest.fixture
def fresh_store(tmp_path):
    s = FactStore(tmp_path / "deriv_corpus.db")
    yield s
    s.close()


def _run_entry(
    entry: dict,
    fresh_store: FactStore,
    registry: PatternRegistry,
) -> tuple[bool, dict]:
    """Run one corpus entry. Returns (ok, diagnostic_info).

    ``ok`` is True iff:
      - The walker's outcome matches expected_outcome.
      - The derivation chain's oracle list matches expected_chain_oracles
        (when specified; on miss this can be None).
      - chain_reliability >= expected_chain_reliability_min.
      - Three-snapshot gate holds: facts and verification_cache row
        counts are equal across pre / mid / post.
    """
    pe = PredicateEquivalence(fresh_store)
    ee = EntityEquivalence(fresh_store)
    et = EntityTaxonomy(fresh_store)
    pd = PredicateDistribution(fresh_store)

    snap_pre = _take_snapshot(fresh_store)

    _store_facts(fresh_store, entry["facts_to_store"])
    _prewarm_substrate(
        fresh_store, entry["substrate_to_pre_warm"],
        predicate_oracle=pe, entity_oracle=ee,
        taxonomy_oracle=et, distribution_oracle=pd,
    )

    snap_mid = _take_snapshot(fresh_store)

    claim = entry["query_claim"]
    pattern = claim.get("pattern", "")
    key_slot_names = KEY_SLOTS_BY_PATTERN.get(pattern, [])

    result = derivation.walk(
        claim, fresh_store,
        key_slot_names=key_slot_names,
        registry=registry,
        predicate_oracle=pe, entity_oracle=ee,
        taxonomy_oracle=et, distribution_oracle=pd,
        llm=None,  # warm path; no LLM needed
    )

    snap_post = _take_snapshot(fresh_store)

    expected = entry["expected_outcome"]
    actual = result.outcome.value

    diagnostics: dict = {
        "entry_id": entry["id"],
        "category": entry["category"],
        "expected": expected,
        "actual": actual,
        "abort_reason": result.abort_reason,
        "chain_oracles": [e.oracle for e in result.chain],
        "chain_reliability": result.chain_reliability,
        "explored_states": result.explored_states,
        "snap_pre": snap_pre,
        "snap_mid": snap_mid,
        "snap_post": snap_post,
    }

    if actual != expected:
        return False, diagnostics

    # Three-snapshot no-persistence gate. facts + verification_cache
    # never grow during derivation, regardless of outcome.
    if snap_post.facts != snap_mid.facts:
        diagnostics["failure"] = "facts grew during derivation walk"
        return False, diagnostics
    if snap_post.cache != snap_mid.cache:
        diagnostics["failure"] = "verification_cache grew during derivation walk"
        return False, diagnostics

    # Chain shape on match.
    if expected == "match":
        expected_chain = entry.get("expected_chain_oracles")
        if expected_chain is not None:
            actual_chain = [e.oracle for e in result.chain]
            if actual_chain != expected_chain:
                diagnostics["failure"] = (
                    f"chain shape mismatch: "
                    f"expected {expected_chain!r}, got {actual_chain!r}"
                )
                return False, diagnostics
        floor = float(entry.get("expected_chain_reliability_min", 0.0))
        if result.chain_reliability < floor:
            diagnostics["failure"] = (
                f"chain_reliability {result.chain_reliability:.3f} "
                f"< floor {floor:.3f}"
            )
            return False, diagnostics

    return True, diagnostics


# ============================================================================
# Aggregate floor (warm path)
# ============================================================================


def test_warm_path_aggregate_floor(fresh_store, registry):
    """All 25 entries should pass on the warm (pre-substrate-populated)
    path — substrate is exactly what the entry specifies, derivation
    has no oracle ambiguity. Floor: 1.0 (every entry passes)."""
    rows = _load_corpus()
    passing = 0
    failures: list[dict] = []
    for entry in rows:
        # Each entry needs its own fresh store; the module-scoped
        # fixture is per-test, but we have multiple entries per test.
        # Use a dedicated tmp store per entry inline.
        store = FactStore(":memory:")
        try:
            ok, diag = _run_entry(entry, store, registry)
        finally:
            store.close()
        if ok:
            passing += 1
        else:
            failures.append(diag)
    rate = passing / len(rows)
    if rate < 1.0:
        msg_lines = [
            f"warm path failures: {len(failures)} / {len(rows)} "
            f"(pass rate {rate:.2%})"
        ]
        for f in failures:
            msg_lines.append(
                f"  {f['entry_id']!r} ({f['category']}): "
                f"expected={f['expected']!r} actual={f['actual']!r} "
                f"abort={f.get('abort_reason')!r} "
                f"failure={f.get('failure', '<outcome-mismatch>')}"
            )
        pytest.fail("\n".join(msg_lines))
    assert rate == 1.0


# ============================================================================
# No-persistence gate (separate, more visible test)
# ============================================================================


def test_no_persistence_three_snapshot_gate(fresh_store, registry):
    """Across all 25 entries, the three-snapshot gate must hold:
    facts and verification_cache never grow during derivation.

    Substrate counts may grow if cold-start LLM ran (live path); on
    the default warm path, substrate is unchanged from pre-warming
    to post-walk.
    """
    rows = _load_corpus()
    for entry in rows:
        store = FactStore(":memory:")
        try:
            ok, diag = _run_entry(entry, store, registry)
        finally:
            store.close()
        snap_mid: _Snapshot = diag["snap_mid"]
        snap_post: _Snapshot = diag["snap_post"]
        assert snap_post.facts == snap_mid.facts, (
            f"{entry['id']}: facts grew during walk "
            f"({snap_mid.facts} -> {snap_post.facts})"
        )
        assert snap_post.cache == snap_mid.cache, (
            f"{entry['id']}: verification_cache grew during walk "
            f"({snap_mid.cache} -> {snap_post.cache})"
        )
        # On warm path with no LLM, substrate also doesn't grow.
        assert snap_post.et == snap_mid.et
        assert snap_post.pd == snap_mid.pd
        assert snap_post.pe == snap_mid.pe
        assert snap_post.ee == snap_mid.ee


# ============================================================================
# Live calibration (gated)
# ============================================================================


@pytest.mark.skipif(
    os.environ.get("RUN_API_TESTS") != "1",
    reason="live LLM derivation calibration; gated behind RUN_API_TESTS=1",
)
def test_live_derivation_calibration(registry):
    """The 25 scenarios run with substrate pre-populated from existing
    calibration gold rows AND a live LLM available for incidental
    cold-start cells the walker hits. Aggregate floor: 0.80.

    Per the Phase 7 plan: 'Pre-populate oracle rows from existing
    calibration corpora rather than inventing new ones — the
    derivation walker is being tested against the substrate as it
    actually calibrates.' The live LLM serves as a safety net for
    any incidental cold-start cells the walker explores beyond the
    entry's pre-warm spec (e.g. predicate_distribution for an
    unrelated direction the walker briefly considers). The chain-
    reliability floor admits cold-start rows (Beta(1,1) confidence
    0.5 > 0.4 floor); compound chain error is what 0.80 absorbs.

    API-spend hop. Project: ~50-100 LLM calls for the full corpus
    (most cells pre-warmed; LLM fires only for incidental cold cells).
    If the actual run spikes above 400, abort and surface.
    """
    from dotenv import load_dotenv
    load_dotenv()
    from src.llm_client import LLMClient
    llm = LLMClient()
    rows = _load_corpus()

    passing = 0
    failures: list[dict] = []
    for entry in rows:
        store = FactStore(":memory:")
        try:
            pe = PredicateEquivalence(store)
            ee = EntityEquivalence(store)
            et = EntityTaxonomy(store)
            pd = PredicateDistribution(store)

            _store_facts(store, entry["facts_to_store"])
            # Pre-warm substrate per the entry spec (existing calibration
            # gold rows). Live LLM available for any cells the walker
            # consults beyond what was pre-warmed.
            _prewarm_substrate(
                store, entry["substrate_to_pre_warm"],
                predicate_oracle=pe, entity_oracle=ee,
                taxonomy_oracle=et, distribution_oracle=pd,
            )

            claim = entry["query_claim"]
            pattern = claim.get("pattern", "")
            key_slot_names = KEY_SLOTS_BY_PATTERN.get(pattern, [])

            result = derivation.walk(
                claim, store,
                key_slot_names=key_slot_names,
                registry=registry,
                predicate_oracle=pe, entity_oracle=ee,
                taxonomy_oracle=et, distribution_oracle=pd,
                llm=llm,
            )
            expected = entry["expected_outcome"]
            actual = result.outcome.value
            if actual == expected:
                passing += 1
            else:
                failures.append({
                    "entry_id": entry["id"],
                    "category": entry["category"],
                    "expected": expected,
                    "actual": actual,
                    "chain_oracles": [e.oracle for e in result.chain],
                    "abort_reason": result.abort_reason,
                })
        finally:
            store.close()

    rate = passing / len(rows)
    if rate < _AGGREGATE_FLOOR:
        msg_lines = [
            f"live derivation calibration FLOOR FAIL: "
            f"{passing}/{len(rows)} = {rate:.2%} < floor {_AGGREGATE_FLOOR:.2%}"
        ]
        for f in failures:
            msg_lines.append(
                f"  {f['entry_id']!r} ({f['category']}): "
                f"expected={f['expected']!r} actual={f['actual']!r} "
                f"chain={f['chain_oracles']!r} abort={f['abort_reason']!r}"
            )
        pytest.fail("\n".join(msg_lines))
    print(
        f"\nlive derivation calibration: {passing}/{len(rows)} = "
        f"{rate:.2%} (floor {_AGGREGATE_FLOOR:.2%})"
    )
