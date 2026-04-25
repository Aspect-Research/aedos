"""Tests for src.extractor.

LLM calls are mocked by default. A single end-to-end test is gated behind
RUN_API_TESTS=1 for occasional real-API sanity checks.
"""

from __future__ import annotations

import pytest
pytestmark = pytest.mark.skip(
    reason="v0.3 migration: extractor rewritten in Section 3; tests rewritten there"
)

import os
from dataclasses import dataclass
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
    """Stand-in for LLMClient — records args and returns a canned tool input."""

    return_value: dict[str, Any]
    calls: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    def extract_with_tool(self, system, user_message, tool, max_tokens=2048):
        self.calls.append(
            {"system": system, "user_message": user_message, "tool": tool}
        )
        return self.return_value


def _mk_extractor(return_value):
    return ClaimExtractor(FakeLLM(return_value=return_value), load_default_registry())


def test_valid_user_fact_passes_through():
    llm_out = {
        "claims": [
            {
                "subject": "user",
                "predicate": "likes",
                "object": "peanut butter",
                "object_type": "entity",
                "polarity": 1,
                "source_text": "I like peanut butter",
            }
        ]
    }
    result = _mk_extractor(llm_out).extract("I like peanut butter", role="user")
    assert len(result.valid_claims) == 1
    assert result.rejected_claims == []
    assert result.valid_claims[0]["predicate"] == "likes"


def test_unknown_predicate_dropped():
    llm_out = {
        "claims": [
            {
                "subject": "user",
                "predicate": "teleports_to",  # not in registry
                "object": "Mars",
                "object_type": "entity",
                "polarity": 1,
                "source_text": "I teleport to Mars",
            }
        ]
    }
    result = _mk_extractor(llm_out).extract("I teleport to Mars", role="user")
    assert result.valid_claims == []
    assert len(result.rejected_claims) == 1
    assert "not in registry" in result.rejected_claims[0]["reason"]


def test_object_type_mismatch_dropped():
    llm_out = {
        "claims": [
            {
                "subject": "user",
                "predicate": "likes",
                "object": "42",
                "object_type": "int",  # likes expects 'entity'
                "polarity": 1,
                "source_text": "x",
            }
        ]
    }
    result = _mk_extractor(llm_out).extract("x", role="user")
    assert result.valid_claims == []
    assert len(result.rejected_claims) == 1
    assert "object_type" in result.rejected_claims[0]["reason"]


def test_missing_field_dropped():
    llm_out = {
        "claims": [
            {
                "subject": "user",
                "predicate": "likes",
                "object": "pb",
                # missing object_type, polarity, source_text
            }
        ]
    }
    result = _mk_extractor(llm_out).extract("x", role="user")
    assert result.valid_claims == []
    assert "missing fields" in result.rejected_claims[0]["reason"]


def test_empty_claims_list_is_fine():
    result = _mk_extractor({"claims": []}).extract("Hi!", role="user")
    assert result.valid_claims == []
    assert result.rejected_claims == []


def test_non_list_claims_returns_empty():
    result = _mk_extractor({"claims": "not a list"}).extract("x", role="user")
    assert result.valid_claims == []


def test_malformed_claim_entry_rejected():
    result = _mk_extractor({"claims": [42, "string"]}).extract("x", role="user")
    assert result.valid_claims == []
    assert len(result.rejected_claims) == 2


def test_polarity_is_coerced_to_int():
    llm_out = {
        "claims": [
            {
                "subject": "user",
                "predicate": "likes",
                "object": "pb",
                "object_type": "entity",
                "polarity": "1",  # stringly-typed
                "source_text": "x",
            }
        ]
    }
    result = _mk_extractor(llm_out).extract("x", role="user")
    assert result.valid_claims[0]["polarity"] == 1


def test_polarity_out_of_range_rejected():
    llm_out = {
        "claims": [
            {
                "subject": "user",
                "predicate": "likes",
                "object": "pb",
                "object_type": "entity",
                "polarity": 2,
                "source_text": "x",
            }
        ]
    }
    result = _mk_extractor(llm_out).extract("x", role="user")
    assert result.valid_claims == []
    assert "polarity must be 0 or 1" in result.rejected_claims[0]["reason"]


def test_multiple_mixed_claims_split_valid_and_rejected():
    llm_out = {
        "claims": [
            {
                "subject": "user",
                "predicate": "likes",
                "object": "pb",
                "object_type": "entity",
                "polarity": 1,
                "source_text": "a",
            },
            {
                "subject": "user",
                "predicate": "teleports",  # unknown
                "object": "Mars",
                "object_type": "entity",
                "polarity": 1,
                "source_text": "b",
            },
            {
                "subject": "strawberry",
                "predicate": "has_count",
                "object": '{"item": "p", "count": 3}',
                "object_type": "count",
                "polarity": 1,
                "source_text": "c",
            },
        ]
    }
    result = _mk_extractor(llm_out).extract("x", role="assistant")
    assert len(result.valid_claims) == 2
    assert len(result.rejected_claims) == 1


