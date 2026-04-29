"""Tests for src.verifiers.retrieval_verifier (v0.3 — slots-aware, multi-attempt)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx
import pytest

from src.fact_store import FactStore
from src.pattern_registry import load_default_registry, reset_cache
from src.verifiers.types import VerificationOutcome
from src.verifiers.retrieval_verifier import (
    JudgeVerdict,
    QueryAttempt,
    RetrievalResult,
    RetrievalVerifier,
    Snippet,
    build_queries,
    default_search,
    parse_judge_response,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_cache()
    yield
    reset_cache()


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "t.db")
    yield s
    s.close()


@dataclass
class FakeLLM:
    rewrite_responses: list[str] = field(default_factory=list)
    rewrite_calls: list[dict] = field(default_factory=list)

    def rewrite(self, system, user_message, max_tokens=2048, **_kwargs):
        self.rewrite_calls.append({"system": system, "user_message": user_message})
        return self.rewrite_responses.pop(0)


_DEFAULT_SLOTS = {"agent": "Donald Trump", "role": "47th President", "org": "United States"}


def _claim(
    pattern="role_assignment",
    predicate="holds_role",
    slots=None,
    polarity=1,
):
    # Use `is None` so callers can pass {} explicitly to test empty-slot cases.
    return {
        "pattern": pattern,
        "predicate": predicate,
        "slots": _DEFAULT_SLOTS if slots is None else slots,
        "polarity": polarity,
        "source_text": "<src>",
    }


def _verifier(store, llm, fake_search):
    return RetrievalVerifier(
        store=store,
        llm=llm,
        registry=load_default_registry(),
        search_fn=fake_search,
        ttl_hours=1,
    )


# ---------- judge response parsing ----------


def test_parse_supported():
    v = parse_judge_response("SUPPORTED\nJustification: ok")
    assert v.verdict == "SUPPORTED"


def test_parse_contradicted():
    v = parse_judge_response("CONTRADICTED\nJustification: snippet 2 disagrees")
    assert v.verdict == "CONTRADICTED"


def test_parse_insufficient():
    v = parse_judge_response("INSUFFICIENT_EVIDENCE\nJustification: silent")
    assert v.verdict == "INSUFFICIENT_EVIDENCE"


def test_parse_malformed_returns_none():
    assert parse_judge_response("MAYBE") is None
    assert parse_judge_response("") is None


# ---- tolerant judge parser (real-world Claude output shapes) ----


def test_parse_handles_markdown_bold_verdict():
    """Claude often wraps the verdict in ** for emphasis. Strip
    markdown markers and accept the verdict word."""
    v = parse_judge_response("**SUPPORTED**\nJustification: matches snippet 1")
    assert v.verdict == "SUPPORTED"
    assert "matches snippet 1" in v.justification


def test_parse_handles_verdict_label_prefix():
    """``Verdict: SUPPORTED`` is a common shape — the model
    interprets the prompt's "VERDICT" placeholder as a label."""
    v = parse_judge_response(
        "Verdict: SUPPORTED\nJustification: page confirms"
    )
    assert v.verdict == "SUPPORTED"


def test_parse_handles_markdown_heading_verdict():
    """``## SUPPORTED`` heading. Strip # and accept."""
    v = parse_judge_response("## SUPPORTED\n\nThe page confirms it.")
    assert v.verdict == "SUPPORTED"


def test_parse_handles_short_preamble():
    """Despite the prompt asking for "no preamble", Claude sometimes
    leads with a sentence. The verdict mid-text should still be
    found via the search-based parser."""
    v = parse_judge_response(
        "Based on the snippets, the verdict is SUPPORTED.\n"
        "Justification: The Marie Curie page mentions both Nobels."
    )
    assert v.verdict == "SUPPORTED"


def test_parse_negation_flips_supported_to_contradicted():
    """``NOT SUPPORTED`` should parse as CONTRADICTED, not SUPPORTED.
    Without negation handling the search-based parser would mis-read
    the literal word."""
    v = parse_judge_response(
        "The claim is NOT SUPPORTED.\nJustification: opposite is true."
    )
    assert v.verdict == "CONTRADICTED"


