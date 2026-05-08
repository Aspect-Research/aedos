"""Derivation walk tests (v0.14 Phase 7c).

Covers the BFS engine's contracts:

  * Single-hop part_of substitution (Williamstown→Massachusetts).
  * Single-hop is_a substitution (cheetahs→animals).
  * Multi-hop part_of (Williamstown→Berkshire→Massachusetts).
  * Polarity-flip mid-chain via predicate_equivalence contradictory.
  * Cycle detection on constructed entity_equivalence cycle.
  * Depth bound (chain that requires depth > MAX_DEPTH misses).
  * Reliability floor (chain whose row drops below 0.4 misses).
  * Predicate distribution gating: distributes_up only / distributes_
    down only / neither (no expansion) / both.
  * Reverse-direction entity_taxonomy rows (parent_subsumed_by_child).
  * entity_taxonomy ``equivalent`` label (no pd gating).
  * entity_equivalence alias substitution.
  * predicate_equivalence with subject_object_swap on relational.
  * predicate_equivalence ``distinct`` does not expand.
  * Tier W only matches on ``verified`` status (other statuses are
    not positive derivation witnesses).
  * No persistence (three-snapshot gate: pre / mid / post).
  * Substrate cold-start writes during walk are allowed.

The substrate is pre-populated via direct ``record()`` calls in
fixtures so no LLM runs in the default test path.
"""

from __future__ import annotations

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
from src.layer4_lookup import derivation, tier_w
from src.layer4_lookup.types import LookupOutcome


_PATTERNS_PATH = (
    Path(__file__).parent.parent
    / "src" / "layer1_extraction" / "patterns.yaml"
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(scope="module")
def registry() -> PatternRegistry:
    return PatternRegistry.from_yaml(_PATTERNS_PATH)


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "derivation.db")
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


def _store_user_fact(
    store: FactStore, *, pattern: str, predicate: str,
    slots: dict, polarity: int = 1,
) -> Fact:
    fact_id = store.insert_fact(Fact(
        pattern=pattern, predicate=predicate, slots=dict(slots),
        polarity=polarity, asserted_by="user",
        verification_status="user_asserted",
    ))
    return store.get_fact(fact_id)


def _walk(claim, store, registry, all_oracles, **overrides):
    """Helper: invoke derivation.walk with the standard oracle bundle."""
    from src.layer2_routing.constants import KEY_SLOTS_BY_PATTERN
    return derivation.walk(
        claim, store,
        key_slot_names=KEY_SLOTS_BY_PATTERN.get(claim["pattern"], []),
        registry=registry,
        **all_oracles,
        **overrides,
    )


# ============================================================================
# Single-hop part_of substitution (Williamstown canonical case)
# ============================================================================


class TestSingleHopPartOfUp:
    """User says 'I live in Williamstown' (Tier U). Model claims 'you
    live in Massachusetts'. The substrate has Williamstown part_of
    Massachusetts AND lives_in distributes_up part_of. Walker derives
    MATCH."""

    def _setup(self, store, registry, taxonomy_oracle, distribution_oracle):
        _store_user_fact(
            store, pattern="spatial_temporal", predicate="lives_in",
            slots={"entity": "user", "location": "Williamstown"},
            polarity=1,
        )
        taxonomy_oracle.record(
            "Williamstown", "Massachusetts", "part_of",
            label="child_subsumed_by_parent",
            reason="town in state",
        )
        distribution_oracle.record(
            "spatial_temporal", "lives_in", 1, "part_of",
            label="distributes_up",
            reason="residence aggregates compositionally",
        )

    def test_williamstown_to_massachusetts_match(
        self, store, registry, all_oracles,
    ):
        self._setup(
            store, registry,
            all_oracles["taxonomy_oracle"],
            all_oracles["distribution_oracle"],
        )
        claim = {
            "pattern": "spatial_temporal",
            "predicate": "lives_in",
            "polarity": 1,
            "slots": {"entity": "user", "location": "Massachusetts"},
            "source_text": "you live in Massachusetts",
        }
        result = _walk(claim, store, registry, all_oracles)
        assert result.outcome is LookupOutcome.MATCH, (
            f"expected MATCH; got {result.outcome.value}; "
            f"abort={result.abort_reason}"
        )
        assert result.matching_tier == "u"
        assert result.matching_fact_id is not None
        # The chain should have an entity_taxonomy edge AND a
        # predicate_distribution edge (composite step).
        oracles_in_chain = [e.oracle for e in result.chain]
        assert "entity_taxonomy" in oracles_in_chain
        assert "predicate_distribution" in oracles_in_chain
        # Min-link reliability is at least the cold-start prior.
        assert result.chain_reliability >= 0.4

    def test_distributes_neither_blocks_match(
        self, store, registry, all_oracles,
    ):
        """If predicate_distribution returns ``neither`` (no
        propagation), the walker can't use the et hop. Walk MISSes."""
        _store_user_fact(
            store, pattern="quantitative", predicate="weighs",
            slots={"subject": "Rex", "value": 30},
            polarity=1,
        )
        all_oracles["taxonomy_oracle"].record(
            "Rex", "dog", "is_a",
            label="child_subsumed_by_parent",
            reason="Rex is a dog (made up)",
        )
        all_oracles["distribution_oracle"].record(
            "quantitative", "weighs", 1, "is_a",
            label="neither",
            reason="weight is individual property; doesn't distribute",
        )
        claim = {
            "pattern": "quantitative",
            "predicate": "weighs",
            "polarity": 1,
            "slots": {"subject": "dog", "value": 30},
            "source_text": "dogs weigh 30",
        }
        result = _walk(claim, store, registry, all_oracles)
        assert result.outcome is LookupOutcome.MISS


