"""Tests for src.layer3_substrate.entity_equivalence.

Coverage:

  * Schema constraints: CHECK on label, CHECK on entity_a < entity_b
    (case-sensitive lex), UNIQUE on (entity_a, entity_b).
  * Canonical-pair helper: ``strip()`` only — NO lowercase. Rejects
    self-pairs and empty inputs. Preserves case (apple/Apple are
    distinct rows from apple/apple-typed-2).
  * lookup(): both orderings of the pair return the same row;
    case is preserved on lookup; miss returns None.
  * record(): inserts canonical row; UPSERT preserves counts;
    rejects unknown label.
  * list_rows(): full list with order.
  * consult() with mocked LLM:
      - cache miss → LLM call → row written → verdict has
        served_from_cache=False.
      - cache hit → no LLM call → verdict served_from_cache=True;
        counts stay 0; last_consulted_at advances.
      - classification_failed: malformed LLM output emits the
        dedicated event, verdict has classification_failed=True
        and label=None, no row written.
      - oracle_consulted event fires on every call.
  * Counts stay 0 across many consults (principle 3).
"""

from __future__ import annotations

import pytest

from src.fact_store import FactStore
from src.layer3_substrate.entity_equivalence import (
    LABELS,
    EntityEquivalence,
    EntityEquivalenceRow,
    EntityEquivalenceVerdict,
    _canonical_pair,
)


# ---- shared fixtures ------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "ee.db")
    yield s
    s.close()


@pytest.fixture
def oracle(store):
    return EntityEquivalence(store)


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


# ---- canonical-pair helper ------------------------------------------------


def test_canonical_pair_lex_smaller_first():
    """ASCII lex: 'NYC' < 'New York City' because 'Y' (0x59) < 'e' (0x65)."""
    a, b, swapped = _canonical_pair("New York City", "NYC")
    assert a == "NYC"
    assert b == "New York City"
    assert swapped is True


def test_canonical_pair_already_sorted():
    a, b, swapped = _canonical_pair("Apple", "apple")
    # ASCII: 'A' (0x41) < 'a' (0x61), so Apple < apple
    assert a == "Apple"
    assert b == "apple"
    assert swapped is False


def test_canonical_pair_does_not_lowercase():
    """Architectural contract: case is semantic for entities."""
    # apple (fruit) and Apple (company) must remain distinct.
    a, b, _ = _canonical_pair("apple", "Apple")
    # Case-sensitive comparison: 'A' < 'a', so Apple is smaller.
    assert a == "Apple"
    assert b == "apple"
    # And these distinct from another casing variant — apple/Apple
    # is one canonical pair; apple/APPLE is a different canonical pair.
    a2, b2, _ = _canonical_pair("apple", "APPLE")
    assert (a, b) != (a2, b2)


def test_canonical_pair_strips_inputs():
    a, b, _ = _canonical_pair("  NYC  ", "New York City")
    assert a == "NYC"
    assert b == "New York City"


def test_canonical_pair_rejects_self_pair():
    with pytest.raises(ValueError, match="self-pair"):
        _canonical_pair("Apple", "Apple")


def test_canonical_pair_rejects_self_pair_after_strip():
    with pytest.raises(ValueError, match="self-pair"):
        _canonical_pair("Apple", "  Apple  ")


def test_canonical_pair_apple_vs_Apple_NOT_self_pair():
    """The non-lowercase contract: apple and Apple are DIFFERENT
    canonical pair members, not a self-pair."""
    a, b, _ = _canonical_pair("apple", "Apple")
    assert a != b


def test_canonical_pair_rejects_empty_input():
    with pytest.raises(ValueError, match="non-empty"):
        _canonical_pair("", "Apple")
    with pytest.raises(ValueError, match="non-empty"):
        _canonical_pair("Apple", "  ")


# ---- schema constraints --------------------------------------------------


def test_schema_check_rejects_unknown_label(store):
    with pytest.raises(Exception):
        store._conn.execute(
            "INSERT INTO entity_equivalence (entity_a, entity_b, label, "
            "created_at) VALUES (?, ?, ?, ?)",
            ("Apple", "apple", "kinda_same", "now"),
        )
        store._conn.commit()


def test_schema_check_rejects_non_canonical_ordering(store):
    """CHECK constraint enforces entity_a < entity_b at the SQL layer."""
    with pytest.raises(Exception):
        store._conn.execute(
            "INSERT INTO entity_equivalence (entity_a, entity_b, label, "
            "created_at) VALUES (?, ?, ?, ?)",
            # 'apple' > 'Apple' in case-sensitive lex, this violates CHECK.
            ("apple", "Apple", "different", "now"),
        )
        store._conn.commit()