def test_parse_negation_flips_contradicted_to_supported():
    """Symmetric — ``not contradicted`` reads as SUPPORTED."""
    v = parse_judge_response(
        "The evidence is not CONTRADICTED here.\nJustification: aligned."
    )
    assert v.verdict == "SUPPORTED"


def test_parse_lowercase_verdict():
    """Case-insensitive — ``supported`` works too."""
    v = parse_judge_response("supported\nJustification: ok")
    assert v.verdict == "SUPPORTED"


def test_parse_first_verdict_wins():
    """If multiple verdicts appear, the earliest one is chosen.
    The judge sometimes lists alternatives ('not CONTRADICTED but
    SUPPORTED') — first wins ensures determinism."""
    v = parse_judge_response(
        "INSUFFICIENT_EVIDENCE\nJustification: could be SUPPORTED or "
        "CONTRADICTED but the snippets don't say."
    )
    assert v.verdict == "INSUFFICIENT_EVIDENCE"


def test_parse_word_boundary_no_partial_match():
    """``SUPPORTED`` must match as a whole word. Random text like
    ``the support team`` should not trigger SUPPORTED via 'support'
    being substring; the alias map has both SUPPORT and SUPPORTED so
    'support' WOULD match — but only as a whole word, not inside
    'unsupported'."""
    # 'unsupported' contains 'supported' as a substring but not as a
    # whole word. The parser should NOT match.
    assert parse_judge_response(
        "The claim is unsupported by anything.\nJustification: x"
    ) is None
    # Bare "support" IS a word and IS aliased to SUPPORTED — that's
    # the intended forgiving behavior.
    v = parse_judge_response("I support this.\nJustification: yes")
    assert v.verdict == "SUPPORTED"


def test_parse_accepts_abbreviations():
    """Phase-2 dogfood (Tokyo→Edo) surfaced the judge LLM emitting
    'SUPPORT' instead of the prompted 'SUPPORTED'. Accept the common
    abbreviated forms as aliases for their canonical labels."""
    cases = [
        ("SUPPORT\nJustification: clear", "SUPPORTED"),
        ("Supports\nJustification: yep", "SUPPORTED"),
        ("CONTRADICT\nJustification: nope", "CONTRADICTED"),
        ("Contradicts\nJustification: explicitly", "CONTRADICTED"),
        ("INSUFFICIENT\nJustification: thin", "INSUFFICIENT_EVIDENCE"),
        ("INCONCLUSIVE\nJustification: ambiguous", "INSUFFICIENT_EVIDENCE"),
        ("UNCLEAR\nJustification: meh", "INSUFFICIENT_EVIDENCE"),
    ]
    for raw, expected in cases:
        v = parse_judge_response(raw)
        assert v is not None, f"parser refused {raw!r}"
        assert v.verdict == expected, (
            f"{raw!r} produced {v.verdict!r}, expected {expected!r}"
        )


# ---------- build_queries ----------


def test_build_queries_role_assignment_three_attempts():
    """Spec: queries try most-specific to least-specific, in order."""
    reg = load_default_registry()
    queries = build_queries(
        reg.get("role_assignment"),
        {"agent": "Donald Trump", "role": "47th President", "org": "United States"},
    )
    assert queries == [
        "Donald Trump 47th President",
        "Donald Trump United States 47th President",
        "Donald Trump",
    ]


def test_build_queries_skips_template_with_missing_slot():
    """If 'org' is missing, the {agent} {org} {role} template is skipped."""
    reg = load_default_registry()
    queries = build_queries(
        reg.get("role_assignment"),
        {"agent": "Tim Cook", "role": "CEO"},
    )
    # Skipped: "{agent} {org} {role}". Remaining: "{agent} {role}", "{agent}".
    assert queries == ["Tim Cook CEO", "Tim Cook"]