def test_role_validation():
    extractor = _mk_extractor({"claims": []})
    with pytest.raises(ValueError, match="role"):
        extractor.extract("x", role="system")


def test_tool_schema_enumerates_object_types():
    """The tool schema is what the LLM sees; it must list every object_type."""
    from src.extractor import RECORD_CLAIMS_TOOL
    from src.pattern_registry import OBJECT_TYPES

    enum = RECORD_CLAIMS_TOOL["input_schema"]["properties"]["claims"]["items"][
        "properties"
    ]["object_type"]["enum"]
    assert set(enum) == OBJECT_TYPES


def test_system_prompt_includes_every_predicate():
    extractor = _mk_extractor({"claims": []})
    for name in extractor.registry.names():
        assert name in extractor._system_prompt


# ---------- abstention behavior (Section 1) ----------


def test_extractor_abstains_on_out_of_vocabulary_claim():
    """Photosynthesis sentence must extract zero claims when the LLM abstains."""
    extractor = _mk_extractor({"claims": []})
    result = extractor.extract(
        "Photosynthesis converts sunlight into chemical energy.", role="user"
    )
    assert result.valid_claims == []
    assert result.rejected_claims == []


def test_marie_curie_routes_to_is_a_not_believes():
    """Section 2 discrimination: profession noun → is_a."""
    extractor = _mk_extractor(
        {
            "claims": [
                {
                    "subject": "Marie Curie",
                    "predicate": "is_a",
                    "object": "physicist",
                    "object_type": "string",
                    "polarity": 1,
                    "source_text": "Marie Curie was a physicist",
                }
            ]
        }
    )
    result = extractor.extract("Marie Curie was a physicist.", role="user")
    assert len(result.valid_claims) == 1
    assert result.valid_claims[0]["predicate"] == "is_a"


def test_donald_trump_routes_to_holds_role_not_is_a_or_believes():
    """Section 2 discrimination: named role → holds_role, never is_a/believes."""
    extractor = _mk_extractor(
        {
            "claims": [
                {
                    "subject": "Donald Trump",
                    "predicate": "holds_role",
                    "object": "US President",
                    "object_type": "string",
                    "polarity": 1,
                    "source_text": "Donald Trump is the US President",
                }
            ]
        }
    )
    result = extractor.extract("Donald Trump is the US President.", role="user")
    assert len(result.valid_claims) == 1
    c = result.valid_claims[0]
    assert c["predicate"] == "holds_role"
    assert c["predicate"] not in ("is_a", "believes")


def test_copula_sentence_does_not_route_to_believes():
    """A copula statement with a specific predicate must use that predicate, not believes.

    The point of the new abstention prompt is that `believes` is reserved
    for explicit user beliefs. A factual copula like "Paris is the capital
    of France" should route to `capital_of`, never `believes`, even when
    no first-person speaker is stating it.
    """
    extractor = _mk_extractor(
        {
            "claims": [
                {
                    "subject": "Paris",
                    "predicate": "capital_of",
                    "object": "France",
                    "object_type": "entity",
                    "polarity": 1,
                    "source_text": "Paris is the capital of France",
                }
            ]
        }
    )
    result = extractor.extract("Paris is the capital of France.", role="user")
    assert len(result.valid_claims) == 1
    c = result.valid_claims[0]
    assert c["predicate"] == "capital_of"
    assert c["predicate"] != "believes"


def test_abstention_prompt_includes_explicit_guidance():
    """The system prompt must contain the abstention guidance and few-shot examples."""
    extractor = _mk_extractor({"claims": []})
    sys = extractor._system_prompt
    assert "abstention is the default" in sys.lower()
    assert "believes" in sys
    # Use a substring that doesn't cross a paragraph break.
    assert "preferred over forcing a poor fit" in sys
    # Reserved-for-user-belief guidance.
    assert "reserved for explicit user beliefs" in sys
    # Few-shot examples must include abstention + the believes-vs-fact discriminator.
    assert "Photosynthesis" in sys
    assert "Fed will cut rates" in sys
    assert "Paris is the capital of France" in sys


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1", reason="real API test gated behind RUN_API_TESTS=1"
)
def test_real_api_roundtrip_user_likes():
    """Hit the real API once to sanity-check the full extractor path."""
    from src.llm_client import LLMClient

    llm = LLMClient()
    extractor = ClaimExtractor(llm, load_default_registry())
    result = extractor.extract("I like peanut butter.", role="user")
    assert any(
        c["predicate"] == "likes" and "peanut butter" in c["object"].lower()
        for c in result.valid_claims
    ), result.to_dict()
