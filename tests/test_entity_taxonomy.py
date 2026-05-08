"""Tests for src.layer3_substrate.entity_taxonomy.

Coverage:

  * Schema constraints: CHECK on label, CHECK on relation_type,
    CHECK (child != parent), UNIQUE (child, parent, relation_type).
  * _normalize_inputs: ``strip()`` only, NO lowercase. Rejects
    empty inputs, self-pairs after strip, unknown relation_type.
  * lookup(): exact-ordering match (no canonical-pair swap);
    different orderings produce different rows; miss returns None;
    case-sensitive.
  * record(): inserts canonical row; UPSERT preserves counts;
    rejects unknown label.
  * list_rows(): full list and per-relation_type filter; ordering.
  * consult() with mocked LLM:
      - cache miss → LLM call → row written → verdict has
        served_from_cache=False.
      - cache hit → no LLM call → verdict served_from_cache=True;
        counts stay 0; last_consulted_at advances.
      - classification_failed: malformed LLM output emits the
        dedicated event, verdict has classification_failed=True
        and label=None, no row written.
      - oracle_consulted event fires on every call.
  * Counts stay 0 across many consults (principle 3 unit test).
  * Directional behavior: passing (child, parent) and (parent,
    child) of the same pair produces TWO distinct rows. The
    canonical-pair encapsulation does NOT apply.
"""

from __future__ import annotations

import pytest

from src.fact_store import FactStore
from src.layer3_substrate.entity_taxonomy import (
    LABELS,
    RELATION_TYPES,
    EntityTaxonomy,
    EntityTaxonomyRow,
    EntityTaxonomyVerdict,
    _normalize_inputs,
)


# ---- shared fixtures ------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "et.db")
    yield s
    s.close()


@pytest.fixture
def oracle(store):
    return EntityTaxonomy(store)


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


# ---- _normalize_inputs ---------------------------------------------------


def test_normalize_inputs_strips_whitespace():
    c, p, rt = _normalize_inputs(
        "  Williamstown  ", " Massachusetts ", "part_of",
    )
    assert c == "Williamstown"
    assert p == "Massachusetts"
    assert rt == "part_of"


def test_normalize_inputs_does_not_lowercase():
    """Architectural contract: case is semantic for entities."""
    c, p, _ = _normalize_inputs("Apple", "fruit", "is_a")
    assert c == "Apple"  # stays capital
    assert p == "fruit"


def test_normalize_inputs_rejects_empty_child():
    with pytest.raises(ValueError, match="non-empty"):
        _normalize_inputs("", "Massachusetts", "part_of")


def test_normalize_inputs_rejects_empty_parent():
    with pytest.raises(ValueError, match="non-empty"):
        _normalize_inputs("Williamstown", "  ", "part_of")


def test_normalize_inputs_rejects_self_pair():
    with pytest.raises(ValueError, match="self-pair"):
        _normalize_inputs("dog", "dog", "is_a")


def test_normalize_inputs_rejects_self_pair_after_strip():
    with pytest.raises(ValueError, match="self-pair"):
        _normalize_inputs("dog", "  dog  ", "is_a")


def test_normalize_inputs_rejects_unknown_relation_type():
    with pytest.raises(ValueError, match="relation_type"):
        _normalize_inputs("dog", "mammal", "subclass_of")


def test_normalize_inputs_apple_vs_fruit_NOT_self_pair():
    """Case-sensitivity: apple and Apple are distinct entities."""
    c, p, _ = _normalize_inputs("Apple", "apple", "is_a")
    # Capital Apple (company) and lowercase apple (fruit) are
    # distinct entities. Self-pair check is case-sensitive.
    assert c != p


# ---- schema constraints --------------------------------------------------


def test_schema_check_rejects_unknown_label(store):
    with pytest.raises(Exception):
        store._conn.execute(
            "INSERT INTO entity_taxonomy (child, parent, "
            "relation_type, label, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("dog", "mammal", "is_a", "kind_of_subsumed", "now"),
        )
        store._conn.commit()


