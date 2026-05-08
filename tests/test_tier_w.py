"""Tier W tests (v0.14 Phase 7b).

Pins three contracts:

  1. **Pattern-registry-driven canonical-key normalization.** Entity
     slots case-preserved (Apple ≠ apple). Numeric values normalized
     (5 == 5.0). String / date slots case-folded.

  2. **Three-stage oracle resolution chain.** Mirrors Tier U:
     literal → predicate_equivalence → entity_equivalence (alias
     identity). Each stage emits its own pipeline event.

  3. **8-state verification_status preservation.** Tier W writes one
     of 6 in-domain values; the column carries them faithfully.
     Refresh/contradiction count semantics on conflict.

Pipeline events: tier_w_hit on literal match; tier_w_lookup on
every stage outcome; tier_w_write on insert/refresh/replace;
cache_contradiction_replaced on overwrite.

The canonicalize-on-the-fact-store-fresh test gates the v1-vs-v2
divergence: if a future change accidentally restores v1's blanket
case-folding, Apple-vs-apple keys collide and the test fires.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.fact_store import FactStore
from src.layer1_extraction.pattern_registry import (
    PatternRegistry,
)
from src.layer3_substrate.entity_equivalence import (
    EntityEquivalence,
)
from src.layer3_substrate.predicate_equivalence import (
    PredicateEquivalence,
)
from src.layer4_lookup import tier_w
from src.layer4_lookup.types import (
    LookupOutcome,
    TierWResult,
)


# ============================================================================
# Fixtures
# ============================================================================


_PATTERNS_PATH = (
    Path(__file__).parent.parent
    / "src" / "layer1_extraction" / "patterns.yaml"
)


@pytest.fixture(scope="module")
def registry() -> PatternRegistry:
    return PatternRegistry.from_yaml(_PATTERNS_PATH)


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "tier_w.db")
    yield s
    s.close()


@pytest.fixture
def predicate_oracle(store):
    return PredicateEquivalence(store)


@pytest.fixture
def entity_oracle(store):
    return EntityEquivalence(store)


class _MockLLM:
    """Queue-backed stub matching ``LLMClient.extract_with_tool``."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def extract_with_tool(self, *, system, user_message, tool, purpose):
        self.calls.append({"user_message": user_message, "purpose": purpose})
        if not self.responses:
            raise AssertionError(
                f"MockLLM ran out of responses for purpose={purpose}; "
                f"unexpected LLM call (oracle should have served from cache)"
            )
        return self.responses.pop(0)


def _spatial_temporal_claim(entity: str, location: str, polarity: int = 1):
    return {
        "pattern": "spatial_temporal",
        "predicate": "lives_in",
        "polarity": polarity,
        "slots": {"entity": entity, "location": location},
        "source_text": f"{entity} lives in {location}",
    }


def _preference_claim(agent: str, obj: str, predicate: str = "likes",
                      polarity: int = 1):
    return {
        "pattern": "preference",
        "predicate": predicate,
        "polarity": polarity,
        "slots": {"agent": agent, "object": obj},
        "source_text": f"{agent} {predicate} {obj}",
    }


# ============================================================================
# Canonical-key normalization (Ambiguity #3 contracts)
# ============================================================================


class TestCanonicalKeyEntityCaseSensitivity:
    """Entity-typed slots preserve case — apple ≠ Apple."""

    def test_lowercase_apple_vs_capital_apple_differ(self, registry):
        claim_lower = _preference_claim("user", "apple")
        claim_upper = _preference_claim("user", "Apple")
        key_lower = tier_w.canonicalize_claim_key(claim_lower, registry)
        key_upper = tier_w.canonicalize_claim_key(claim_upper, registry)
        assert key_lower != key_upper, (
            f"entity case must be preserved; got identical keys for "
            f"apple and Apple: {key_lower}"
        )
        assert "object=apple" in key_lower
        assert "object=Apple" in key_upper

    def test_nyc_vs_lowercase_nyc_differ(self, registry):
        a = _spatial_temporal_claim("user", "NYC")
        b = _spatial_temporal_claim("user", "nyc")
        assert (
            tier_w.canonicalize_claim_key(a, registry)
            != tier_w.canonicalize_claim_key(b, registry)
        )

    def test_williamstown_case_preserved(self, registry):
        # Williamstown is a real entity; lowercasing would lose
        # the proper-noun signal that helps the oracle distinguish.
        claim = _spatial_temporal_claim("user", "Williamstown")
        key = tier_w.canonicalize_claim_key(claim, registry)
        assert "Williamstown" in key
        assert "williamstown" not in key.replace("Williamstown", "")


