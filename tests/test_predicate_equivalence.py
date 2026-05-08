"""Tests for src.layer3_substrate.predicate_equivalence.

Coverage:

  * Schema constraints: CHECK on label, slot_reversal, and
    predicate_a < predicate_b. UNIQUE on (pattern, predicate_a,
    predicate_b).
  * Canonical-pair helper: lowercase + strip; rejects self-pairs;
    no stem stripping.
  * lookup(): both orderings of the pair return the same row;
    pre-canonicalization (case folding) on inputs; miss returns None.
  * record(): inserts canonical row; UPSERT preserves counts;
    rejects unknown label and slot_reversal at the Python layer
    (validation before SQL CHECK).
  * list_rows(): full list and per-pattern filter; ordering.
  * consult() with mocked LLM:
      - cache miss → LLM call → row written → verdict carries
        served_from_cache=False.
      - cache hit → no LLM call → verdict carries served_from_cache=
        True; counts stay 0; last_consulted_at advances.
      - classification_failed: malformed LLM output emits the
        dedicated event, returns verdict with classification_failed=
        True and label=None, no row written.
      - oracle_consulted event fires on every call regardless of
        outcome.
  * Counts stay 0 across many consults (principle 3 unit test).
"""

from __future__ import annotations

import pytest

from src.fact_store import FactStore
from src.layer3_substrate.predicate_equivalence import (
    LABELS,
    SLOT_REVERSALS,
    PredicateEquivalence,
    PredicateEquivalenceRow,
    PredicateEquivalenceVerdict,
    _canonical_pair,
)


# ---- shared fixtures ------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "pe.db")
    yield s
    s.close()


@pytest.fixture
def oracle(store):
    return PredicateEquivalence(store)


class _MockLLM:
    """Queue-based LLM stub.

    ``responses`` is a list of dicts; each ``extract_with_tool`` call
    pops the next one. ``raises`` is an exception type to raise on
    the next call (one-shot). ``calls`` records every call for
    assertion. Compatible with the LLMClient.extract_with_tool
    signature (system, user_message, tool, purpose).
    """

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
    a, b, swapped = _canonical_pair("likes", "dislikes")
    assert a == "dislikes"
    assert b == "likes"
    assert swapped is True


def test_canonical_pair_already_sorted():
    a, b, swapped = _canonical_pair("authored_by", "wrote")
    assert a == "authored_by"
    assert b == "wrote"
    assert swapped is False


def test_canonical_pair_lowercases_inputs():
    a, b, _ = _canonical_pair("Likes", "DISLIKES")
    assert a == "dislikes"
    assert b == "likes"


def test_canonical_pair_strips_inputs():
    a, b, _ = _canonical_pair("  likes  ", "dislikes")
    assert a == "dislikes"
    assert b == "likes"


def test_canonical_pair_rejects_self_pair():
    with pytest.raises(ValueError, match="self-pairs"):
        _canonical_pair("likes", "likes")


def test_canonical_pair_rejects_self_pair_after_normalization():
    # 'Likes' and 'likes' fold to the same string.
    with pytest.raises(ValueError, match="self-pairs"):
        _canonical_pair("Likes", "likes")


def test_canonical_pair_rejects_empty_input():
    with pytest.raises(ValueError, match="non-empty"):
        _canonical_pair("", "likes")
    with pytest.raises(ValueError, match="non-empty"):
        _canonical_pair("likes", "")


def test_canonical_pair_does_not_strip_stems():
    """Architectural contract: no stem stripping. The oracle, not the
    canonical helper, decides whether is_likes ≡ likes."""
    a, b, swapped = _canonical_pair("is_likes", "likes")
    # Lex order: "is_likes" < "likes" (i < l)
    assert a == "is_likes"
    assert b == "likes"
    assert swapped is False
    # And these remain distinct rows in the table.
    a2, b2, _ = _canonical_pair("likes", "loves")
    assert (a, b) != (a2, b2)


# ---- schema constraints --------------------------------------------------


