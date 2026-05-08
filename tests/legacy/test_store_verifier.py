"""Unit tests for ``store_lookup_verify`` (v0.14 Phase 8.6 — A1).

Phase 8.6's lookup filter restricts Tier 2 (user store) matches to
``asserted_by="user"`` rows only. Pre-Phase-8.6 the lookup matched
against ANY fact in the table — including ``python_verifier``-asserted
corrected values from the user-world-claim contradiction path. That
silent cross-author match mis-badged downstream traces as "served
from user_store" when the user had never made the claim. The fix
restores the architectural commitment that Tier 2 holds user-asserted
facts only.

The strawberry case in the wild:

  1. User asks "How many r's are in strawberry?" — extractor (pre-fix)
     confabulates value=2.
  2. Python verifier corrects to 3, stores corrected fact with
     ``asserted_by="python_verifier"``.
  3. Model later says "I count 3 r's" — assistant-side extraction
     produces the same has_count claim shape, value=3.
  4. ``store_lookup_verify`` matches against the python_verifier row
     and returns MATCH; the trace shows "served from user_store
     (matched user fact id=N)".

After A1 the lookup ignores the python_verifier row; the assistant's
claim falls through to fresh verification (or to Tier 3, the
verification_cache). The audit trail is preserved (the dual-write
storage path is untouched); only the lookup rule is corrected.
"""

from __future__ import annotations

import pytest

from src.legacy.fact_store import Fact, FactStore
from src.legacy.verifiers.store_verifier import (
    StoreLookupOutcome,
    store_lookup_verify,
)


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "store_verifier.db")
    yield s
    s.close()


def _claim(*, pattern, predicate, slots, polarity=1):
    return {
        "pattern": pattern,
        "predicate": predicate,
        "slots": dict(slots),
        "polarity": polarity,
    }


def _insert_fact(store, *, pattern, predicate, slots, polarity, asserted_by):
    return store.insert_fact(Fact(
        pattern=pattern, predicate=predicate, slots=dict(slots),
        polarity=polarity, asserted_by=asserted_by,
        verification_status=(
            "user_asserted" if asserted_by == "user" else "verified"
        ),
    ))


def test_user_asserted_fact_matches(store):
    """The baseline: a user-asserted fact matches a same-shaped query."""
    _insert_fact(
        store, pattern="preference", predicate="loves",
        slots={"agent": "user", "object": "sushi"}, polarity=1,
        asserted_by="user",
    )
    claim = _claim(pattern="preference", predicate="loves",
                   slots={"agent": "user", "object": "sushi"})
    result = store_lookup_verify(claim, store, key_slot_names=["agent", "object"])
    assert result.outcome is StoreLookupOutcome.MATCH
    assert result.matching_fact is not None
    assert result.matching_fact.asserted_by == "user"


def test_python_verifier_fact_does_not_match(store):
    """**Phase 8.6 A1.** A ``python_verifier``-asserted fact MUST NOT
    match user-side lookup. Pre-fix this returned MATCH; post-fix MISS.

    Concrete trace: the strawberry case stored a corrected has_count
    fact under asserted_by='python_verifier'. The model's later "3 r's"
    claim hit Tier 2 against this fact, mis-badging the verdict as
    user-confirmed. With the filter, the lookup ignores it."""
    _insert_fact(
        store, pattern="quantitative", predicate="has_count",
        slots={"subject": "strawberry", "property": "letter_r", "value": 3},
        polarity=1, asserted_by="python_verifier",
    )
    claim = _claim(
        pattern="quantitative", predicate="has_count",
        slots={"subject": "strawberry", "property": "letter_r", "value": 3},
    )
    result = store_lookup_verify(
        claim, store, key_slot_names=["subject", "property"],
    )
    assert result.outcome is StoreLookupOutcome.MISS, (
        f"python_verifier-asserted fact matched user-side lookup: "
        f"{result.outcome}. Phase 8.6 A1 filter not enforced."
    )


def test_model_asserted_fact_does_not_match(store):
    """Symmetry check: model-asserted facts are also not user-store
    matches. Phase 8.6 doesn't carve out separate behavior for model
    rows; only ``asserted_by="user"`` qualifies."""
    _insert_fact(
        store, pattern="preference", predicate="loves",
        slots={"agent": "user", "object": "olives"}, polarity=1,
        asserted_by="model",
    )
    claim = _claim(pattern="preference", predicate="loves",
                   slots={"agent": "user", "object": "olives"})
    result = store_lookup_verify(claim, store, key_slot_names=["agent", "object"])
    assert result.outcome is StoreLookupOutcome.MISS