class TestCanonicalKeyNumericNormalization:
    """Numeric slot values: 5 and 5.0 produce the same key."""

    def test_int_and_int_float_collide(self, registry):
        claim_int = {
            "pattern": "quantitative",
            "predicate": "has_count",
            "polarity": 1,
            "slots": {"subject": "strawberry", "value": 5},
            "source_text": "strawberries have 5 seeds",
        }
        claim_float = dict(claim_int)
        claim_float["slots"] = {"subject": "strawberry", "value": 5.0}
        assert (
            tier_w.canonicalize_claim_key(claim_int, registry)
            == tier_w.canonicalize_claim_key(claim_float, registry)
        )

    def test_non_integer_float_preserved(self, registry):
        claim = {
            "pattern": "quantitative",
            "predicate": "weighs",
            "polarity": 1,
            "slots": {"subject": "object", "value": 5.5},
            "source_text": "weighs 5.5",
        }
        key = tier_w.canonicalize_claim_key(claim, registry)
        assert "value=5.5" in key

    def test_bool_normalized(self, registry):
        claim = {
            "pattern": "quantitative",
            "predicate": "has_property",
            "polarity": 1,
            "slots": {"subject": "X", "value": True},
            "source_text": "X has property true",
        }
        key = tier_w.canonicalize_claim_key(claim, registry)
        assert "value=true" in key


class TestCanonicalKeyPredicateNormalization:
    """Predicates ARE case-presentational — Likes / likes / LIKES collide."""

    def test_predicate_case_collides(self, registry):
        a = _preference_claim("user", "olives", predicate="likes")
        b = _preference_claim("user", "olives", predicate="Likes")
        c = _preference_claim("user", "olives", predicate="LIKES")
        ka = tier_w.canonicalize_claim_key(a, registry)
        kb = tier_w.canonicalize_claim_key(b, registry)
        kc = tier_w.canonicalize_claim_key(c, registry)
        assert ka == kb == kc

    def test_predicate_whitespace_collapsed(self, registry):
        a = _preference_claim("user", "olives", predicate="likes")
        b = _preference_claim("user", "olives", predicate="  likes  ")
        assert (
            tier_w.canonicalize_claim_key(a, registry)
            == tier_w.canonicalize_claim_key(b, registry)
        )


class TestCanonicalKeyTense:
    """Past vs present tense produce different keys."""

    def test_past_tense_differs(self, registry):
        claim_present = _spatial_temporal_claim("user", "Boston")
        claim_past = dict(claim_present)
        claim_past["source_text"] = "user was in Boston"
        kp = tier_w.canonicalize_claim_key(claim_present, registry)
        kt = tier_w.canonicalize_claim_key(claim_past, registry)
        assert kp != kt
        assert "|t=present|" in kp
        assert "|t=past|" in kt


class TestCanonicalKeyPolarity:
    """Polarity is encoded in the key."""

    def test_polarity_encoded(self, registry):
        a = _preference_claim("user", "olives", polarity=1)
        b = _preference_claim("user", "olives", polarity=0)
        assert (
            tier_w.canonicalize_claim_key(a, registry)
            != tier_w.canonicalize_claim_key(b, registry)
        )