def test_schema_check_rejects_unknown_label(store):
    with pytest.raises(Exception):  # sqlite3.IntegrityError
        store._conn.execute(
            "INSERT INTO predicate_equivalence (pattern, predicate_a, "
            "predicate_b, label, slot_reversal, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("preference", "a", "b", "maybe_equivalent", "none", "now"),
        )
        store._conn.commit()


def test_schema_check_rejects_unknown_slot_reversal(store):
    with pytest.raises(Exception):
        store._conn.execute(
            "INSERT INTO predicate_equivalence (pattern, predicate_a, "
            "predicate_b, label, slot_reversal, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("preference", "a", "b", "equivalent", "weird_swap", "now"),
        )
        store._conn.commit()


def test_schema_check_rejects_non_canonical_ordering(store):
    """The CHECK constraint enforces predicate_a < predicate_b at the
    SQL layer. The canonical helper enforces it at the Python layer
    too, but the SQL CHECK is the safety net."""
    with pytest.raises(Exception):
        store._conn.execute(
            "INSERT INTO predicate_equivalence (pattern, predicate_a, "
            "predicate_b, label, slot_reversal, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            # 'likes' > 'dislikes' so this violates the CHECK.
            ("preference", "likes", "dislikes",
             "contradictory", "none", "now"),
        )
        store._conn.commit()


def test_schema_check_rejects_self_pair(store):
    """Self-pairs (predicate_a == predicate_b) violate the strict
    less-than CHECK."""
    with pytest.raises(Exception):
        store._conn.execute(
            "INSERT INTO predicate_equivalence (pattern, predicate_a, "
            "predicate_b, label, slot_reversal, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("preference", "likes", "likes",
             "equivalent", "none", "now"),
        )
        store._conn.commit()


def test_schema_unique_pattern_predicate_pair(store, oracle):
    oracle.record("preference", "dislikes", "likes",
                  "contradictory", "none", "antonyms")
    with pytest.raises(Exception):
        store._conn.execute(
            "INSERT INTO predicate_equivalence (pattern, predicate_a, "
            "predicate_b, label, slot_reversal, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("preference", "dislikes", "likes",
             "equivalent", "none", "now"),
        )
        store._conn.commit()


# ---- record() -------------------------------------------------------------


def test_record_inserts_with_zero_counts(oracle):
    row = oracle.record("preference", "likes", "dislikes",
                        "contradictory", "none", "antonym pair")
    assert isinstance(row, PredicateEquivalenceRow)
    assert row.pattern == "preference"
    assert row.predicate_a == "dislikes"  # canonicalized
    assert row.predicate_b == "likes"
    assert row.label == "contradictory"
    assert row.slot_reversal == "none"
    assert row.reason == "antonym pair"
    assert row.affirmed_count == 0
    assert row.contradicted_count == 0
    assert row.created_at is not None
    assert row.last_consulted_at is not None
    assert row.id is not None


def test_record_canonicalizes_arguments(oracle):
    """Caller can pass predicates in either order; record always
    stores the lex-smaller one as predicate_a."""
    row1 = oracle.record("preference", "likes", "dislikes",
                         "contradictory", "none", None)
    row2 = oracle.record("preference", "dislikes", "likes",
                         "contradictory", "none", "updated")
    # Same row UPSERTed; the canonical pair produced one row.
    assert row1.id == row2.id
    assert row2.reason == "updated"


def test_record_upsert_preserves_counts(store, oracle):
    """Counts are independent-external-evidence only. Re-recording a
    row with new label/reason MUST NOT touch the counts."""
    oracle.record("preference", "likes", "dislikes",
                  "contradictory", "none", "first")
    # Manually bump the counts as if the operator-action endpoint had
    # incremented them. Phase 8 owns the legitimate increment path;
    # this is a unit-level proxy.
    store._conn.execute(
        "UPDATE predicate_equivalence SET affirmed_count = 7, "
        "contradicted_count = 2 WHERE pattern = ? AND predicate_a = ? "
        "AND predicate_b = ?",
        ("preference", "dislikes", "likes"),
    )
    store._conn.commit()
    # Re-record. The classifier discovered the same pair and is
    # re-asserting the verdict — counts must stay (7, 2).
    re_row = oracle.record("preference", "likes", "dislikes",
                           "contradictory", "none", "second")
    assert re_row.affirmed_count == 7
    assert re_row.contradicted_count == 2
    assert re_row.reason == "second"


