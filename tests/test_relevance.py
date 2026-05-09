"""Tests for src.layer4_lookup.relevance — the substrate consultation
gate (v0.14.4).

Pure-function tests for tokenization + active-context computation +
relevance check. The integration tests (gate actually skipping the
oracle in tier_u / tier_w) live in test_relevance_gate_integration.py.
"""

from __future__ import annotations

from src.layer4_lookup.relevance import (
    candidate_tokens,
    compute_active_context,
    is_candidate_relevant,
    tokenize,
)


# ---- tokenize ---------------------------------------------------------


def test_tokenize_lowercases_and_drops_short_tokens():
    assert tokenize("Hello A B World") == ["hello", "world"]


def test_tokenize_drops_stopwords():
    assert tokenize("the cat is on the mat") == ["cat", "mat"]


def test_tokenize_handles_punctuation_and_apostrophes():
    # Apostrophes split tokens — "don't" → ["don", "t"], "t" is < 2
    # chars, dropped.
    assert tokenize("Don't worry — it's all good") == ["don", "worry", "all", "good"]


def test_tokenize_keeps_alphanumeric():
    # Numbers survive tokenization — useful for year/value matching.
    assert tokenize("born 1867 in Warsaw") == ["born", "1867", "warsaw"]


def test_tokenize_empty_or_non_string():
    assert tokenize("") == []
    assert tokenize(None) == []
    assert tokenize(42) == []


def test_tokenize_keeps_comparative_words_that_nltk_drops():
    """Tight stopword set: 'more' / 'most' / 'few' / 'many' are
    semantically meaningful for comparative claims; keep them."""
    assert "more" in tokenize("more than a few people")
    assert "most" in tokenize("most populous city")
    assert "few" in tokenize("few options remain")
    assert "many" in tokenize("many years ago")


# ---- compute_active_context ------------------------------------------


def test_active_context_includes_slot_values():
    claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Cairo", "location": "Egypt", "relation_kind": "containment"},
    }
    tokens = compute_active_context(claim)
    assert "cairo" in tokens
    assert "egypt" in tokens
    assert "containment" in tokens


def test_active_context_includes_source_text():
    claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Cairo"},
        "source_text": "Cairo is in Egypt Standard Time UTC+2",
    }
    tokens = compute_active_context(claim)
    assert "cairo" in tokens
    assert "egypt" in tokens
    assert "standard" in tokens
    assert "utc" in tokens


def test_active_context_includes_anchor():
    claim = {
        "pattern": "relational", "predicate": "passes_across",
        "slots": {"subject": "social rank", "object": "generations"},
        "anchor_entity": "baboon",
    }
    tokens = compute_active_context(claim)
    assert "baboon" in tokens
    assert "social" in tokens


def test_active_context_includes_user_message():
    claim = {
        "pattern": "categorical", "predicate": "is_a",
        "slots": {"entity": "lizard", "category": "reptile"},
        "source_text": "lizards are reptiles",
    }
    tokens = compute_active_context(claim, current_user_message="What is a lizard?")
    assert "lizard" in tokens
    assert "reptile" in tokens
    # The user message contributes "lizard" too (already there) but
    # also signals the question framing.


def test_active_context_handles_list_slots():
    """event.participants is a list of strings — tokens should be
    flattened across the list."""
    claim = {
        "pattern": "event", "predicate": "founded",
        "slots": {
            "event_type": "company_founding",
            "participants": ["Dario Amodei", "Daniela Amodei", "Anthropic"],
        },
    }
    tokens = compute_active_context(claim)
    assert "dario" in tokens
    assert "amodei" in tokens
    assert "anthropic" in tokens


def test_active_context_skips_non_string_slot_values():
    """Numeric slot values (value=3) don't contribute string tokens."""
    claim = {
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"subject": "strawberry", "property": "letter_r", "value": 3},
        "source_text": "strawberry has 3 r's",
    }
    tokens = compute_active_context(claim)
    assert "strawberry" in tokens
    assert "letter" in tokens
    # "3" survives because we DO accept alphanumeric tokens of length
    # 1+; but length-1 tokens drop out, so "3" is excluded.
    assert "3" not in tokens