# ============================================================================
# Single-hop is_a substitution (cheetahs case)
# ============================================================================


class TestSingleHopIsADown:
    """User says 'I love animals'. Model claims 'you love cheetahs'.
    Substrate has cheetah is_a animal AND loves distributes_down
    is_a. Walker derives MATCH on the parent-class fact."""

    def test_animals_to_cheetahs_match(
        self, store, registry, all_oracles,
    ):
        _store_user_fact(
            store, pattern="preference", predicate="loves",
            slots={"agent": "user", "object": "animals"},
            polarity=1,
        )
        all_oracles["taxonomy_oracle"].record(
            "cheetah", "animals", "is_a",
            label="child_subsumed_by_parent",
            reason="cheetahs are a kind of animal",
        )
        all_oracles["distribution_oracle"].record(
            "preference", "loves", 1, "is_a",
            label="distributes_down",
            reason="loving a category propagates to instances",
        )
        claim = {
            "pattern": "preference",
            "predicate": "loves",
            "polarity": 1,
            "slots": {"agent": "user", "object": "cheetah"},
            "source_text": "you love cheetahs",
        }
        result = _walk(claim, store, registry, all_oracles)
        assert result.outcome is LookupOutcome.MATCH
        assert result.matching_tier == "u"


# ============================================================================
# Polarity flip mid-chain (Phase 7 cheetahs-via-derivation)
# ============================================================================


class TestPolarityFlipMidChain:
    """The Phase 7 extension of Phase 3's cheetahs case. Stored
    'I dislike animals' (p=1). Query 'you don't like cheetahs' (p=0).

    Chain:
      1. predicate_equivalence(likes, dislikes) = contradictory →
         state becomes (preference, dislikes, agent=user,
         object=cheetah, polarity=1)
      2. entity_taxonomy(cheetah, animals, is_a) =
         child_subsumed_by_parent →
         (preference, dislikes, agent=user, object=animals, polarity=1)
      3. predicate_distribution(preference, dislikes, p=1, is_a) =
         distributes_down ratifies the substitution.
      4. Literal match against the stored dislikes/p=1/animals fact.
    """

    def test_phase7_cheetahs_via_derivation(
        self, store, registry, all_oracles,
    ):
        _store_user_fact(
            store, pattern="preference", predicate="dislikes",
            slots={"agent": "user", "object": "animals"},
            polarity=1,
        )
        all_oracles["predicate_oracle"].record(
            "preference", "likes", "dislikes",
            label="contradictory", slot_reversal="none",
            reason="antonym preference predicates",
        )
        all_oracles["taxonomy_oracle"].record(
            "cheetah", "animals", "is_a",
            label="child_subsumed_by_parent",
            reason="cheetahs are a kind of animal",
        )
        all_oracles["distribution_oracle"].record(
            "preference", "dislikes", 1, "is_a",
            label="distributes_down",
            reason="categorical aversion inherits to instances",
        )
        claim = {
            "pattern": "preference",
            "predicate": "likes",
            "polarity": 0,
            "slots": {"agent": "user", "object": "cheetah"},
            "source_text": "you don't like cheetahs",
        }
        result = _walk(claim, store, registry, all_oracles)
        assert result.outcome is LookupOutcome.MATCH
        oracles_in_chain = [e.oracle for e in result.chain]
        assert "predicate_equivalence" in oracles_in_chain
        assert "entity_taxonomy" in oracles_in_chain
        assert "predicate_distribution" in oracles_in_chain
        # At least one edge marked as 'contradictory' (the polarity flip).
        flip_edges = [e for e in result.chain if e.label == "contradictory"]
        assert flip_edges, "expected the contradictory predicate_equivalence edge"


