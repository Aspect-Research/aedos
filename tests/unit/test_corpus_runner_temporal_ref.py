"""v0.16.1 WS8 Stage 1: the temporal_scope corpus runner checks the
event-relative *_ref fields.

The corpus runner (tests/calibration/test_corpus_runner.py) is calibration-gated
and only runs live under --run-calibration. These unit tests instead drive the
runner's `_run_temporal_scope` comparison function directly with a STUB extractor
(no LLM/KB cost), pinning the WS8 tightening: the runner asserts
`valid_until_ref` (before→event) and `valid_from_ref` (after/since→event), NOT
just the absolute `valid_from` / `valid_until` pair. Before the tightening a
relative-scope case that produced no *_ref field would have passed merely by
matching valid_from/valid_until=None — the relative-scope cases were untested.

These tests must NEVER weaken the runner: they confirm the *_ref expectations
are enforced (a mismatch on a ref fails the case).
"""

from __future__ import annotations

from aedos.llm.client import LLMClient
from tests.calibration.test_corpus_runner import _run_temporal_scope


class _StubTransport:
    """Returns one pre-configured raw claim from extract_with_tool, mimicking
    the LLM's tool output. chat() is unused here."""

    def __init__(self, raw_claim: dict):
        self._raw = raw_claim

    def extract_with_tool(self, system=None, user_message=None, tool=None, **kwargs):
        return {"claims": [self._raw]}

    def chat(self, *a, **kw):
        return ""


class _StubHarness:
    """Minimal stand-in exposing only `.client` — the single attribute
    `_run_temporal_scope` reads off the harness."""

    def __init__(self, raw_claim: dict):
        self.client = LLMClient(_transport=_StubTransport(raw_claim))


def _raw(**kwargs) -> dict:
    base = {
        "subject": "The team",
        "predicate": "had",
        "object": "five members",
        "polarity": 1,
        "source_text": "src",
        "verb_tense": "past",
        "valid_from": None,
        "valid_until": None,
        "valid_during_ref": None,
        "valid_from_ref": None,
        "valid_until_ref": None,
    }
    return {**base, **kwargs}


class TestCorpusRunnerChecksEventRelativeRefs:
    def test_before_event_passes_only_when_valid_until_ref_matches(self):
        # ts_relative_007 shape: "before the acquisition" → valid_until_ref.
        case = {
            "id": "ts_relative_007",
            "category": "relative_scope",
            "text": "The team had five members before the acquisition",
            "expected_scope": {"valid_until_ref": "claim_acquisition"},
        }
        # Correct extraction (ref present) → pass.
        h_ok = _StubHarness(_raw(
            subject="The team", object="five members",
            valid_until_ref="claim_acquisition",
        ))
        assert _run_temporal_scope(h_ok, case) is True

    def test_after_event_passes_only_when_valid_from_ref_matches(self):
        # ts_relative_008 shape: "after the election" → valid_from_ref.
        case = {
            "id": "ts_relative_008",
            "category": "relative_scope",
            "text": "After the election, she was President",
            "expected_scope": {"valid_from_ref": "claim_election"},
        }
        h_ok = _StubHarness(_raw(
            subject="she", object="President",
            valid_from_ref="claim_election",
        ))
        assert _run_temporal_scope(h_ok, case) is True

    def test_runner_fails_when_ref_missing(self):
        # The tightening's whole point: an extraction that omits the *_ref the
        # corpus expects must FAIL. Pre-tightening this would have spuriously
        # PASSED (valid_from/valid_until both None on each side).
        case = {
            "id": "ts_relative_007",
            "category": "relative_scope",
            "text": "The team had five members before the acquisition",
            "expected_scope": {"valid_until_ref": "claim_acquisition"},
        }
        # Past-tense, no ref → extractor produces valid_until=before_present and
        # leaves valid_until_ref None: a mismatch the runner now catches.
        h_miss = _StubHarness(_raw(
            subject="The team", object="five members",
            valid_until_ref=None,
        ))
        assert _run_temporal_scope(h_miss, case) is False

    def test_runner_fails_when_wrong_ref_field_set(self):
        # "before" event but extractor mis-routes to valid_from_ref → fail.
        case = {
            "id": "ts_relative_007",
            "category": "relative_scope",
            "text": "The team had five members before the acquisition",
            "expected_scope": {"valid_until_ref": "claim_acquisition"},
        }
        h_wrong = _StubHarness(_raw(
            subject="The team", object="five members",
            valid_from_ref="claim_acquisition",  # wrong field
        ))
        assert _run_temporal_scope(h_wrong, case) is False

    def test_during_still_checks_valid_during_ref(self):
        # The runner also checks valid_during_ref (Rule 15), unchanged.
        case = {
            "id": "ts_relative_003",
            "category": "relative_scope",
            "text": "France was in a recession during the war",
            "expected_scope": {"valid_during_ref": "claim_wartime"},
        }
        h_ok = _StubHarness(_raw(
            subject="France", predicate="in_a_recession", object="",
            valid_during_ref="claim_wartime",
        ))
        assert _run_temporal_scope(h_ok, case) is True