def test_record_rejects_unknown_label(oracle):
    with pytest.raises(ValueError, match="label"):
        oracle.record("preference", "likes", "dislikes",
                      "kinda_equivalent", "none", None)


def test_record_rejects_unknown_slot_reversal(oracle):
    with pytest.raises(ValueError, match="slot_reversal"):
        oracle.record("preference", "likes", "dislikes",
                      "contradictory", "weird", None)


# ---- lookup() -------------------------------------------------------------


def test_lookup_miss_returns_none(oracle):
    assert oracle.lookup("preference", "likes", "dislikes") is None


def test_lookup_both_orderings_return_same_row(oracle):
    row = oracle.record("preference", "likes", "dislikes",
                        "contradictory", "none", "antonyms")
    a = oracle.lookup("preference", "likes", "dislikes")
    b = oracle.lookup("preference", "dislikes", "likes")
    assert a is not None and b is not None
    assert a.id == b.id == row.id


def test_lookup_normalizes_case(oracle):
    oracle.record("preference", "likes", "dislikes",
                  "contradictory", "none", None)
    found = oracle.lookup("preference", "Likes", "DISLIKES")
    assert found is not None


def test_lookup_does_not_strip_stems(oracle):
    """is_likes and likes are distinct rows in the table; the helper
    does not stem-strip. The oracle would classify (is_likes, likes)
    as equivalent if asked, but the lookup helper doesn't pre-fold
    them."""
    oracle.record("preference", "likes", "loves",
                  "distinct", "none", "intensity")
    # Looking up an unrelated pair shouldn't hit the loves row.
    miss = oracle.lookup("preference", "is_likes", "loves")
    assert miss is None


def test_lookup_rejects_self_pair(oracle):
    with pytest.raises(ValueError):
        oracle.lookup("preference", "likes", "likes")


# ---- list_rows() ----------------------------------------------------------


def test_list_rows_empty(oracle):
    assert oracle.list_rows() == []


def test_list_rows_returns_all_in_order(oracle):
    oracle.record("preference", "likes", "dislikes",
                  "contradictory", "none", None)
    oracle.record("relational", "wrote", "authored_by",
                  "equivalent", "subject_object_swap", None)
    oracle.record("preference", "loves", "hates",
                  "contradictory", "none", None)
    rows = oracle.list_rows()
    assert len(rows) == 3
    # Sorted by (pattern, predicate_a, predicate_b)
    assert rows[0].pattern == "preference"
    assert rows[0].predicate_a == "dislikes"
    assert rows[1].pattern == "preference"
    assert rows[1].predicate_a == "hates"
    assert rows[2].pattern == "relational"


def test_list_rows_filtered_by_pattern(oracle):
    oracle.record("preference", "likes", "dislikes",
                  "contradictory", "none", None)
    oracle.record("relational", "wrote", "authored_by",
                  "equivalent", "subject_object_swap", None)
    rows = oracle.list_rows(pattern="preference")
    assert len(rows) == 1
    assert rows[0].pattern == "preference"


# ---- consult() with mocked LLM -------------------------------------------


def test_consult_miss_calls_llm_and_writes_row(store, oracle):
    llm = _MockLLM(responses=[
        {"label": "contradictory", "slot_reversal": "none",
         "reason": "antonym predicates"},
    ])
    verdict = oracle.consult(
        "preference", "likes", "dislikes",
        llm=llm, source_turn_id=None,
    )
    assert isinstance(verdict, PredicateEquivalenceVerdict)
    assert verdict.label == "contradictory"
    assert verdict.slot_reversal == "none"
    assert verdict.served_from_cache is False
    assert verdict.classification_failed is False
    assert verdict.row_id is not None
    assert verdict.confidence == 0.5  # Beta(1,1) at zero counts
    assert len(llm.calls) == 1
    assert llm.calls[0]["purpose"] == "predicate_equivalence"