# ============================================================================
# Multi-hop part_of chain
# ============================================================================


class TestMultiHopPartOf:
    """Williamstown → Berkshire County → Massachusetts. Walker traverses
    two part_of hops + one pd ratification each (4 edges, 2 logical
    steps). Stored 'I live in Williamstown', claim 'you live in
    Massachusetts'."""

    def test_two_hop_part_of_match(
        self, store, registry, all_oracles,
    ):
        _store_user_fact(
            store, pattern="spatial_temporal", predicate="lives_in",
            slots={"entity": "user", "location": "Williamstown"},
            polarity=1,
        )
        # Direct row Williamstown → Massachusetts also exists in the
        # calibration corpus, but for a multi-hop test we register the
        # intermediate hops only (the walker chains them).
        all_oracles["taxonomy_oracle"].record(
            "Williamstown", "Berkshire County", "part_of",
            label="child_subsumed_by_parent",
            reason="town in county",
        )
        all_oracles["taxonomy_oracle"].record(
            "Berkshire County", "Massachusetts", "part_of",
            label="child_subsumed_by_parent",
            reason="county in state",
        )
        all_oracles["distribution_oracle"].record(
            "spatial_temporal", "lives_in", 1, "part_of",
            label="distributes_up",
            reason="residence aggregates compositionally",
        )
        claim = {
            "pattern": "spatial_temporal",
            "predicate": "lives_in",
            "polarity": 1,
            "slots": {"entity": "user", "location": "Massachusetts"},
            "source_text": "you live in Massachusetts",
        }
        result = _walk(claim, store, registry, all_oracles)
        assert result.outcome is LookupOutcome.MATCH
        # Two logical steps; each composite emits 2 edges.
        assert len(result.chain) >= 4
        et_edges = [e for e in result.chain if e.oracle == "entity_taxonomy"]
        assert len(et_edges) == 2


# ============================================================================
# Reverse-direction taxonomy row (parent_subsumed_by_child)
# ============================================================================


class TestReverseDirectionTaxonomy:
    """A row written with caller-inverted args. The walker must
    interpret the label correctly to determine substitution direction."""

    def test_parent_subsumed_by_child_used_correctly(
        self, store, registry, all_oracles,
    ):
        # Stored: 'I live in Williamstown'. Claim: 'you live in MA'.
        # Substrate has the row written with args inverted:
        # (child=Massachusetts, parent=Williamstown,
        #  label=parent_subsumed_by_child)
        # — meaning the natural direction is reversed, Williamstown
        # is the more specific.
        _store_user_fact(
            store, pattern="spatial_temporal", predicate="lives_in",
            slots={"entity": "user", "location": "Williamstown"},
            polarity=1,
        )
        all_oracles["taxonomy_oracle"].record(
            "Massachusetts", "Williamstown", "part_of",
            label="parent_subsumed_by_child",
            reason="caller inverted args; Williamstown is the more specific",
        )
        all_oracles["distribution_oracle"].record(
            "spatial_temporal", "lives_in", 1, "part_of",
            label="distributes_up",
            reason="residence aggregates",
        )
        claim = {
            "pattern": "spatial_temporal",
            "predicate": "lives_in",
            "polarity": 1,
            "slots": {"entity": "user", "location": "Massachusetts"},
            "source_text": "you live in MA",
        }
        result = _walk(claim, store, registry, all_oracles)
        assert result.outcome is LookupOutcome.MATCH


# ============================================================================
# Equivalent taxonomy row (no pd gating)
# ============================================================================