def test_schema_check_rejects_unknown_relation_type(store):
    with pytest.raises(Exception):
        store._conn.execute(
            "INSERT INTO entity_taxonomy (child, parent, "
            "relation_type, label, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("dog", "mammal", "subclass_of",
             "child_subsumed_by_parent", "now"),
        )
        store._conn.commit()


def test_schema_check_rejects_self_pair(store):
    with pytest.raises(Exception):
        store._conn.execute(
            "INSERT INTO entity_taxonomy (child, parent, "
            "relation_type, label, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("dog", "dog", "is_a",
             "child_subsumed_by_parent", "now"),
        )
        store._conn.commit()


def test_schema_unique_triple(store, oracle):
    oracle.record("dog", "mammal", "is_a",
                  "child_subsumed_by_parent", "first")
    with pytest.raises(Exception):
        store._conn.execute(
            "INSERT INTO entity_taxonomy (child, parent, "
            "relation_type, label, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("dog", "mammal", "is_a", "neither", "now"),
        )
        store._conn.commit()


def test_schema_swapped_pair_is_NOT_constrained_unique(store, oracle):
    """The (child, parent) ordering is positional and meaningful;
    UNIQUE applies to the exact ordering. (dog, mammal, is_a) and
    (mammal, dog, is_a) are TWO DISTINCT rows."""
    oracle.record("dog", "mammal", "is_a",
                  "child_subsumed_by_parent", "natural")
    # The reverse is a separate row — UNIQUE doesn't conflict.
    oracle.record("mammal", "dog", "is_a",
                  "parent_subsumed_by_child", "inverted")
    rows = oracle.list_rows()
    assert len(rows) == 2


# ---- record() -------------------------------------------------------------


def test_record_inserts_with_zero_counts(oracle):
    row = oracle.record("Williamstown", "Massachusetts", "part_of",
                        "child_subsumed_by_parent", "town in state")
    assert isinstance(row, EntityTaxonomyRow)
    assert row.child == "Williamstown"
    assert row.parent == "Massachusetts"
    assert row.relation_type == "part_of"
    assert row.label == "child_subsumed_by_parent"
    assert row.affirmed_count == 0
    assert row.contradicted_count == 0
    assert row.id is not None


def test_record_does_not_canonicalize_swap(oracle):
    """Directional storage: passing (child, parent) and the reverse
    creates two distinct rows."""
    row1 = oracle.record("Williamstown", "Massachusetts", "part_of",
                         "child_subsumed_by_parent", "natural")
    row2 = oracle.record("Massachusetts", "Williamstown", "part_of",
                         "parent_subsumed_by_child", "inverted")
    assert row1.id != row2.id


def test_record_preserves_case(oracle):
    """Apple/fruit and apple/fruit are stored as distinct rows."""
    oracle.record("Apple", "fruit", "is_a", "neither",
                  "company is not a fruit")
    oracle.record("apple", "fruit", "is_a",
                  "child_subsumed_by_parent", "fruit kind")
    rows = oracle.list_rows()
    assert len(rows) == 2
    labels_by_child = {r.child: r.label for r in rows}
    assert labels_by_child["Apple"] == "neither"
    assert labels_by_child["apple"] == "child_subsumed_by_parent"


def test_record_upsert_preserves_counts(store, oracle):
    oracle.record("dog", "mammal", "is_a",
                  "child_subsumed_by_parent", "first")
    # Manually bump counts as if Phase 8's operator endpoint had.
    store._conn.execute(
        "UPDATE entity_taxonomy SET affirmed_count = 7, "
        "contradicted_count = 2 "
        "WHERE child = ? AND parent = ? AND relation_type = ?",
        ("dog", "mammal", "is_a"),
    )
    store._conn.commit()
    re_row = oracle.record("dog", "mammal", "is_a",
                           "child_subsumed_by_parent", "second")
    assert re_row.affirmed_count == 7
    assert re_row.contradicted_count == 2
    assert re_row.reason == "second"


def test_record_rejects_unknown_label(oracle):
    with pytest.raises(ValueError, match="label"):
        oracle.record("dog", "mammal", "is_a", "kind_of", None)


def test_record_rejects_unknown_relation_type(oracle):
    with pytest.raises(ValueError, match="relation_type"):
        oracle.record("dog", "mammal", "subclass_of",
                      "child_subsumed_by_parent", None)