def test_consult_hit_does_not_call_llm(oracle):
    """First call writes the row; second call serves from cache."""
    llm = _MockLLM(responses=[
        {"label": "contradictory", "slot_reversal": "none",
         "reason": "antonym predicates"},
    ])
    oracle.consult("preference", "likes", "dislikes", llm=llm)
    assert len(llm.calls) == 1
    # Second consult — different argument order, same pair after
    # canonicalization. Must hit the cache, NOT call the LLM.
    verdict = oracle.consult(
        "preference", "dislikes", "likes", llm=llm,
    )
    assert len(llm.calls) == 1  # unchanged
    assert verdict.served_from_cache is True
    assert verdict.label == "contradictory"


def test_consult_hit_advances_last_consulted_at(oracle):
    llm = _MockLLM(responses=[
        {"label": "contradictory", "slot_reversal": "none",
         "reason": "antonyms"},
    ])
    oracle.consult("preference", "likes", "dislikes", llm=llm)
    first = oracle.lookup("preference", "likes", "dislikes")
    # Second consult — hit; bumps last_consulted_at.
    oracle.consult("preference", "likes", "dislikes", llm=llm)
    second = oracle.lookup("preference", "likes", "dislikes")
    assert first.last_consulted_at is not None
    assert second.last_consulted_at is not None
    assert second.last_consulted_at >= first.last_consulted_at


def test_consult_hit_does_not_increment_counts(oracle):
    """Principle 3: reads are not writes. 50 consults of the same
    pair must leave (affirmed_count, contradicted_count) at (0, 0)."""
    llm = _MockLLM(responses=[
        {"label": "contradictory", "slot_reversal": "none",
         "reason": "antonyms"},
    ])
    for _ in range(50):
        oracle.consult("preference", "likes", "dislikes", llm=llm)
    assert len(llm.calls) == 1
    row = oracle.lookup("preference", "likes", "dislikes")
    assert row.affirmed_count == 0
    assert row.contradicted_count == 0


def test_consult_classification_failed_returns_soft_miss(store, oracle):
    """Malformed LLM output produces a verdict that the caller treats
    as a soft miss. No row is written."""
    llm = _MockLLM(responses=[
        {"label": "maybe_equivalent",  # not in LABELS
         "slot_reversal": "none", "reason": "I'm not sure"},
    ])
    verdict = oracle.consult(
        "preference", "likes", "dislikes", llm=llm,
    )
    assert verdict.classification_failed is True
    assert verdict.label is None
    assert verdict.row_id is None
    assert verdict.served_from_cache is False
    # No row written.
    assert oracle.lookup("preference", "likes", "dislikes") is None


def test_consult_classification_failed_on_missing_reason(oracle):
    llm = _MockLLM(responses=[
        {"label": "equivalent", "slot_reversal": "none"},  # no reason
    ])
    verdict = oracle.consult(
        "preference", "likes", "loves", llm=llm,
    )
    assert verdict.classification_failed is True


def test_consult_classification_failed_on_unknown_slot_reversal(oracle):
    llm = _MockLLM(responses=[
        {"label": "equivalent", "slot_reversal": "weird_swap",
         "reason": "yes"},
    ])
    verdict = oracle.consult(
        "preference", "likes", "loves", llm=llm,
    )
    assert verdict.classification_failed is True


def test_consult_classification_failed_on_llm_exception(oracle):
    llm = _MockLLM(responses=[], raises=RuntimeError("api blew up"))
    verdict = oracle.consult(
        "preference", "likes", "dislikes", llm=llm,
    )
    assert verdict.classification_failed is True
    assert "api blew up" in (verdict.reason or "")


