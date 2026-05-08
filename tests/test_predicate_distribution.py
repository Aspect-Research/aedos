"""Tests for src.layer3_substrate.predicate_distribution.

Coverage:

  * Schema constraints: CHECK on label, polarity, taxonomy_relation_
    type. UNIQUE on the 4-tuple.
  * _normalize_predicate: lowercase + strip; rejects empty input.
  * _validate_inputs: pattern non-empty (strip-only); polarity ∈ {0,1};
    relation_type ∈ {is_a, part_of}.
  * lookup(): exact 4-tuple match; predicate is case-folded; miss
    returns None.
  * record(): inserts the row; UPSERT preserves counts; rejects
    unknown label/polarity/relation_type at the Python layer.
  * list_rows(): full list; pattern filter; polarity filter; combined
    filters.
  * consult() with mocked LLM:
      - cache miss → LLM call → row written → verdict has
        served_from_cache=False.
      - cache hit → no LLM call → verdict served_from_cache=True;
        counts stay 0; last_consulted_at advances.
      - classification_failed handling.
      - oracle_consulted event fires on every call.
  * Counts stay 0 across many consults (principle 3).
  * Singleton-key shape: same (pattern, predicate, polarity) under
    different relation_types are TWO distinct rows that may have
    different labels — the oracle's directional-asymmetry capability.
  * Polarity-as-key: same (pattern, predicate, relation_type) under
    different polarities are distinct rows.
"""

from __future__ import annotations

import pytest

from src.fact_store import FactStore
from src.layer3_substrate.predicate_distribution import (
    LABELS,
    POLARITIES,
    RELATION_TYPES,
    PredicateDistribution,
    PredicateDistributionRow,
    PredicateDistributionVerdict,
    _normalize_predicate,
    _validate_inputs,
)


# ---- shared fixtures ------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "pd.db")
    yield s
    s.close()


@pytest.fixture
def oracle(store):
    return PredicateDistribution(store)


class _MockLLM:
    """Queue-based LLM stub. Compatible with LLMClient.extract_with_tool."""

    def __init__(self, responses=None, raises=None):
        self.responses = list(responses or [])
        self.raises = raises
        self.calls: list[dict] = []

    def extract_with_tool(self, *, system, user_message, tool, purpose):
        self.calls.append(
            {"system": system, "user_message": user_message,
             "tool": tool, "purpose": purpose}
        )
        if self.raises is not None:
            exc, self.raises = self.raises, None
            raise exc
        if not self.responses:
            raise AssertionError("MockLLM ran out of responses")
        return self.responses.pop(0)


# ---- normalization helpers -----------------------------------------------


def test_normalize_predicate_lowercases_and_strips():
    assert _normalize_predicate("  Likes  ") == "likes"
    assert _normalize_predicate("LIVES_IN") == "lives_in"


def test_normalize_predicate_rejects_empty():
    with pytest.raises(ValueError, match="non-empty"):
        _normalize_predicate("")
    with pytest.raises(ValueError, match="non-empty"):
        _normalize_predicate("   ")


def test_validate_inputs_canonical():
    pat, pred, pol, rt = _validate_inputs(
        "preference", "Likes", 1, "is_a",
    )
    assert pat == "preference"
    assert pred == "likes"  # lowercased
    assert pol == 1
    assert rt == "is_a"


def test_validate_inputs_strips_pattern_only():
    """Pattern names are extractor-controlled and case-sensitive;
    they get strip but NOT lowercase."""
    pat, _, _, _ = _validate_inputs(
        "  preference  ", "likes", 1, "is_a",
    )
    assert pat == "preference"


def test_validate_inputs_rejects_empty_pattern():
    with pytest.raises(ValueError, match="pattern"):
        _validate_inputs("", "likes", 1, "is_a")


def test_validate_inputs_rejects_unknown_polarity():
    with pytest.raises(ValueError, match="polarity"):
        _validate_inputs("preference", "likes", 2, "is_a")


def test_validate_inputs_rejects_unknown_relation_type():
    with pytest.raises(ValueError, match="taxonomy_relation_type"):
        _validate_inputs("preference", "likes", 1, "subclass_of")


# ---- schema constraints --------------------------------------------------


