"""Phase 3-4 integration tests: Tier U + substrate oracles.

The cheetahs case is the **gate** for Phase 3 and a regression
target for Phase 4. If ``test_cheetahs_case_via_oracle`` fails,
neither phase ships.

The cheetahs case demonstrates the architecture's central claim: that
bounded LLM oracles compose into a system that correctly resolves
antonym + polarity-flip cases. v0.13 (the predecessor) treats
``(preference, dislikes, polarity=1)`` and
``(preference, likes, polarity=0)`` as different propositions because
the literal-match SQL filter requires identical predicate strings;
the v1 cache's stem-stripping + Jaccard token similarity doesn't
catch the antonym case (``likes`` and ``dislikes`` have zero token
overlap after stem stripping). The oracle bridges them by
classifying ``(likes, dislikes) -> contradictory + none`` and
returning a verdict that Tier U interprets as "polarity flip yields
match."

Coverage in this file:

  * Cheetahs case end-to-end: stored ``dislikes`` p=1, claim
    ``likes`` p=0 → Tier U returns MATCH with polarity_flipped=True
    and the matching fact's predicate is dislikes.
  * Distinct-but-related: stored ``likes`` p=1, claim ``loves`` p=1
    → Tier U returns MISS (oracle says distinct; no signal).
  * Literal match short-circuits the oracle (no LLM call).
  * Literal contradiction short-circuits the oracle.
  * Oracle's slot_reversal != 'none' is correctly classified but
    NOT consumed by Phase 3's tier_u — falls through to MISS.
  * Oracle classification_failed → falls through to MISS without
    crashing the lookup.
  * Multiple candidates: tier_u iterates and picks the first
    candidate whose oracle verdict produces a signal.
  * Warm-cache: second invocation of the cheetahs case incurs no
    LLM call (the oracle row is already there).
"""

from __future__ import annotations

import pytest

from src.fact_store import Fact, FactStore
from src.layer3_substrate.entity_equivalence import (
    EntityEquivalence,
)
from src.layer3_substrate.predicate_equivalence import (
    PredicateEquivalence,
)
from src.layer4_lookup.tier_u import (
    TierUOutcome,
    TierUResult,
    lookup,
)


# ---- shared fixtures + LLM stub ------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "tier_u.db")
    yield s
    s.close()


@pytest.fixture
def oracle(store):
    return PredicateEquivalence(store)


