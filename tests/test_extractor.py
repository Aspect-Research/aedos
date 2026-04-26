"""Tests for src.extractor (v0.3 — pattern-based)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.extractor import ClaimExtractor, ExtractionResult
from src.pattern_registry import load_default_registry, reset_cache


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_cache()
    yield
    reset_cache()


@dataclass
class FakeLLM:
    return_value: dict[str, Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def extract_with_tool(self, system, user_message, tool, max_tokens=2048):
        self.calls.append(
            {"system": system, "user_message": user_message, "tool": tool}
        )
        return self.return_value


def _mk(return_value):
    return ClaimExtractor(FakeLLM(return_value=return_value), load_default_registry())


# ---------- abstention ----------


def test_empty_facts_list_is_fine():
    result = _mk({"facts": []}).extract("Hello!", role="user")
    assert result.valid_facts == []
    assert result.rejected_facts == []


def test_photosynthesis_abstention():
    """Spec scenario A: out-of-vocabulary, must abstain."""
    result = _mk({"facts": []}).extract(
        "Photosynthesis converts sunlight into chemical energy.", role="user"
    )
    assert result.valid_facts == []


def test_aesthetic_judgment_abstention():
    """The sunset was beautiful — no pattern fits."""
    result = _mk({"facts": []}).extract("The sunset was beautiful.", role="user")
    assert result.valid_facts == []


# ---------- per-pattern happy paths ----------


def test_categorical_extraction():
    payload = {
        "facts": [
            {
                "pattern": "categorical",
                "predicate": "is_a",
                "slots": {"entity": "Marie Curie", "category": "physicist"},
                "polarity": 1,
                "source_text": "Marie Curie was a physicist",
            }
        ]
    }
    result = _mk(payload).extract("Marie Curie was a physicist.", role="user")
    assert len(result.valid_facts) == 1
    f = result.valid_facts[0]
    assert f["pattern"] == "categorical"
    assert f["predicate"] == "is_a"
    assert f["slots"]["category"] == "physicist"


def test_role_assignment_with_temporal_scope():
    """The Trump-trace fix: extractor must populate valid_from / valid_until."""
    payload = {
        "facts": [
            {
                "pattern": "role_assignment",
                "predicate": "served_as",
                "slots": {
                    "agent": "Donald Trump",
                    "role": "45th President",
                    "org": "United States",
                    "valid_from": "2017-01-20",
                    "valid_until": "2021-01-20",
                },
                "polarity": 1,
                "source_text": "Trump served as the 45th president from 2017 to 2021",
            }
        ]
    }
    result = _mk(payload).extract(
        "Trump served as the 45th president from 2017 to 2021.", role="user"
    )
    assert len(result.valid_facts) == 1
    slots = result.valid_facts[0]["slots"]
    assert slots["valid_from"] == "2017-01-20"
    assert slots["valid_until"] == "2021-01-20"


def test_role_assignment_currently_held_omits_valid_until():
    payload = {
        "facts": [
            {
                "pattern": "role_assignment",
                "predicate": "holds_role",
                "slots": {"agent": "Donald Trump", "role": "47th President"},
                "polarity": 1,
                "source_text": "Donald Trump is the 47th President",
            }
        ]
    }
    result = _mk(payload).extract("Donald Trump is the 47th President.", role="user")
    assert len(result.valid_facts) == 1
    assert "valid_until" not in result.valid_facts[0]["slots"]


def test_relational_election_outcome_not_succession():
    """Trump-defeated-Harris is relational, not succeeded_by."""
    payload = {
        "facts": [
            {
                "pattern": "relational",
                "predicate": "defeated_in_election",
                "slots": {
                    "subject": "Donald Trump",
                    "relation": "defeated_in_election",
                    "object": "Kamala Harris",
                    "valid_from": "2024",
                },
                "polarity": 1,
                "source_text": "Trump defeated Kamala Harris in the 2024 election",
            }
        ]
    }
    result = _mk(payload).extract(
        "Trump defeated Kamala Harris in the 2024 election.", role="user"
    )
    f = result.valid_facts[0]
    assert f["predicate"] == "defeated_in_election"
    assert f["predicate"] != "succeeded_by"


def test_propositional_attitude_user_belief():
    payload = {
        "facts": [
            {
                "pattern": "propositional_attitude",
                "predicate": "believes",
                "slots": {
                    "agent": "user",
                    "attitude": "thinks",
                    "proposition": "Fed will cut rates",
                },
                "polarity": 1,
                "source_text": "I think the Fed will cut rates",
            }
        ]
    }
    result = _mk(payload).extract("I think the Fed will cut rates.", role="user")
    f = result.valid_facts[0]
    assert f["pattern"] == "propositional_attitude"
    assert f["slots"]["agent"] == "user"


def test_preference_user_love():
    payload = {
        "facts": [
            {
                "pattern": "preference",
                "predicate": "loves",
                "slots": {"agent": "user", "object": "peanut butter"},
                "polarity": 1,
                "source_text": "I love peanut butter",
            }
        ]
    }
    result = _mk(payload).extract("I love peanut butter.", role="user")
    assert result.valid_facts[0]["pattern"] == "preference"
    assert result.valid_facts[0]["predicate"] == "loves"


def test_quantitative_count():
    payload = {
        "facts": [
            {
                "pattern": "quantitative",
                "predicate": "has_count",
                "slots": {"subject": "strawberry", "property": "letter_p", "value": 2},
                "polarity": 1,
                "source_text": "Strawberry has 2 p's",
            }
        ]
    }
    result = _mk(payload).extract("Strawberry has 2 p's.", role="user")
    assert result.valid_facts[0]["pattern"] == "quantitative"


def test_spatial_temporal_user():
    payload = {
        "facts": [
            {
                "pattern": "spatial_temporal",
                "predicate": "lives_in",
                "slots": {
                    "entity": "user",
                    "location": "Williamstown",
                    "relation_kind": "residence",
                },
                "polarity": 1,
                "source_text": "I live in Williamstown",
            }
        ]
    }
    result = _mk(payload).extract("I live in Williamstown.", role="user")
    assert result.valid_facts[0]["pattern"] == "spatial_temporal"
    assert result.valid_facts[0]["slots"]["entity"] == "user"


def test_event_pattern():
    payload = {
        "facts": [
            {
                "pattern": "event",
                "predicate": "was_inaugurated",
                "slots": {
                    "event_type": "inauguration",
                    "participants": ["Donald Trump"],
                    "occurred_at": "2025-01-20",
                },
                "polarity": 1,
                "source_text": "Trump was inaugurated on January 20, 2025",
            }
        ]
    }
    result = _mk(payload).extract(
        "Trump was inaugurated on January 20, 2025.", role="user"
    )
    assert result.valid_facts[0]["pattern"] == "event"


# ---------- multi-pattern from one sentence ----------


def test_one_sentence_two_facts_two_patterns():
    """Section 9 #3: 'Tokyo is a city in Japan' → categorical + spatial_temporal."""
    payload = {
        "facts": [
            {
                "pattern": "categorical",
                "predicate": "is_a",
                "slots": {"entity": "Tokyo", "category": "city"},
                "polarity": 1,
                "source_text": "Tokyo is a city",
            },
            {
                "pattern": "spatial_temporal",
                "predicate": "located_in",
                "slots": {
                    "entity": "Tokyo",
                    "location": "Japan",
                    "relation_kind": "containment",
                },
                "polarity": 1,
                "source_text": "Tokyo is a city in Japan",
            },
        ]
    }
    result = _mk(payload).extract("Tokyo is a city in Japan.", role="user")
    assert len(result.valid_facts) == 2
    patterns = sorted(f["pattern"] for f in result.valid_facts)
    assert patterns == ["categorical", "spatial_temporal"]