class TestCanonicalKeyShape:
    """The encoded shape is the contract Phase 9 / trace UIs grep against."""

    def test_shape_segments_present(self, registry):
        claim = _preference_claim("user", "olives")
        key = tier_w.canonicalize_claim_key(claim, registry)
        # Shape: pattern|predicate|p=N|t=tense|key1=val&key2=val
        parts = key.split("|")
        assert parts[0] == "preference"
        assert parts[1] == "likes"
        assert parts[2] == "p=1"
        assert parts[3] == "t=present"
        assert parts[4] == "agent=user&object=olives"

    def test_slots_alphabetically_sorted(self, registry):
        # Use spatial_temporal: entity comes alphabetically before
        # location. The key must reflect the sort.
        claim = _spatial_temporal_claim("user", "Boston")
        key = tier_w.canonicalize_claim_key(claim, registry)
        slots_block = key.split("|", 4)[-1]
        assert slots_block == "entity=user&location=Boston"


# ============================================================================
# Literal lookup
# ============================================================================


class TestLiteralLookup:
    """Stage 1 of the resolution chain: SQL match on canonical_key."""

    def test_miss_when_table_empty(
        self, store, registry, predicate_oracle,
    ):
        claim = _preference_claim("user", "olives")
        result = tier_w.lookup(
            claim, store, predicate_oracle,
            key_slot_names=["agent", "object"],
            registry=registry,
        )
        assert result.outcome is LookupOutcome.MISS
        assert result.via == []

    def test_match_on_literal_same_key(
        self, store, registry, predicate_oracle,
    ):
        claim = _spatial_temporal_claim("user", "Boston")
        outcome = tier_w.write_verifier_result(
            claim, store,
            verification_status="verified",
            registry=registry,
            evidence={"snippets": [{"url": "https://example.com/boston"}]},
        )
        assert outcome.action == "inserted"

        result = tier_w.lookup(
            claim, store, predicate_oracle,
            key_slot_names=["entity", "location"],
            registry=registry,
        )
        assert result.outcome is LookupOutcome.MATCH
        assert result.matching_row_id == outcome.row_id
        assert result.verification_status == "verified"
        assert result.via == []
        assert result.evidence == {
            "snippets": [{"url": "https://example.com/boston"}]
        }

    def test_contradiction_on_opposite_polarity_literal(
        self, store, registry, predicate_oracle,
    ):
        # Cache holds (lives_in, polarity=1). Query is (lives_in, polarity=0).
        # The opposite-polarity claim has a different canonical_key (polarity
        # is encoded), but the lookup checks both polarities and surfaces
        # the contradiction.
        positive = _spatial_temporal_claim("user", "Boston", polarity=1)
        tier_w.write_verifier_result(
            positive, store,
            verification_status="verified",
            registry=registry,
        )
        negative = _spatial_temporal_claim("user", "Boston", polarity=0)
        result = tier_w.lookup(
            negative, store, predicate_oracle,
            key_slot_names=["entity", "location"],
            registry=registry,
        )
        assert result.outcome is LookupOutcome.CONTRADICTION
        assert result.contradicting_row_id is not None
        assert result.via == []

    def test_expired_row_returns_miss(
        self, store, registry, predicate_oracle,
    ):
        claim = _spatial_temporal_claim("user", "Boston")
        # Write with TTL=1 second.
        tier_w.write_verifier_result(
            claim, store,
            verification_status="verified",
            registry=registry,
            ttl_seconds=1,
        )
        # Wait for expiry. (1-second sleep is acceptable in tests; the
        # alternative is a clock-mocking harness, which is overkill
        # for this single contract.)
        time.sleep(1.1)
        result = tier_w.lookup(
            claim, store, predicate_oracle,
            key_slot_names=["entity", "location"],
            registry=registry,
        )
        assert result.outcome is LookupOutcome.MISS

    def test_volatile_write_skipped(
        self, store, registry, predicate_oracle,
    ):
        claim = _spatial_temporal_claim("user", "Boston")
        outcome = tier_w.write_verifier_result(
            claim, store,
            verification_status="verified",
            registry=registry,
            ttl_seconds=0,  # volatile
        )
        assert outcome.action == "skipped_volatile"
        assert outcome.row_id is None
        # No row should have been written.
        result = tier_w.lookup(
            claim, store, predicate_oracle,
            key_slot_names=["entity", "location"],
            registry=registry,
        )
        assert result.outcome is LookupOutcome.MISS

    def test_immutable_write_no_expiry(
        self, store, registry, predicate_oracle,
    ):
        claim = _preference_claim("user", "olives")
        outcome = tier_w.write_verifier_result(
            claim, store,
            verification_status="verified",
            registry=registry,
            stability_class="immutable",
            ttl_seconds=None,
        )
        assert outcome.action == "inserted"

        time.sleep(0.05)  # any non-zero wait
        result = tier_w.lookup(
            claim, store, predicate_oracle,
            key_slot_names=["agent", "object"],
            registry=registry,
        )
        assert result.outcome is LookupOutcome.MATCH