class _MockLLM:
    """Queue-backed stub (matches LLMClient.extract_with_tool)."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def extract_with_tool(self, *, system, user_message, tool, purpose):
        self.calls.append({"user_message": user_message, "purpose": purpose})
        if not self.responses:
            raise AssertionError(
                f"MockLLM ran out of responses for purpose={purpose}; "
                f"unexpected LLM call (cache should have served it)"
            )
        return self.responses.pop(0)


def _store_user_fact(
    store: FactStore, *, pattern: str, predicate: str,
    slots: dict, polarity: int,
) -> Fact:
    fact_id = store.insert_fact(Fact(
        pattern=pattern, predicate=predicate, slots=dict(slots),
        polarity=polarity, asserted_by="user",
        verification_status="user_asserted",
    ))
    fact = store.get_fact(fact_id)
    assert fact is not None
    return fact


# ---- THE GATE -------------------------------------------------------------


def test_cheetahs_case_via_oracle(store, oracle):
    """The canonical Phase 3 gate.

    Setup: user previously stated "I really dislike cheetahs" — the
    extractor produced ``(preference, dislikes, agent=user,
    object=cheetahs, polarity=1)``. Stored in Tier U.

    Now the model says "you really don't like cheetahs" — the
    extractor produces ``(preference, likes, agent=user,
    object=cheetahs, polarity=0)``. Tier U lookup must find the
    stored fact via ``predicate_equivalence`` resolving
    ``(likes, dislikes) -> contradictory + none`` and applying the
    polarity flip.
    """
    stored = _store_user_fact(
        store,
        pattern="preference", predicate="dislikes",
        slots={"agent": "user", "object": "cheetahs"},
        polarity=1,
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "cheetahs"},
        "polarity": 0, "source_text": "you really don't like cheetahs",
    }
    llm = _MockLLM(responses=[
        {"label": "contradictory", "slot_reversal": "none",
         "reason": "antonym predicates over the same agent/object"},
    ])

    result = lookup(
        claim, store, oracle,
        key_slot_names=["agent", "object"], llm=llm,
    )

    assert result.outcome is TierUOutcome.MATCH, (
        f"cheetahs case must MATCH; got {result.outcome.value}"
    )
    assert result.matching_fact is not None
    assert result.matching_fact.id == stored.id
    assert result.matching_fact.predicate == "dislikes"
    assert result.matching_fact.polarity == 1
    assert result.via == ["predicate_equivalence"]
    assert result.predicate_equivalence_row_id is not None
    assert result.polarity_flipped is True
    assert result.slot_reversal_applied is False
    # The oracle's row is in the table for the operator to inspect.
    row = oracle.lookup("preference", "likes", "dislikes")
    assert row is not None
    assert row.label == "contradictory"


def test_cheetahs_case_warm_cache_no_llm_call(store, oracle):
    """Second invocation must not call the LLM."""
    _store_user_fact(
        store, pattern="preference", predicate="dislikes",
        slots={"agent": "user", "object": "cheetahs"}, polarity=1,
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "cheetahs"},
        "polarity": 0, "source_text": "you really don't like cheetahs",
    }
    llm = _MockLLM(responses=[
        {"label": "contradictory", "slot_reversal": "none",
         "reason": "antonyms"},
    ])
    # First call: LLM runs.
    lookup(claim, store, oracle,
           key_slot_names=["agent", "object"], llm=llm)
    assert len(llm.calls) == 1
    # Second call: cache hit, no LLM call.
    result = lookup(claim, store, oracle,
                    key_slot_names=["agent", "object"], llm=llm)
    assert len(llm.calls) == 1  # unchanged
    assert result.outcome is TierUOutcome.MATCH
    assert result.via == ["predicate_equivalence"]
    assert result.polarity_flipped is True


# ---- distinct negative case (gates against over-merging) -----------------


def test_distinct_predicates_yield_miss(store, oracle):
    """User stored 'I like olives'; model says 'you love olives'.

    likes/loves differ in intensity — the oracle should classify
    them as DISTINCT, and Tier U should fall through to MISS rather
    than MATCH on a wrong-equivalent verdict.
    """
    _store_user_fact(
        store, pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"}, polarity=1,
    )
    claim = {
        "pattern": "preference", "predicate": "loves",
        "slots": {"agent": "user", "object": "olives"},
        "polarity": 1, "source_text": "you love olives",
    }
    llm = _MockLLM(responses=[
        {"label": "distinct", "slot_reversal": "none",
         "reason": "same direction, different intensity"},
    ])

    result = lookup(claim, store, oracle,
                    key_slot_names=["agent", "object"], llm=llm)

    assert result.outcome is TierUOutcome.MISS
    assert result.via == []
    assert result.matching_fact is None
    assert result.contradicting_fact is None
    # The oracle row IS written though — future calls hit the cache.
    row = oracle.lookup("preference", "likes", "loves")
    assert row is not None
    assert row.label == "distinct"


# ---- literal-match short-circuits ----------------------------------------


def test_literal_match_no_oracle_call(store, oracle):
    """Same predicate + same polarity → MATCH via literal lookup; the
    oracle is never consulted."""
    stored = _store_user_fact(
        store, pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"}, polarity=1,
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "olives"},
        "polarity": 1, "source_text": "user likes olives",
    }
    llm = _MockLLM(responses=[])  # would AssertionError if consulted

    result = lookup(claim, store, oracle,
                    key_slot_names=["agent", "object"], llm=llm)

    assert result.outcome is TierUOutcome.MATCH
    assert result.matching_fact.id == stored.id
    assert result.via == []
    assert result.polarity_flipped is False
    assert len(llm.calls) == 0


def test_literal_contradiction_no_oracle_call(store, oracle):
    """Same predicate + opposite polarity → CONTRADICTION via literal
    lookup; no oracle consultation."""
    stored = _store_user_fact(
        store, pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "olives"}, polarity=1,
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "olives"},
        "polarity": 0, "source_text": "user does not like olives",
    }
    llm = _MockLLM(responses=[])

    result = lookup(claim, store, oracle,
                    key_slot_names=["agent", "object"], llm=llm)

    assert result.outcome is TierUOutcome.CONTRADICTION
    assert result.contradicting_fact.id == stored.id
    assert result.via == []
    assert len(llm.calls) == 0


# ---- equivalent + same polarity (oracle MATCH on same-direction synonyms)


def test_oracle_equivalent_same_polarity_yields_match(store, oracle):
    """If the user said 'I was born in Paris' (predicate=was_born_in)
    and the model says 'you were born in Paris' (predicate=born_in),
    the oracle classifies (born_in, was_born_in) as
    equivalent + none. Tier U returns MATCH with no polarity flip."""
    stored = _store_user_fact(
        store, pattern="spatial_temporal", predicate="was_born_in",
        slots={"entity": "user", "location": "Paris"}, polarity=1,
    )
    claim = {
        "pattern": "spatial_temporal", "predicate": "born_in",
        "slots": {"entity": "user", "location": "Paris"},
        "polarity": 1, "source_text": "you were born in Paris",
    }
    llm = _MockLLM(responses=[
        {"label": "equivalent", "slot_reversal": "none",
         "reason": "surface tense variation"},
    ])

    result = lookup(claim, store, oracle,
                    key_slot_names=["entity", "location"], llm=llm)

    assert result.outcome is TierUOutcome.MATCH
    assert result.matching_fact.id == stored.id
    assert result.via == ["predicate_equivalence"]
    assert result.polarity_flipped is False
    assert result.slot_reversal_applied is False


def test_oracle_equivalent_opposite_polarity_yields_contradiction(
    store, oracle,
):
    stored = _store_user_fact(
        store, pattern="spatial_temporal", predicate="was_born_in",
        slots={"entity": "user", "location": "Paris"}, polarity=1,
    )
    claim = {
        "pattern": "spatial_temporal", "predicate": "born_in",
        "slots": {"entity": "user", "location": "Paris"},
        "polarity": 0, "source_text": "you were not born in Paris",
    }
    llm = _MockLLM(responses=[
        {"label": "equivalent", "slot_reversal": "none",
         "reason": "surface tense variation"},
    ])

    result = lookup(claim, store, oracle,
                    key_slot_names=["entity", "location"], llm=llm)

    assert result.outcome is TierUOutcome.CONTRADICTION
    assert result.contradicting_fact.id == stored.id
    assert result.polarity_flipped is False


def test_oracle_contradictory_same_polarity_yields_contradiction(
    store, oracle,
):
    """Stored: (dislikes, p=1) — "user dislikes cheetahs".
    Claim:  (likes, p=1)    — "you really do like cheetahs".

    Oracle: (likes, dislikes) → contradictory + none. Both predicates
    are positively asserted (same polarity); since they're antonyms,
    the assertions disagree → CONTRADICTION with polarity_flipped=True.

    This is the symmetric counterpart to the cheetahs-case MATCH:
    contradictory verdicts produce MATCH on opposite polarities and
    CONTRADICTION on same polarities.
    """
    stored = _store_user_fact(
        store, pattern="preference", predicate="dislikes",
        slots={"agent": "user", "object": "cheetahs"}, polarity=1,
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "cheetahs"},
        "polarity": 1, "source_text": "you really do like cheetahs",
    }
    llm = _MockLLM(responses=[
        {"label": "contradictory", "slot_reversal": "none",
         "reason": "antonyms"},
    ])

    result = lookup(claim, store, oracle,
                    key_slot_names=["agent", "object"], llm=llm)

    assert result.outcome is TierUOutcome.CONTRADICTION
    assert result.contradicting_fact.id == stored.id
    assert result.polarity_flipped is True


# ---- slot_reversal != 'none' is classified but not consumed --------------


def test_oracle_slot_reversal_swap_falls_through_to_miss(store, oracle):
    """Phase 3 deferment: oracle classifies active/passive correctly,
    but Tier U doesn't apply the swap. The candidate set is
    identity-slots-as-is only; a stored fact with swapped slot values
    isn't in the candidate set, AND even if it were, the oracle's
    slot_reversal != 'none' verdict produces no signal in Phase 3."""
    # Stored under SAME identity slots as the claim, but different
    # predicate that the oracle will say swaps args.
    stored = _store_user_fact(
        store, pattern="relational", predicate="parent_of",
        slots={"subject": "Alice", "object": "Bob"}, polarity=1,
    )
    claim = {
        "pattern": "relational", "predicate": "child_of",
        "slots": {"subject": "Alice", "object": "Bob"},
        "polarity": 1, "source_text": "Alice is a child of Bob",
    }
    llm = _MockLLM(responses=[
        {"label": "equivalent", "slot_reversal": "subject_object_swap",
         "reason": "active/passive of parenthood"},
    ])

    result = lookup(claim, store, oracle,
                    key_slot_names=["subject", "object"], llm=llm)

    # The candidate IS in the set (identity slots match as-is). The
    # oracle classifies the predicate pair correctly. But
    # slot_reversal != 'none' produces no Phase 3 signal — the slot
    # values DON'T actually agree under the swap mapping (Alice and
    # Bob are in the same positions, not swapped). So MISS is the
    # correct outcome, and the deferment is consistent with the
    # underlying semantics.
    assert result.outcome is TierUOutcome.MISS
    # The oracle row was written though — Phase 4/7 picks it up.
    row = oracle.lookup("relational", "parent_of", "child_of")
    assert row is not None
    assert row.slot_reversal == "subject_object_swap"
    # Stored fact was untouched.
    untouched = store.get_fact(stored.id)
    assert untouched.predicate == "parent_of"


# ---- oracle classification_failed gracefully degrades --------------------


def test_classification_failed_falls_through_to_miss(store, oracle):
    """A malformed LLM response from the oracle must not crash the
    Tier U lookup. The oracle returns
    classification_failed=True; tier_u treats it as no-signal and
    continues to the next candidate (or returns MISS at the end)."""
    _store_user_fact(
        store, pattern="preference", predicate="dislikes",
        slots={"agent": "user", "object": "cheetahs"}, polarity=1,
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "cheetahs"},
        "polarity": 0, "source_text": "you really don't like cheetahs",
    }
    llm = _MockLLM(responses=[
        # Malformed: label not in LABELS
        {"label": "kinda", "slot_reversal": "none", "reason": "I don't know"},
    ])

    result = lookup(claim, store, oracle,
                    key_slot_names=["agent", "object"], llm=llm)

    assert result.outcome is TierUOutcome.MISS
    # The oracle emitted the failure event but didn't crash the turn.
    # No row was written to the table.
    assert oracle.lookup("preference", "likes", "dislikes") is None


# ---- multiple candidates -------------------------------------------------


def test_multiple_candidates_first_signal_wins(store, oracle):
    """When multiple stored facts share the pattern + identity slots
    (different predicates), tier_u iterates and returns the first
    candidate whose oracle verdict produces a signal."""
    distinct_fact = _store_user_fact(
        store, pattern="preference", predicate="loves",
        slots={"agent": "user", "object": "cheetahs"}, polarity=1,
    )
    matching_fact = _store_user_fact(
        store, pattern="preference", predicate="dislikes",
        slots={"agent": "user", "object": "cheetahs"}, polarity=1,
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "cheetahs"},
        "polarity": 0, "source_text": "you don't like cheetahs",
    }
    llm = _MockLLM(responses=[
        # First candidate (loves): distinct → no signal, continue.
        {"label": "distinct", "slot_reversal": "none",
         "reason": "different intensity"},
        # Second candidate (dislikes): contradictory → signal, MATCH.
        {"label": "contradictory", "slot_reversal": "none",
         "reason": "antonyms"},
    ])

    result = lookup(claim, store, oracle,
                    key_slot_names=["agent", "object"], llm=llm)

    assert result.outcome is TierUOutcome.MATCH
    # The matching_fact MUST be the dislikes one, not the loves one.
    assert result.matching_fact.id == matching_fact.id
    assert result.matching_fact.predicate == "dislikes"
    assert result.via == ["predicate_equivalence"]
    assert result.polarity_flipped is True
    # Both oracle classifications were consulted.
    assert len(llm.calls) == 2


# ---- candidate set scoping -----------------------------------------------


def test_candidates_scoped_by_user_id(store, oracle):
    """A stored fact for user A must NOT resolve a Tier U lookup for
    user B. Identity-slot match alone isn't enough; user_id scoping
    is enforced."""
    _store_user_fact(
        store, pattern="preference", predicate="dislikes",
        slots={"agent": "user", "object": "cheetahs"}, polarity=1,
    )
    # The default user_id is 'default_user'; ask Tier U about a
    # different user.
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "cheetahs"},
        "polarity": 0, "source_text": "you don't like cheetahs",
    }
    llm = _MockLLM(responses=[])  # MUST NOT be called (no candidates)

    result = lookup(claim, store, oracle,
                    key_slot_names=["agent", "object"], llm=llm,
                    user_id="some_other_user")

    assert result.outcome is TierUOutcome.MISS
    assert len(llm.calls) == 0


def test_candidates_exclude_model_asserted_facts(store, oracle):
    """Tier U is the user microtheory. Model-asserted facts in the
    same table must not contribute candidates."""
    # Insert a model-asserted fact with the same shape as a user
    # would have stored.
    store.insert_fact(Fact(
        pattern="preference", predicate="dislikes",
        slots={"agent": "user", "object": "cheetahs"},
        polarity=1, asserted_by="model",  # NOT 'user'
        verification_status="verified",
    ))
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "cheetahs"},
        "polarity": 0, "source_text": "you don't like cheetahs",
    }
    llm = _MockLLM(responses=[])

    result = lookup(claim, store, oracle,
                    key_slot_names=["agent", "object"], llm=llm)

    assert result.outcome is TierUOutcome.MISS
    assert len(llm.calls) == 0


def test_candidates_exclude_closed_facts(store, oracle):
    """A fact with valid_until set is closed; it must not contribute
    a candidate even if the user originally asserted it."""
    closed_id = store.insert_fact(Fact(
        pattern="preference", predicate="dislikes",
        slots={"agent": "user", "object": "cheetahs"},
        polarity=1, asserted_by="user",
        verification_status="user_asserted",
    ))
    store.close_fact(closed_id)
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "cheetahs"},
        "polarity": 0, "source_text": "you don't like cheetahs",
    }
    llm = _MockLLM(responses=[])

    result = lookup(claim, store, oracle,
                    key_slot_names=["agent", "object"], llm=llm)

    assert result.outcome is TierUOutcome.MISS
    assert len(llm.calls) == 0


# ---- result shape --------------------------------------------------------


def test_tier_u_result_to_dict_carries_all_fields(store, oracle):
    """TierUResult.to_dict() exposes the full audit shape for the
    trace UI."""
    _store_user_fact(
        store, pattern="preference", predicate="dislikes",
        slots={"agent": "user", "object": "cheetahs"}, polarity=1,
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "cheetahs"},
        "polarity": 0, "source_text": "you don't like cheetahs",
    }
    llm = _MockLLM(responses=[
        {"label": "contradictory", "slot_reversal": "none",
         "reason": "antonyms"},
    ])
    result = lookup(claim, store, oracle,
                    key_slot_names=["agent", "object"], llm=llm)
    payload = result.to_dict()
    assert payload["outcome"] == "match"
    assert payload["via"] == ["predicate_equivalence"]
    assert payload["polarity_flipped"] is True
    assert payload["slot_reversal_applied"] is False
    assert payload["predicate_equivalence_row_id"] is not None
    assert payload["entity_equivalence_row_ids"] == []
    assert payload["matching_fact_id"] is not None
    assert payload["contradicting_fact_id"] is None


# ============================================================================
# Phase 4 - entity_equivalence integration
# ============================================================================
#
# Phase 4 extends tier_u with an alias-identity broadening stage that
# runs AFTER step 1 (literal match) and step 2 (predicate equivalence
# on exact-identity candidates). Step 3 gathers all user-asserted facts
# under the pattern (no identity-slot filter), then for each candidate
# consults entity_equivalence on each non-literal-matching identity
# slot. If every identity slot qualifies (literal, lexical user, or
# alias-equivalent per the oracle), the candidate qualifies; the
# literal + predicate-equivalence pipeline then runs on the qualifying
# candidate.
#
# The phase 3 cheetahs case must remain green under three calling
# conventions: with entity_oracle=None, with entity_oracle present
# but slots match literally (entity oracle never consulted), and with
# entity_oracle present and consulted but not contributing.


@pytest.fixture
def entity_oracle(store):
    return EntityEquivalence(store)


# ---- Phase 3 regression - cheetahs through Phase 4 plumbing -------------


def test_cheetahs_case_with_entity_oracle_none(store, oracle):
    """Backwards-compat: Phase 3 callers passing only the predicate
    oracle (no entity_oracle kwarg) keep working. The cheetahs case
    resolves through step 2 just as it did in Phase 3."""
    _store_user_fact(
        store, pattern="preference", predicate="dislikes",
        slots={"agent": "user", "object": "cheetahs"}, polarity=1,
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "cheetahs"},
        "polarity": 0, "source_text": "you don't like cheetahs",
    }
    llm = _MockLLM(responses=[
        {"label": "contradictory", "slot_reversal": "none",
         "reason": "antonyms"},
    ])
    result = lookup(
        claim, store, oracle,
        key_slot_names=["agent", "object"], llm=llm,
    )
    assert result.outcome is TierUOutcome.MATCH
    assert result.via == ["predicate_equivalence"]
    assert result.polarity_flipped is True


def test_cheetahs_case_with_entity_oracle_present_not_consulted(
    store, oracle, entity_oracle,
):
    """The cost-correctness invariant: when the cheaper SQL +
    predicate_equivalence path resolves the cheetahs case, the
    entity oracle is NOT consulted. Step 2 fires and step 3 is
    never entered.

    Concretely: stored (preference, dislikes, agent=user,
    object=cheetahs, polarity=1); claim (preference, likes,
    agent=user, object=cheetahs, polarity=0). Step 1 misses. Step
    2 finds the dislikes candidate at exact identity slots and runs
    predicate_equivalence -> contradictory + none -> MATCH. Step 3
    never runs because step 2 already returned.
    """
    _store_user_fact(
        store, pattern="preference", predicate="dislikes",
        slots={"agent": "user", "object": "cheetahs"}, polarity=1,
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "cheetahs"},
        "polarity": 0, "source_text": "you don't like cheetahs",
    }
    llm = _MockLLM(responses=[
        {"label": "contradictory", "slot_reversal": "none",
         "reason": "antonyms"},
    ])
    result = lookup(
        claim, store, oracle,
        key_slot_names=["agent", "object"], llm=llm,
        entity_oracle=entity_oracle,
    )
    assert result.outcome is TierUOutcome.MATCH
    # Cost-correctness assertion: via list contains only the
    # predicate oracle. Entity oracle wasn't consulted.
    assert result.via == ["predicate_equivalence"]
    assert result.entity_equivalence_row_ids == []
    assert len(llm.calls) == 1
    assert llm.calls[0]["purpose"] == "predicate_equivalence"


# ---- alias-identity-positive (the canonical Phase 4 case) --------------


def test_alias_identity_match_via_entity_equivalence(
    store, oracle, entity_oracle,
):
    """User stored (spatial_temporal, lives_in, entity=user,
    location='NYC', polarity=1); model claims (spatial_temporal,
    lives_in, entity=user, location='New York City', polarity=1).

    Step 1 misses (no exact slot match). Step 2 misses (no
    exact-identity candidates). Step 3 consults entity_equivalence
    on (NYC, New York City) -> same. Candidate qualifies; literal
    predicate match -> MATCH via entity_equivalence with no flip
    and no predicate_equivalence consultation."""
    stored = _store_user_fact(
        store, pattern="spatial_temporal", predicate="lives_in",
        slots={"entity": "user", "location": "NYC"}, polarity=1,
    )
    claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "slots": {"entity": "user", "location": "New York City"},
        "polarity": 1, "source_text": "you live in New York City",
    }
    llm = _MockLLM(responses=[
        {"label": "same",
         "reason": "common abbreviation for the same city"},
    ])
    result = lookup(
        claim, store, oracle,
        key_slot_names=["entity", "location"], llm=llm,
        entity_oracle=entity_oracle,
    )
    assert result.outcome is TierUOutcome.MATCH
    assert result.matching_fact.id == stored.id
    assert result.via == ["entity_equivalence"]
    assert len(result.entity_equivalence_row_ids) == 1
    assert result.predicate_equivalence_row_id is None
    assert result.polarity_flipped is False
    assert len(llm.calls) == 1
    assert llm.calls[0]["purpose"] == "entity_equivalence"


def test_alias_identity_warm_cache_no_llm_call(
    store, oracle, entity_oracle,
):
    """Second call of the alias-resolution case must not call the
    LLM. Both oracles' caches handle the second invocation purely
    via SQL."""
    _store_user_fact(
        store, pattern="spatial_temporal", predicate="lives_in",
        slots={"entity": "user", "location": "NYC"}, polarity=1,
    )
    claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "slots": {"entity": "user", "location": "New York City"},
        "polarity": 1, "source_text": "you live in New York City",
    }
    llm = _MockLLM(responses=[
        {"label": "same", "reason": "alias"},
    ])
    lookup(claim, store, oracle,
           key_slot_names=["entity", "location"], llm=llm,
           entity_oracle=entity_oracle)
    assert len(llm.calls) == 1
    result = lookup(
        claim, store, oracle,
        key_slot_names=["entity", "location"], llm=llm,
        entity_oracle=entity_oracle,
    )
    assert len(llm.calls) == 1  # unchanged
    assert result.outcome is TierUOutcome.MATCH
    assert result.via == ["entity_equivalence"]


# ---- case-disambiguation negative -------------------------------------


def test_case_disambiguation_yields_miss(
    store, oracle, entity_oracle,
):
    """User stored (preference, likes, object='apple'); model claims
    (preference, likes, object='Apple'). Case carries entity-
    disambiguation signal. Oracle says different. -> MISS."""
    _store_user_fact(
        store, pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "apple"}, polarity=1,
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "Apple"},
        "polarity": 1, "source_text": "you like Apple",
    }
    llm = _MockLLM(responses=[
        {"label": "different",
         "reason": "case disambiguation: fruit vs company"},
    ])
    result = lookup(
        claim, store, oracle,
        key_slot_names=["agent", "object"], llm=llm,
        entity_oracle=entity_oracle,
    )
    assert result.outcome is TierUOutcome.MISS
    assert result.via == []
    assert result.entity_equivalence_row_ids == []
    # Oracle row IS written for future calls regardless of
    # whether THIS lookup succeeded.
    row = entity_oracle.lookup("apple", "Apple")
    assert row is not None
    assert row.label == "different"


def test_over_merge_tempting_yields_miss(
    store, oracle, entity_oracle,
):
    """User stored (spatial_temporal, lives_in, location='Tokyo');
    model claims (lives_in, location='Japan'). Containment is not
    equivalence; oracle says different. Phase 5/7 derivation walker
    will eventually MATCH this through entity_taxonomy +
    predicate_distribution; Phase 4 correctly MISSES."""
    _store_user_fact(
        store, pattern="spatial_temporal", predicate="lives_in",
        slots={"entity": "user", "location": "Tokyo"}, polarity=1,
    )
    claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "slots": {"entity": "user", "location": "Japan"},
        "polarity": 1, "source_text": "you live in Japan",
    }
    llm = _MockLLM(responses=[
        {"label": "different",
         "reason": "containment is not equivalence"},
    ])
    result = lookup(
        claim, store, oracle,
        key_slot_names=["entity", "location"], llm=llm,
        entity_oracle=entity_oracle,
    )
    assert result.outcome is TierUOutcome.MISS


# ---- alias + predicate equivalence (both oracles consulted) -----------


def test_alias_identity_plus_predicate_equivalence(
    store, oracle, entity_oracle,
):
    """User stored (preference, dislikes, object='peanut butter',
    polarity=1); model claims (preference, likes, object='PB',
    polarity=0). Step 1 misses. Step 2 misses (no exact-identity
    candidates - object differs). Step 3: entity_equivalence on
    (PB, peanut butter) -> same; candidate qualifies. Then
    predicate_equivalence on (likes, dislikes) -> contradictory +
    none. Polarity 0 vs 1 differs -> MATCH (cheetahs logic) with
    polarity_flipped=True. via list captures both oracles in
    consultation order."""
    stored = _store_user_fact(
        store, pattern="preference", predicate="dislikes",
        slots={"agent": "user", "object": "peanut butter"}, polarity=1,
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "PB"},
        "polarity": 0, "source_text": "you don't like PB",
    }
    llm = _MockLLM(responses=[
        {"label": "same",
         "reason": "common abbreviation"},
        {"label": "contradictory", "slot_reversal": "none",
         "reason": "antonyms"},
    ])
    result = lookup(
        claim, store, oracle,
        key_slot_names=["agent", "object"], llm=llm,
        entity_oracle=entity_oracle,
    )
    assert result.outcome is TierUOutcome.MATCH
    assert result.matching_fact.id == stored.id
    assert result.via == ["entity_equivalence", "predicate_equivalence"]
    assert len(result.entity_equivalence_row_ids) == 1
    assert result.predicate_equivalence_row_id is not None
    assert result.polarity_flipped is True
    purposes = [c["purpose"] for c in llm.calls]
    assert purposes == ["entity_equivalence", "predicate_equivalence"]


# ---- entity_equivalence classification_failed gracefully degrades -----


def test_entity_classification_failed_falls_through(
    store, oracle, entity_oracle,
):
    """When entity_equivalence's LLM produces malformed output, the
    candidate is rejected from the alias-identity set. tier_u
    continues considering other candidates; if none qualify, MISS.
    No row written for the failed classification."""
    _store_user_fact(
        store, pattern="spatial_temporal", predicate="lives_in",
        slots={"entity": "user", "location": "NYC"}, polarity=1,
    )
    claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "slots": {"entity": "user", "location": "New York City"},
        "polarity": 1, "source_text": "you live in New York City",
    }
    llm = _MockLLM(responses=[
        {"label": "kinda_same", "reason": "not really"},
    ])
    result = lookup(
        claim, store, oracle,
        key_slot_names=["entity", "location"], llm=llm,
        entity_oracle=entity_oracle,
    )
    assert result.outcome is TierUOutcome.MISS
    assert result.via == []
    assert entity_oracle.lookup("NYC", "New York City") is None


# ---- lexical user skips oracle call -----------------------------------


def test_lexical_user_skips_oracle_call(
    store, oracle, entity_oracle,
):
    """When both claim and candidate have agent='user' (lexical
    user), entity_equivalence is NOT consulted on the agent slot.
    The lexical-user check (is_user) handles canonicalization. Only
    non-user slot pairs fire the oracle."""
    stored = _store_user_fact(
        store, pattern="preference", predicate="likes",
        slots={"agent": "user", "object": "peanut butter"}, polarity=1,
    )
    claim = {
        "pattern": "preference", "predicate": "likes",
        "slots": {"agent": "user", "object": "PB"},
        "polarity": 1, "source_text": "you like PB",
    }
    llm = _MockLLM(responses=[
        # Only ONE oracle call expected (object slot). If the agent
        # slot also fires, MockLLM exhausts and raises.
        {"label": "same", "reason": "common abbreviation"},
    ])
    result = lookup(
        claim, store, oracle,
        key_slot_names=["agent", "object"], llm=llm,
        entity_oracle=entity_oracle,
    )
    assert result.outcome is TierUOutcome.MATCH
    assert result.matching_fact.id == stored.id
    assert result.via == ["entity_equivalence"]
    assert len(llm.calls) == 1


# ---- alias-identity scoping by user_id --------------------------------


def test_alias_identity_scoped_by_user_id(
    store, oracle, entity_oracle,
):
    """A different user's facts must NOT participate in alias-
    identity broadening."""
    _store_user_fact(
        store, pattern="spatial_temporal", predicate="lives_in",
        slots={"entity": "user", "location": "NYC"}, polarity=1,
    )
    claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "slots": {"entity": "user", "location": "New York City"},
        "polarity": 1, "source_text": "you live in New York City",
    }
    llm = _MockLLM(responses=[])  # MUST NOT be called
    result = lookup(
        claim, store, oracle,
        key_slot_names=["entity", "location"], llm=llm,
        entity_oracle=entity_oracle,
        user_id="some_other_user",
    )
    assert result.outcome is TierUOutcome.MISS
    assert len(llm.calls) == 0


# ---- TierUResult shape with both oracles ------------------------------


def test_tier_u_result_to_dict_with_both_oracles(
    store, oracle, entity_oracle,
):
    """to_dict carries the full audit shape including the new
    entity_equivalence_row_ids list."""
    _store_user_fact(
        store, pattern="spatial_temporal", predicate="lives_in",
        slots={"entity": "user", "location": "NYC"}, polarity=1,
    )
    claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "slots": {"entity": "user", "location": "New York City"},
        "polarity": 1, "source_text": "you live in New York City",
    }
    llm = _MockLLM(responses=[
        {"label": "same", "reason": "alias"},
    ])
    result = lookup(
        claim, store, oracle,
        key_slot_names=["entity", "location"], llm=llm,
        entity_oracle=entity_oracle,
    )
    payload = result.to_dict()
    assert payload["outcome"] == "match"
    assert payload["via"] == ["entity_equivalence"]
    assert isinstance(payload["entity_equivalence_row_ids"], list)
    assert len(payload["entity_equivalence_row_ids"]) == 1
    assert payload["predicate_equivalence_row_id"] is None
    assert payload["polarity_flipped"] is False
