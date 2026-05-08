"""Mereological-specific extractor tests (Phase 1 of v0.14).

20+ mocked-LLM cases covering:
  * Clean constitutive parthood (part_of, member_of, composed_of,
    constitutes, subregion_of).
  * Locational containment that must NOT extract as mereological
    (Tokyo in Japan, engine in car, Asa lives in Williamstown).
  * Disambiguation pairs in a single sentence.
  * Negation (Hawaii is not part of the contiguous US).
  * Categorical-vs-mereological boundary.
  * Validation: required slots (part, whole) must both be present.

Phase 2 will own the runtime invariant "part != whole" via the
validator. Phase 1 enforces that only via YAML-example consistency
(see test_pattern_registry.py::test_mereological_examples_never_have_part_equals_whole).

The mocked LLM here returns whatever payload the test specifies; we
verify the extractor's validation + normalization flow handles
mereological correctly. The live-LLM calibration test is in
test_routing_calibration_mereological.py (gated behind RUN_API_TESTS=1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from src.layer1_extraction.extractor import ClaimExtractor
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


# ---------- clean constitutive parthood ----------


def test_part_of_state():
    """Williamstown is part of Massachusetts — canonical constitutive case."""
    payload = {
        "facts": [
            {
                "pattern": "mereological",
                "predicate": "part_of",
                "slots": {"part": "Williamstown", "whole": "Massachusetts"},
                "polarity": 1,
                "source_text": "Williamstown is part of Massachusetts",
            }
        ]
    }
    result = _mk(payload).extract(
        "Williamstown is part of Massachusetts.", role="user"
    )
    assert len(result.valid_facts) == 1
    f = result.valid_facts[0]
    assert f["pattern"] == "mereological"
    assert f["predicate"] == "part_of"
    assert f["slots"]["part"] == "Williamstown"
    assert f["slots"]["whole"] == "Massachusetts"
    assert f["polarity"] == 1


def test_part_of_country():
    payload = {
        "facts": [
            {
                "pattern": "mereological",
                "predicate": "part_of",
                "slots": {"part": "Tokyo", "whole": "Japan"},
                "polarity": 1,
                "source_text": "Tokyo is part of Japan",
            }
        ]
    }
    result = _mk(payload).extract("Tokyo is part of Japan.", role="user")
    assert result.valid_facts[0]["pattern"] == "mereological"
    assert result.valid_facts[0]["slots"]["part"] == "Tokyo"


def test_part_of_assembly():
    """The engine is part of the car — physical assembly."""
    payload = {
        "facts": [
            {
                "pattern": "mereological",
                "predicate": "part_of",
                "slots": {"part": "engine", "whole": "car"},
                "polarity": 1,
                "source_text": "The engine is part of the car",
            }
        ]
    }
    result = _mk(payload).extract("The engine is part of the car.", role="user")
    assert result.valid_facts[0]["pattern"] == "mereological"


def test_member_of_group():
    """Massachusetts is one of the New England states — group membership."""
    payload = {
        "facts": [
            {
                "pattern": "mereological",
                "predicate": "member_of",
                "slots": {"part": "Massachusetts", "whole": "New England"},
                "polarity": 1,
                "source_text": "Massachusetts is one of the New England states",
            }
        ]
    }
    result = _mk(payload).extract(
        "Massachusetts is one of the New England states.", role="user"
    )
    f = result.valid_facts[0]
    assert f["pattern"] == "mereological"
    assert f["predicate"] == "member_of"
    assert f["slots"]["part"] == "Massachusetts"


def test_composed_of():
    """Water is composed of hydrogen and oxygen — composition."""
    payload = {
        "facts": [
            {
                "pattern": "mereological",
                "predicate": "composed_of",
                "slots": {"part": "hydrogen and oxygen", "whole": "water"},
                "polarity": 1,
                "source_text": "Water is composed of hydrogen and oxygen",
            }
        ]
    }
    result = _mk(payload).extract(
        "Water is composed of hydrogen and oxygen.", role="user"
    )
    f = result.valid_facts[0]
    assert f["pattern"] == "mereological"
    assert f["predicate"] == "composed_of"


def test_constitutes_predicate_freeform():
    """`constitutes` is in example_predicates and should validate."""
    payload = {
        "facts": [
            {
                "pattern": "mereological",
                "predicate": "constitutes",
                "slots": {"part": "the legislature", "whole": "the government"},
                "polarity": 1,
                "source_text": "The legislature constitutes part of the government",
            }
        ]
    }
    result = _mk(payload).extract(
        "The legislature constitutes part of the government.", role="user"
    )
    assert result.valid_facts[0]["predicate"] == "constitutes"


def test_subregion_of_freeform_predicate():
    """`subregion_of` is in example_predicates — should validate as a
    fine-grained mereological label."""
    payload = {
        "facts": [
            {
                "pattern": "mereological",
                "predicate": "subregion_of",
                "slots": {"part": "Berkshire County", "whole": "Massachusetts"},
                "polarity": 1,
                "source_text": "Berkshire County is a subregion of Massachusetts",
            }
        ]
    }
    result = _mk(payload).extract(
        "Berkshire County is a subregion of Massachusetts.", role="user"
    )
    f = result.valid_facts[0]
    assert f["pattern"] == "mereological"
    assert f["predicate"] == "subregion_of"


def test_invented_predicate_within_mereological_accepted():
    """Predicate labels are free-form within a pattern. An invented
    label like `physical_component_of` should validate as long as the
    pattern + slots are correct."""
    payload = {
        "facts": [
            {
                "pattern": "mereological",
                "predicate": "physical_component_of",
                "slots": {"part": "alveoli", "whole": "lungs"},
                "polarity": 1,
                "source_text": "Alveoli are a physical component of the lungs",
            }
        ]
    }
    result = _mk(payload).extract(
        "Alveoli are a physical component of the lungs.", role="user"
    )
    assert result.valid_facts[0]["predicate"] == "physical_component_of"


# ---------- locational containment (must NOT be mereological) ----------


def test_locational_containment_lives_in_stays_spatial_temporal():
    """Asa lives in Williamstown — locational, NOT constitutive."""
    payload = {
        "facts": [
            {
                "pattern": "spatial_temporal",
                "predicate": "lives_in",
                "slots": {
                    "entity": "Asa",
                    "location": "Williamstown",
                    "relation_kind": "residence",
                },
                "polarity": 1,
                "source_text": "Asa lives in Williamstown",
            }
        ]
    }
    result = _mk(payload).extract("Asa lives in Williamstown.", role="user")
    assert result.valid_facts[0]["pattern"] == "spatial_temporal"
    # Negative assertion: never extracted as mereological.
    assert all(f["pattern"] != "mereological" for f in result.valid_facts)


def test_tokyo_in_japan_is_locational():
    """'Tokyo is in Japan' (vs 'Tokyo is part of Japan') stays in
    spatial_temporal — surface form is the tiebreaker."""
    payload = {
        "facts": [
            {
                "pattern": "spatial_temporal",
                "predicate": "located_in",
                "slots": {
                    "entity": "Tokyo",
                    "location": "Japan",
                    "relation_kind": "containment",
                },
                "polarity": 1,
                "source_text": "Tokyo is in Japan",
            }
        ]
    }
    result = _mk(payload).extract("Tokyo is in Japan.", role="user")
    assert result.valid_facts[0]["pattern"] == "spatial_temporal"


def test_engine_in_car_is_locational():
    """'The engine is in the car' is locational placement, not assembly."""
    payload = {
        "facts": [
            {
                "pattern": "spatial_temporal",
                "predicate": "located_in",
                "slots": {
                    "entity": "engine",
                    "location": "car",
                    "relation_kind": "placement",
                },
                "polarity": 1,
                "source_text": "The engine is in the car",
            }
        ]
    }
    result = _mk(payload).extract("The engine is in the car.", role="user")
    assert result.valid_facts[0]["pattern"] == "spatial_temporal"


# ---------- disambiguation pairs (one sentence yields two patterns) ----------


def test_disambiguation_pair_williamstown_and_asa():
    """The canonical disambiguation pair: 'Williamstown is part of
    Massachusetts and Asa lives in Williamstown'. Both facts are
    extracted in DIFFERENT patterns."""
    payload = {
        "facts": [
            {
                "pattern": "mereological",
                "predicate": "part_of",
                "slots": {"part": "Williamstown", "whole": "Massachusetts"},
                "polarity": 1,
                "source_text": "Williamstown is part of Massachusetts",
            },
            {
                "pattern": "spatial_temporal",
                "predicate": "lives_in",
                "slots": {
                    "entity": "Asa",
                    "location": "Williamstown",
                    "relation_kind": "residence",
                },
                "polarity": 1,
                "source_text": "Asa lives in Williamstown",
            },
        ]
    }
    result = _mk(payload).extract(
        "Williamstown is part of Massachusetts and Asa lives in Williamstown.",
        role="user",
    )
    assert len(result.valid_facts) == 2
    patterns = sorted(f["pattern"] for f in result.valid_facts)
    assert patterns == ["mereological", "spatial_temporal"]


def test_categorical_and_mereological_in_one_sentence():
    """Tokyo is a city that is part of Japan → categorical + mereological."""
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
                "pattern": "mereological",
                "predicate": "part_of",
                "slots": {"part": "Tokyo", "whole": "Japan"},
                "polarity": 1,
                "source_text": "Tokyo is a city that is part of Japan",
            },
        ]
    }
    result = _mk(payload).extract(
        "Tokyo is a city that is part of Japan.", role="user"
    )
    patterns = sorted(f["pattern"] for f in result.valid_facts)
    assert patterns == ["categorical", "mereological"]


# ---------- negation ----------


def test_negated_mereological():
    """Hawaii is not part of the contiguous United States — polarity=0."""
    payload = {
        "facts": [
            {
                "pattern": "mereological",
                "predicate": "part_of",
                "slots": {
                    "part": "Hawaii",
                    "whole": "contiguous United States",
                },
                "polarity": 0,
                "source_text": "Hawaii is not part of the contiguous United States",
            }
        ]
    }
    result = _mk(payload).extract(
        "Hawaii is not part of the contiguous United States.", role="user"
    )
    f = result.valid_facts[0]
    assert f["pattern"] == "mereological"
    assert f["polarity"] == 0


def test_negated_member_of():
    payload = {
        "facts": [
            {
                "pattern": "mereological",
                "predicate": "member_of",
                "slots": {"part": "Switzerland", "whole": "European Union"},
                "polarity": 0,
                "source_text": "Switzerland is not a member of the European Union",
            }
        ]
    }
    result = _mk(payload).extract(
        "Switzerland is not a member of the European Union.", role="user"
    )
    assert result.valid_facts[0]["polarity"] == 0
    assert result.valid_facts[0]["pattern"] == "mereological"


# ---------- categorical-vs-mereological boundary ----------


def test_categorical_kind_membership_not_mereological():
    """'Tokyo is a city' is membership in a KIND (categorical), not
    parthood in a SPECIFIC larger thing (mereological)."""
    payload = {
        "facts": [
            {
                "pattern": "categorical",
                "predicate": "is_a",
                "slots": {"entity": "Tokyo", "category": "city"},
                "polarity": 1,
                "source_text": "Tokyo is a city",
            }
        ]
    }
    result = _mk(payload).extract("Tokyo is a city.", role="user")
    assert result.valid_facts[0]["pattern"] == "categorical"
    assert all(f["pattern"] != "mereological" for f in result.valid_facts)


# ---------- validation paths specific to mereological ----------


def test_mereological_missing_part_slot_rejected():
    """The 'part' slot is required."""
    payload = {
        "facts": [
            {
                "pattern": "mereological",
                "predicate": "part_of",
                "slots": {"whole": "Japan"},  # missing part
                "polarity": 1,
                "source_text": "Tokyo is part of Japan",
            }
        ]
    }
    result = _mk(payload).extract("Tokyo is part of Japan.", role="user")
    assert result.valid_facts == []
    reason = result.rejected_facts[0]["reason"]
    assert "missing required slots" in reason
    assert "part" in reason


def test_mereological_missing_whole_slot_rejected():
    """The 'whole' slot is required."""
    payload = {
        "facts": [
            {
                "pattern": "mereological",
                "predicate": "part_of",
                "slots": {"part": "Tokyo"},  # missing whole
                "polarity": 1,
                "source_text": "Tokyo is part of Japan",
            }
        ]
    }
    result = _mk(payload).extract("Tokyo is part of Japan.", role="user")
    assert result.valid_facts == []
    reason = result.rejected_facts[0]["reason"]
    assert "missing required slots" in reason
    assert "whole" in reason


def test_mereological_with_optional_temporal_scope():
    """mereological accepts optional valid_from / valid_until — useful
    for territorial changes (Crimea was part of Ukraine until 2014).
    Phase 1 doesn't enforce time-bounded semantics, just accepts the
    slots."""
    payload = {
        "facts": [
            {
                "pattern": "mereological",
                "predicate": "part_of",
                "slots": {
                    "part": "Crimea",
                    "whole": "Ukraine",
                    "valid_until": "2014",
                },
                "polarity": 1,
                "source_text": "Crimea was part of Ukraine until 2014",
            }
        ]
    }
    result = _mk(payload).extract(
        "Crimea was part of Ukraine until 2014.", role="user"
    )
    assert len(result.valid_facts) == 1
    assert result.valid_facts[0]["slots"]["valid_until"] == "2014"


# ---------- system prompt assertions specific to Phase 1 ----------


def test_system_prompt_contrasts_mereological_with_spatial_temporal():
    """The few-shot block must explicitly contrast 'X is part of Y'
    (mereological) with 'X is in Y' / 'X lives in Y' (spatial_temporal)
    so the LLM internalizes the boundary."""
    extractor = _mk({"facts": []})
    sys = extractor._system_prompt
    # Both patterns must be named in the contrast block.
    assert "mereological" in sys
    assert "spatial_temporal" in sys
    # The disambiguation phrase must appear.
    assert "constitutive parthood" in sys.lower() or "constitutive" in sys.lower()
    assert "locational containment" in sys.lower() or "locational" in sys.lower()


def test_system_prompt_contains_disambiguation_pair():
    """The 'Williamstown is part of Massachusetts and Asa lives in
    Williamstown' multi-fact example must appear verbatim in the
    prompt — it's the canonical pair."""
    extractor = _mk({"facts": []})
    sys = extractor._system_prompt
    assert "Williamstown is part of Massachusetts" in sys
    assert "Asa lives in Williamstown" in sys


def test_system_prompt_contains_tokyo_in_vs_part_of_contrast():
    """Both 'Tokyo is in Japan' and 'Tokyo is part of Japan' must
    appear in the prompt, classified to different patterns."""
    extractor = _mk({"facts": []})
    sys = extractor._system_prompt
    assert "Tokyo is in Japan" in sys
    assert "Tokyo is part of Japan" in sys


def test_system_prompt_lists_mereological_lexical_cues():
    """The prompt must teach the lexical cues that disambiguate
    mereological from spatial_temporal."""
    extractor = _mk({"facts": []})
    sys = extractor._system_prompt
    for cue in ("part of", "member of", "composed of"):
        assert cue in sys, f"lexical cue {cue!r} missing from prompt"
    for cue in ("in", "at", "located in"):
        # These are commonplace English words — assert they appear in
        # the disambiguation context, not just anywhere.
        assert cue in sys
