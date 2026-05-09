"""Tests for src.layer2_routing.reconciler (Layer 2.5).

Pure-function unit tests; no LLM, no store. Covers the verifier-
shape mismatch override (the Cairo case) and the pass-through
behavior for compatible (pattern, verifier) tuples.
"""

from __future__ import annotations

import pytest

from src.layer1_extraction.pattern_registry import (
    load_default_registry, reset_cache,
)
from src.layer2_routing.reconciler import reconcile_routing
from src.layer2_routing.types import (
    Decision, RoutingOutcome, ValidationResult,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_cache()
    yield
    reset_cache()


@pytest.fixture
def registry():
    return load_default_registry()


def _make_decision(claim, *, method, memo_hit=False):
    """Build a Decision shaped like Router.classify would emit."""
    return Decision(
        claim=claim,
        outcome=RoutingOutcome.CLASSIFIED,
        method=method,
        reason=f"router picked {method!r}",
        memo_hit=memo_hit,
        validation=ValidationResult.passed(),
        routing_decision={
            "method": method,
            "reason": f"router picked {method!r}",
            "python_inputs_self_contained": None,
            "retrieval_query_hint": None,
            "canonical_constants_needed": None,
        },
        notes=[],
    )


# ---- Cairo regression ------------------------------------------------


def test_python_on_spatial_temporal_overrides_to_retrieval(registry):
    """Cairo timezone case: spatial_temporal.located_in routed to
    python. Pattern has no value slot → python verifier can't
    compare. Reconciler overrides to spatial_temporal's
    default_routing_method (retrieval per the schema)."""
    claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "polarity": 1,
        "slots": {"entity": "Cairo", "location": "Egypt Standard Time",
                  "relation_kind": "timezone"},
        "source_text": "Cairo is in Egypt Standard Time",
    }
    layer2 = _make_decision(claim, method="python")
    new_layer2, result = reconcile_routing(claim, layer2, registry)
    assert result.reconciled
    assert result.override_method == "retrieval"
    assert new_layer2.method == "retrieval"
    assert new_layer2.routing_decision["original_method"] == "python"
    # Notes carry the override audit trail for trace UI.
    assert any("routing reconciled" in n for n in new_layer2.notes)


def test_python_on_mereological_overrides(registry):
    """Same gap on mereological: 'X is part of Y' has no value slot."""
    claim = {
        "pattern": "mereological", "predicate": "part_of",
        "polarity": 1,
        "slots": {"part": "Williamstown", "whole": "Massachusetts"},
        "source_text": "Williamstown is part of Massachusetts",
    }
    layer2 = _make_decision(claim, method="python")
    _, result = reconcile_routing(claim, layer2, registry)
    assert result.reconciled
    assert result.override_method == "retrieval"


def test_python_on_categorical_overrides(registry):
    claim = {
        "pattern": "categorical", "predicate": "is_a",
        "polarity": 1,
        "slots": {"entity": "Tokyo", "category": "city"},
        "source_text": "Tokyo is a city",
    }
    layer2 = _make_decision(claim, method="python")
    _, result = reconcile_routing(claim, layer2, registry)
    assert result.reconciled
    assert result.override_method == "retrieval"


# ---- Compatible tuples pass through unchanged ------------------------


def test_python_on_quantitative_passes_through(registry):
    """quantitative HAS a value slot — python is a legitimate
    routing target. No override."""
    claim = {
        "pattern": "quantitative", "predicate": "has_count",
        "polarity": 1,
        "slots": {"subject": "strawberry", "property": "letter_r", "value": 3},
        "source_text": "strawberry has 3 r's",
    }
    layer2 = _make_decision(claim, method="python")
    new_layer2, result = reconcile_routing(claim, layer2, registry)
    assert not result.reconciled
    assert new_layer2.method == "python"


def test_retrieval_on_spatial_temporal_passes_through(registry):
    """Retrieval doesn't need a value slot — works on any pattern.
    No override needed."""
    claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "polarity": 1,
        "slots": {"entity": "Cairo", "location": "Egypt"},
        "source_text": "Cairo is in Egypt",
    }
    layer2 = _make_decision(claim, method="retrieval")
    new_layer2, result = reconcile_routing(claim, layer2, registry)
    assert not result.reconciled
    assert new_layer2.method == "retrieval"