def test_external_asserted_fact_does_not_match(store):
    _insert_fact(
        store, pattern="preference", predicate="loves",
        slots={"agent": "user", "object": "ramen"}, polarity=1,
        asserted_by="external",
    )
    claim = _claim(pattern="preference", predicate="loves",
                   slots={"agent": "user", "object": "ramen"})
    result = store_lookup_verify(claim, store, key_slot_names=["agent", "object"])
    assert result.outcome is StoreLookupOutcome.MISS


def test_user_contradiction_still_fires(store):
    """Polarity-flipped user-asserted facts continue to surface as
    CONTRADICTION. The filter restricts to user-asserted rows but
    doesn't change polarity-flip semantics."""
    _insert_fact(
        store, pattern="preference", predicate="loves",
        slots={"agent": "user", "object": "kale"}, polarity=0,
        asserted_by="user",
    )
    claim = _claim(pattern="preference", predicate="loves",
                   slots={"agent": "user", "object": "kale"}, polarity=1)
    result = store_lookup_verify(claim, store, key_slot_names=["agent", "object"])
    assert result.outcome is StoreLookupOutcome.CONTRADICTION
    assert result.contradicting_fact is not None
    assert result.contradicting_fact.asserted_by == "user"


def test_python_verifier_contradiction_does_not_fire(store):
    """Mirror of the match case for the contradiction branch: a
    python_verifier-asserted opposite-polarity row MUST NOT surface
    as a Tier 2 contradiction. Phase 8.6 A1's filter applies to both
    branches (find_currently_valid AND find_contradictions)."""
    _insert_fact(
        store, pattern="preference", predicate="loves",
        slots={"agent": "user", "object": "kale"}, polarity=0,
        asserted_by="python_verifier",
    )
    claim = _claim(pattern="preference", predicate="loves",
                   slots={"agent": "user", "object": "kale"}, polarity=1)
    result = store_lookup_verify(claim, store, key_slot_names=["agent", "object"])
    assert result.outcome is StoreLookupOutcome.MISS, (
        f"python_verifier opposite-polarity fact surfaced as Tier 2 "
        f"contradiction: {result.outcome}"
    )


def test_strawberry_regression_python_verifier_only(store):
    """**Phase 8.6 regression test for the canonical strawberry bug.**

    Reproduce ONLY the python_verifier-asserted "corrected value"
    storage state — no user-asserted row of any shape — and confirm
    the lookup returns MISS. Pre-A1 this returned MATCH and mis-
    badged the trace as "served from user_store". Post-A1 it falls
    through cleanly so fresh verification (or Tier 3 cache) handles
    the model's claim."""
    _insert_fact(
        store, pattern="quantitative", predicate="has_count",
        slots={"subject": "strawberry", "property": "letter_r", "value": 3},
        polarity=1, asserted_by="python_verifier",
    )
    claim = _claim(
        pattern="quantitative", predicate="has_count",
        slots={"subject": "strawberry", "property": "letter_r", "value": 3},
    )
    result = store_lookup_verify(
        claim, store, key_slot_names=["subject", "property"],
    )
    assert result.outcome is StoreLookupOutcome.MISS, (
        f"python_verifier-only state matched as user-asserted: "
        f"{result.outcome}. matching_fact={result.matching_fact}"
    )


def test_strawberry_regression_dual_write_finds_user_row_only(store):
    """**Phase 8.6 dual-write trace state.** When BOTH the user's
    wrong version (asserted_by='user') and the python_verifier
    corrected version exist (the v0.10 dual-write path), the lookup
    matches the USER row only — the python_verifier row is invisible
    at Tier 2.

    The matched row may be the user's wrong-value row (value=2) since
    key_slot_names don't include `value` for has_count. That's fine
    architecturally: the lookup tells the caller "the user said this
    same shape with these key slots"; the value-level mismatch is the
    caller's concern. What the filter MUST guarantee is that the
    matched row's asserted_by is 'user' — never 'python_verifier'."""
    _insert_fact(
        store, pattern="quantitative", predicate="has_count",
        slots={"subject": "strawberry", "property": "letter_r", "value": 3},
        polarity=1, asserted_by="python_verifier",
    )
    _insert_fact(
        store, pattern="quantitative", predicate="has_count",
        slots={"subject": "strawberry", "property": "letter_r", "value": 2},
        polarity=1, asserted_by="user",
    )
    claim = _claim(
        pattern="quantitative", predicate="has_count",
        slots={"subject": "strawberry", "property": "letter_r", "value": 3},
    )
    result = store_lookup_verify(
        claim, store, key_slot_names=["subject", "property"],
    )
    # The lookup matches by (subject, property), not value, so we
    # expect a match against the user row.
    assert result.outcome is StoreLookupOutcome.MATCH
    assert result.matching_fact is not None
    assert result.matching_fact.asserted_by == "user", (
        f"matched fact's asserted_by={result.matching_fact.asserted_by!r}; "
        f"Phase 8.6 A1 filter must guarantee 'user' on Tier 2 matches"
    )