# ---- lookup() -------------------------------------------------------------


def test_lookup_miss_returns_none(oracle):
    assert oracle.lookup("dog", "mammal", "is_a") is None


def test_lookup_exact_ordering_only(oracle):
    """Directional: (dog, mammal) is NOT the same lookup as
    (mammal, dog). Each direction is a separate row."""
    oracle.record("dog", "mammal", "is_a",
                  "child_subsumed_by_parent", "natural")
    found = oracle.lookup("dog", "mammal", "is_a")
    assert found is not None
    assert found.label == "child_subsumed_by_parent"
    # Reverse ordering misses (no canonical-pair swap).
    miss = oracle.lookup("mammal", "dog", "is_a")
    assert miss is None


def test_lookup_distinct_relation_types(oracle):
    """Same (child, parent) under different relation_type produces
    different rows."""
    oracle.record("Williamstown", "Massachusetts", "part_of",
                  "child_subsumed_by_parent", "compositional")
    oracle.record("Williamstown", "Massachusetts", "is_a",
                  "neither",
                  "Williamstown is not a kind of Massachusetts")
    a = oracle.lookup("Williamstown", "Massachusetts", "part_of")
    b = oracle.lookup("Williamstown", "Massachusetts", "is_a")
    assert a is not None and b is not None
    assert a.id != b.id
    assert a.label == "child_subsumed_by_parent"
    assert b.label == "neither"


def test_lookup_case_sensitive(oracle):
    """Apple/fruit and apple/fruit are distinct rows."""
    oracle.record("Apple", "fruit", "is_a", "neither", None)
    found_capital = oracle.lookup("Apple", "fruit", "is_a")
    assert found_capital is not None
    miss_lower = oracle.lookup("apple", "fruit", "is_a")
    assert miss_lower is None


def test_lookup_rejects_self_pair(oracle):
    with pytest.raises(ValueError):
        oracle.lookup("dog", "dog", "is_a")


def test_lookup_rejects_unknown_relation_type(oracle):
    with pytest.raises(ValueError):
        oracle.lookup("dog", "mammal", "subclass_of")


# ---- list_rows() ----------------------------------------------------------


def test_list_rows_empty(oracle):
    assert oracle.list_rows() == []


def test_list_rows_returns_all_in_order(oracle):
    oracle.record("dog", "mammal", "is_a",
                  "child_subsumed_by_parent", None)
    oracle.record("Williamstown", "Massachusetts", "part_of",
                  "child_subsumed_by_parent", None)
    oracle.record("cheetah", "animal", "is_a",
                  "child_subsumed_by_parent", None)
    rows = oracle.list_rows()
    assert len(rows) == 3
    # Sorted by (relation_type, child, parent). is_a < part_of.
    assert rows[0].relation_type == "is_a"
    assert rows[1].relation_type == "is_a"
    assert rows[2].relation_type == "part_of"


def test_list_rows_filtered_by_relation_type(oracle):
    oracle.record("dog", "mammal", "is_a",
                  "child_subsumed_by_parent", None)
    oracle.record("Williamstown", "Massachusetts", "part_of",
                  "child_subsumed_by_parent", None)
    is_a_rows = oracle.list_rows(relation_type="is_a")
    part_of_rows = oracle.list_rows(relation_type="part_of")
    assert len(is_a_rows) == 1
    assert len(part_of_rows) == 1
    assert is_a_rows[0].relation_type == "is_a"
    assert part_of_rows[0].relation_type == "part_of"


def test_list_rows_rejects_unknown_relation_type_filter(oracle):
    with pytest.raises(ValueError):
        oracle.list_rows(relation_type="subclass_of")


# ---- consult() with mocked LLM -------------------------------------------