def test_build_queries_never_prepends_current():
    """Spec: 'do not prepend current' to any query."""
    reg = load_default_registry()
    queries = build_queries(
        reg.get("role_assignment"),
        {"agent": "Donald Trump", "role": "47th President", "org": "United States"},
    )
    for q in queries:
        assert "current" not in q.lower(), q


def test_build_queries_event_handles_participant_list():
    reg = load_default_registry()
    queries = build_queries(
        reg.get("event"),
        {"event_type": "inauguration", "participants": ["Donald Trump"]},
    )
    assert any("inauguration" in q and "Donald Trump" in q for q in queries)


def test_build_queries_converts_snake_case_predicate_to_natural_language():
    """AEDOS-internal snake_case predicates (parent_of, founded_by,
    presidential_campaign, etc.) get converted to space-separated form
    in the query templates so search engines rank correctly. Without
    this, queries like 'Donald Trump parent_of Donald Jr.' return
    junk; with it, 'Donald Trump parent of Donald Jr.' lands the
    Donald Trump Jr. Wikipedia page.
    """
    reg = load_default_registry()
    queries = build_queries(
        reg.get("relational"),
        {
            "subject": "Donald Trump",
            "relation": "parent_of",  # snake_case predicate
            "object": "Donald Jr.",
        },
    )
    # First template is "{subject} {relation} {object}" — must use
    # natural-language form of relation.
    assert "Donald Trump parent of Donald Jr." in queries
    # No raw snake_case in any query.
    for q in queries:
        assert "parent_of" not in q, q


def test_build_queries_event_type_converted_to_natural_language():
    """event_type slot values are also AEDOS internal IDs
    (presidential_campaign, etc.) — same conversion applies."""
    reg = load_default_registry()
    queries = build_queries(
        reg.get("event"),
        {
            "event_type": "presidential_campaign",
            "participants": ["Donald Trump"],
            "occurred_at": "2024",
        },
    )
    # Templates: {event_type} {participants_joined} → {event_type}
    # {occurred_at} → {event_type}. All should use space-separated form.
    for q in queries:
        assert "presidential_campaign" not in q, q
        assert "presidential campaign" in q.lower(), q


def test_build_queries_natural_lang_conversion_is_query_only():
    """The judge sees the original (raw) slot values; only the query
    templates get the natural-language form. _enrich_slots returns a
    NEW dict — the input slots dict is not mutated."""
    from src.verifiers.retrieval_verifier import _enrich_slots
    original = {
        "subject": "Trump Organization",
        "relation": "founded_by",
        "object": "Donald Trump",
    }
    snapshot = dict(original)
    enriched = _enrich_slots(original)
    # Input not mutated.
    assert original == snapshot
    # Enriched has space-form.
    assert enriched["relation"] == "founded by"
    # Other slots untouched.
    assert enriched["subject"] == "Trump Organization"


# ---------- multi-attempt strategy ----------


def test_first_attempt_with_two_results_is_used(store):
    """If attempt 1 returns >= 2 results, attempt 2 should not run."""
    call_log: list[str] = []

    def fake_search(q):
        call_log.append(q)
        # First query returns 2 results, second should never be called
        return [
            Snippet("t1", "sn1", "u1"),
            Snippet("t2", "sn2", "u2"),
        ]

    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJ: ok"])
    v = _verifier(store, llm, fake_search)
    r = v.verify(_claim())
    assert call_log == ["Donald Trump 47th President"]
    assert r.outcome is VerificationOutcome.VERIFIED
    used = [a for a in r.attempts if a.used]
    assert len(used) == 1
    assert used[0].query == "Donald Trump 47th President"


def test_falls_through_to_next_attempt_when_first_returns_zero(store):
    """Section 9 #5: attempt 1 returns 0; attempt 2 returns 3; attempt 2 wins."""
    call_log: list[str] = []
    results = {
        "Donald Trump 47th President": [],
        "Donald Trump United States 47th President": [
            Snippet("t1", "sn1", "u1"),
            Snippet("t2", "sn2", "u2"),
            Snippet("t3", "sn3", "u3"),
        ],
    }

    def fake_search(q):
        call_log.append(q)
        return results.get(q, [])

    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJ: snippet 1"])
    v = _verifier(store, llm, fake_search)
    r = v.verify(_claim())
    assert call_log == [
        "Donald Trump 47th President",
        "Donald Trump United States 47th President",
    ]
    assert r.outcome is VerificationOutcome.VERIFIED
    # Trace records BOTH attempts; second one is .used=True.
    assert len(r.attempts) == 2
    assert r.attempts[0].used is False
    assert r.attempts[0].result_count == 0
    assert r.attempts[1].used is True
    assert r.attempts[1].result_count == 3


