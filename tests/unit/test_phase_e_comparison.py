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


class _FailingTransport(_FakeTransport):
    """Transport that raises on every extract_with_tool — every case should
    surface as a runner_error AND produce a transcript entry."""

    def extract_with_tool(self, *a, **k):
        raise RuntimeError("synthetic provider failure")


class _OverrideCapturingTransport(_FakeTransport):
    """Captures the AEDOS_OVERRIDE_MODEL_BY_PURPOSE the harness set for the run."""

    def __init__(self):
        self.seen_override = None

    def extract_with_tool(self, *a, **k):
        if self.seen_override is None:
            import os
            raw = os.environ.get("AEDOS_OVERRIDE_MODEL_BY_PURPOSE")
            self.seen_override = json.loads(raw) if raw else {}
        return super().extract_with_tool(*a, **k)


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


class TestErrorCapture:
    """Failure transcripts — the load-bearing fix after the V4-Flash diagnostic
    found 15 errored cases with zero transcript data."""

    def test_failed_call_writes_transcript_entry(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pec, "_RESULTS_DIR", tmp_path)
        result = pec.run_comparison(
            "kimi-k2.6", "extraction_corpus",
            load_env=False, write=True, transport=_FailingTransport(),
        )
        # Every case errors at the LLM call → all are runner_error.
        assert result["runner_errors"] == result["total_cases"]
        transcript = json.loads(
            (tmp_path / "kimi-k2.6__extraction_corpus.transcript.json").read_text(encoding="utf-8")
        )
        all_calls = [c for entry in transcript for c in entry["calls"]]
        assert len(all_calls) == result["total_cases"]
        assert all(c["error"] is not None for c in all_calls)
        assert all(c["response"] is None for c in all_calls)
        assert all("synthetic provider failure" in c["error"] for c in all_calls)
        # Transport path bypasses the SDK, so raw_response stays None.
        assert all(c["raw_response"] is None for c in all_calls)

    def test_successful_call_has_no_error_field_set(self):
        result = pec.run_comparison(
            "kimi-k2.6", "extraction_corpus",
            load_env=False, write=False, transport=_FakeTransport(),
            case_ids=["norm_001"],
        )
        assert result["runner_errors"] == 0


class TestCaseIdsFilter:
    def test_runs_only_selected_cases(self):
        result = pec.run_comparison(
            "kimi-k2.6", "extraction_corpus",
            load_env=False, write=False, transport=_FakeTransport(),
            case_ids=["norm_001", "norm_002", "temporal_001"],
        )
        assert result["total_cases"] == 3
        assert {o["case_id"] for o in result["per_case_outcomes"]} == {
            "norm_001", "norm_002", "temporal_001"}

    def test_unknown_case_ids_raises(self):
        with pytest.raises(ValueError, match="no cases matched"):
            pec.run_comparison(
                "kimi-k2.6", "extraction_corpus",
                load_env=False, write=False, transport=_FakeTransport(),
                case_ids=["does_not_exist"],
            )