def test_consult_miss_calls_llm_and_writes_row(oracle):
    llm = _MockLLM(responses=[
        {"label": "child_subsumed_by_parent",
         "reason": "Williamstown is one of Massachusetts's towns"},
    ])
    verdict = oracle.consult(
        "Williamstown", "Massachusetts", "part_of",
        llm=llm, source_turn_id=None,
    )
    assert isinstance(verdict, EntityTaxonomyVerdict)
    assert verdict.label == "child_subsumed_by_parent"
    assert verdict.served_from_cache is False
    assert verdict.classification_failed is False
    assert verdict.row_id is not None
    assert verdict.confidence == 0.5  # Beta(1,1) at zero counts
    assert len(llm.calls) == 1
    assert llm.calls[0]["purpose"] == "entity_taxonomy"


def test_consult_hit_does_not_call_llm(oracle):
    llm = _MockLLM(responses=[
        {"label": "child_subsumed_by_parent", "reason": "town"},
    ])
    oracle.consult(
        "Williamstown", "Massachusetts", "part_of", llm=llm,
    )
    assert len(llm.calls) == 1
    # Same exact ordering — hit.
    verdict = oracle.consult(
        "Williamstown", "Massachusetts", "part_of", llm=llm,
    )
    assert len(llm.calls) == 1  # unchanged
    assert verdict.served_from_cache is True
    assert verdict.label == "child_subsumed_by_parent"


def test_consult_swapped_ordering_misses_cache(oracle):
    """Directional: cached (Williamstown, Massachusetts, part_of)
    does NOT serve a swapped lookup. The walker must ask each
    direction separately."""
    llm = _MockLLM(responses=[
        {"label": "child_subsumed_by_parent",
         "reason": "Williamstown is part of Massachusetts"},
        {"label": "parent_subsumed_by_child",
         "reason": "inversion: Williamstown is the more specific"},
    ])
    oracle.consult(
        "Williamstown", "Massachusetts", "part_of", llm=llm,
    )
    assert len(llm.calls) == 1
    # Swap arguments. Different row, different LLM call.
    verdict = oracle.consult(
        "Massachusetts", "Williamstown", "part_of", llm=llm,
    )
    assert len(llm.calls) == 2
    assert verdict.served_from_cache is False
    assert verdict.label == "parent_subsumed_by_child"


def test_consult_distinct_relation_types_miss_each_other(oracle):
    """Same (child, parent) under different relation_types — distinct
    rows, distinct cache entries."""
    llm = _MockLLM(responses=[
        {"label": "child_subsumed_by_parent",
         "reason": "Williamstown is part of Massachusetts"},
        {"label": "neither",
         "reason": "not a categorical kind relation"},
    ])
    oracle.consult(
        "Williamstown", "Massachusetts", "part_of", llm=llm,
    )
    assert len(llm.calls) == 1
    # Same entities, different relation_type — separate consult.
    verdict = oracle.consult(
        "Williamstown", "Massachusetts", "is_a", llm=llm,
    )
    assert len(llm.calls) == 2
    assert verdict.label == "neither"


def test_consult_hit_advances_last_consulted_at(oracle):
    llm = _MockLLM(responses=[
        {"label": "child_subsumed_by_parent", "reason": "kind of"},
    ])
    oracle.consult("dog", "mammal", "is_a", llm=llm)
    first = oracle.lookup("dog", "mammal", "is_a")
    oracle.consult("dog", "mammal", "is_a", llm=llm)
    second = oracle.lookup("dog", "mammal", "is_a")
    assert second.last_consulted_at >= first.last_consulted_at


def test_consult_hit_does_not_increment_counts(oracle):
    """Principle 3: 50 consults of the same triple leave counts at 0."""
    llm = _MockLLM(responses=[
        {"label": "child_subsumed_by_parent", "reason": "kind of"},
    ])
    for _ in range(50):
        oracle.consult("dog", "mammal", "is_a", llm=llm)
    assert len(llm.calls) == 1
    row = oracle.lookup("dog", "mammal", "is_a")
    assert row.affirmed_count == 0
    assert row.contradicted_count == 0


def test_consult_classification_failed_on_unknown_label(oracle):
    llm = _MockLLM(responses=[
        {"label": "kind_of_subsumed", "reason": "..."},
    ])
    verdict = oracle.consult("dog", "mammal", "is_a", llm=llm)
    assert verdict.classification_failed is True
    assert verdict.label is None
    assert verdict.row_id is None
    assert oracle.lookup("dog", "mammal", "is_a") is None