def test_falls_through_when_first_returns_only_one_result(store):
    """A single result isn't enough — < 2 → continue."""
    call_log: list[str] = []
    results = {
        "Donald Trump 47th President": [Snippet("t", "s", "u")],
        "Donald Trump United States 47th President": [
            Snippet("t1", "s1", "u1"),
            Snippet("t2", "s2", "u2"),
        ],
    }

    def fake_search(q):
        call_log.append(q)
        return results.get(q, [])

    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJ: ok"])
    v = _verifier(store, llm, fake_search)
    r = v.verify(_claim())
    assert len(call_log) == 2
    assert r.attempts[0].result_count == 1
    assert r.attempts[0].used is False


def test_all_attempts_return_zero_yields_no_results_flag(store):
    def fake_search(q):
        return []

    llm = FakeLLM()
    v = _verifier(store, llm, fake_search)
    r = v.verify(_claim())
    assert r.outcome is VerificationOutcome.INCONCLUSIVE
    assert r.error_flag == "no_results"


def test_network_error_on_one_attempt_continues_to_next(store):
    """An error on attempt 1 should not abort the strategy."""
    call_log: list[str] = []

    def fake_search(q):
        call_log.append(q)
        if q == "Donald Trump 47th President":
            raise httpx.ConnectError("flake")
        return [Snippet("t1", "s1", "u1"), Snippet("t2", "s2", "u2")]

    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJ: ok"])
    v = _verifier(store, llm, fake_search)
    r = v.verify(_claim())
    assert r.outcome is VerificationOutcome.VERIFIED
    assert r.attempts[0].error is not None
    assert r.attempts[1].used is True


# ---------- pipeline_events logging ----------


def test_attempts_logged_to_pipeline_events(store):
    """Section 5 spec: each attempt gets a retrieval_query_attempt event."""
    def fake_search(q):
        if q == "Donald Trump 47th President":
            return []
        return [Snippet("t1", "s1", "u1"), Snippet("t2", "s2", "u2")]

    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJ: ok"])
    v = _verifier(store, llm, fake_search)
    tid = store.insert_turn("assistant", "draft")
    v.verify(_claim(), source_turn_id=tid)
    events = store.get_pipeline_events(tid)
    attempts_logged = [e for e in events if e["stage"] == "retrieval_query_attempt"]
    assert len(attempts_logged) == 2
    assert attempts_logged[0]["data"]["query"] == "Donald Trump 47th President"
    assert attempts_logged[0]["data"]["result_count"] == 0


# ---------- current vs historical judge prompts ----------


def test_current_claim_uses_current_judge_prompt(store):
    """A claim with no valid_until uses the CURRENT judge prompt. Note
    that the CURRENT prompt now ALSO instructs the model to handle
    past-tense source text as a historical assertion (v0.7.6) so it
    references the word HISTORICAL inside its tense-awareness rules —
    the discriminator is the CURRENT-TENSE header, not the absence of
    'HISTORICAL'."""
    def fake_search(q):
        return [Snippet("t1", "s1", "u1"), Snippet("t2", "s2", "u2")]

    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJ: ok"])
    v = _verifier(store, llm, fake_search)
    v.verify(_claim())  # no valid_until
    sys = llm.rewrite_calls[0]["system"]
    assert "CURRENT-TENSE" in sys
    # The HISTORICAL prompt has its own distinct opening line; check
    # we didn't accidentally route to it.
    assert "HISTORICAL claim with an explicit time period" not in sys