def test_schema_check_rejects_unknown_label(store):
    with pytest.raises(Exception):
        store._conn.execute(
            "INSERT INTO predicate_distribution (pattern, predicate, "
            "polarity, taxonomy_relation_type, label, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("preference", "likes", 1, "is_a", "kinda_distributes",
             "now"),
        )
        store._conn.commit()


def test_schema_check_rejects_unknown_polarity(store):
    with pytest.raises(Exception):
        store._conn.execute(
            "INSERT INTO predicate_distribution (pattern, predicate, "
            "polarity, taxonomy_relation_type, label, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("preference", "likes", 2, "is_a", "distributes_down",
             "now"),
        )
        store._conn.commit()


def test_schema_check_rejects_unknown_relation_type(store):
    with pytest.raises(Exception):
        store._conn.execute(
            "INSERT INTO predicate_distribution (pattern, predicate, "
            "polarity, taxonomy_relation_type, label, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("preference", "likes", 1, "subclass_of",
             "distributes_down", "now"),
        )
        store._conn.commit()


def test_schema_unique_4tuple(store, oracle):
    oracle.record("preference", "likes", 1, "is_a",
                  "distributes_down", "first")
    with pytest.raises(Exception):
        store._conn.execute(
            "INSERT INTO predicate_distribution (pattern, predicate, "
            "polarity, taxonomy_relation_type, label, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("preference", "likes", 1, "is_a", "neither", "now"),
        )
        store._conn.commit()


# ---- record() -------------------------------------------------------------


def test_record_inserts_with_zero_counts(oracle):
    row = oracle.record("preference", "likes", 1, "is_a",
                        "distributes_down",
                        "categorical preferences inherit downward")
    assert isinstance(row, PredicateDistributionRow)
    assert row.pattern == "preference"
    assert row.predicate == "likes"
    assert row.polarity == 1
    assert row.taxonomy_relation_type == "is_a"
    assert row.label == "distributes_down"
    assert row.affirmed_count == 0
    assert row.contradicted_count == 0
    assert row.id is not None


def test_record_normalizes_predicate(oracle):
    """Predicate is lowercased on the way in."""
    row = oracle.record("preference", "LIKES", 1, "is_a",
                        "distributes_down", None)
    assert row.predicate == "likes"
    # Looking up by either case finds it.
    assert oracle.lookup("preference", "likes", 1, "is_a") is not None
    assert oracle.lookup("preference", "Likes", 1, "is_a") is not None


def test_record_distinct_relation_types(oracle):
    """Same (pattern, predicate, polarity) under different relation_
    types creates two distinct rows."""
    row1 = oracle.record("spatial_temporal", "lives_in", 1, "is_a",
                         "neither", "no categorical inheritance")
    row2 = oracle.record("spatial_temporal", "lives_in", 1, "part_of",
                         "distributes_up", "compositional aggregation")
    assert row1.id != row2.id
    assert row1.label == "neither"
    assert row2.label == "distributes_up"


def test_record_distinct_polarities(oracle):
    """Same (pattern, predicate, relation_type) under different
    polarities are distinct rows."""
    row1 = oracle.record("preference", "likes", 1, "is_a",
                         "distributes_down", "positive case")
    row2 = oracle.record("preference", "likes", 0, "is_a",
                         "neither", "negated case")
    assert row1.id != row2.id


def test_record_upsert_preserves_counts(store, oracle):
    oracle.record("preference", "likes", 1, "is_a",
                  "distributes_down", "first")
    store._conn.execute(
        "UPDATE predicate_distribution SET affirmed_count = 4, "
        "contradicted_count = 1 WHERE pattern = ? AND predicate = ? "
        "AND polarity = ? AND taxonomy_relation_type = ?",
        ("preference", "likes", 1, "is_a"),
    )
    store._conn.commit()
    re_row = oracle.record("preference", "likes", 1, "is_a",
                           "distributes_down", "second")
    assert re_row.affirmed_count == 4
    assert re_row.contradicted_count == 1
    assert re_row.reason == "second"


def test_record_rejects_unknown_label(oracle):
    with pytest.raises(ValueError, match="label"):
        oracle.record("preference", "likes", 1, "is_a",
                      "distributes_sideways", None)


def test_record_rejects_unknown_polarity(oracle):
    with pytest.raises(ValueError, match="polarity"):
        oracle.record("preference", "likes", 2, "is_a",
                      "distributes_down", None)