def test_user_authoritative_on_preference_passes_through(registry):
    """user_authoritative routes to Tier U; no value slot needed."""
    claim = {
        "pattern": "preference", "predicate": "loves",
        "polarity": 1,
        "slots": {"agent": "user", "object": "olives"},
        "source_text": "I love olives",
    }
    layer2 = _make_decision(claim, method="user_authoritative")
    _, result = reconcile_routing(claim, layer2, registry)
    assert not result.reconciled


# ---- Anomaly + edge cases --------------------------------------------


def test_routing_anomaly_passes_through_unchanged(registry):
    """Anomaly decisions have no method to reconcile."""
    claim = {
        "pattern": "preference", "predicate": "loves",
        "polarity": 1,
        "slots": {"agent": "baboon", "object": "fruit"},
        "source_text": "baboons love fruit",
    }
    layer2 = Decision(
        claim=claim,
        outcome=RoutingOutcome.ROUTING_ANOMALY,
        method=None, reason=None, memo_hit=False,
        validation=ValidationResult.passed(),  # placeholder
        routing_decision=None, notes=[],
    )
    new_layer2, result = reconcile_routing(claim, layer2, registry)
    assert not result.reconciled
    assert new_layer2.outcome is RoutingOutcome.ROUTING_ANOMALY


def test_unknown_pattern_passes_through(registry):
    """An unknown-pattern claim shouldn't be reconciled — the
    validator should have caught it."""
    claim = {
        "pattern": "made_up", "predicate": "x",
        "polarity": 1, "slots": {"value": 1},
        "source_text": "x",
    }
    layer2 = _make_decision(claim, method="python")
    _, result = reconcile_routing(claim, layer2, registry)
    assert not result.reconciled


# ---- v0.14.5 multi-signal reconciler --------------------------------


def test_vowel_count_router_picks_retrieval_overridden_to_python(registry):
    """Vowel-count regression: schema says python_with_canonical_constants,
    predicate vowel_count IS in quantitative.triage_verify_predicates,
    extractor self-attested python. Router (LLM) picked retrieval. The
    multi-signal reconciler overrides to schema's default."""
    claim = {
        "pattern": "quantitative", "predicate": "vowel_count",
        "polarity": 1,
        "slots": {"subject": "I don't want to eat bread.",
                  "property": "vowel_count", "value": 6},
        "source_text": "6 vowels",
        "expected_verifier": "python",
    }
    layer2 = _make_decision(claim, method="retrieval")
    new_layer2, result = reconcile_routing(claim, layer2, registry)
    assert result.reconciled, (
        f"expected override; got reason={result.reason!r}"
    )
    assert new_layer2.method == "python_with_canonical_constants"
    assert "multi-signal" in result.reason
    assert "vowel_count" in result.reason or "extractor=python" in result.reason


def test_letter_count_router_picks_retrieval_overridden(registry):
    """Same regression for letter_count — another quantitative
    computable predicate."""
    claim = {
        "pattern": "quantitative", "predicate": "letter_count",
        "polarity": 1,
        "slots": {"subject": "strawberry", "property": "letter_r", "value": 3},
        "source_text": "Strawberry has 3 r's",
        "expected_verifier": "python",
    }
    layer2 = _make_decision(claim, method="retrieval")
    new_layer2, result = reconcile_routing(claim, layer2, registry)
    assert result.reconciled
    assert new_layer2.method == "python_with_canonical_constants"


def test_schema_plus_predicate_allow_list_alone_overrides(registry):
    """Even WITHOUT extractor self-attestation, the schema_default
    + predicate_allow_list signals together count as 2-of-3 agreement
    and override the router's pick."""
    claim = {
        "pattern": "quantitative", "predicate": "current_time",
        "polarity": 1,
        "slots": {"subject": "Cairo", "property": "time", "value": "9:56 am"},
        "source_text": "9:56 am",
        # expected_verifier omitted — only signals 1 + 2 fire.
    }
    layer2 = _make_decision(claim, method="retrieval")
    _, result = reconcile_routing(claim, layer2, registry)
    assert result.reconciled
    assert "schema_default" in result.reason


