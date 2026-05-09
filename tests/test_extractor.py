"""Tests for src.layer1_extraction.extractor (v0.14).

Ports v1's test_extractor.py mocked-LLM tests. Pattern-by-pattern
behaviour is identical for the eight legacy patterns (no member_of
references in the v1 corpus, so no rewrites). The mereological pattern
gets its own dedicated test file (test_extractor_mereological.py).

The pipeline-integration tests at the bottom of v1's
test_extractor_substitution_check.py are NOT ported here — they
depend on Pipeline / Router / Corrector / LLMRouter, none of which
exist in v2 yet. They'll come back when their dependencies port
(Phase 2+).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.layer1_extraction.extractor import (
    ClaimExtractor,
    ExtractionResult,
)
from src.layer1_extraction.pattern_registry import (
    load_default_registry,
    reset_cache,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_cache()
    yield
    reset_cache()


@dataclass
class FakeLLM:
    return_value: dict[str, Any]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def extract_with_tool(self, system, user_message, tool, max_tokens=2048, **_kwargs):
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
    result = _mk({"facts": []}).extract(
        "Photosynthesis converts sunlight into chemical energy.", role="user"
    )
    assert result.valid_facts == []


def test_aesthetic_judgment_abstention():
    result = _mk({"facts": []}).extract("The sunset was beautiful.", role="user")
    assert result.valid_facts == []


# ---------- per-pattern happy paths (8 legacy patterns) ----------


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
    """'Tokyo is a city in Japan' → categorical + spatial_temporal.

    'in Japan' is locational containment (spatial_temporal), not
    constitutive parthood — the mereological pattern is the wrong fit
    for the surface form 'in'.
    """
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
                "slots": {"entity": "Marie Curie"},
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


def test_facts_not_a_list_returns_empty_result():
    for bad_facts in (None, "not-a-list", {"hi": 1}, 42):
        payload = {"facts": bad_facts}
        result = _mk(payload).extract("...", role="user")
        assert result.valid_facts == []
        assert result.rejected_facts == []


def test_non_dict_fact_rejected():
    payload = {"facts": ["just a string", 42, None]}
    result = _mk(payload).extract("...", role="user")
    assert result.valid_facts == []
    assert len(result.rejected_facts) == 3
    reasons = [r["reason"] for r in result.rejected_facts]
    assert all("not a dict" in r for r in reasons)
    assert "str" in reasons[0]
    assert "int" in reasons[1]
    assert "NoneType" in reasons[2]


def test_slots_not_a_dict_rejected():
    payload = {
        "facts": [
            {
                "pattern": "preference",
                "predicate": "likes",
                "slots": ["agent", "user"],
                "polarity": 1,
                "source_text": "x",
            }
        ]
    }
    result = _mk(payload).extract("...", role="user")
    assert result.valid_facts == []
    assert "slots must be a dict" in result.rejected_facts[0]["reason"]


def test_polarity_non_numeric_rejected():
    payload = {
        "facts": [
            {
                "pattern": "preference",
                "predicate": "likes",
                "slots": {"agent": "user", "object": "x"},
                "polarity": "positive",
                "source_text": "x",
            }
        ]
    }
    result = _mk(payload).extract("...", role="user")
    assert result.valid_facts == []
    assert "polarity must be an int" in result.rejected_facts[0]["reason"]


def test_predicate_must_be_non_empty_string():
    for bad_pred in ("", "   ", 42, None):
        payload = {
            "facts": [
                {
                    "pattern": "preference",
                    "predicate": bad_pred,
                    "slots": {"agent": "user", "object": "x"},
                    "polarity": 1,
                    "source_text": "x",
                }
            ]
        }
        result = _mk(payload).extract("...", role="user")
        assert result.valid_facts == []
        assert "predicate must be a non-empty string" \
               in result.rejected_facts[0]["reason"]


def test_role_validation():
    extractor = _mk({"facts": []})
    with pytest.raises(ValueError, match="role"):
        extractor.extract("x", role="system")


def test_tool_schema_lists_all_pattern_names():
    """Tool schema enum must include all 9 pattern names (8 legacy +
    mereological). v0.14.3: per-role tools — assistant requires
    expected_verifier, user does not. Both must list all patterns."""
    reg = load_default_registry()
    extractor = _mk({"facts": []})
    for tool in (extractor._record_tool_assistant, extractor._record_tool_user):
        enum = tool["input_schema"]["properties"]["facts"]["items"][
            "properties"
        ]["pattern"]["enum"]
        assert set(enum) == set(reg.names())
        assert "mereological" in enum
    # Per-role required-fields contract.
    asst_required = extractor._record_tool_assistant["input_schema"][
        "properties"]["facts"]["items"]["required"]
    user_required = extractor._record_tool_user["input_schema"][
        "properties"]["facts"]["items"]["required"]
    assert "expected_verifier" in asst_required
    assert "expected_verifier" not in user_required


def test_system_prompt_includes_every_pattern():
    extractor = _mk({"facts": []})
    sys = extractor._system_prompt
    for name in extractor.registry.names():
        assert name in sys, f"pattern {name!r} missing from prompt"


def test_system_prompt_includes_temporal_few_shot():
    extractor = _mk({"facts": []})
    sys = extractor._system_prompt
    assert "valid_from" in sys
    assert "valid_until" in sys
    assert "2017" in sys


def test_system_prompt_says_nine_patterns():
    """The abstain rule and pattern enumeration must reflect 9 patterns,
    not 8."""
    extractor = _mk({"facts": []})
    sys = extractor._system_prompt
    assert "NINE patterns" in sys or "nine patterns" in sys
    # The pattern list must enumerate all 9 names somewhere in the rules
    # block (the describe_for_prompt block also names them, but the
    # rules-section enumeration is what teaches the LLM that the set
    # is closed at 9).
    rules_block = sys.split("# Rules", 1)[1].split("# Slot rules", 1)[0]
    for name in (
        "role_assignment", "preference", "quantitative", "spatial_temporal",
        "categorical", "relational", "event", "propositional_attitude",
        "mereological",
    ):
        assert name in rules_block, f"{name!r} missing from rules-section enumeration"


# ---------- context for self-reference resolution ----------


def test_context_passed_in_user_message_when_provided():
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
    assert "this sentence" in msg or "self-references" in msg


def test_no_context_when_omitted():
    extractor = _mk({"facts": []})
    extractor.extract("Hello.", role="user")
    msg = extractor.llm.calls[0]["user_message"]
    assert "Preceding speaker's message" not in msg


def test_self_referential_count_few_shot_in_prompt():
    extractor = _mk({"facts": []})
    sys = extractor._system_prompt
    assert "this sentence" in sys
    assert "words_containing_letter" in sys


def test_hedged_count_few_shot_in_prompt():
    extractor = _mk({"facts": []})
    sys = extractor._system_prompt
    assert "PRIMARY" in sys or "primary" in sys
    assert "If counting all instances" in sys or "interpretation" in sys
    assert "three free trees" in sys


def test_context_block_does_not_use_alarming_negation():
    extractor = _mk({"facts": []})
    extractor.extract("Two words.", role="assistant", context="anything")
    msg = extractor.llm.calls[0]["user_message"]
    assert "do NOT extract" not in msg
    assert "Extract every fact-stating clause" in msg


def test_context_user_message_mentions_hedged_extraction_rule():
    extractor = _mk({"facts": []})
    extractor.extract("Two.", role="assistant", context="ctx")
    msg = extractor.llm.calls[0]["user_message"]
    assert "hedged" in msg or "conditional" in msg
    assert "PRIMARY" in msg or "primary" in msg


# ---------- substitution-detection check (port from
#            test_extractor_substitution_check.py — pure unit tests) ----------


def _make_result(facts: list[dict]) -> ExtractionResult:
    return ExtractionResult(valid_facts=list(facts))


def test_substring_match_no_warning():
    result = _make_result([{
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"value": 3},
        "source_text": "Saturn has 3 moons",
    }])
    ClaimExtractor._flag_substitutions(result, "I think Saturn has 3 moons total.")
    assert result.warnings == []


def test_substituted_number_flagged():
    result = _make_result([{
        "pattern": "quantitative", "predicate": "has_count",
        "slots": {"value": 146},
        "source_text": "Saturn has 146 confirmed moons",
    }])
    ClaimExtractor._flag_substitutions(
        result, "Saturn has 274 confirmed moons.",
    )
    assert len(result.warnings) == 1
    w = result.warnings[0]
    assert w["fact_index"] == 0
    assert w["kind"] == "source_text_not_in_input"
    assert "146" in w["detail"]


def test_punctuation_difference_no_warning():
    result = _make_result([{
        "pattern": "relational", "predicate": "involved_in",
        "slots": {},
        "source_text": 'reality TV show "The Apprentice"',
    }])
    ClaimExtractor._flag_substitutions(
        result,
        'He hosted the reality TV show "The Apprentice." on NBC.',
    )
    assert result.warnings == []


def test_multiple_facts_warned_independently():
    result = _make_result([
        {"pattern": "x", "predicate": "y", "slots": {},
         "source_text": "Saturn has 3 moons"},
        {"pattern": "x", "predicate": "y", "slots": {},
         "source_text": "Mars has 5 moons"},
        {"pattern": "x", "predicate": "y", "slots": {},
         "source_text": "Earth has 1 moon"},
    ])
    ClaimExtractor._flag_substitutions(
        result,
        "Saturn has 3 moons. Mars has 2 moons. Earth has 1 moon.",
    )
    assert len(result.warnings) == 1
    assert result.warnings[0]["fact_index"] == 1


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


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="real API test gated behind RUN_API_TESTS=1",
)
def test_real_api_letter_count_question_returns_no_facts():
    """**Phase 8.6a calibration gate (v2).** Mirror of the v1 test. The
    bug surfaced in real chat testing: the extractor LLM confabulated
    a value into a has_count slot when the user asked a counting
    question. Phase 8.6's first commit gate: this test must pass on
    a live API run before 8.6a merges."""
    from dotenv import load_dotenv
    load_dotenv()
    from src.llm_client import LLMClient

    llm = LLMClient()
    extractor = ClaimExtractor(llm, load_default_registry())

    cases = [
        "How many r's are in 'strawberry'?",
        "How many r's are in strawperry?",
        "How many letters are in 'antidisestablishmentarianism'?",
        "How many vowels does 'communication' contain?",
        "Count the words in 'the quick brown fox'.",
        "What's the digit count for 1234567?",
    ]
    for prompt in cases:
        result = extractor.extract(prompt, role="user")
        assert result.valid_facts == [], (
            f"letter/word/digit-count question extracted user facts "
            f"(Phase 8.6a calibration regression):\n"
            f"  prompt: {prompt!r}\n  facts: {result.valid_facts}"
        )


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="real API test gated behind RUN_API_TESTS=1",
)
def test_real_api_letter_count_assertion_does_extract():
    """**Phase 8.6a contrast gate (v2).** Declarative letter-count
    assertion DOES extract — confirms the prompt didn't over-abstain
    on assertions while learning to abstain on questions."""
    from dotenv import load_dotenv
    load_dotenv()
    from src.llm_client import LLMClient

    llm = LLMClient()
    extractor = ClaimExtractor(llm, load_default_registry())

    result = extractor.extract("Strawberry has 7 r's", role="user")
    quant = [f for f in result.valid_facts if f["pattern"] == "quantitative"]
    assert quant, (
        f"declarative letter-count failed to extract: {result.valid_facts}"
    )
    f = quant[0]
    assert f["slots"].get("value") == 7, (
        f"extractor substituted: expected value=7 (verbatim), "
        f"got {f['slots'].get('value')!r}"
    )


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="real API test gated behind RUN_API_TESTS=1",
)
def test_real_api_tautological_is_a_does_not_extract():
    """**Phase 8.6c calibration gate (v2).** A noun phrase whose head
    category appears as a suffix of itself ("the waggle-dance
    communication system") must NOT yield an is_a claim. Pre-fix the
    extractor emitted vacuous is_a(entity='waggle-dance communication
    system', category='communication system') tautologies."""
    from dotenv import load_dotenv
    load_dotenv()
    from src.llm_client import LLMClient

    llm = LLMClient()
    extractor = ClaimExtractor(llm, load_default_registry())

    cases = [
        ("the whole waggle-dance communication system enables foragers "
         "to share food locations.", "assistant"),
        ("the European parliamentary system is a parliamentary system.",
         "assistant"),
    ]
    for text, role in cases:
        result = extractor.extract(text, role=role)
        is_a = [
            f for f in result.valid_facts
            if f["pattern"] == "categorical" and f["predicate"] == "is_a"
        ]
        for f in is_a:
            entity = (f["slots"].get("entity") or "").strip().lower()
            category = (f["slots"].get("category") or "").strip().lower()
            assert not entity.endswith(" " + category) and entity != category, (
                f"tautological is_a extracted (Phase 8.6c regression):\n"
                f"  text: {text!r}\n  fact: {f}"
            )


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="real API test gated behind RUN_API_TESTS=1",
)
def test_real_api_real_categorical_is_a_does_extract():
    """**Phase 8.6c contrast gate (v2).** Confirms the prompt did not
    over-abstain on legitimate categorical claims."""
    from dotenv import load_dotenv
    load_dotenv()
    from src.llm_client import LLMClient

    llm = LLMClient()
    extractor = ClaimExtractor(llm, load_default_registry())

    result = extractor.extract(
        "The waggle dance is a form of communication.",
        role="assistant",
    )
    is_a = [
        f for f in result.valid_facts
        if f["pattern"] == "categorical" and f["predicate"] == "is_a"
    ]
    assert is_a, (
        f"real categorical claim did not extract is_a (Phase 8.6c "
        f"over-abstain regression): {result.valid_facts}"
    )