def test_record_rejects_unknown_relation_type(oracle):
    with pytest.raises(ValueError, match="taxonomy_relation_type"):
        oracle.record("preference", "likes", 1, "subclass_of",
                      "distributes_down", None)


# ---- lookup() -------------------------------------------------------------


def test_lookup_miss_returns_none(oracle):
    assert oracle.lookup(
        "preference", "likes", 1, "is_a",
    ) is None


def test_lookup_normalizes_predicate_case(oracle):
    oracle.record("preference", "likes", 1, "is_a",
                  "distributes_down", None)
    found = oracle.lookup("preference", "Likes", 1, "is_a")
    assert found is not None


def test_lookup_distinguishes_relation_type(oracle):
    """The directional-asymmetry test: same (pattern, predicate,
    polarity) under different relation_types are distinct lookups."""
    oracle.record("spatial_temporal", "lives_in", 1, "part_of",
                  "distributes_up", None)
    found_part_of = oracle.lookup(
        "spatial_temporal", "lives_in", 1, "part_of",
    )
    miss_is_a = oracle.lookup(
        "spatial_temporal", "lives_in", 1, "is_a",
    )
    assert found_part_of is not None
    assert miss_is_a is None


def test_lookup_distinguishes_polarity(oracle):
    oracle.record("preference", "likes", 1, "is_a",
                  "distributes_down", None)
    found_pos = oracle.lookup("preference", "likes", 1, "is_a")
    miss_neg = oracle.lookup("preference", "likes", 0, "is_a")
    assert found_pos is not None
    assert miss_neg is None


def test_lookup_rejects_unknown_polarity(oracle):
    with pytest.raises(ValueError):
        oracle.lookup("preference", "likes", 2, "is_a")


def test_lookup_rejects_unknown_relation_type(oracle):
    with pytest.raises(ValueError):
        oracle.lookup("preference", "likes", 1, "subclass_of")


# ---- list_rows() ----------------------------------------------------------


def test_list_rows_empty(oracle):
    assert oracle.list_rows() == []


def test_list_rows_returns_all_in_order(oracle):
    oracle.record("preference", "likes", 1, "is_a",
                  "distributes_down", None)
    oracle.record("preference", "dislikes", 1, "is_a",
                  "distributes_down", None)
    oracle.record("spatial_temporal", "lives_in", 1, "part_of",
                  "distributes_up", None)
    rows = oracle.list_rows()
    assert len(rows) == 3
    # Sorted by (pattern, predicate, polarity, taxonomy_relation_type)
    assert rows[0].pattern == "preference"
    assert rows[0].predicate == "dislikes"
    assert rows[1].pattern == "preference"
    assert rows[1].predicate == "likes"
    assert rows[2].pattern == "spatial_temporal"


def test_list_rows_filtered_by_pattern(oracle):
    oracle.record("preference", "likes", 1, "is_a",
                  "distributes_down", None)
    oracle.record("spatial_temporal", "lives_in", 1, "part_of",
                  "distributes_up", None)
    rows = oracle.list_rows(pattern="preference")
    assert len(rows) == 1
    assert rows[0].pattern == "preference"


def test_list_rows_filtered_by_polarity(oracle):
    oracle.record("preference", "likes", 1, "is_a",
                  "distributes_down", None)
    oracle.record("preference", "likes", 0, "is_a",
                  "neither", None)
    rows = oracle.list_rows(polarity=1)
    assert len(rows) == 1
    assert rows[0].polarity == 1


def test_list_rows_filtered_by_pattern_and_polarity(oracle):
    oracle.record("preference", "likes", 1, "is_a",
                  "distributes_down", None)
    oracle.record("preference", "likes", 0, "is_a",
                  "neither", None)
    oracle.record("spatial_temporal", "lives_in", 1, "part_of",
                  "distributes_up", None)
    rows = oracle.list_rows(pattern="preference", polarity=1)
    assert len(rows) == 1
    assert rows[0].pattern == "preference"
    assert rows[0].polarity == 1


def test_list_rows_rejects_unknown_polarity(oracle):
    with pytest.raises(ValueError):
        oracle.list_rows(polarity=2)


# ---- consult() with mocked LLM -------------------------------------------