class TestPricingReverification:
    _GLM = {"model": "z-ai/glm-5.1", "price_in_per_m": 0.0, "price_out_per_m": 0.0}

    def test_detects_changed_pricing(self):
        models = [{"id": "z-ai/glm-5.1",
                   "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]
        r = pec._reverify_pricing("glm-5.1", self._GLM, models=models)
        assert r["ok"] is False
        assert r["live_in"] == 1.0 and r["live_out"] == 2.0
        assert "CHANGED" in r["message"]

    def test_accepts_unchanged_pricing(self):
        models = [{"id": "z-ai/glm-5.1", "pricing": {"prompt": "0", "completion": "0"}}]
        r = pec._reverify_pricing("glm-5.1", self._GLM, models=models)
        assert r["ok"] is True

    def test_model_delisted_fails_verification(self):
        r = pec._reverify_pricing("glm-5.1", self._GLM, models=[])
        assert r["ok"] is False
        assert "no longer listed" in r["message"]


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

    def test_disable_thinking_candidate_routes_with_reasoning_off(self, monkeypatch):
        # No real candidate currently has disable_thinking=True (DeepSeek V4
        # was flipped to False after the Morph grammar-compile finding — see
        # docs/phase_E/deepseek_v4_flash_structural_errors.md). Synthesize an
        # entry to keep coverage of the True branch — the wiring still matters
        # for any future candidate that needs the disable-thinking payload.
        monkeypatch.setitem(pec._CANDIDATES, "_disable_test", {
            "model": "test/x", "price_in_per_m": 0.1, "price_out_per_m": 0.1,
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env_var": "OPENROUTER_API_KEY",
            "disable_thinking": True,
        })
        transport = _OverrideCapturingTransport()
        pec.run_comparison("_disable_test", "extraction_corpus",
                           load_env=False, write=False, transport=transport)
        assert transport.seen_override["*"]["extra_body"] == {"reasoning": {"enabled": False}}

    def test_thinking_enabled_candidate_has_no_extra_body(self):
        # kimi-k2.6 has disable_thinking=False → no extra_body in the override.
        transport = _OverrideCapturingTransport()
        pec.run_comparison("kimi-k2.6", "extraction_corpus",
                           load_env=False, write=False, transport=transport)
        assert "extra_body" not in transport.seen_override["*"]

    def test_transport_run_skips_pricing_verification(self):
        # Pricing re-verification is a live-run guard; a transport run is free
        # and offline, so it is skipped and recorded as None.
        result = pec.run_comparison(
            "kimi-k2.6", "extraction_corpus",
            load_env=False, write=False, transport=_FakeTransport(),
        )
        assert result["pricing_verification"] is None

    def test_override_env_var_is_restored_after_run(self):
        import os
        before = os.environ.get("AEDOS_OVERRIDE_MODEL_BY_PURPOSE")
        pec.run_comparison(
            "kimi-k2.6", "extraction_corpus",
            load_env=False, write=False, transport=_FakeTransport(),
        )
        assert os.environ.get("AEDOS_OVERRIDE_MODEL_BY_PURPOSE") == before

    def test_disabled_candidate_without_transport_is_refused(self):
        # Both DeepSeek V4 variants are disabled after the Phase E diagnostic;
        # a live run on either must be refused with the disabled reason.
        with pytest.raises(RuntimeError, match="is disabled"):
            pec.run_comparison("deepseek-v4-flash", "extraction_corpus",
                               load_env=False, write=False)

    def test_disabled_candidate_runs_with_transport(self):
        # The disabled refusal is gated on `transport is None`, so harness
        # unit tests can still exercise the wiring against disabled entries.
        result = pec.run_comparison(
            "deepseek-v4-flash", "extraction_corpus",
            case_ids=["norm_001"], load_env=False, write=False,
            transport=_FakeTransport(),
        )
        assert result["total_cases"] == 1

    def test_unfilled_candidate_without_transport_is_refused(self, monkeypatch):
        # All six real candidates now have model IDs; inject a synthetic
        # unfilled one to confirm a live run on a model-less candidate is
        # refused with a clear error.
        monkeypatch.setitem(pec._CANDIDATES, "_unfilled_test", {
            "model": None, "price_in_per_m": None, "price_out_per_m": None,
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env_var": "OPENROUTER_API_KEY",
        })
        with pytest.raises(ValueError, match="exact OpenRouter model ID"):
            pec.run_comparison("_unfilled_test", "extraction_corpus",
                               load_env=False, write=False)

    def test_unknown_candidate_and_corpus_rejected(self):
        with pytest.raises(KeyError):
            pec.run_comparison("nonesuch", "extraction_corpus",
                               load_env=False, write=False, transport=_FakeTransport())
        with pytest.raises(KeyError):
            pec.run_comparison("kimi-k2.6", "nonesuch_corpus",
                               load_env=False, write=False, transport=_FakeTransport())