# ---------- free-form predicates within a pattern ----------


def test_freeform_predicate_within_preference_accepted():
    """is_obsessed_with isn't in example_predicates but is valid within preference."""
    payload = {
        "facts": [
            {
                "pattern": "preference",
                "predicate": "is_obsessed_with",
                "slots": {"agent": "user", "object": "sourdough"},
                "polarity": 1,
                "source_text": "I'm obsessed with sourdough",
            }
        ]
    }
    result = _mk(payload).extract("I'm obsessed with sourdough.", role="user")
    assert len(result.valid_facts) == 1
    assert result.valid_facts[0]["predicate"] == "is_obsessed_with"


# ---------- validation rejection paths ----------


def test_unknown_pattern_rejected():
    payload = {
        "facts": [
            {
                "pattern": "telepathy",
                "predicate": "x",
                "slots": {},
                "polarity": 1,
                "source_text": "...",
            }
        ]
    }
    result = _mk(payload).extract("...", role="user")
    assert result.valid_facts == []
    assert "unknown pattern" in result.rejected_facts[0]["reason"]


def test_missing_required_slot_rejected():
    payload = {
        "facts": [
            {
                "pattern": "categorical",
                "predicate": "is_a",
                "slots": {"entity": "Marie Curie"},  # missing 'category'
                "polarity": 1,
                "source_text": "...",
            }
        ]
    }
    result = _mk(payload).extract("...", role="user")
    assert result.valid_facts == []
    assert "missing required slots" in result.rejected_facts[0]["reason"]


def test_missing_top_level_field_rejected():
    payload = {
        "facts": [
            {"pattern": "preference", "predicate": "likes", "polarity": 1}
            # missing slots, source_text
        ]
    }
    result = _mk(payload).extract("...", role="user")
    assert result.valid_facts == []
    assert "missing field" in result.rejected_facts[0]["reason"]