def test_consult_miss_calls_llm_and_writes_row(oracle):
    llm = _MockLLM(responses=[
        {"label": "distributes_down",
         "reason": "categorical preference inherits downward"},
    ])
    verdict = oracle.consult(
        "preference", "likes", 1, "is_a",
        llm=llm, source_turn_id=None,
    )
    assert isinstance(verdict, PredicateDistributionVerdict)
    assert verdict.label == "distributes_down"
    assert verdict.served_from_cache is False
    assert verdict.classification_failed is False
    assert verdict.row_id is not None
    assert verdict.confidence == 0.5  # Beta(1,1) at zero counts
    assert len(llm.calls) == 1
    assert llm.calls[0]["purpose"] == "predicate_distribution"


def test_consult_hit_does_not_call_llm(oracle):
    llm = _MockLLM(responses=[
        {"label": "distributes_down",
         "reason": "categorical preference"},
    ])
    oracle.consult("preference", "likes", 1, "is_a", llm=llm)
    assert len(llm.calls) == 1
    # Same exact 4-tuple — hit.
    verdict = oracle.consult(
        "preference", "likes", 1, "is_a", llm=llm,
    )
    assert len(llm.calls) == 1  # unchanged
    assert verdict.served_from_cache is True


def test_consult_directional_asymmetry(oracle):
    """The canonical test for this oracle: same (pattern, predicate,
    polarity) under different relation_types must produce TWO LLM
    calls and possibly DIFFERENT labels."""
    llm = _MockLLM(responses=[
        {"label": "neither",
         "reason": "categorical chains do not preserve residence"},
        {"label": "distributes_up",
         "reason": "compositional aggregation: part-resident is "
                   "whole-resident"},
    ])
    v1 = oracle.consult(
        "spatial_temporal", "lives_in", 1, "is_a", llm=llm,
    )
    v2 = oracle.consult(
        "spatial_temporal", "lives_in", 1, "part_of", llm=llm,
    )
    assert len(llm.calls) == 2
    assert v1.label == "neither"
    assert v2.label == "distributes_up"
    # Both rows persist independently.
    assert oracle.lookup(
        "spatial_temporal", "lives_in", 1, "is_a",
    ).label == "neither"
    assert oracle.lookup(
        "spatial_temporal", "lives_in", 1, "part_of",
    ).label == "distributes_up"


def test_consult_polarity_asymmetry(oracle):
    """Same (pattern, predicate, relation_type) under different
    polarities produces distinct rows."""
    llm = _MockLLM(responses=[
        {"label": "distributes_down", "reason": "positive likes"},
        {"label": "neither",
         "reason": "negated case is silent on instances"},
    ])
    oracle.consult("preference", "likes", 1, "is_a", llm=llm)
    oracle.consult("preference", "likes", 0, "is_a", llm=llm)
    assert len(llm.calls) == 2
    pos = oracle.lookup("preference", "likes", 1, "is_a")
    neg = oracle.lookup("preference", "likes", 0, "is_a")
    assert pos.label == "distributes_down"
    assert neg.label == "neither"


def test_consult_hit_advances_last_consulted_at(oracle):
    llm = _MockLLM(responses=[
        {"label": "distributes_down", "reason": "yes"},
    ])
    oracle.consult("preference", "likes", 1, "is_a", llm=llm)
    first = oracle.lookup("preference", "likes", 1, "is_a")
    oracle.consult("preference", "likes", 1, "is_a", llm=llm)
    second = oracle.lookup("preference", "likes", 1, "is_a")
    assert second.last_consulted_at >= first.last_consulted_at


def test_consult_hit_does_not_increment_counts(oracle):
    """Principle 3: 50 consults of the same 4-tuple leave counts at 0."""
    llm = _MockLLM(responses=[
        {"label": "distributes_down", "reason": "yes"},
    ])
    for _ in range(50):
        oracle.consult("preference", "likes", 1, "is_a", llm=llm)
    assert len(llm.calls) == 1
    row = oracle.lookup("preference", "likes", 1, "is_a")
    assert row.affirmed_count == 0
    assert row.contradicted_count == 0


def test_consult_classification_failed_on_unknown_label(oracle):
    llm = _MockLLM(responses=[
        {"label": "distributes_sideways", "reason": "..."},
    ])
    verdict = oracle.consult(
        "preference", "likes", 1, "is_a", llm=llm,
    )
    assert verdict.classification_failed is True
    assert verdict.label is None
    assert verdict.row_id is None
    assert oracle.lookup(
        "preference", "likes", 1, "is_a",
    ) is None