class TestEquivalentTaxonomy:
    """Holland and Netherlands — same level under part_of. Substitution
    happens without consulting predicate_distribution."""

    def test_equivalent_row_substitutes_without_pd(
        self, store, registry, all_oracles,
    ):
        _store_user_fact(
            store, pattern="spatial_temporal", predicate="lives_in",
            slots={"entity": "user", "location": "Holland"},
            polarity=1,
        )
        all_oracles["taxonomy_oracle"].record(
            "Holland", "Netherlands", "part_of",
            label="equivalent",
            reason="same country, two surface forms",
        )
        # Note: no predicate_distribution row recorded. The walker
        # must NOT consult it for an equivalent substitution.
        claim = {
            "pattern": "spatial_temporal",
            "predicate": "lives_in",
            "polarity": 1,
            "slots": {"entity": "user", "location": "Netherlands"},
            "source_text": "you live in Netherlands",
        }
        result = _walk(claim, store, registry, all_oracles)
        assert result.outcome is LookupOutcome.MATCH
        # Chain has only the et edge — no pd edge.
        oracle_set = {e.oracle for e in result.chain}
        assert "entity_taxonomy" in oracle_set
        assert "predicate_distribution" not in oracle_set


# ============================================================================
# entity_equivalence alias
# ============================================================================


class TestEntityEquivalenceExpansion:
    """The walker can substitute via entity_equivalence rows with
    label='same'. NYC ↔ New York City."""

    def test_alias_substitution(
        self, store, registry, all_oracles,
    ):
        _store_user_fact(
            store, pattern="spatial_temporal", predicate="lives_in",
            slots={"entity": "user", "location": "NYC"},
            polarity=1,
        )
        all_oracles["entity_oracle"].record(
            "NYC", "New York City", "same", reason="city alias",
        )
        claim = {
            "pattern": "spatial_temporal",
            "predicate": "lives_in",
            "polarity": 1,
            "slots": {"entity": "user", "location": "New York City"},
            "source_text": "you live in New York City",
        }
        result = _walk(claim, store, registry, all_oracles)
        assert result.outcome is LookupOutcome.MATCH
        oracles_in_chain = [e.oracle for e in result.chain]
        assert "entity_equivalence" in oracles_in_chain

    def test_different_label_no_substitution(
        self, store, registry, all_oracles,
    ):
        _store_user_fact(
            store, pattern="spatial_temporal", predicate="lives_in",
            slots={"entity": "user", "location": "Tokyo"},
            polarity=1,
        )
        all_oracles["entity_oracle"].record(
            "Japan", "Tokyo", "different",
            reason="containment, not equivalence",
        )
        claim = {
            "pattern": "spatial_temporal",
            "predicate": "lives_in",
            "polarity": 1,
            "slots": {"entity": "user", "location": "Japan"},
            "source_text": "you live in Japan",
        }
        result = _walk(claim, store, registry, all_oracles)
        assert result.outcome is LookupOutcome.MISS


# ============================================================================
# predicate_equivalence: distinct, subject_object_swap, not-relational guard
# ============================================================================


class TestPredicateEquivalenceExpansion:
    def test_distinct_no_expansion(
        self, store, registry, all_oracles,
    ):
        _store_user_fact(
            store, pattern="preference", predicate="likes",
            slots={"agent": "user", "object": "olives"},
            polarity=1,
        )
        all_oracles["predicate_oracle"].record(
            "preference", "likes", "loves",
            label="distinct", slot_reversal="none",
            reason="different intensity",
        )
        claim = {
            "pattern": "preference",
            "predicate": "loves",
            "polarity": 1,
            "slots": {"agent": "user", "object": "olives"},
            "source_text": "you love olives",
        }
        result = _walk(claim, store, registry, all_oracles)
        assert result.outcome is LookupOutcome.MISS

    def test_subject_object_swap_relational(
        self, store, registry, all_oracles,
    ):
        _store_user_fact(
            store, pattern="relational", predicate="wrote",
            slots={"subject": "Asa", "object": "the paper"},
            polarity=1,
        )
        all_oracles["predicate_oracle"].record(
            "relational", "authored_by", "wrote",
            label="equivalent", slot_reversal="subject_object_swap",
            reason="active/passive of same authorship relation",
        )
        claim = {
            "pattern": "relational",
            "predicate": "authored_by",
            "polarity": 1,
            "slots": {"subject": "the paper", "object": "Asa"},
            "source_text": "the paper was authored by Asa",
        }
        result = _walk(claim, store, registry, all_oracles)
        assert result.outcome is LookupOutcome.MATCH

    def test_subject_object_swap_skipped_on_non_relational(
        self, store, registry, all_oracles,
    ):
        """A subject_object_swap classification on a non-relational
        pattern should not trigger expansion. The walker preserves
        slot semantics."""
        _store_user_fact(
            store, pattern="preference", predicate="likes",
            slots={"agent": "user", "object": "olives"},
            polarity=1,
        )
        # Pretend (incorrectly) that some preference pair has slot-swap.
        # The walker should ignore it.
        all_oracles["predicate_oracle"].record(
            "preference", "likes", "delights_in",
            label="equivalent", slot_reversal="subject_object_swap",
            reason="hypothetical mis-labelling",
        )
        claim = {
            "pattern": "preference",
            "predicate": "delights_in",
            "polarity": 1,
            "slots": {"agent": "olives", "object": "user"},
            "source_text": "olives delight in user",  # nonsense
        }
        result = _walk(claim, store, registry, all_oracles)
        # The mis-classified pair should NOT trigger swap on
        # non-relational. Walk misses.
        assert result.outcome is LookupOutcome.MISS


