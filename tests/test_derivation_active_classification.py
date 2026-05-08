"""Bounded-active classification budget tests (v0.14 Phase 8c).

The Phase 7 derivation walker is purely passive over the substrate:
``llm`` is honored only by ``_resolve_pd`` and only when the cell is
cold. Phase 8 adds a budget that bounds how many cold pd cells the
walker may classify per walk.

Tests:

  * ``budget=0`` reproduces Phase 7 behavior: regardless of llm, no
    pd classifications fire during the walk. The walk completes on
    whatever the substrate already had.
  * ``budget=N`` admits up to N classifications. The walker
    populates the substrate as a side effect.
  * Budget exhaustion is graceful — the walk completes; subsequent
    cold cells are skipped without crashing.
  * Pipeline events fire correctly:
      * ``derivation_walk_active_classification`` per cold-cell write
      * ``derivation_walk_budget_exhausted`` once when budget hits 0
        and a cold cell is encountered.
  * Pinning test (the Phase 8 plan refinement): the budget bounds
    *count*, not *eligibility*. Cells the walker would never query
    (off-path) are not classified by virtue of the budget being
    larger than the walker's actual cold-cell needs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.fact_store import Fact, FactStore
from src.layer1_extraction.pattern_registry import (
    PatternRegistry,
)
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


_PATTERNS_PATH = (
    Path(__file__).parent.parent
    / "src" / "layer1_extraction" / "patterns.yaml"
)


# ============================================================================
# Mock LLM
# ============================================================================


class _ProgrammedLLM:
    """A fake LLMClient.extract_with_tool that returns canned labels.

    The walker only calls extract_with_tool on predicate_distribution
    cold cells (entity_equivalence, entity_taxonomy, predicate_
    equivalence are SQL-only inside _expand). The mock answers each
    call from a queue keyed by (pattern, predicate, polarity, rt) —
    this lets tests pre-program 'distributes_up' for in-path cells
    and 'neither' for off-path cells if needed.
    """

    def __init__(self, default_label: str = "distributes_up"):
        self.default_label = default_label
        self.responses: dict[tuple, dict] = {}
        self.calls: list[dict] = []

    def program(self, key: tuple, label: str, reason: str = "test"):
        self.responses[key] = {"label": label, "reason": reason}

    def extract_with_tool(self, *, system, user_message, tool, **_):
        # Decode the call from user_message — predicate_distribution
        # builds a JSON-like description there. Easier: parse by
        # checking the JSON line in the message.
        # The user_message format is documented in
        # predicate_distribution._build_user_message; we just record
        # and return the canned response.
        self.calls.append({
            "system_len": len(system or ""),
            "user_message": user_message,
            "tool_name": tool.get("name") if isinstance(tool, dict) else None,
        })
        # Try to parse the 4-tuple from user_message for keyed routing.
        key = _parse_pd_user_message(user_message)
        if key in self.responses:
            return self.responses[key]
        return {"label": self.default_label, "reason": "default"}


def _parse_pd_user_message(msg: str) -> tuple:
    """Best-effort extract (pattern, predicate, polarity, rt) from the
    user message. Returns ('', '', -1, '') on parse failure (which
    means no programmed match → default_label fires)."""
    try:
        # The predicate_distribution user_message is a JSON-formatted
        # block; pull pattern/predicate/polarity/rt fields.
        for line in msg.splitlines():
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                obj = json.loads(line)
                return (
                    obj.get("pattern", ""),
                    obj.get("predicate", ""),
                    obj.get("polarity", -1),
                    obj.get("taxonomy_relation_type", ""),
                )
    except (json.JSONDecodeError, ValueError):
        pass
    return ("", "", -1, "")


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(scope="module")
def registry() -> PatternRegistry:
    return PatternRegistry.from_yaml(_PATTERNS_PATH)


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "derivation_budget.db")
    yield s
    s.close()


@pytest.fixture
def predicate_oracle(store):
    return PredicateEquivalence(store)


@pytest.fixture
def entity_oracle(store):
    return EntityEquivalence(store)


@pytest.fixture
def taxonomy_oracle(store):
    return EntityTaxonomy(store)


@pytest.fixture
def distribution_oracle(store):
    return PredicateDistribution(store)


@pytest.fixture
def all_oracles(
    predicate_oracle, entity_oracle, taxonomy_oracle, distribution_oracle,
):
    return {
        "predicate_oracle": predicate_oracle,
        "entity_oracle": entity_oracle,
        "taxonomy_oracle": taxonomy_oracle,
        "distribution_oracle": distribution_oracle,
    }


def _setup_williamstown_substrate(store, taxonomy_oracle):
    """Canonical Williamstown/Massachusetts derivation case.

    U holds the SPECIFIC fact (lives_in(user, Williamstown)); the
    walker derives the GENERAL claim (lives_in(user, Massachusetts))
    via distributes_up over part_of. Predicate_distribution is left
    cold so budget tests exercise the active-classification path.
    """
    taxonomy_oracle.record(
        "Williamstown", "Massachusetts", "part_of",
        "child_subsumed_by_parent", reason="setup",
    )
    # User fact at the SPECIFIC entity:
    store.insert_fact(Fact(
        pattern="spatial_temporal", predicate="lives_in",
        slots={"entity": "user", "location": "Williamstown"},
        polarity=1, asserted_by="user",
        verification_status="user_asserted",
    ))


def _claim_lives_in_massachusetts() -> dict:
    """Model claim at the GENERAL entity. Walker substitutes
    Massachusetts→Williamstown (distributes_up over part_of) and
    matches the U fact."""
    return {
        "pattern": "spatial_temporal",
        "predicate": "lives_in",
        "polarity": 1,
        "slots": {"entity": "user", "location": "Massachusetts"},
        "source_text": "the user lives in Massachusetts",
    }


def _claim_lives_in_williamstown() -> dict:
    """Used for MISS-path tests where no derivation should resolve."""
    return {
        "pattern": "spatial_temporal",
        "predicate": "lives_in",
        "polarity": 1,
        "slots": {"entity": "user", "location": "Williamstown"},
        "source_text": "the user lives in Williamstown",
    }


# ============================================================================
# Tests
# ============================================================================


def test_budget_zero_phase_7_parity_no_classifications(
    store, registry, all_oracles,
):
    """budget=0 reproduces Phase 7 passive behavior. Even with an LLM
    available, no pd classifications fire during the walk."""
    _setup_williamstown_substrate(store, all_oracles["taxonomy_oracle"])
    llm = _ProgrammedLLM(default_label="distributes_up")

    # With pd cold, the walk MISSES (no classification means no
    # gating; the et+pd composite is skipped).
    result = derivation.walk(
        _claim_lives_in_williamstown(), store,
        key_slot_names=["entity", "location"],
        registry=registry,
        **all_oracles,
        llm=llm,
        active_classification_budget=0,
    )
    assert result.outcome is LookupOutcome.MISS
    assert llm.calls == [], "no LLM calls should fire when budget=0"

    # And the substrate has no new pd rows.
    pd_count = store._conn.execute(
        "SELECT COUNT(*) AS c FROM predicate_distribution"
    ).fetchone()["c"]
    assert pd_count == 0


def test_budget_zero_no_llm_phase_7_parity_no_classifications(
    store, registry, all_oracles,
):
    """Budget=0 + llm=None is the strictest Phase 7 mode."""
    _setup_williamstown_substrate(store, all_oracles["taxonomy_oracle"])

    result = derivation.walk(
        _claim_lives_in_williamstown(), store,
        key_slot_names=["entity", "location"],
        registry=registry,
        **all_oracles,
        llm=None,
        active_classification_budget=0,
    )
    assert result.outcome is LookupOutcome.MISS
    pd_count = store._conn.execute(
        "SELECT COUNT(*) AS c FROM predicate_distribution"
    ).fetchone()["c"]
    assert pd_count == 0


def test_budget_positive_admits_classification_and_finds_match(
    store, registry, all_oracles,
):
    """budget=5 + llm provided + cold pd → classification fires;
    walker resolves the chain."""
    _setup_williamstown_substrate(store, all_oracles["taxonomy_oracle"])
    llm = _ProgrammedLLM(default_label="distributes_up")

    result = derivation.walk(
        _claim_lives_in_massachusetts(), store,
        key_slot_names=["entity", "location"],
        registry=registry,
        **all_oracles,
        llm=llm,
        active_classification_budget=5,
    )
    assert result.outcome is LookupOutcome.MATCH

    # Substrate gained a pd row.
    pd_rows = store._conn.execute(
        "SELECT pattern, predicate, polarity, taxonomy_relation_type, label "
        "FROM predicate_distribution"
    ).fetchall()
    assert len(pd_rows) == 1
    r = pd_rows[0]
    assert r["pattern"] == "spatial_temporal"
    assert r["predicate"] == "lives_in"
    assert r["polarity"] == 1
    assert r["taxonomy_relation_type"] == "part_of"
    assert r["label"] == "distributes_up"


def test_budget_decrements_per_classification(
    store, registry, all_oracles,
):
    """One classification consumes one unit of budget."""
    _setup_williamstown_substrate(store, all_oracles["taxonomy_oracle"])
    llm = _ProgrammedLLM(default_label="distributes_up")

    result = derivation.walk(
        _claim_lives_in_massachusetts(), store,
        key_slot_names=["entity", "location"],
        registry=registry,
        **all_oracles,
        llm=llm,
        active_classification_budget=3,
    )
    assert result.outcome is LookupOutcome.MATCH
    # Only one cold cell encountered (lives_in/p=1/part_of); budget
    # goes from 3 to 2.
    assert len(llm.calls) == 1


# ============================================================================
# Budget exhaustion + graceful fall-through
# ============================================================================


def test_budget_exhaustion_graceful_fall_through(
    store, registry, all_oracles,
):
    """Two distinct cold cells with budget=1: first classifies,
    second triggers exhaustion event and is skipped. Walk completes
    (does not crash, does not hang)."""
    # Set up a substrate where the walker will encounter two distinct
    # pd cells (different relation_types) during exploration.
    tax = all_oracles["taxonomy_oracle"]
    # part_of: Williamstown → Massachusetts
    tax.record("Williamstown", "Massachusetts", "part_of",
               "child_subsumed_by_parent", reason="setup")
    # is_a: Williamstown → town (an additional relation type at the
    # initial slot value, forcing a second pd cell to be queried)
    tax.record("Williamstown", "town", "is_a",
               "child_subsumed_by_parent", reason="setup")

    # No matching fact at any of the parent entities — the walk will
    # explore both et+pd composites and terminate MISS gracefully.
    llm = _ProgrammedLLM(default_label="neither")  # nothing gates

    result = derivation.walk(
        _claim_lives_in_williamstown(), store,
        key_slot_names=["entity", "location"],
        registry=registry,
        **all_oracles,
        llm=llm,
        active_classification_budget=1,
    )

    assert result.outcome is LookupOutcome.MISS  # no productive expansion
    # The first cell got classified; the second got skipped.
    pd_count = store._conn.execute(
        "SELECT COUNT(*) AS c FROM predicate_distribution"
    ).fetchone()["c"]
    assert pd_count == 1, (
        f"exactly one pd row should have been written under budget=1, got {pd_count}"
    )
    assert len(llm.calls) == 1


def test_budget_exhausted_event_emitted_once(
    store, registry, all_oracles,
):
    """The derivation_walk_budget_exhausted event fires at most once
    per walk, even if multiple cold cells encounter the depleted budget."""
    tax = all_oracles["taxonomy_oracle"]
    tax.record("Williamstown", "Massachusetts", "part_of",
               "child_subsumed_by_parent", reason="setup")
    tax.record("Williamstown", "town", "is_a",
               "child_subsumed_by_parent", reason="setup")
    # Three distinct relation_types would mean three distinct pd cells,
    # but we only have part_of and is_a at the initial slot value.

    llm = _ProgrammedLLM(default_label="neither")

    turn_id = store.insert_turn("user", "test")
    derivation.walk(
        _claim_lives_in_williamstown(), store,
        key_slot_names=["entity", "location"],
        registry=registry,
        **all_oracles,
        llm=llm,
        active_classification_budget=0,  # exhausted from the start
        source_turn_id=turn_id,
    )

    events = store.get_pipeline_events(turn_id)
    exhausted = [
        e for e in events
        if e["stage"] == "derivation_walk_budget_exhausted"
    ]
    assert len(exhausted) == 1, (
        f"expected exactly one budget_exhausted event, got {len(exhausted)}"
    )


def test_active_classification_event_emitted_per_cold_write(
    store, registry, all_oracles,
):
    """Each cold pd cell that gets classified emits one event."""
    tax = all_oracles["taxonomy_oracle"]
    tax.record("Williamstown", "Massachusetts", "part_of",
               "child_subsumed_by_parent", reason="setup")
    tax.record("Williamstown", "town", "is_a",
               "child_subsumed_by_parent", reason="setup")

    llm = _ProgrammedLLM(default_label="neither")

    turn_id = store.insert_turn("user", "test")
    derivation.walk(
        _claim_lives_in_williamstown(), store,
        key_slot_names=["entity", "location"],
        registry=registry,
        **all_oracles,
        llm=llm,
        active_classification_budget=10,
        source_turn_id=turn_id,
    )

    events = store.get_pipeline_events(turn_id)
    classifications = [
        e for e in events
        if e["stage"] == "derivation_walk_active_classification"
    ]
    # Two distinct cells (part_of, is_a) — two events.
    assert len(classifications) == 2
    rt_set = {
        e["data"]["key"]["taxonomy_relation_type"] for e in classifications
    }
    assert rt_set == {"part_of", "is_a"}


# ============================================================================
# Pinning test (the Phase 8 plan refinement)
# ============================================================================


def test_budget_bounds_count_not_eligibility(
    store, registry, all_oracles,
):
    """Budget bounds *count*, not *eligibility*.

    Construct a substrate where the walker, exploring lives_in/p=1
    from Williamstown, queries exactly two pd cells (part_of and is_a
    at the initial slot value). Run with budget=10 (plenty); verify:
      (a) exactly the 2 expected (relation_type) cells get classified
      (b) NO additional pd cells beyond those 2 appear in the substrate
      (c) cells that the walker doesn't query (e.g. opposite polarity,
          different pattern) remain absent regardless of the budget

    This pins that budget licensure does NOT widen eligibility beyond
    what the walker actually visits.
    """
    tax = all_oracles["taxonomy_oracle"]
    # Two on-path et rows (both at slot value 'Williamstown'):
    tax.record("Williamstown", "Massachusetts", "part_of",
               "child_subsumed_by_parent", reason="setup")
    tax.record("Williamstown", "town", "is_a",
               "child_subsumed_by_parent", reason="setup")
    # Two off-path et rows (entities that never appear in any visited
    # state because their child/parent are unrelated to the initial
    # slot value or any reachable substitution):
    tax.record("zebra", "mammal", "is_a",
               "child_subsumed_by_parent", reason="off-path setup")
    tax.record("Toronto", "Canada", "part_of",
               "child_subsumed_by_parent", reason="off-path setup")

    llm = _ProgrammedLLM(default_label="neither")

    result = derivation.walk(
        _claim_lives_in_williamstown(), store,
        key_slot_names=["entity", "location"],
        registry=registry,
        **all_oracles,
        llm=llm,
        active_classification_budget=10,
    )
    assert result.outcome is LookupOutcome.MISS

    # (a) Exactly two pd cells classified — the on-path keys.
    pd_rows = store._conn.execute(
        "SELECT pattern, predicate, polarity, taxonomy_relation_type "
        "FROM predicate_distribution ORDER BY taxonomy_relation_type"
    ).fetchall()
    keys = [
        (r["pattern"], r["predicate"], r["polarity"],
         r["taxonomy_relation_type"]) for r in pd_rows
    ]
    assert keys == [
        ("spatial_temporal", "lives_in", 1, "is_a"),
        ("spatial_temporal", "lives_in", 1, "part_of"),
    ], f"expected exactly the on-path pd cells, got {keys}"

    # (b) Two — not 4, not 10.
    assert len(pd_rows) == 2

    # (c) Off-path cells (e.g. opposite polarity, different pattern)
    # were never queried, so don't exist.
    off_path = store._conn.execute(
        "SELECT COUNT(*) AS c FROM predicate_distribution "
        "WHERE polarity = 0 OR pattern != 'spatial_temporal'"
    ).fetchone()["c"]
    assert off_path == 0


def test_budget_pinning_cold_unrelated_pd_cells_untouched(
    store, registry, all_oracles,
):
    """Pre-warm an unrelated pd cell (different pattern + predicate);
    run a walk with high budget; verify the unrelated cell's
    last_consulted_at and count are unchanged (the walker didn't
    consult it)."""
    pd = all_oracles["distribution_oracle"]
    # Pre-warm an unrelated pd cell that the walk should NOT touch.
    pd.record(
        "preference", "likes", 1, "is_a", "distributes_down",
        reason="setup",
    )

    tax = all_oracles["taxonomy_oracle"]
    tax.record("Williamstown", "Massachusetts", "part_of",
               "child_subsumed_by_parent", reason="setup")
    # User fact at SPECIFIC entity (Williamstown); claim at GENERAL
    # (Massachusetts) — derives via distributes_up over part_of.
    store.insert_fact(Fact(
        pattern="spatial_temporal", predicate="lives_in",
        slots={"entity": "user", "location": "Williamstown"},
        polarity=1, asserted_by="user",
        verification_status="user_asserted",
    ))

    llm = _ProgrammedLLM(default_label="distributes_up")

    # Capture initial state of the unrelated row.
    before = store._conn.execute(
        "SELECT last_consulted_at, affirmed_count, contradicted_count "
        "FROM predicate_distribution "
        "WHERE pattern = 'preference' AND predicate = 'likes'"
    ).fetchone()

    result = derivation.walk(
        _claim_lives_in_massachusetts(), store,
        key_slot_names=["entity", "location"],
        registry=registry,
        **all_oracles,
        llm=llm,
        active_classification_budget=10,
    )
    assert result.outcome is LookupOutcome.MATCH

    # Unrelated row: last_consulted_at, counts unchanged.
    after = store._conn.execute(
        "SELECT last_consulted_at, affirmed_count, contradicted_count "
        "FROM predicate_distribution "
        "WHERE pattern = 'preference' AND predicate = 'likes'"
    ).fetchone()
    assert after["last_consulted_at"] == before["last_consulted_at"]
    assert after["affirmed_count"] == before["affirmed_count"]
    assert after["contradicted_count"] == before["contradicted_count"]


# ============================================================================
# Default budget
# ============================================================================


def test_default_budget_is_twenty():
    """Architectural commitment: default budget is 20."""
    assert derivation.DEFAULT_ACTIVE_CLASSIFICATION_BUDGET == 20


def test_walk_default_budget_value(
    store, registry, all_oracles,
):
    """When the caller doesn't specify a budget, walk() uses the
    default. Pin behaviorally rather than via signature inspection."""
    _setup_williamstown_substrate(store, all_oracles["taxonomy_oracle"])
    llm = _ProgrammedLLM(default_label="distributes_up")
    turn_id = store.insert_turn("user", "test")

    result = derivation.walk(
        _claim_lives_in_massachusetts(), store,
        key_slot_names=["entity", "location"],
        registry=registry,
        **all_oracles,
        llm=llm,
        source_turn_id=turn_id,
    )
    assert result.outcome is LookupOutcome.MATCH

    # Default 20 budget; one classification fired; budget_remaining=19
    # in the completion event.
    events = store.get_pipeline_events(turn_id)
    completed = [e for e in events if e["stage"] == "derivation_walk_completed"][-1]
    assert completed["data"]["active_classifications"] == 1
    assert completed["data"]["budget_remaining"] == 19


# ============================================================================
# Cycle detection unaffected by budget mechanism
# ============================================================================


def test_budget_does_not_break_cycle_detection(
    store, registry, all_oracles,
):
    """Construct a synthetic entity_equivalence cycle. The walker
    must terminate cleanly regardless of budget."""
    eq = all_oracles["entity_oracle"]
    eq.record("alpha", "beta", "same", reason="cycle setup")
    eq.record("beta", "gamma", "same", reason="cycle setup")
    eq.record("alpha", "gamma", "same", reason="cycle setup")

    claim = {
        "pattern": "spatial_temporal",
        "predicate": "lives_in",
        "polarity": 1,
        "slots": {"entity": "user", "location": "alpha"},
    }
    llm = _ProgrammedLLM(default_label="distributes_up")

    # No matching fact anywhere — walker explores, hits the cycle,
    # and returns MISS without infinite-looping.
    result = derivation.walk(
        claim, store,
        key_slot_names=["entity", "location"],
        registry=registry,
        **all_oracles,
        llm=llm,
        active_classification_budget=10,
    )
    assert result.outcome is LookupOutcome.MISS
    # No pd cells classified (no et rows on alpha/beta/gamma; only ee).
    pd_count = store._conn.execute(
        "SELECT COUNT(*) AS c FROM predicate_distribution"
    ).fetchone()["c"]
    assert pd_count == 0