def test_consult_classification_failed_on_missing_reason(oracle):
    llm = _MockLLM(responses=[{"label": "distributes_down"}])
    verdict = oracle.consult(
        "preference", "likes", 1, "is_a", llm=llm,
    )
    assert verdict.classification_failed is True


def test_consult_classification_failed_on_llm_exception(oracle):
    llm = _MockLLM(responses=[], raises=RuntimeError("api blew up"))
    verdict = oracle.consult(
        "preference", "likes", 1, "is_a", llm=llm,
    )
    assert verdict.classification_failed is True
    assert "api blew up" in (verdict.reason or "")


def test_consult_emits_oracle_consulted_event_on_hit(store, oracle):
    llm = _MockLLM(responses=[
        {"label": "distributes_down", "reason": "yes"},
    ])
    turn_id = store.insert_turn("user", "irrelevant")
    oracle.consult(
        "preference", "likes", 1, "is_a",
        llm=llm, source_turn_id=turn_id,
    )
    oracle.consult(
        "preference", "likes", 1, "is_a",
        llm=llm, source_turn_id=turn_id,
    )
    events = store.get_pipeline_events(turn_id)
    stages = [e["stage"] for e in events]
    assert stages.count("oracle_consulted") == 2
    assert "predicate_distribution_write" in stages
    assert "predicate_distribution_hit" in stages


def test_consult_emits_classification_failed_event(store, oracle):
    llm = _MockLLM(responses=[
        {"label": "weird", "reason": "nope"},
    ])
    turn_id = store.insert_turn("user", "irrelevant")
    oracle.consult(
        "preference", "likes", 1, "is_a",
        llm=llm, source_turn_id=turn_id,
    )
    events = store.get_pipeline_events(turn_id)
    stages = [e["stage"] for e in events]
    assert "predicate_distribution_classification_failed" in stages


def test_consult_without_llm_on_miss_raises(oracle):
    with pytest.raises(RuntimeError, match="no LLM provided"):
        oracle.consult("preference", "likes", 1, "is_a", llm=None)


def test_consult_without_llm_on_hit_works(oracle):
    oracle.record("preference", "likes", 1, "is_a",
                  "distributes_down", "categorical inheritance")
    verdict = oracle.consult(
        "preference", "likes", 1, "is_a", llm=None,
    )
    assert verdict.served_from_cache is True
    assert verdict.label == "distributes_down"


def test_consult_neither_label(oracle):
    """The neither label is the high-stakes one — wrong-distribution
    calls would let the Phase 7 walker propagate facts wrongly."""
    llm = _MockLLM(responses=[
        {"label": "neither",
         "reason": "weight is an individual property"},
    ])
    verdict = oracle.consult(
        "quantitative", "weighs", 1, "is_a", llm=llm,
    )
    assert verdict.label == "neither"


def test_consult_distributes_up_label(oracle):
    llm = _MockLLM(responses=[
        {"label": "distributes_up",
         "reason": "compositional residence aggregation"},
    ])
    verdict = oracle.consult(
        "spatial_temporal", "lives_in", 1, "part_of", llm=llm,
    )
    assert verdict.label == "distributes_up"


def test_consult_both_label(oracle):
    """The 'both' label is rare but must round-trip cleanly."""
    llm = _MockLLM(responses=[
        {"label": "both",
         "reason": "hypothetical bidirectional case"},
    ])
    verdict = oracle.consult(
        "relational", "co_extensive_with", 1, "is_a", llm=llm,
    )
    assert verdict.label == "both"


# ---- public constants -----------------------------------------------------


def test_label_set_is_closed():
    assert set(LABELS) == {
        "distributes_up", "distributes_down", "both", "neither",
    }


def test_relation_type_set_is_closed():
    assert set(RELATION_TYPES) == {"is_a", "part_of"}


def test_polarities_set_is_closed():
    assert set(POLARITIES) == {0, 1}


def test_row_confidence_uses_counts(oracle):
    row = oracle.record("preference", "likes", 1, "is_a",
                        "distributes_down", None)
    assert row.confidence() == 0.5