def test_schema_check_rejects_self_pair(store):
    with pytest.raises(Exception):
        store._conn.execute(
            "INSERT INTO entity_equivalence (entity_a, entity_b, label, "
            "created_at) VALUES (?, ?, ?, ?)",
            ("Apple", "Apple", "same", "now"),
        )
        store._conn.commit()


def test_schema_unique_pair(store, oracle):
    oracle.record("NYC", "New York City", "same", "alias")
    with pytest.raises(Exception):
        store._conn.execute(
            "INSERT INTO entity_equivalence (entity_a, entity_b, label, "
            "created_at) VALUES (?, ?, ?, ?)",
            ("NYC", "New York City", "different", "now"),
        )
        store._conn.commit()


# ---- record() -------------------------------------------------------------


def test_record_inserts_with_zero_counts(oracle):
    row = oracle.record("NYC", "New York City", "same", "alias")
    assert isinstance(row, EntityEquivalenceRow)
    assert row.entity_a == "NYC"
    assert row.entity_b == "New York City"
    assert row.label == "same"
    assert row.affirmed_count == 0
    assert row.contradicted_count == 0
    assert row.id is not None


def test_record_canonicalizes_arguments(oracle):
    """Caller can pass entities in either order; record always
    stores the lex-smaller as entity_a."""
    row1 = oracle.record("New York City", "NYC", "same", "first")
    row2 = oracle.record("NYC", "New York City", "same", "updated")
    # Same row UPSERTed.
    assert row1.id == row2.id
    assert row2.reason == "updated"


def test_record_preserves_case(oracle):
    """apple and Apple are stored as distinct rows from apple and apple."""
    oracle.record("Apple", "apple", "different", "case disambig")
    # Confirm the stored row preserves case.
    rows = oracle.list_rows()
    assert any(
        r.entity_a == "Apple" and r.entity_b == "apple" for r in rows
    )


def test_record_upsert_preserves_counts(store, oracle):
    oracle.record("NYC", "New York City", "same", "first")
    # Manually bump counts as if Phase 8's operator endpoint had.
    store._conn.execute(
        "UPDATE entity_equivalence SET affirmed_count = 5, "
        "contradicted_count = 1 WHERE entity_a = ? AND entity_b = ?",
        ("NYC", "New York City"),
    )
    store._conn.commit()
    re_row = oracle.record("NYC", "New York City", "same", "second")
    assert re_row.affirmed_count == 5
    assert re_row.contradicted_count == 1
    assert re_row.reason == "second"


def test_record_rejects_unknown_label(oracle):
    with pytest.raises(ValueError, match="label"):
        oracle.record("NYC", "New York City", "kinda_same", None)


# ---- lookup() -------------------------------------------------------------


def test_lookup_miss_returns_none(oracle):
    assert oracle.lookup("NYC", "New York City") is None


def test_lookup_both_orderings_return_same_row(oracle):
    row = oracle.record("NYC", "New York City", "same", "alias")
    a = oracle.lookup("NYC", "New York City")
    b = oracle.lookup("New York City", "NYC")
    assert a and b
    assert a.id == b.id == row.id


def test_lookup_case_sensitive(oracle):
    """apple/Apple is a different pair from apple/apple-typed-2."""
    oracle.record("Apple", "apple", "different", "case disambig")
    # Same case yields a self-pair, rejected.
    with pytest.raises(ValueError):
        oracle.lookup("Apple", "Apple")
    # Different case found.
    found = oracle.lookup("Apple", "apple")
    assert found is not None
    assert found.label == "different"


def test_lookup_rejects_self_pair(oracle):
    with pytest.raises(ValueError):
        oracle.lookup("Apple", "Apple")


# ---- list_rows() ----------------------------------------------------------


def test_list_rows_empty(oracle):
    assert oracle.list_rows() == []


def test_list_rows_returns_all_in_order(oracle):
    oracle.record("NYC", "New York City", "same", None)
    oracle.record("Apple", "apple", "different", None)
    oracle.record("UN", "United Nations", "same", None)
    rows = oracle.list_rows()
    assert len(rows) == 3
    # Sorted by (entity_a, entity_b) — case-sensitive ASCII.
    # Capital letters sort before lowercase: 'A' < 'N' < 'U' < 'a'
    # so Apple/apple < NYC/New York City < UN/United Nations
    assert rows[0].entity_a == "Apple"
    assert rows[1].entity_a == "NYC"
    assert rows[2].entity_a == "UN"


# ---- consult() with mocked LLM -------------------------------------------


def test_consult_miss_calls_llm_and_writes_row(oracle):
    llm = _MockLLM(responses=[
        {"label": "same", "reason": "common abbreviation"},
    ])
    verdict = oracle.consult(
        "NYC", "New York City", llm=llm, source_turn_id=None,
    )
    assert isinstance(verdict, EntityEquivalenceVerdict)
    assert verdict.label == "same"
    assert verdict.served_from_cache is False
    assert verdict.classification_failed is False
    assert verdict.row_id is not None
    assert verdict.confidence == 0.5  # Beta(1,1) at zero counts
    assert len(llm.calls) == 1
    assert llm.calls[0]["purpose"] == "entity_equivalence"