def test_single_signal_disagreement_passes_through(registry):
    """When ONLY the extractor disagrees with the router (predicate
    not in allow-list, single signal), no override fires. The LLM
    router has the deciding vote on ambiguous predicates."""
    claim = {
        "pattern": "quantitative", "predicate": "molecular_weight",
        "polarity": 1,
        "slots": {"subject": "water", "property": "molar_mass_g_mol", "value": 18.015},
        "source_text": "18.015 g/mol",
        "expected_verifier": "retrieval",  # extractor disagrees with schema
    }
    # Router picks python_with_canonical_constants (the schema default).
    # Signals: schema=python (signal 1), allow-list NOT triggered
    # (molecular_weight not in quantitative.triage_verify_predicates),
    # extractor=retrieval (signal 3). 1-1 split. No 2-of-3.
    layer2 = _make_decision(claim, method="python_with_canonical_constants")
    _, result = reconcile_routing(claim, layer2, registry)
    assert not result.reconciled


def test_router_pick_in_same_family_as_consensus_no_override(registry):
    """Router picked python_with_canonical_constants; signals say
    python (different method, same family). No override — they're
    in the same family, the schema considers them interchangeable."""
    claim = {
        "pattern": "quantitative", "predicate": "vowel_count",
        "polarity": 1,
        "slots": {"subject": "test", "property": "vowel_count", "value": 1},
        "source_text": "1 vowel",
        "expected_verifier": "python",
    }
    layer2 = _make_decision(claim, method="python_with_canonical_constants")
    _, result = reconcile_routing(claim, layer2, registry)
    assert not result.reconciled


def test_user_authoritative_router_pick_never_overridden(registry):
    """The LLM router owns user_authoritative routing decisions —
    those carry per-claim semantic judgment the schema can't
    capture. Even if schema + extractor agree on retrieval, the
    user_authoritative pick stands."""
    claim = {
        "pattern": "preference", "predicate": "loves",
        "polarity": 1,
        "slots": {"agent": "user", "object": "olives"},
        "source_text": "I love olives",
        "expected_verifier": "retrieval",  # nonsensical but extractor said it
    }
    layer2 = _make_decision(claim, method="user_authoritative")
    _, result = reconcile_routing(claim, layer2, registry)
    assert not result.reconciled, (
        "user_authoritative router pick must never be auto-overridden"
    )


def test_consensus_on_user_authoritative_never_triggers_override(registry):
    """Inverse direction: when all signals point at user_authoritative
    or unverifiable (non-overridable methods), the multi-signal check
    doesn't fire — those signals don't contribute to the family-based
    consensus by design (_method_family returns None for them)."""
    claim = {
        "pattern": "preference", "predicate": "loves",
        "polarity": 1,
        "slots": {"agent": "user", "object": "olives"},
        "source_text": "I love olives",
        "expected_verifier": "user_authoritative",
    }
    # Pretend the router picked retrieval (wrong but possible).
    # Signals: schema=user_authoritative (filtered, family=None),
    # allow-list NOT in (preference's triage_verify_predicates is
    # empty), extractor=user_authoritative (filtered). No
    # python/retrieval consensus → no override.
    layer2 = _make_decision(claim, method="retrieval")
    _, result = reconcile_routing(claim, layer2, registry)
    assert not result.reconciled


def test_full_three_signal_agreement_overrides(registry):
    """The vowel-count case at full 3-of-3 strength: schema +
    predicate-allow-list + extractor all say python_with_canonical_
    constants. Router picked retrieval. Override is unambiguous."""
    claim = {
        "pattern": "quantitative", "predicate": "current_time",
        "polarity": 1,
        "slots": {"subject": "Cairo", "property": "time", "value": "9:56 am"},
        "source_text": "9:56 am in Cairo",
        "expected_verifier": "python",
    }
    layer2 = _make_decision(claim, method="retrieval")
    _, result = reconcile_routing(claim, layer2, registry)
    assert result.reconciled
    # All three signal sources should appear in the reason.
    assert "schema_default" in result.reason
    assert "predicate_allow_list" in result.reason
    assert "extractor" in result.reason