def test_polarity_out_of_range_rejected():
    payload = {
        "facts": [
            {
                "pattern": "preference",
                "predicate": "likes",
                "slots": {"agent": "user", "object": "x"},
                "polarity": 2,
                "source_text": "x",
            }
        ]
    }
    result = _mk(payload).extract("...", role="user")
    assert result.valid_facts == []
    assert "polarity must be 0 or 1" in result.rejected_facts[0]["reason"]


def test_role_validation():
    extractor = _mk({"facts": []})
    with pytest.raises(ValueError, match="role"):
        extractor.extract("x", role="system")


def test_tool_schema_lists_all_pattern_names():
    from src.pattern_registry import load_default_registry as _load

    reg = _load()
    extractor = _mk({"facts": []})
    enum = extractor._record_tool["input_schema"]["properties"]["facts"]["items"][
        "properties"
    ]["pattern"]["enum"]
    assert set(enum) == set(reg.names())


def test_system_prompt_includes_every_pattern():
    extractor = _mk({"facts": []})
    sys = extractor._system_prompt
    for name in extractor.registry.names():
        assert name in sys, f"pattern {name!r} missing from prompt"


def test_system_prompt_includes_temporal_few_shot():
    """The Trump-trace fix: prompt must show the example with valid_from/valid_until."""
    extractor = _mk({"facts": []})
    sys = extractor._system_prompt
    assert "valid_from" in sys
    assert "valid_until" in sys
    assert "2017" in sys


# ---------- context for self-reference resolution (v0.4 bugfix) ----------


def test_context_passed_in_user_message_when_provided():
    """When extracting from an assistant draft, the preceding user message
    is bundled in as context so 'this sentence' can resolve to literal text.
    """
    payload = {
        "facts": [
            {
                "pattern": "quantitative",
                "predicate": "has_count",
                "slots": {
                    "subject": "How many words in 'the quick brown fox' contain 'o'?",
                    "property": "words_containing_letter_o",
                    "value": 2,
                },
                "polarity": 1,
                "source_text": "Two words contain 'o'.",
            }
        ]
    }
    extractor = _mk(payload)
    extractor.extract(
        "Two words contain 'o'.",
        role="assistant",
        context="How many words in 'the quick brown fox' contain 'o'?",
    )
    msg = extractor.llm.calls[0]["user_message"]
    assert "Preceding speaker's message" in msg
    assert "the quick brown fox" in msg
    # The instruction to resolve self-references must be present.
    assert "this sentence" in msg or "self-references" in msg


def test_no_context_when_omitted():
    """Backward compatibility: extract(text, role) still works."""
    extractor = _mk({"facts": []})
    extractor.extract("Hello.", role="user")
    msg = extractor.llm.calls[0]["user_message"]
    assert "Preceding speaker's message" not in msg


def test_self_referential_count_few_shot_in_prompt():
    """The extractor's system prompt teaches resolving 'this sentence'."""
    extractor = _mk({"facts": []})
    sys = extractor._system_prompt
    # A few-shot example with literal-sentence-as-subject must appear.
    assert "this sentence" in sys
    assert "words_containing_letter" in sys


def test_hedged_count_few_shot_in_prompt():
    """Prompt teaches extraction of conditional/hedged count claims.

    Regression for: assistant says 'N if X, else M' and extractor returns []
    instead of extracting the primary value N.
    """
    extractor = _mk({"facts": []})
    sys = extractor._system_prompt
    # The hedged example must appear so the LLM learns to handle it.
    assert "PRIMARY" in sys or "primary" in sys
    assert "If counting all instances" in sys or "interpretation" in sys
    assert "three free trees" in sys


def test_context_block_does_not_use_alarming_negation():
    """The 'do NOT extract' phrasing was over-discouraging extraction in
    edge cases. The new phrasing is positive: 'extract from speaker's text'.
    """
    extractor = _mk({"facts": []})
    extractor.extract("Two words.", role="assistant", context="anything")
    msg = extractor.llm.calls[0]["user_message"]
    assert "do NOT extract" not in msg
    # Positive instruction is present.
    assert "Extract every fact-stating clause" in msg


def test_context_user_message_mentions_hedged_extraction_rule():
    """The per-call instructions mention hedged-claim handling."""
    extractor = _mk({"facts": []})
    extractor.extract("Two.", role="assistant", context="ctx")
    msg = extractor.llm.calls[0]["user_message"]
    assert "hedged" in msg or "conditional" in msg
    assert "PRIMARY" in msg or "primary" in msg


# ---------- real API gated test ----------


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="real API test gated behind RUN_API_TESTS=1",
)
def test_real_api_roundtrip_user_likes():
    from src.llm_client import LLMClient

    llm = LLMClient()
    extractor = ClaimExtractor(llm, load_default_registry())
    result = extractor.extract("I like peanut butter.", role="user")
    assert any(
        f["pattern"] == "preference" and "peanut butter" in str(f["slots"]).lower()
        for f in result.valid_facts
    ), result.to_dict()