# ============================================================================
# Cycle detection
# ============================================================================


class TestCycleDetection:
    """A constructed entity_equivalence cycle X≡Y, Y≡Z, Z≡X must
    terminate the BFS without exploring forever."""

    def test_cycle_terminates_with_miss(
        self, store, registry, all_oracles,
    ):
        # No facts in U or W. The walker can substitute slot value
        # X→Y→Z→X via entity_equivalence aliases. Visited-set
        # prevents the X cycle re-visit.
        all_oracles["entity_oracle"].record("X", "Y", "same", reason="constructed")
        all_oracles["entity_oracle"].record("Y", "Z", "same", reason="constructed")
        # The Z-X pair is the cycle closer.
        all_oracles["entity_oracle"].record("X", "Z", "same", reason="constructed")
        claim = {
            "pattern": "preference",
            "predicate": "likes",
            "polarity": 1,
            "slots": {"agent": "user", "object": "X"},
            "source_text": "you like X",
        }
        result = _walk(claim, store, registry, all_oracles)
        # No matching fact anywhere. The walk terminates cleanly
        # rather than infinite-looping.
        assert result.outcome is LookupOutcome.MISS
        assert result.abort_reason == "exhausted"
        # Explored states is finite — bounded by visited-set.
        assert result.explored_states < 10


# ============================================================================
# Depth bound
# ============================================================================


class TestDepthBound:
    def test_chain_at_max_depth_admitted(
        self, store, registry, all_oracles,
    ):
        """A chain that fits exactly within MAX_DEPTH=4 is admitted."""
        # Build a 4-hop chain: A part_of B part_of C part_of D part_of E.
        _store_user_fact(
            store, pattern="spatial_temporal", predicate="lives_in",
            slots={"entity": "user", "location": "A"},
            polarity=1,
        )
        for child, parent in [("A", "B"), ("B", "C"), ("C", "D"), ("D", "E")]:
            all_oracles["taxonomy_oracle"].record(
                child, parent, "part_of",
                label="child_subsumed_by_parent",
                reason="constructed chain",
            )
        all_oracles["distribution_oracle"].record(
            "spatial_temporal", "lives_in", 1, "part_of",
            label="distributes_up",
            reason="residence aggregates",
        )
        claim = {
            "pattern": "spatial_temporal",
            "predicate": "lives_in",
            "polarity": 1,
            "slots": {"entity": "user", "location": "E"},
            "source_text": "you live in E",
        }
        result = _walk(claim, store, registry, all_oracles)
        assert result.outcome is LookupOutcome.MATCH

    def test_chain_beyond_max_depth_misses(
        self, store, registry, all_oracles,
    ):
        """A chain requiring 5 hops (one beyond MAX_DEPTH) misses."""
        _store_user_fact(
            store, pattern="spatial_temporal", predicate="lives_in",
            slots={"entity": "user", "location": "A"},
            polarity=1,
        )
        for child, parent in [
            ("A", "B"), ("B", "C"), ("C", "D"), ("D", "E"), ("E", "F"),
        ]:
            all_oracles["taxonomy_oracle"].record(
                child, parent, "part_of",
                label="child_subsumed_by_parent",
                reason="constructed chain",
            )
        all_oracles["distribution_oracle"].record(
            "spatial_temporal", "lives_in", 1, "part_of",
            label="distributes_up",
            reason="residence aggregates",
        )
        claim = {
            "pattern": "spatial_temporal",
            "predicate": "lives_in",
            "polarity": 1,
            "slots": {"entity": "user", "location": "F"},
            "source_text": "you live in F",
        }
        result = _walk(claim, store, registry, all_oracles)
        assert result.outcome is LookupOutcome.MISS