def test_historical_claim_uses_historical_judge_prompt(store):
    """A claim with valid_until set should use the HISTORICAL prompt and pass dates."""
    def fake_search(q):
        return [Snippet("t1", "s1", "u1"), Snippet("t2", "s2", "u2")]

    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJ: ok"])
    v = _verifier(store, llm, fake_search)
    v.verify(_claim(slots={
        "agent": "Donald Trump", "role": "45th President", "org": "United States",
        "valid_from": "2017-01-20", "valid_until": "2021-01-20",
    }))
    sys = llm.rewrite_calls[0]["system"]
    user = llm.rewrite_calls[0]["user_message"]
    assert "HISTORICAL" in sys
    assert "2017-01-20" in user
    assert "2021-01-20" in user


# ---------- caching is per-query attempt ----------


def test_each_attempt_caches_independently(store):
    """Cache is keyed by the actual query string, so each attempt caches separately."""
    call_log: list[str] = []
    results = {
        "Donald Trump 47th President": [],
        "Donald Trump United States 47th President": [
            Snippet("t1", "s1", "u1"), Snippet("t2", "s2", "u2"),
        ],
    }

    def fake_search(q):
        call_log.append(q)
        return list(results.get(q, []))

    llm = FakeLLM(rewrite_responses=["SUPPORTED\nJ: ok", "SUPPORTED\nJ: ok"])
    v = _verifier(store, llm, fake_search)
    v.verify(_claim())
    v.verify(_claim())
    # First call: 2 search calls. Second call: ZERO (both cached).
    assert call_log == [
        "Donald Trump 47th President",
        "Donald Trump United States 47th President",
    ]


# ---------- failure modes ----------


def test_judge_parse_error(store):
    def fake_search(q):
        return [Snippet("t1", "s1", "u1"), Snippet("t2", "s2", "u2")]

    llm = FakeLLM(rewrite_responses=["uhh"])
    v = _verifier(store, llm, fake_search)
    r = v.verify(_claim())
    assert r.outcome is VerificationOutcome.INCONCLUSIVE
    assert r.error_flag == "judge_parse_error"


def test_judge_call_failure_returns_judge_error(store):
    @dataclass
    class CrashLLM:
        def rewrite(self, *args, **kwargs):
            raise RuntimeError("boom")

    def fake_search(q):
        return [Snippet("t1", "s1", "u1"), Snippet("t2", "s2", "u2")]

    v = RetrievalVerifier(
        store=store, llm=CrashLLM(), registry=load_default_registry(),
        search_fn=fake_search, ttl_hours=1,
    )
    r = v.verify(_claim())
    assert r.outcome is VerificationOutcome.INCONCLUSIVE
    assert r.error_flag == "judge_error"


def test_no_query_constructible_for_pattern(store):
    """If no template can be filled, return early with a specific error_flag."""
    def fake_search(q):
        return []

    llm = FakeLLM()
    v = _verifier(store, llm, fake_search)
    # role_assignment requires agent + role; both missing.
    r = v.verify(_claim(slots={}))
    # This will actually fail at extractor's required-slot validation in the
    # real pipeline; but the verifier itself must also be defensive.
    assert r.outcome is VerificationOutcome.INCONCLUSIVE
    assert r.error_flag == "no_query_constructible"


# ---------- real API gated test ----------


@pytest.mark.skipif(
    os.getenv("RUN_API_TESTS") != "1",
    reason="real network + LLM gated behind RUN_API_TESTS=1",
)
def test_real_retrieval_marie_curie(tmp_path):
    from src.llm_client import LLMClient

    s = FactStore(tmp_path / "real.db")
    try:
        v = RetrievalVerifier(
            store=s, llm=LLMClient(), registry=load_default_registry(), ttl_hours=1
        )
        r = v.verify({
            "pattern": "categorical",
            "predicate": "is_a",
            "slots": {"entity": "Marie Curie", "category": "physicist"},
            "polarity": 1,
            "source_text": "Marie Curie was a physicist",
        })
        assert r.outcome is not VerificationOutcome.CONTRADICTED, r.to_dict()
    finally:
        s.close()