def test_consult_hit_does_not_call_llm(oracle):
    llm = _MockLLM(responses=[
        {"label": "same", "reason": "alias"},
    ])
    oracle.consult("NYC", "New York City", llm=llm)
    assert len(llm.calls) == 1
    # Different argument order, same canonical pair.
    verdict = oracle.consult("New York City", "NYC", llm=llm)
    assert len(llm.calls) == 1  # unchanged
    assert verdict.served_from_cache is True
    assert verdict.label == "same"


def test_consult_hit_advances_last_consulted_at(oracle):
    llm = _MockLLM(responses=[
        {"label": "same", "reason": "alias"},
    ])
    oracle.consult("NYC", "New York City", llm=llm)
    first = oracle.lookup("NYC", "New York City")
    oracle.consult("NYC", "New York City", llm=llm)
    second = oracle.lookup("NYC", "New York City")
    assert second.last_consulted_at >= first.last_consulted_at


def test_consult_hit_does_not_increment_counts(oracle):
    """Principle 3: 50 consults of the same pair leave counts at 0."""
    llm = _MockLLM(responses=[
        {"label": "same", "reason": "alias"},
    ])
    for _ in range(50):
        oracle.consult("NYC", "New York City", llm=llm)
    assert len(llm.calls) == 1
    row = oracle.lookup("NYC", "New York City")
    assert row.affirmed_count == 0
    assert row.contradicted_count == 0


def test_consult_classification_failed_on_unknown_label(oracle):
    llm = _MockLLM(responses=[
        {"label": "kinda_same", "reason": "..."},
    ])
    verdict = oracle.consult("NYC", "New York City", llm=llm)
    assert verdict.classification_failed is True
    assert verdict.label is None
    assert verdict.row_id is None
    assert oracle.lookup("NYC", "New York City") is None


def test_consult_classification_failed_on_missing_reason(oracle):
    llm = _MockLLM(responses=[{"label": "same"}])  # no reason
    verdict = oracle.consult("NYC", "New York City", llm=llm)
    assert verdict.classification_failed is True


def test_consult_classification_failed_on_llm_exception(oracle):
    llm = _MockLLM(responses=[], raises=RuntimeError("api blew up"))
    verdict = oracle.consult("NYC", "New York City", llm=llm)
    assert verdict.classification_failed is True
    assert "api blew up" in (verdict.reason or "")


def test_consult_emits_oracle_consulted_event_on_hit(store, oracle):
    llm = _MockLLM(responses=[
        {"label": "same", "reason": "alias"},
    ])
    turn_id = store.insert_turn("user", "irrelevant")
    oracle.consult("NYC", "New York City", llm=llm, source_turn_id=turn_id)
    oracle.consult("NYC", "New York City", llm=llm, source_turn_id=turn_id)
    events = store.get_pipeline_events(turn_id)
    stages = [e["stage"] for e in events]
    assert stages.count("oracle_consulted") == 2
    assert "entity_equivalence_write" in stages
    assert "entity_equivalence_hit" in stages


def test_consult_emits_classification_failed_event(store, oracle):
    llm = _MockLLM(responses=[
        {"label": "weird", "reason": "nope"},
    ])
    turn_id = store.insert_turn("user", "irrelevant")
    oracle.consult(
        "NYC", "New York City", llm=llm, source_turn_id=turn_id,
    )
    events = store.get_pipeline_events(turn_id)
    stages = [e["stage"] for e in events]
    assert "entity_equivalence_classification_failed" in stages


def test_consult_without_llm_on_miss_raises(oracle):
    with pytest.raises(RuntimeError, match="no LLM provided"):
        oracle.consult("NYC", "New York City", llm=None)


def test_consult_without_llm_on_hit_works(oracle):
    oracle.record("NYC", "New York City", "same", "alias")
    verdict = oracle.consult("NYC", "New York City", llm=None)
    assert verdict.served_from_cache is True
    assert verdict.label == "same"


def test_consult_different_label(oracle):
    """The 'different' label is the high-stakes one — wrong-same
    calls contaminate the store. Confirm the oracle records and
    serves it correctly."""
    llm = _MockLLM(responses=[
        {"label": "different", "reason": "case disambiguation"},
    ])
    verdict = oracle.consult("apple", "Apple", llm=llm)
    assert verdict.label == "different"
    row = oracle.lookup("apple", "Apple")
    assert row.label == "different"


# ---- public constants -----------------------------------------------------


def test_label_set_is_closed():
    assert set(LABELS) == {"same", "different"}


def test_row_confidence_uses_counts(oracle):
    row = oracle.record("NYC", "New York City", "same", None)
    assert row.confidence() == 0.5