def test_consult_emits_oracle_consulted_event_on_hit(store, oracle):
    """The trace UI grep-target ``oracle_consulted`` fires on every
    consultation, including cache hits."""
    llm = _MockLLM(responses=[
        {"label": "contradictory", "slot_reversal": "none",
         "reason": "antonyms"},
    ])
    turn_id = store.insert_turn("user", "irrelevant")
    oracle.consult(
        "preference", "likes", "dislikes",
        llm=llm, source_turn_id=turn_id,
    )
    oracle.consult(
        "preference", "likes", "dislikes",
        llm=llm, source_turn_id=turn_id,
    )
    events = store.get_pipeline_events(turn_id)
    stages = [e["stage"] for e in events]
    assert stages.count("oracle_consulted") == 2
    assert "predicate_equivalence_write" in stages
    assert "predicate_equivalence_hit" in stages


def test_consult_emits_classification_failed_event(store, oracle):
    llm = _MockLLM(responses=[
        {"label": "weird", "slot_reversal": "none", "reason": "nope"},
    ])
    turn_id = store.insert_turn("user", "irrelevant")
    oracle.consult(
        "preference", "likes", "dislikes",
        llm=llm, source_turn_id=turn_id,
    )
    events = store.get_pipeline_events(turn_id)
    stages = [e["stage"] for e in events]
    assert "predicate_equivalence_classification_failed" in stages
    failure_event = next(
        e for e in events
        if e["stage"] == "predicate_equivalence_classification_failed"
    )
    assert failure_event["data"]["pattern"] == "preference"
    assert "label" in failure_event["data"]["reason"]


def test_consult_without_llm_on_miss_raises(oracle):
    """The caller must supply an LLM when the cache will miss; else
    the oracle has no way to classify the pair."""
    with pytest.raises(RuntimeError, match="no LLM provided"):
        oracle.consult("preference", "likes", "dislikes", llm=None)


def test_consult_without_llm_on_hit_works(oracle):
    """Warm-cache lookup needs no LLM."""
    oracle.record("preference", "likes", "dislikes",
                  "contradictory", "none", "antonyms")
    verdict = oracle.consult(
        "preference", "likes", "dislikes", llm=None,
    )
    assert verdict.served_from_cache is True
    assert verdict.label == "contradictory"


# ---- equivalent + slot_reversal classification ---------------------------


def test_consult_active_passive_records_subject_object_swap(oracle):
    """Phase 3 deliverable: the oracle CLASSIFIES active/passive
    correctly. Tier U's consumption of slot_reversal is deferred to
    Phase 4/7, but the row in the table is right."""
    llm = _MockLLM(responses=[
        {"label": "equivalent", "slot_reversal": "subject_object_swap",
         "reason": "active/passive of the same authorship relation"},
    ])
    verdict = oracle.consult(
        "relational", "wrote", "authored_by", llm=llm,
    )
    assert verdict.label == "equivalent"
    assert verdict.slot_reversal == "subject_object_swap"
    row = oracle.lookup("relational", "wrote", "authored_by")
    assert row.label == "equivalent"
    assert row.slot_reversal == "subject_object_swap"


def test_consult_distinct_records_none_reversal(oracle):
    llm = _MockLLM(responses=[
        {"label": "distinct", "slot_reversal": "none",
         "reason": "different intensity"},
    ])
    verdict = oracle.consult("preference", "likes", "loves", llm=llm)
    assert verdict.label == "distinct"
    assert verdict.slot_reversal == "none"


# ---- public constants -----------------------------------------------------


def test_label_set_is_closed():
    assert set(LABELS) == {"equivalent", "contradictory", "distinct"}


def test_slot_reversal_set_is_closed():
    assert set(SLOT_REVERSALS) == {
        "none", "subject_object_swap", "participant_reorder"
    }


def test_row_confidence_uses_counts(oracle):
    row = oracle.record("preference", "likes", "dislikes",
                        "contradictory", "none", None)
    # Initial counts (0, 0) → 0.5
    assert row.confidence() == 0.5