# ============================================================================
# Predicate-equivalence broadening (stage 2)
# ============================================================================


class TestPredicateEquivalenceBroadening:
    """Stage 2: cache row's predicate differs from the claim, but
    the predicate_equivalence oracle says they're paraphrase variants."""

    def test_match_via_predicate_equivalence(
        self, store, registry, predicate_oracle,
    ):
        # Cache: (preference, dislikes, agent=user, object=olives, p=1)
        cached_claim = _preference_claim(
            "user", "olives", predicate="dislikes", polarity=1,
        )
        tier_w.write_verifier_result(
            cached_claim, store,
            verification_status="verified",
            registry=registry,
        )
        # Pre-warm the oracle: (likes, dislikes) → contradictory
        predicate_oracle.record(
            "preference", "dislikes", "likes",
            label="contradictory", slot_reversal="none",
            reason="antonym preference predicates",
        )

        # Query: (preference, likes, agent=user, object=olives, p=0).
        # The polarity flip + contradictory verdict makes this a MATCH
        # on the cached dislikes/p=1 row.
        query = _preference_claim(
            "user", "olives", predicate="likes", polarity=0,
        )
        result = tier_w.lookup(
            query, store, predicate_oracle,
            key_slot_names=["agent", "object"],
            registry=registry,
        )
        assert result.outcome is LookupOutcome.MATCH
        assert "predicate_equivalence" in result.via
        assert result.polarity_flipped is True
        assert result.predicate_equivalence_row_id is not None

    def test_distinct_falls_through_to_miss(
        self, store, registry, predicate_oracle,
    ):
        cached_claim = _preference_claim(
            "user", "olives", predicate="likes", polarity=1,
        )
        tier_w.write_verifier_result(
            cached_claim, store,
            verification_status="verified",
            registry=registry,
        )
        predicate_oracle.record(
            "preference", "likes", "loves",
            label="distinct", slot_reversal="none",
            reason="different intensity",
        )

        query = _preference_claim(
            "user", "olives", predicate="loves", polarity=1,
        )
        result = tier_w.lookup(
            query, store, predicate_oracle,
            key_slot_names=["agent", "object"],
            registry=registry,
        )
        assert result.outcome is LookupOutcome.MISS

    def test_slot_reversal_falls_through(
        self, store, registry, predicate_oracle,
    ):
        """Phase 7 doesn't consume slot_reversal != 'none' verdicts at
        the cache layer (deferred; mirrors Tier U)."""
        cached_claim = _preference_claim(
            "user", "olives", predicate="dislikes", polarity=1,
        )
        tier_w.write_verifier_result(
            cached_claim, store,
            verification_status="verified",
            registry=registry,
        )
        predicate_oracle.record(
            "preference", "dislikes", "likes",
            label="contradictory", slot_reversal="subject_object_swap",
            reason="hypothetical slot-swap antonym",
        )
        query = _preference_claim(
            "user", "olives", predicate="likes", polarity=0,
        )
        result = tier_w.lookup(
            query, store, predicate_oracle,
            key_slot_names=["agent", "object"],
            registry=registry,
        )
        assert result.outcome is LookupOutcome.MISS


# ============================================================================
# Alias-identity broadening (stage 3)
# ============================================================================