# ============================================================================
# Reliability floor
# ============================================================================


class TestReliabilityFloor:
    def test_low_confidence_row_prunes_branch(
        self, store, registry,
        predicate_oracle, entity_oracle, taxonomy_oracle, distribution_oracle,
    ):
        """A taxonomy row with high contradiction count drops below
        the floor and is pruned. The walk falls through to MISS even
        though the structural chain would otherwise match."""
        _store_user_fact(
            store, pattern="spatial_temporal", predicate="lives_in",
            slots={"entity": "user", "location": "Williamstown"},
            polarity=1,
        )
        # Record the row, then simulate it being heavily contradicted
        # by directly bumping contradicted_count via SQL (the test is
        # an existence proof that the floor works; production
        # bumping happens via the operator endpoint).
        taxonomy_oracle.record(
            "Williamstown", "Massachusetts", "part_of",
            label="child_subsumed_by_parent",
            reason="set up to be drastically contradicted below",
        )
        store._conn.execute(
            "UPDATE entity_taxonomy SET contradicted_count = 10 "
            "WHERE child = ? AND parent = ? AND relation_type = ?",
            ("Williamstown", "Massachusetts", "part_of"),
        )
        store._conn.commit()
        distribution_oracle.record(
            "spatial_temporal", "lives_in", 1, "part_of",
            label="distributes_up",
            reason="residence aggregates",
        )
        claim = {
            "pattern": "spatial_temporal",
            "predicate": "lives_in",
            "polarity": 1,
            "slots": {"entity": "user", "location": "Massachusetts"},
            "source_text": "you live in Massachusetts",
        }
        all_oracles = {
            "predicate_oracle": predicate_oracle,
            "entity_oracle": entity_oracle,
            "taxonomy_oracle": taxonomy_oracle,
            "distribution_oracle": distribution_oracle,
        }
        result = _walk(claim, store, registry, all_oracles)
        # Confidence: (0+1)/(0+10+2) = 1/12 ≈ 0.083 < 0.4 floor.
        # Branch pruned; walk misses.
        assert result.outcome is LookupOutcome.MISS


# ============================================================================
# Tier W only matches on verified
# ============================================================================