def test_consult_classification_failed_on_missing_reason(oracle):
    llm = _MockLLM(responses=[{"label": "neither"}])
    verdict = oracle.consult("dog", "mammal", "is_a", llm=llm)
    assert verdict.classification_failed is True


def test_consult_classification_failed_on_llm_exception(oracle):
    llm = _MockLLM(responses=[], raises=RuntimeError("api blew up"))
    verdict = oracle.consult("dog", "mammal", "is_a", llm=llm)
    assert verdict.classification_failed is True
    assert "api blew up" in (verdict.reason or "")


def test_consult_emits_oracle_consulted_event_on_hit(store, oracle):
    llm = _MockLLM(responses=[
        {"label": "child_subsumed_by_parent", "reason": "kind of"},
    ])
    turn_id = store.insert_turn("user", "irrelevant")
    oracle.consult("dog", "mammal", "is_a",
                   llm=llm, source_turn_id=turn_id)
    oracle.consult("dog", "mammal", "is_a",
                   llm=llm, source_turn_id=turn_id)
    events = store.get_pipeline_events(turn_id)
    stages = [e["stage"] for e in events]
    assert stages.count("oracle_consulted") == 2
    assert "entity_taxonomy_write" in stages
    assert "entity_taxonomy_hit" in stages


def test_consult_emits_classification_failed_event(store, oracle):
    llm = _MockLLM(responses=[
        {"label": "weird_label", "reason": "nope"},
    ])
    turn_id = store.insert_turn("user", "irrelevant")
    oracle.consult("dog", "mammal", "is_a",
                   llm=llm, source_turn_id=turn_id)
    events = store.get_pipeline_events(turn_id)
    stages = [e["stage"] for e in events]
    assert "entity_taxonomy_classification_failed" in stages


def test_consult_without_llm_on_miss_raises(oracle):
    with pytest.raises(RuntimeError, match="no LLM provided"):
        oracle.consult("dog", "mammal", "is_a", llm=None)


def test_consult_without_llm_on_hit_works(oracle):
    oracle.record("dog", "mammal", "is_a",
                  "child_subsumed_by_parent", "kind of")
    verdict = oracle.consult("dog", "mammal", "is_a", llm=None)
    assert verdict.served_from_cache is True
    assert verdict.label == "child_subsumed_by_parent"


def test_consult_neither_label(oracle):
    """The neither label is the high-stakes one for over-subsumption
    cases — wrong-subsumption calls would let the Phase 7 walker
    propagate facts wrongly."""
    llm = _MockLLM(responses=[
        {"label": "neither",
         "reason": "Apple the company is not a fruit"},
    ])
    verdict = oracle.consult("Apple", "fruit", "is_a", llm=llm)
    assert verdict.label == "neither"
    row = oracle.lookup("Apple", "fruit", "is_a")
    assert row.label == "neither"


def test_consult_parent_subsumed_by_child_label(oracle):
    """The inversion label — caller passed arguments backwards."""
    llm = _MockLLM(responses=[
        {"label": "parent_subsumed_by_child",
         "reason": "inversion: golden retriever is the more specific"},
    ])
    verdict = oracle.consult("mammal", "golden retriever", "is_a",
                             llm=llm)
    assert verdict.label == "parent_subsumed_by_child"


def test_consult_equivalent_label(oracle):
    """The equivalent label — same level under the relation."""
    llm = _MockLLM(responses=[
        {"label": "equivalent",
         "reason": "Holland and Netherlands denote the same country"},
    ])
    verdict = oracle.consult("Holland", "Netherlands", "is_a",
                             llm=llm)
    assert verdict.label == "equivalent"


# ---- public constants -----------------------------------------------------


def test_label_set_is_closed():
    assert set(LABELS) == {
        "child_subsumed_by_parent",
        "parent_subsumed_by_child",
        "equivalent",
        "neither",
    }


def test_relation_type_set_is_closed():
    assert set(RELATION_TYPES) == {"is_a", "part_of"}


def test_row_confidence_uses_counts(oracle):
    row = oracle.record("dog", "mammal", "is_a",
                        "child_subsumed_by_parent", None)
    assert row.confidence() == 0.5
