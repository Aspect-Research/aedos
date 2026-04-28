"""Tests for the canonical-constants cross-check (v0.5 §5).

When the LLM router returns ``python_with_canonical_constants``, the
code-generation pipeline runs twice at different temperatures and the
two results are compared. Agreement → accept. Disagreement → log and
return a disagreement status so the router falls back.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.fact_store import FactStore
from src.verifiers.code_generation.pipeline import (
    CodeGenVerificationResult,
    CodeGenerationVerifier,
)


@dataclass
class TemperatureScriptedLLM:
    """LLM that returns different code based on the rewrite temperature.

    extracts: shared queue (prompt builder pops once per pipeline run).
    rewrites_by_temp: dict[float, list[str]] — temperature → script of code outputs.
    """

    extracts: list[dict] = field(default_factory=list)
    rewrites_by_temp: dict = field(default_factory=dict)
    rewrite_calls: list[dict] = field(default_factory=list)
    corrector_model: str = "claude-haiku-4-5"

    def extract_with_tool(self, system, user_message, tool, max_tokens=2048):
        return self.extracts.pop(0)

    def rewrite(self, system, user_message, max_tokens=2048, temperature=None,
                model=None):
        # ``model`` is the v0.5.x cross-check override (CROSS_CHECK_MODEL).
        # The mock keys scripts by temperature only — it doesn't care which
        # model name the cross-check picks, just that the call shape is
        # accepted.
        self.rewrite_calls.append({"temperature": temperature, "model": model})
        scripts = self.rewrites_by_temp.get(temperature, [])
        return scripts.pop(0)


def _claim(value):
    return {
        "pattern": "quantitative", "predicate": "us_states_starting_with_letter",
        "slots": {"subject": "US states", "property": "starting_with_A", "value": value},
        "polarity": 1, "source_text": "x",
    }


def _prompt():
    return {
        "prompt": "Compute the count of US states whose name begins with the letter 'A'. Print only the integer.",
        "expected_output_type": "int",
    }


# ---------- agreement ----------


def test_cross_check_agreement_accepts(tmp_path):
    """Both temperatures produce code that prints 4 → verified, no disagreement event."""
    store = FactStore(tmp_path / "x.db")
    llm = TemperatureScriptedLLM(
        extracts=[_prompt(), _prompt()],
        rewrites_by_temp={
            0.0: ["print(len([s for s in ['Alabama','Alaska','Arizona','Arkansas'] if s.startswith('A')]))"],
            0.3: ["print(sum(1 for s in ['Alabama','Alaska','Arizona','Arkansas']))"],
        },
    )
    verifier = CodeGenerationVerifier(store=store, llm=llm)
    turn_id = store.insert_turn("user", "test")
    result = verifier.verify_with_cross_check(_claim(4), source_turn_id=turn_id)
    assert result.status == "verified"
    assert result.actual_value == 4

    events = store.get_pipeline_events(turn_id)
    stages = {e["stage"] for e in events}
    assert "canonical_constants_cross_check" in stages
    assert "canonical_constants_disagreement" not in stages

    # Both temperatures were exercised.
    temps = sorted({c["temperature"] for c in llm.rewrite_calls})
    assert temps == [0.0, 0.3]
    store.close()


def test_cross_check_trace_carries_both_generations(tmp_path):
    store = FactStore(tmp_path / "x.db")
    llm = TemperatureScriptedLLM(
        extracts=[_prompt(), _prompt()],
        rewrites_by_temp={
            0.0: ["print(4)"],  # different code shapes …
            0.3: ["print(2 + 2)"],
        },
    )
    # NB: print(4) and print(2+2) both emit "4" → equal computed values.
    verifier = CodeGenerationVerifier(store=store, llm=llm)
    turn_id = store.insert_turn("user", "test")
    result = verifier.verify_with_cross_check(_claim(4), source_turn_id=turn_id)

    cross = result.trace.get("cross_check")
    assert cross is not None, "trace should carry cross_check artifacts"
    assert "a" in cross and "b" in cross
    assert cross["a"]["actual_value"] == cross["b"]["actual_value"] == 4
    # Different code on each side.
    assert cross["a"]["code"]["code"] != cross["b"]["code"]["code"]
    store.close()


# ---------- disagreement ----------


def test_cross_check_disagreement_falls_through(tmp_path):
    """Two generations disagree on the computed value → disagreement
    status, disagreement event logged, no value returned.
    """
    store = FactStore(tmp_path / "x.db")
    llm = TemperatureScriptedLLM(
        extracts=[_prompt(), _prompt()],
        rewrites_by_temp={
            0.0: ["print(4)"],
            0.3: ["print(5)"],
        },
    )
    verifier = CodeGenerationVerifier(store=store, llm=llm)
    turn_id = store.insert_turn("user", "test")
    result = verifier.verify_with_cross_check(_claim(4), source_turn_id=turn_id)

    assert result.status == "canonical_constants_disagreement"
    assert result.actual_value is None
    assert "disagreed" in result.explanation.lower()

    events = store.get_pipeline_events(turn_id)
    stages = [e["stage"] for e in events]
    assert "canonical_constants_disagreement" in stages
    # The disagreement event carries both generations.
    disagreement = next(
        e for e in events if e["stage"] == "canonical_constants_disagreement"
    )
    assert disagreement["data"]["a"]["actual_value"] == 4
    assert disagreement["data"]["b"]["actual_value"] == 5
    store.close()


def test_cross_check_with_one_side_failing_is_disagreement(tmp_path):
    """If one generation succeeds and the other fails (different status),
    the cross-check treats this as disagreement.
    """
    store = FactStore(tmp_path / "x.db")
    llm = TemperatureScriptedLLM(
        extracts=[_prompt(), _prompt()],
        rewrites_by_temp={
            0.0: ["print(4)"],
            0.3: ["raise SystemError('boom')"],   # exits non-zero
        },
    )
    verifier = CodeGenerationVerifier(store=store, llm=llm)
    turn_id = store.insert_turn("user", "test")
    result = verifier.verify_with_cross_check(_claim(4), source_turn_id=turn_id)

    assert result.status == "canonical_constants_disagreement"
    events = store.get_pipeline_events(turn_id)
    assert any(e["stage"] == "canonical_constants_disagreement" for e in events)
    store.close()


# ---------- v0.5.x: cross-check forces a temperature-accepting model ----


def test_cross_check_overrides_model_to_cross_check_model(tmp_path):
    """v0.5.x: Opus 4.7 silently drops temperature, which would erase
    the cross-check signal. The cross-check forces CROSS_CHECK_MODEL
    (Sonnet 4.6) on both generations regardless of llm.corrector_model."""
    from src.verifiers.code_generation.pipeline import CROSS_CHECK_MODEL

    store = FactStore(tmp_path / "x.db")
    llm = TemperatureScriptedLLM(
        extracts=[_prompt(), _prompt()],
        rewrites_by_temp={
            0.0: ["print(4)"],
            0.3: ["print(4)"],
        },
        corrector_model="claude-opus-4-7",  # default that drops temperature
    )
    verifier = CodeGenerationVerifier(store=store, llm=llm)
    turn_id = store.insert_turn("user", "test")
    verifier.verify_with_cross_check(_claim(4), source_turn_id=turn_id)

    # Both rewrite calls received the cross-check model override.
    assert all(c["model"] == CROSS_CHECK_MODEL for c in llm.rewrite_calls)
    # And both temperatures were exercised — variation signal preserved.
    temps = sorted({c["temperature"] for c in llm.rewrite_calls})
    assert temps == [0.0, 0.3]
    store.close()


def test_non_cross_check_path_does_not_override_model(tmp_path):
    """The single-shot ``verify`` path must NOT pass the cross-check
    model override — that's only the cross-check's call. Single-shot
    verification uses the configured corrector_model as before."""
    store = FactStore(tmp_path / "x.db")
    llm = TemperatureScriptedLLM(
        extracts=[_prompt()],
        rewrites_by_temp={None: ["print(4)"]},  # no temperature
        corrector_model="claude-opus-4-7",
    )
    verifier = CodeGenerationVerifier(store=store, llm=llm)
    turn_id = store.insert_turn("user", "test")
    verifier.verify(_claim(4), source_turn_id=turn_id)

    # The single-shot path passes model=None (no override).
    assert all(c["model"] is None for c in llm.rewrite_calls)
    store.close()