class TestTierWPositiveWitnessOnly:
    """A Tier W row with ``retrieval_inconclusive`` or ``contradicted``
    status doesn't constitute a positive derivation witness. Only
    ``verified`` rows count as a chain endpoint in Tier W."""

    def test_tier_w_verified_row_matches(
        self, store, registry, all_oracles,
    ):
        # Setup: Tier W has verified "user lives_in Massachusetts".
        # Substrate: Massachusetts part_of US (Massachusetts is more
        # specific). lives_in distributes_UP part_of (specific →
        # general).
        # Claim: "user lives_in US" (more general).
        # Walker substitutes US → Massachusetts (going DOWN to find
        # a more-specific witness; pd ratifies because lives_in
        # distributes_UP, so a Massachusetts witness implies the
        # US claim). Match in Tier W on the verified row.
        cached_claim = {
            "pattern": "spatial_temporal",
            "predicate": "lives_in",
            "polarity": 1,
            "slots": {"entity": "user", "location": "Massachusetts"},
            "source_text": "user lives in Massachusetts",
        }
        tier_w.write_verifier_result(
            cached_claim, store,
            verification_status="verified",
            registry=registry,
        )
        all_oracles["taxonomy_oracle"].record(
            "Massachusetts", "US", "part_of",
            label="child_subsumed_by_parent",
            reason="state in country",
        )
        all_oracles["distribution_oracle"].record(
            "spatial_temporal", "lives_in", 1, "part_of",
            label="distributes_up",
            reason="residence aggregates compositionally",
        )
        claim = {
            "pattern": "spatial_temporal",
            "predicate": "lives_in",
            "polarity": 1,
            "slots": {"entity": "user", "location": "US"},
            "source_text": "you live in the US",
        }
        result = _walk(claim, store, registry, all_oracles)
        assert result.outcome is LookupOutcome.MATCH, (
            f"expected Tier W match; got {result.outcome.value} "
            f"with abort={result.abort_reason}"
        )
        assert result.matching_tier == "w"

    def test_tier_w_inconclusive_row_does_not_match(
        self, store, registry, all_oracles,
    ):
        cached_claim = {
            "pattern": "spatial_temporal",
            "predicate": "lives_in",
            "polarity": 1,
            "slots": {"entity": "user", "location": "Massachusetts"},
            "source_text": "user lives in Massachusetts",
        }
        tier_w.write_verifier_result(
            cached_claim, store,
            verification_status="retrieval_inconclusive",
            registry=registry,
        )
        all_oracles["taxonomy_oracle"].record(
            "Williamstown", "Massachusetts", "part_of",
            label="child_subsumed_by_parent",
            reason="town in state",
        )
        all_oracles["distribution_oracle"].record(
            "spatial_temporal", "lives_in", 1, "part_of",
            label="distributes_up",
            reason="residence aggregates",
        )
        claim = {
            "pattern": "spatial_temporal",
            "predicate": "lives_in",
            "polarity": 1,
            "slots": {"entity": "user", "location": "Williamstown"},
            "source_text": "you live in Williamstown",
        }
        result = _walk(claim, store, registry, all_oracles)
        # Inconclusive Tier W row doesn't count as a positive witness.
        assert result.outcome is LookupOutcome.MISS


# ============================================================================
# No persistence (three-snapshot gate)
# ============================================================================


def _snapshot_counts(store: FactStore) -> dict[str, int]:
    """Take counts of facts, verification_cache, and substrate tables."""
    cur = store._conn.execute("SELECT COUNT(*) FROM facts").fetchone()
    facts = int(cur[0] or 0)
    cur = store._conn.execute(
        "SELECT COUNT(*) FROM verification_cache").fetchone()
    cache = int(cur[0] or 0)
    et = int(store._conn.execute(
        "SELECT COUNT(*) FROM entity_taxonomy").fetchone()[0] or 0)
    pd = int(store._conn.execute(
        "SELECT COUNT(*) FROM predicate_distribution").fetchone()[0] or 0)
    pe = int(store._conn.execute(
        "SELECT COUNT(*) FROM predicate_equivalence").fetchone()[0] or 0)
    ee = int(store._conn.execute(
        "SELECT COUNT(*) FROM entity_equivalence").fetchone()[0] or 0)
    return {
        "facts": facts, "verification_cache": cache,
        "entity_taxonomy": et, "predicate_distribution": pd,
        "predicate_equivalence": pe, "entity_equivalence": ee,
    }


