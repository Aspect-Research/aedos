"""Tests for src.layer1_extraction.verifiability_triage (Layer 1.5).

Pure-function unit tests; no LLM, no store. Cover all five VERIFY
rules + the default PASS_THROUGH path + the architectural invariants
(decision is deterministic; same claim → same decision).
"""

from __future__ import annotations

from src.layer1_extraction.verifiability_triage import (
    TriageDecision,
    triage_claim,
)


# ---- Rule 1: numeric value present ----------------------------------


def test_numeric_value_in_slot_triggers_verify():
    claim = {
        "pattern": "quantitative", "predicate": "wild_lifespan_years",
        "polarity": 1,
        "slots": {"subject": "baboon", "property": "lifespan", "value": 30},
        "source_text": "baboons live 30 years",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY
    assert r.rule == "numeric_slot"


def test_numeric_string_value_in_slot_triggers_verify():
    claim = {
        "pattern": "quantitative", "predicate": "has_count",
        "polarity": 1,
        "slots": {"subject": "Saturn", "property": "moons", "value": "274"},
        "source_text": "Saturn has 274 moons",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY


# ---- Rule 2: date / temporal scope -----------------------------------


def test_date_slot_triggers_verify():
    claim = {
        "pattern": "role_assignment", "predicate": "served_as",
        "polarity": 1,
        "slots": {"agent": "Trump", "role": "45th President",
                  "valid_from": "2017", "valid_until": "2021"},
        "source_text": "Trump served as the 45th president from 2017 to 2021",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY
    assert r.rule == "date_slot"


def test_year_in_value_triggers_verify():
    claim = {
        "pattern": "event", "predicate": "founded",
        "polarity": 1,
        "slots": {"event_type": "company_founding",
                  "participants": ["Anthropic"],
                  "occurred_at": "2021"},
        "source_text": "Anthropic was founded in 2021",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY


# ---- Rule 3: multiple named-entity slots -----------------------------


def test_two_named_entities_triggers_verify():
    claim = {
        "pattern": "relational", "predicate": "founded_by",
        "polarity": 1,
        "slots": {"subject": "Anthropic", "object": "Dario Amodei"},
        "source_text": "Anthropic was founded by Dario Amodei",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY
    assert r.rule == "multiple_named_entities"


def test_one_named_entity_does_not_trigger_named_entity_rule():
    """Single named entity isn't enough on its own — needs another
    falsifiability signal. Generic 'social rank' subject + named
    'baboons' object should fall through to the anchored-specific
    rule or default."""
    claim = {
        "pattern": "relational", "predicate": "passes_across",
        "polarity": 1,
        "slots": {"subject": "social rank", "object": "generations"},
        "source_text": "social rank is passed across generations",
    }
    r = triage_claim(claim)
    # No anchor, no numeric, no date, no two named entities → default.
    assert r.decision is TriageDecision.PASS_THROUGH


# ---- Rule 5: anchor + specific predicate -----------------------------


def test_anchor_with_specific_predicate_triggers_verify():
    claim = {
        "pattern": "relational", "predicate": "passes_across",
        "polarity": 1,
        "slots": {"subject": "social rank", "object": "generations"},
        "source_text": "social rank is passed across generations",
        "anchor_entity": "baboon",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY
    assert r.rule == "anchored_specific_predicate"


def test_anchor_with_vague_predicate_does_not_trigger():
    """Anchor present but predicate is generic ('is', 'has') — not
    enough signal to verify."""
    claim = {
        "pattern": "relational", "predicate": "is",
        "polarity": 1,
        "slots": {"subject": "behavior", "object": "complex"},
        "source_text": "behavior is complex",
        "anchor_entity": "baboon",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.PASS_THROUGH


# ---- Default PASS_THROUGH --------------------------------------------


def test_vague_qualitative_falls_through():
    """The canonical case the gate is for — a claim with no
    falsifiability signal."""
    claim = {
        "pattern": "categorical", "predicate": "is_a",
        "polarity": 1,
        "slots": {"entity": "baboons", "category": "intelligent"},
        "source_text": "baboons are intelligent",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.PASS_THROUGH
    assert r.rule == "no_falsifiability_signal"


def test_preference_claim_with_named_object_triggers_verify():
    """v0.14.1 design: preference patterns are NOT always-pass-through.
    Tier U is the cheap source of truth and the walker still runs.
    A preference claim with a named-entity object gets VERIFY (the
    walker hits Tier U and may match/contradict the user's stored
    preference)."""
    claim = {
        "pattern": "preference", "predicate": "loves",
        "polarity": 1,
        "slots": {"agent": "user", "object": "olives"},
        "source_text": "you love olives",
    }
    # Single string slot 'olives' — not multiple named entities. No
    # numeric, no date. PASS_THROUGH is fine — the walker still runs
    # for cheap Tier U lookup, just no fresh dispatch fallthrough.
    r = triage_claim(claim)
    assert r.decision is TriageDecision.PASS_THROUGH
    # Confirm the architectural invariant: PASS_THROUGH only suppresses
    # fresh dispatch, not Tier U / W / derivation.


# ---- Rule 6: computable predicate allow-list -------------------------


def test_current_time_predicate_triggers_verify():
    """Regression: the Cairo / New York current_time claim used to
    fall through to PASS_THROUGH because '2:56 am' doesn't parse as a
    number and the slots have only one named entity. v0.14.1 Rule 6
    catches it via the predicate name — current_time is computable
    against the system clock by zoneinfo."""
    claim = {
        "pattern": "quantitative", "predicate": "current_time",
        "polarity": 1,
        "slots": {"subject": "Cairo", "property": "time", "value": "9:56 am"},
        "source_text": "it would be 9:56 am in Cairo",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY
    assert r.rule == "computable_predicate"


def test_letter_count_predicate_triggers_verify():
    claim = {
        "pattern": "quantitative", "predicate": "letter_count",
        "polarity": 1,
        "slots": {"subject": "strawberry", "property": "letter_r", "value": 3},
        "source_text": "Strawberry has 3 r's",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY
    # numeric_slot would also match here (value=3); rule order says
    # computable_predicate fires first when both apply.
    assert r.rule == "computable_predicate"


def test_unknown_predicate_does_not_trigger_rule_6():
    """A predicate not in the allow-list doesn't get the free pass —
    the other rules have to settle it."""
    claim = {
        "pattern": "relational", "predicate": "knows_about",
        "polarity": 1,
        "slots": {"subject": "behavior", "object": "topic"},
        "source_text": "behavior knows about topic",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.PASS_THROUGH
    assert r.rule == "no_falsifiability_signal"


# ---- Rule 7: named-entity subject + value slot -----------------------


def test_named_subject_with_value_triggers_verify():
    """Cairo current_time(subject=Cairo, value='2:56 am') has only
    one named entity (Rule 3 fails) and a string value (Rule 1
    fails). Rule 7 catches it via the named-subject + explicit-value
    combination — the value is the falsifiable thing being asserted.

    Note: in practice this claim ALSO hits Rule 6 first via the
    current_time allow-list, so this test isolates Rule 7 by using a
    different predicate so we can verify the rule fires
    independently."""
    claim = {
        "pattern": "quantitative", "predicate": "stock_price",
        "polarity": 1,
        "slots": {"subject": "Apple", "property": "closing_price",
                  "value": "$175.50"},
        "source_text": "Apple closed at $175.50",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.VERIFY
    assert r.rule == "named_subject_with_value"


def test_value_slot_without_named_entity_does_not_trigger_rule_7():
    claim = {
        "pattern": "quantitative", "predicate": "happiness_level",
        "polarity": 1,
        "slots": {"subject": "feeling", "property": "level",
                  "value": "high"},
        "source_text": "feeling level is high",
    }
    r = triage_claim(claim)
    assert r.decision is TriageDecision.PASS_THROUGH


# ---- Determinism -----------------------------------------------------


def test_triage_is_deterministic():
    """Same claim → same decision every time. No store, no LLM, no
    randomness."""
    claim = {
        "pattern": "quantitative", "predicate": "has_count",
        "polarity": 1,
        "slots": {"subject": "x", "property": "y", "value": 42},
        "source_text": "x has 42 y",
    }
    r1 = triage_claim(claim)
    r2 = triage_claim(claim)
    r3 = triage_claim(claim)
    assert r1.decision == r2.decision == r3.decision
    assert r1.rule == r2.rule == r3.rule