class TestAliasIdentityBroadening:
    """Stage 3: cache rows under (pattern) with no exact key match;
    entity_equivalence resolves aliases."""

    def test_match_via_entity_equivalence(
        self, store, registry, predicate_oracle, entity_oracle,
    ):
        cached_claim = _spatial_temporal_claim("user", "NYC", polarity=1)
        tier_w.write_verifier_result(
            cached_claim, store,
            verification_status="verified",
            registry=registry,
        )
        entity_oracle.record("NYC", "New York City", "same",
                             reason="city alias")

        query = _spatial_temporal_claim(
            "user", "New York City", polarity=1,
        )
        result = tier_w.lookup(
            query, store, predicate_oracle,
            key_slot_names=["entity", "location"],
            registry=registry,
            entity_oracle=entity_oracle,
        )
        assert result.outcome is LookupOutcome.MATCH
        assert "entity_equivalence" in result.via
        assert result.entity_equivalence_row_ids != []

    def test_different_entities_miss(
        self, store, registry, predicate_oracle, entity_oracle,
    ):
        cached_claim = _spatial_temporal_claim("user", "Tokyo", polarity=1)
        tier_w.write_verifier_result(
            cached_claim, store,
            verification_status="verified",
            registry=registry,
        )
        entity_oracle.record("Japan", "Tokyo", "different",
                             reason="containment, not equivalence")

        query = _spatial_temporal_claim("user", "Japan", polarity=1)
        result = tier_w.lookup(
            query, store, predicate_oracle,
            key_slot_names=["entity", "location"],
            registry=registry,
            entity_oracle=entity_oracle,
        )
        assert result.outcome is LookupOutcome.MISS

    def test_apple_vs_apple_case_disambiguation(
        self, store, registry, predicate_oracle, entity_oracle,
    ):
        """The apple/Apple case-disambiguation works at the cache
        layer because canonical keys preserve case AND
        entity_equivalence's case-sensitivity contract."""
        cached_claim = _preference_claim(
            "user", "apple", predicate="likes", polarity=1,
        )  # the fruit
        tier_w.write_verifier_result(
            cached_claim, store,
            verification_status="verified",
            registry=registry,
        )
        entity_oracle.record(
            "Apple", "apple", "different",
            reason="capitalization disambiguates company vs fruit",
        )
        query = _preference_claim(
            "user", "Apple", predicate="likes", polarity=1,
        )  # the company
        result = tier_w.lookup(
            query, store, predicate_oracle,
            key_slot_names=["agent", "object"],
            registry=registry,
            entity_oracle=entity_oracle,
        )
        assert result.outcome is LookupOutcome.MISS, (
            "Apple-vs-apple case-disambiguation must surface as MISS "
            "even at the cache layer"
        )


# ============================================================================
# write_verifier_result — the 8-state status preservation contract
# ============================================================================


class TestWriteVerifierResult:
    """Tier W writes the full 8-state verification_status to the
    ``verdict`` column (Ambiguity #2 of the Phase 7 plan)."""

    @pytest.mark.parametrize("status", [
        "verified",
        "contradicted",
        "unverifiable_in_principle",
        "retrieval_inconclusive",
        "retrieval_failed",
        "unverifiable_pending_implementation",
    ])
    def test_in_domain_statuses_round_trip(
        self, store, registry, predicate_oracle, status,
    ):
        claim = _spatial_temporal_claim("user", "Boston")
        outcome = tier_w.write_verifier_result(
            claim, store,
            verification_status=status,
            registry=registry,
        )
        assert outcome.action == "inserted"

        result = tier_w.lookup(
            claim, store, predicate_oracle,
            key_slot_names=["entity", "location"],
            registry=registry,
        )
        assert result.outcome is LookupOutcome.MATCH
        assert result.verification_status == status

    @pytest.mark.parametrize("bad_status", [
        "user_asserted",       # Tier U's domain
        "routing_anomaly",     # Layer 2's terminal state
        "made_up",             # invalid
        "VERIFIED",            # case-sensitive
    ])
    def test_out_of_domain_status_rejected(
        self, store, registry, bad_status,
    ):
        claim = _spatial_temporal_claim("user", "Boston")
        with pytest.raises(ValueError):
            tier_w.write_verifier_result(
                claim, store,
                verification_status=bad_status,
                registry=registry,
            )

    def test_refresh_count_bumps_on_same_verdict(
        self, store, registry,
    ):
        claim = _spatial_temporal_claim("user", "Boston")
        first = tier_w.write_verifier_result(
            claim, store,
            verification_status="verified",
            registry=registry,
        )
        second = tier_w.write_verifier_result(
            claim, store,
            verification_status="verified",
            registry=registry,
        )
        assert first.action == "inserted"
        assert second.action == "refreshed"
        # Verify the row's refresh_count reflects the second write.
        row = store._conn.execute(
            "SELECT refresh_count, contradiction_count FROM verification_cache "
            "WHERE canonical_key = ?",
            (second.canonical_key,),
        ).fetchone()
        assert row["refresh_count"] == 1
        assert row["contradiction_count"] == 0

    def test_contradiction_count_bumps_on_different_verdict(
        self, store, registry,
    ):
        claim = _spatial_temporal_claim("user", "Boston")
        tier_w.write_verifier_result(
            claim, store,
            verification_status="verified",
            registry=registry,
        )
        outcome = tier_w.write_verifier_result(
            claim, store,
            verification_status="contradicted",
            registry=registry,
        )
        assert outcome.action == "contradicted_and_replaced"
        assert outcome.prior_verdict == "verified"
        row = store._conn.execute(
            "SELECT verdict, refresh_count, contradiction_count "
            "FROM verification_cache WHERE canonical_key = ?",
            (outcome.canonical_key,),
        ).fetchone()
        assert row["verdict"] == "contradicted"
        assert row["refresh_count"] == 0
        assert row["contradiction_count"] == 1