class TestNoPersistenceThreeSnapshot:
    """Per the Phase 7 plan: derivation MATCH must NOT write to facts
    or verification_cache. Substrate counts may grow during the walk
    (cold-start LLM-driven writes) but are zero in this test because
    we pre-warm via record(). Three snapshots: pre-test, mid-test
    (post substrate-warming), post-test (post derivation MATCH)."""

    def test_facts_and_cache_unchanged_across_match(
        self, store, registry, all_oracles,
    ):
        snap_pre = _snapshot_counts(store)

        # ---- mid-test setup: facts + substrate rows (the only
        # legitimate writes; the walker reads these but doesn't
        # write any new ones).
        _store_user_fact(
            store, pattern="spatial_temporal", predicate="lives_in",
            slots={"entity": "user", "location": "Williamstown"},
            polarity=1,
        )
        all_oracles["taxonomy_oracle"].record(
            "Williamstown", "Massachusetts", "part_of",
            label="child_subsumed_by_parent",
            reason="town in state",
        )
        all_oracles["distribution_oracle"].record(
            "spatial_temporal", "lives_in", 1, "part_of",
            label="distributes_up",
            reason="residence aggregates",
        )
        snap_mid = _snapshot_counts(store)
        # facts: 1 (the user fact). verification_cache: still 0.
        # Substrate counts: 1 et, 1 pd; the others unchanged.
        assert snap_mid["facts"] == snap_pre["facts"] + 1
        assert snap_mid["verification_cache"] == snap_pre["verification_cache"]
        assert snap_mid["entity_taxonomy"] == snap_pre["entity_taxonomy"] + 1
        assert snap_mid["predicate_distribution"] == snap_pre["predicate_distribution"] + 1

        # ---- run derivation
        claim = {
            "pattern": "spatial_temporal",
            "predicate": "lives_in",
            "polarity": 1,
            "slots": {"entity": "user", "location": "Massachusetts"},
            "source_text": "you live in Massachusetts",
        }
        result = _walk(claim, store, registry, all_oracles)
        assert result.outcome is LookupOutcome.MATCH

        snap_post = _snapshot_counts(store)
        # Pre-test → post-test: facts + cache unchanged (mid setup
        # added one fact, but no further fact write happened during
        # the walk). Substrate counts equal mid (no cold-start
        # writes during this test because rows pre-warmed).
        assert snap_post["facts"] == snap_mid["facts"], (
            "derivation MATCH must not write any facts"
        )
        assert snap_post["verification_cache"] == snap_mid["verification_cache"], (
            "derivation MATCH must not write any verification_cache rows"
        )
        # Substrate row counts identical to mid because no LLM ran.
        assert snap_post["entity_taxonomy"] == snap_mid["entity_taxonomy"]
        assert snap_post["predicate_distribution"] == snap_mid["predicate_distribution"]
        assert snap_post["predicate_equivalence"] == snap_mid["predicate_equivalence"]
        assert snap_post["entity_equivalence"] == snap_mid["entity_equivalence"]


# ============================================================================
# Pipeline events
# ============================================================================


class TestPipelineEvents:
    def test_attempt_and_completed_events_match_path(
        self, store, registry, all_oracles,
    ):
        store.insert_turn("user", "anything")
        turn_id = 1
        _store_user_fact(
            store, pattern="spatial_temporal", predicate="lives_in",
            slots={"entity": "user", "location": "Williamstown"},
            polarity=1,
        )
        all_oracles["taxonomy_oracle"].record(
            "Williamstown", "Massachusetts", "part_of",
            label="child_subsumed_by_parent", reason="town in state",
        )
        all_oracles["distribution_oracle"].record(
            "spatial_temporal", "lives_in", 1, "part_of",
            label="distributes_up", reason="residence aggregates",
        )
        claim = {
            "pattern": "spatial_temporal",
            "predicate": "lives_in",
            "polarity": 1,
            "slots": {"entity": "user", "location": "Massachusetts"},
            "source_text": "you live in Massachusetts",
        }
        _walk(claim, store, registry, all_oracles, source_turn_id=turn_id)
        events = store.get_pipeline_events(turn_id)
        stages = [e["stage"] for e in events]
        assert "derivation_walk_attempt" in stages
        assert "derivation_walk_completed" in stages
        completed = [e for e in events if e["stage"] == "derivation_walk_completed"]
        assert completed[0]["data"].get("outcome") == "match"

    def test_aborted_depth_event_on_overrun(
        self, store, registry, all_oracles,
    ):
        store.insert_turn("user", "anything")
        turn_id = 1
        _store_user_fact(
            store, pattern="spatial_temporal", predicate="lives_in",
            slots={"entity": "user", "location": "A"},
            polarity=1,
        )
        for child, parent in [
            ("A", "B"), ("B", "C"), ("C", "D"), ("D", "E"), ("E", "F"),
        ]:
            all_oracles["taxonomy_oracle"].record(
                child, parent, "part_of",
                label="child_subsumed_by_parent", reason="chain",
            )
        all_oracles["distribution_oracle"].record(
            "spatial_temporal", "lives_in", 1, "part_of",
            label="distributes_up", reason="residence aggregates",
        )
        claim = {
            "pattern": "spatial_temporal",
            "predicate": "lives_in",
            "polarity": 1,
            "slots": {"entity": "user", "location": "F"},
            "source_text": "you live in F",
        }
        _walk(claim, store, registry, all_oracles, source_turn_id=turn_id)
        events = store.get_pipeline_events(turn_id)
        stages = [e["stage"] for e in events]
        # The 5-hop chain over MAX_DEPTH=4 emits aborted_depth at least
        # once before the BFS exhausts.
        assert "derivation_walk_aborted_depth" in stages
