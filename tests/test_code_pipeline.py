"""Tests for src.verifiers.code_generation.pipeline orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from src.fact_store import FactStore
from src.verifiers.code_generation.pipeline import (
    CodeGenVerificationResult,
    CodeGenerationVerifier,
    verify_via_code_generation,
)


@dataclass
class ScriptedLLM:
    """Mock LLM that scripts each stage's response in order.

    Stage 1 (triage) and Stage 2 (prompt builder) call extract_with_tool.
    Stage 3 (code writer) calls rewrite. Pop in the order they're invoked.
    """

    extracts: list[dict[str, Any]] = field(default_factory=list)
    rewrites: list[str] = field(default_factory=list)
    extract_calls: list[dict] = field(default_factory=list)
    rewrite_calls: list[dict] = field(default_factory=list)
    corrector_model: str = "claude-haiku-4-5"

    def extract_with_tool(self, system, user_message, tool, max_tokens=2048):
        self.extract_calls.append(
            {"tool_name": tool["name"], "user_message": user_message}
        )
        return self.extracts.pop(0)

    def rewrite(self, system, user_message, max_tokens=2048):
        self.rewrite_calls.append({"user_message": user_message})
        return self.rewrites.pop(0)


def _claim(pattern, predicate, slots, polarity=1, source_text="<src>"):
    return {
        "pattern": pattern, "predicate": predicate, "slots": slots,
        "polarity": polarity, "source_text": source_text,
    }


# ---------- happy path: end-to-end ----------


def test_strawperpy_verified_end_to_end(tmp_path):
    """Triage→prompt→code→sandbox→compare for a verifiable counting claim.

    'strawperpy'.count('r') == 2, so value=2 verifies and value=3 contradicts.
    """
    store = FactStore(tmp_path / "x.db")
    llm = ScriptedLLM(
        extracts=[
            {"verifiable": True, "reason": "char counting"},
            {"prompt": "Count occurrences of 'r' in 'strawperpy'. Print only int.",
             "expected_output_type": "int"},
        ],
        rewrites=["print('strawperpy'.count('r'))"],
    )
    claim = _claim("quantitative", "has_count",
                   {"subject": "strawperpy", "property": "letter_r", "value": 2})
    user_turn_id = store.insert_turn("user", "test")

    result = verify_via_code_generation(
        claim, llm, store=store, source_turn_id=user_turn_id,
    )

    assert result.status == "verified"
    assert result.actual_value == 2

    # Pipeline events for every stage.
    events = store.get_pipeline_events(user_turn_id)
    stages = {e["stage"] for e in events}
    assert "code_triage" in stages
    assert "code_prompt_built" in stages
    assert "code_generated" in stages
    assert "code_executed" in stages
    assert "code_comparison" in stages
    store.close()


def test_strawperpy_contradicted(tmp_path):
    """Same setup, but the claim asserts the wrong value."""
    store = FactStore(tmp_path / "x.db")
    llm = ScriptedLLM(
        extracts=[
            {"verifiable": True, "reason": "char counting"},
            {"prompt": "Count 'r' in 'strawperpy'. Print int.",
             "expected_output_type": "int"},
        ],
        rewrites=["print('strawperpy'.count('r'))"],
    )
    claim = _claim("quantitative", "has_count",
                   {"subject": "strawperpy", "property": "letter_r", "value": 99})
    turn_id = store.insert_turn("user", "test")

    result = verify_via_code_generation(claim, llm, store=store, source_turn_id=turn_id)
    assert result.status == "contradicted"
    assert result.actual_value == 2  # the real count
    store.close()


# ---------- triage gates the pipeline ----------


def test_not_verifiable_short_circuits(tmp_path):
    """Triage says NO → no prompt, no code, no sandbox."""
    store = FactStore(tmp_path / "x.db")
    llm = ScriptedLLM(
        extracts=[{"verifiable": False, "reason": "requires biographical data"}],
    )
    claim = _claim("quantitative", "born_in_year",
                   {"subject": "Donald Trump", "property": "birth_year", "value": 1946})
    turn_id = store.insert_turn("user", "test")

    result = verify_via_code_generation(claim, llm, store=store, source_turn_id=turn_id)
    assert result.status == "not_python_verifiable"
    assert "biographical" in result.explanation
    # No code-writer or comparator events because we short-circuited.
    events = store.get_pipeline_events(turn_id)
    stages = {e["stage"] for e in events}
    assert "code_triage" in stages
    assert "code_generated" not in stages
    assert "code_comparison" not in stages
    # And the LLM was called exactly once (just the triage call).
    assert len(llm.extract_calls) == 1
    assert len(llm.rewrite_calls) == 0
    store.close()


# ---------- leak detection logs and retries ----------


def test_leak_detected_logs_warning_and_retries(tmp_path):
    store = FactStore(tmp_path / "x.db")
    llm = ScriptedLLM(
        extracts=[
            # Triage: verifiable
            {"verifiable": True, "reason": "ok"},
            # Prompt attempt 1 leaks "7"
            {"prompt": "Confirm that count is 7. Print int.",
             "expected_output_type": "int"},
            # Prompt attempt 2 is clean
            {"prompt": "Compute the count of 'r' in 'strawperpy'. Print int.",
             "expected_output_type": "int"},
        ],
        rewrites=["print('strawperpy'.count('r'))"],
    )
    claim = _claim("quantitative", "has_count",
                   {"subject": "strawperpy", "property": "letter_r", "value": 7})
    turn_id = store.insert_turn("user", "test")

    result = verify_via_code_generation(claim, llm, store=store, source_turn_id=turn_id)
    # The claim asserted 7 but the count is 3 → contradicted.
    assert result.status == "contradicted"

    events = store.get_pipeline_events(turn_id)
    stages = [e["stage"] for e in events]
    # One leakage event was emitted.
    assert "code_prompt_leakage_detected" in stages
    # And the prompt-built event recorded both attempts with the second as final.
    built = next(e for e in events if e["stage"] == "code_prompt_built")
    assert len(built["data"]["attempts"]) == 2
    assert built["data"]["attempts"][0]["leak_detected"] is True
    assert built["data"]["attempts"][1]["leak_detected"] is False
    assert built["data"]["compromised"] is False
    store.close()


# ---------- timeout path ----------


def test_code_execution_timeout(tmp_path):
    store = FactStore(tmp_path / "x.db")
    llm = ScriptedLLM(
        extracts=[
            {"verifiable": True, "reason": "ok"},
            # Prompt must not contain the asserted value (98) — that would
            # trip the leak detector and trigger a retry.
            {"prompt": "Compute the count of x. Print only int.",
             "expected_output_type": "int"},
        ],
        rewrites=["import time; time.sleep(10); print(0)"],
    )
    claim = _claim("quantitative", "has_count",
                   {"subject": "x", "property": "y", "value": 98})
    turn_id = store.insert_turn("user", "test")

    result = verify_via_code_generation(
        claim, llm, store=store, source_turn_id=turn_id,
        sandbox_timeout_seconds=1,
    )
    assert result.status == "code_execution_failed"
    assert "timed out" in result.explanation.lower() or "1s" in result.explanation
    store.close()


# ---------- CodeGenerationVerifier wrapper ----------


def test_verifier_class_delegates_to_pipeline(tmp_path):
    store = FactStore(tmp_path / "x.db")
    llm = ScriptedLLM(
        extracts=[
            {"verifiable": True, "reason": "ok"},
            # The asserted value is 42; keep it out of the neutral prompt.
            {"prompt": "Compute the literal value below. Print only int.",
             "expected_output_type": "int"},
        ],
        rewrites=["print(42)"],
    )
    verifier = CodeGenerationVerifier(store=store, llm=llm)
    claim = _claim("quantitative", "has_count",
                   {"subject": "x", "property": "y", "value": 42})
    turn_id = store.insert_turn("user", "test")
    result = verifier.verify(claim, source_turn_id=turn_id)
    assert isinstance(result, CodeGenVerificationResult)
    assert result.status == "verified"
    store.close()