# ============================================================================
# Pipeline events
# ============================================================================


class TestPipelineEvents:
    """The trace UI greps these stage names; pin them."""

    def test_tier_w_hit_on_literal_match(
        self, store, registry, predicate_oracle,
    ):
        claim = _spatial_temporal_claim("user", "Boston")
        store.insert_turn("user", "I live in Boston")
        turn_id = 1
        tier_w.write_verifier_result(
            claim, store,
            verification_status="verified",
            registry=registry,
            source_turn_id=turn_id,
        )
        tier_w.lookup(
            claim, store, predicate_oracle,
            key_slot_names=["entity", "location"],
            registry=registry,
            source_turn_id=turn_id,
        )
        events = store.get_pipeline_events(turn_id)
        stages = [e["stage"] for e in events]
        assert "tier_w_write" in stages
        assert "tier_w_hit" in stages
        assert "tier_w_lookup" in stages

    def test_tier_w_lookup_miss_event(
        self, store, registry, predicate_oracle,
    ):
        store.insert_turn("user", "anything")
        turn_id = 1
        claim = _spatial_temporal_claim("user", "Boston")
        tier_w.lookup(
            claim, store, predicate_oracle,
            key_slot_names=["entity", "location"],
            registry=registry,
            source_turn_id=turn_id,
        )
        events = store.get_pipeline_events(turn_id)
        miss_events = [
            e for e in events
            if e["stage"] == "tier_w_lookup"
            and e["data"].get("outcome") == "miss"
        ]
        assert len(miss_events) == 1

    def test_cache_contradiction_replaced_event(
        self, store, registry,
    ):
        store.insert_turn("user", "anything")
        turn_id = 1
        claim = _spatial_temporal_claim("user", "Boston")
        tier_w.write_verifier_result(
            claim, store,
            verification_status="verified",
            registry=registry,
            source_turn_id=turn_id,
        )
        tier_w.write_verifier_result(
            claim, store,
            verification_status="contradicted",
            registry=registry,
            source_turn_id=turn_id,
        )
        events = store.get_pipeline_events(turn_id)
        stages = [e["stage"] for e in events]
        assert "cache_contradiction_replaced" in stages


# ============================================================================
# Migration sketch (documentation only)
# ============================================================================


class TestMigrationDocumentationOnly:
    """The migration helper is documentation only — never called from
    production. The test pins that it exists and produces a
    non-empty playbook string."""

    def test_migration_playbook_documented(self):
        playbook = tier_w._document_migration_pattern()
        assert isinstance(playbook, str)
        assert "verified" in playbook
        assert "retrieval_inconclusive" in playbook
        assert "retrieval_failed" in playbook
        assert "NOT a migration script" in playbook