def test_active_context_empty_for_blank_claim():
    assert compute_active_context({}) == frozenset()


# ---- candidate_tokens -------------------------------------------------


def test_candidate_tokens_collects_string_slots():
    tokens = candidate_tokens(
        ["Cairo", "Egypt", "containment"], source_text="Cairo time is 9am",
    )
    assert "cairo" in tokens
    assert "egypt" in tokens
    assert "time" in tokens


def test_candidate_tokens_handles_lists():
    tokens = candidate_tokens(
        [["Dario Amodei", "Anthropic"], "company_founding"],
    )
    assert "dario" in tokens
    assert "amodei" in tokens
    assert "anthropic" in tokens


# ---- is_candidate_relevant -------------------------------------------


def test_relevant_when_tokens_overlap():
    active = frozenset({"cairo", "egypt", "time"})
    candidate = frozenset({"cairo", "lifespan"})
    assert is_candidate_relevant(active, candidate) is True


def test_not_relevant_when_no_overlap():
    """Cairo↔lizard regression: zero token overlap → skip."""
    active = frozenset({"lizard", "reptile", "scaly"})
    candidate = frozenset({"cairo", "egypt", "time"})
    assert is_candidate_relevant(active, candidate) is False


def test_back_compat_none_active_returns_true():
    """active_tokens=None → no gating (back-compat for callers that
    don't supply context)."""
    candidate = frozenset({"cairo"})
    assert is_candidate_relevant(None, candidate) is True


def test_back_compat_empty_active_returns_true():
    """active_tokens=frozenset() → no gating (defensive)."""
    candidate = frozenset({"cairo"})
    assert is_candidate_relevant(frozenset(), candidate) is True


def test_empty_candidate_returns_true():
    """A candidate we can't tokenize (numeric-only slot values, no
    source_text) gets pass-through — don't filter what we can't
    analyze."""
    active = frozenset({"strawberry", "letter"})
    assert is_candidate_relevant(active, frozenset()) is True


# ---- end-to-end Cairo / lizard / NYC scenarios -----------------------


def test_cairo_vs_lizard_filtered_out():
    """The motivating regression: a Cairo turn-1 claim cached in
    Tier W is irrelevant to a turn-2 lizard claim. Active context
    for the lizard turn shares no tokens with the Cairo candidate."""
    cairo_claim = {
        "pattern": "spatial_temporal", "predicate": "located_in",
        "slots": {"entity": "Cairo", "location": "Egypt"},
        "source_text": "Cairo is in Egypt",
    }
    lizard_claim = {
        "pattern": "categorical", "predicate": "is_a",
        "slots": {"entity": "lizard", "category": "reptile"},
        "source_text": "lizards are reptiles",
    }
    active = compute_active_context(
        lizard_claim, current_user_message="What is a lizard?",
    )
    candidate = candidate_tokens(
        list(cairo_claim["slots"].values()),
        source_text=cairo_claim["source_text"],
    )
    assert is_candidate_relevant(active, candidate) is False


def test_nyc_vs_new_york_city_passes():
    """The cross-turn alias case: user said 'I live in NYC' five
    turns ago; assistant now says 'you live in New York City'. The
    claim's slot values + source_text + the user message containing
    'NYC' should overlap enough to KEEP the candidate alive for
    entity_equivalence consultation."""
    stored_user_fact_slots = ["user", "NYC"]
    stored_source = "I live in NYC"
    new_claim = {
        "pattern": "spatial_temporal", "predicate": "lives_in",
        "slots": {"entity": "user", "location": "New York City"},
        "source_text": "you live in New York City",
    }
    active = compute_active_context(
        new_claim, current_user_message="Where did I say I live again?",
    )
    candidate = candidate_tokens(stored_user_fact_slots, source_text=stored_source)
    # Shared tokens: "user", "live" — non-empty intersection.
    assert is_candidate_relevant(active, candidate) is True
