"""Phase E2 — offline tests for the comparison harness.

A fake LLM transport drives `run_comparison` with no network, no key, no cost:
the extraction corpus is LLM-only, so the whole harness path (override, runner
invocation, per-case classification, aggregation, result writing) is exercised
offline.
"""

from __future__ import annotations

import json

import pytest

from tests.evaluation import phase_e_comparison as pec


class _FakeTransport:
    """Returns one structurally-valid claim for the extraction tool."""

    def extract_with_tool(self, system, user_message, tool, model="", purpose=None):
        if tool.get("name") == "extract_claims":
            return {"claims": [{
                "subject": user_message,
                "predicate": "calib_predicate",
                "object": "calib_object",
                "polarity": 1,
                "source_text": user_message,
                "verb_tense": "present",
            }]}
        return {}

    def chat(self, system, messages, model="", purpose=None):
        return ""


class TestClassify:
    def test_verdict_corpus_false_verified(self):
        assert pec._classify("derivation_corpus", False, "verified", None) == "false_verified"

    def test_verdict_corpus_false_abstention(self):
        assert pec._classify("derivation_corpus", False, "no_grounding_found", None) == "false_abstention"
        # no walk happened at all → also an abstention
        assert pec._classify("derivation_corpus", False, None, None) == "false_abstention"

    def test_verdict_corpus_false_contradicted(self):
        assert pec._classify("derivation_corpus", False, "contradicted", None) == "false_contradicted"

    def test_verdict_corpus_correct(self):
        assert pec._classify("derivation_corpus", True, "verified", None) == "correct"

    def test_non_verdict_corpus_collapses_to_pass_fail(self):
        assert pec._classify("extraction_corpus", True, None, None) == "correct"
        assert pec._classify("extraction_corpus", False, None, None) == "failed"

    def test_runner_error_dominates(self):
        assert pec._classify("derivation_corpus", False, "verified", "KeyError: x") == "runner_error"


class TestCost:
    def test_cost_from_tokens_and_pricing(self):
        cand = {"price_in_per_m": 1.0, "price_out_per_m": 2.0}
        assert pec._cost(1_000_000, 500_000, cand) == 2.0

    def test_cost_is_none_when_pricing_absent(self):
        assert pec._cost(1000, 1000, {"price_in_per_m": None, "price_out_per_m": None}) is None


class TestRunComparisonOffline:
    def test_extraction_run_with_fake_transport(self):
        result = pec.run_comparison(
            "kimi-k2.6", "extraction_corpus",
            load_env=False, write=False, transport=_FakeTransport(),
        )
        assert result["corpus"] == "extraction_corpus"
        assert result["candidate"] == "kimi-k2.6"
        assert result["total_cases"] == 57
        assert result["passed"] + result["failed"] + result["runner_errors"] == 57
        # extraction has no verdict → soundness counts are null, not 0
        assert result["false_verifieds"] is None
        assert result["abstentions_on_positive"] is None
        assert len(result["per_case_outcomes"]) == 57
        assert {o["classification"] for o in result["per_case_outcomes"]} <= {
            "correct", "failed", "runner_error"}

    def test_write_emits_result_and_transcript_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pec, "_RESULTS_DIR", tmp_path)
        pec.run_comparison(
            "kimi-k2.6", "extraction_corpus",
            load_env=False, write=True, transport=_FakeTransport(),
        )
        result_file = tmp_path / "kimi-k2.6__extraction_corpus.json"
        transcript_file = tmp_path / "kimi-k2.6__extraction_corpus.transcript.json"
        assert result_file.exists() and transcript_file.exists()
        on_disk = json.loads(result_file.read_text(encoding="utf-8"))
        assert on_disk["total_cases"] == 57

    def test_override_env_var_is_restored_after_run(self):
        import os
        before = os.environ.get("AEDOS_OVERRIDE_MODEL_BY_PURPOSE")
        pec.run_comparison(
            "kimi-k2.6", "extraction_corpus",
            load_env=False, write=False, transport=_FakeTransport(),
        )
        assert os.environ.get("AEDOS_OVERRIDE_MODEL_BY_PURPOSE") == before

    def test_unfilled_candidate_without_transport_is_refused(self):
        # Every candidate ships with model=None — a live run must be refused
        # until the operator fills the exact OpenRouter model ID.
        with pytest.raises(ValueError, match="exact OpenRouter model ID"):
            pec.run_comparison("kimi-k2.6", "extraction_corpus", load_env=False, write=False)

    def test_unknown_candidate_and_corpus_rejected(self):
        with pytest.raises(KeyError):
            pec.run_comparison("nonesuch", "extraction_corpus",
                               load_env=False, write=False, transport=_FakeTransport())
        with pytest.raises(KeyError):
            pec.run_comparison("kimi-k2.6", "nonesuch_corpus",
                               load_env=False, write=False, transport=_FakeTransport())
