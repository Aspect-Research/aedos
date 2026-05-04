"""Tests for comparative-claim detection + query construction (v0.7.9).

Covers the structural decomposer (predicate paths + source-text
backstop), the query template generator, and the integration into
RetrievalVerifier (prepend templates + retry-on-inconclusive)."""

from __future__ import annotations

import pytest

from src.fact_store import FactStore
from src.pattern_registry import PatternRegistry
from src.verifiers.comparative import (
    comparative_queries,
    detect_comparative,
)
from src.verifiers.retrieval_verifier import (
    RetrievalVerifier,
    Snippet,
)


@pytest.fixture
def store(tmp_path):
    s = FactStore(tmp_path / "v.db")
    yield s
    s.close()


# ============================================================
# detect_comparative — predicate path
# ============================================================


def test_detect_decomposes_had_superlative_predicate():
    """`had_heaviest_losses` predicate decomposes to
    superlative=heaviest, measure=losses."""
    claim = {
        "pattern": "relational",
        "predicate": "had_heaviest_losses",
        "slots": {
            "subject": "Soviet Union",
            "relation": "had_heaviest_losses",
            "object": "World War II",
        },
        "polarity": 1,
        "source_text": "the Soviet Union's losses by far the heaviest of any nation in WWII",
    }
    c = detect_comparative(claim)
    assert c is not None
    assert c.subject == "Soviet Union"
    assert c.domain == "World War II"
    assert c.superlative == "heaviest"
    assert c.measure == "losses"


def test_detect_decomposes_has_the_largest_predicate():
    claim = {
        "pattern": "quantitative",
        "predicate": "has_the_largest_population",
        "slots": {"subject": "China", "object": "world"},
        "polarity": 1,
        "source_text": "China has the largest population in the world",
    }
    c = detect_comparative(claim)
    assert c is not None
    assert c.superlative == "largest"
    assert c.measure == "population"


def test_detect_decomposes_is_the_most_predicate():
    claim = {
        "pattern": "categorical",
        "predicate": "is_the_most_populous_country",
        "slots": {"entity": "India", "category": "populous_country"},
        "polarity": 1,
        "source_text": "India is the most populous country",
    }
    c = detect_comparative(claim)
    assert c is not None
    assert c.superlative == "most"


# ============================================================
# detect_comparative — source-text backstop
# ============================================================


def test_detect_uses_source_text_when_predicate_lacks_marker():
    """If the predicate doesn't carry the superlative but the source
    text does, the backstop catches it."""
    claim = {
        "pattern": "relational",
        "predicate": "casualty_count_in",
        "slots": {
            "subject": "Soviet Union",
            "relation": "casualty_count_in",
            "object": "World War II",
        },
        "polarity": 1,
        "source_text": "the Soviet Union had the most casualties of any nation in WWII",
    }
    c = detect_comparative(claim)
    assert c is not None
    assert c.superlative == "most"


# ============================================================
# detect_comparative — negative cases
# ============================================================


def test_detect_returns_none_for_non_comparative_claim():
    claim = {
        "pattern": "categorical",
        "predicate": "is_a",
        "slots": {"entity": "Tokyo", "category": "city"},
        "polarity": 1,
        "source_text": "Tokyo is a city",
    }
    assert detect_comparative(claim) is None


def test_detect_returns_none_when_subject_missing():
    """No subject slot → can't form a query → no comparative routing."""
    claim = {
        "pattern": "relational",
        "predicate": "had_heaviest_losses",
        "slots": {"object": "World War II"},
        "polarity": 1,
        "source_text": "the heaviest losses of any nation",
    }
    assert detect_comparative(claim) is None


def test_detect_returns_none_when_domain_missing():
    """No object/domain slot → no comparison universe → skip."""
    claim = {
        "pattern": "relational",
        "predicate": "had_heaviest_losses",
        "slots": {"subject": "Soviet Union"},
        "polarity": 1,
        "source_text": "the heaviest losses",
    }
    assert detect_comparative(claim) is None


# ============================================================
# comparative_queries — template output
# ============================================================


def test_comparative_queries_lead_with_ranking_pages():
    from src.verifiers.comparative import ComparativeClaim
    c = ComparativeClaim(
        superlative="heaviest", measure="casualties",
        domain="World War II", subject="Soviet Union",
    )
    qs = comparative_queries(c)
    # First query should be a "list of …" page (Wikipedia convention).
    assert qs[0].startswith("list of")
    # Ranking + by-country variants present.
    assert any("by country" in q for q in qs)
    # Subject-anchored fallback present so we always have at least
    # one query that mentions the subject.
    assert any("Soviet Union" in q for q in qs)
    # No duplicates.
    assert len(qs) == len(set(qs))


def test_comparative_queries_dedupes():
    from src.verifiers.comparative import ComparativeClaim
    # Pathological case: short measure + identical to domain.
    c = ComparativeClaim(superlative="largest", measure="x", domain="x", subject="A")
    qs = comparative_queries(c)
    assert len(qs) == len(set(qs))


# ============================================================
# Integration: RetrievalVerifier prepends comparative queries
# ============================================================


class _StubLLM:
    """LLM stub that returns canned judge responses in order."""
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.rewrite_calls: list[dict] = []

    def rewrite(self, system, user_message, **kwargs):
        self.rewrite_calls.append({"system": system, "user_message": user_message})
        if not self._responses:
            raise RuntimeError("ran out of canned responses")
        return self._responses.pop(0)


def _verifier_with_search(store, llm, search_results_by_query):
    """Build a verifier whose search_fn returns canned snippets per
    query string. Also returns the list of queries actually issued."""
    issued_queries: list[str] = []

    def fake_search(q):
        issued_queries.append(q)
        return search_results_by_query.get(q, [])

    return (
        RetrievalVerifier(
            store=store, llm=llm,
            registry=PatternRegistry.from_yaml("patterns.yaml"),
            search_fn=fake_search,
        ),
        issued_queries,
    )


def test_comparative_claim_prepends_comparative_queries(store):
    """Detection fires → comparative templates run BEFORE the standard
    relational queries."""
    snips = [Snippet("Casualties WWII", "USSR 27 million dead", "u1"),
             Snippet("List of WWII casualties", "USSR top of the list", "u2")]
    sr = {"list of World War II losses": snips}
    llm = _StubLLM(["SUPPORTED\nJustification: top of the ranking"])
    v, issued = _verifier_with_search(store, llm, sr)

    claim = {
        "pattern": "relational",
        "predicate": "had_heaviest_losses",
        "slots": {
            "subject": "Soviet Union",
            "relation": "had_heaviest_losses",
            "object": "World War II",
        },
        "polarity": 1,
        "source_text": "the Soviet Union's losses by far the heaviest of any nation in WWII",
    }
    result = v.verify(claim)
    assert result.outcome.value == "verified"
    # First query issued should be the comparative "list of …" template.
    assert issued[0].startswith("list of")


def test_non_comparative_claim_uses_standard_queries(store):
    """No detection fires → only standard pattern queries run, in
    their original order. Backwards compat."""
    sr = {
        "Donald Trump capital of United States": [
            Snippet("t1", "s1", "u1"), Snippet("t2", "s2", "u2"),
        ],
    }
    llm = _StubLLM(["SUPPORTED\nJ: ok"])
    v, issued = _verifier_with_search(store, llm, sr)

    claim = {
        "pattern": "relational",
        "predicate": "capital_of",
        "slots": {
            "subject": "Donald Trump",
            "relation": "capital_of",
            "object": "United States",
        },
        "polarity": 1,
        "source_text": "Donald Trump capital of United States",
    }
    v.verify(claim)
    # First query is the standard "{subject} {relation} {object}".
    # Verifier converts snake_case slot values to natural language for the query.
    assert issued[0] == "Donald Trump capital of United States"


def test_comparative_retries_on_inconclusive(store):
    """First viable judge pass returns INSUFFICIENT_EVIDENCE → verifier
    advances to the next viable attempt for comparative claims."""
    snips_a = [Snippet("Battle 1", "tangential", "u1"), Snippet("Battle 2", "tangential", "u2")]
    snips_b = [Snippet("List", "USSR top", "u3"), Snippet("Ranking", "USSR top", "u4")]
    sr = {
        "list of World War II losses": snips_a,        # first comparative — judge says insufficient
        "losses of World War II by country": snips_b,  # second comparative — judge says supported
    }
    llm = _StubLLM([
        "INSUFFICIENT_EVIDENCE\nJ: tangential",
        "SUPPORTED\nJ: USSR is top of the list",
    ])
    v, issued = _verifier_with_search(store, llm, sr)

    claim = {
        "pattern": "relational",
        "predicate": "had_heaviest_losses",
        "slots": {
            "subject": "Soviet Union",
            "relation": "had_heaviest_losses",
            "object": "World War II",
        },
        "polarity": 1,
        "source_text": "the Soviet Union's losses by far the heaviest of any nation in WWII",
    }
    result = v.verify(claim)
    assert result.outcome.value == "verified"
    # Both queries were tried → judge ran twice.
    assert len(llm.rewrite_calls) == 2


def test_non_comparative_now_retries_on_inconclusive(store):
    """v0.12.x (Phase 2a): non-comparative claims also walk the strategy
    list when the judge returns INCONCLUSIVE. Was previously single-shot
    and reserved for comparative-only retry; now applies to all claims
    because Phase-1 router changes pushed more medical/encyclopedic-
    fuzzy claims into retrieval, where second-attempt reformulation
    often lands the right snippets."""
    snips_a = [Snippet("a", "x", "u1"), Snippet("b", "y", "u2")]
    snips_b = [Snippet("c", "z", "u3"), Snippet("d", "w", "u4")]
    sr = {
        # Standard relational templates — both produce ≥ 2 results.
        "Donald Trump capital of United States": snips_a,
        "Donald Trump United States": snips_b,
        "Donald Trump": snips_b,
    }
    # Three INCONCLUSIVE responses since the verifier now walks all
    # three viable attempts up to the retry cap. Plus one EMPTY
    # response for the Phase 2b reformulation hop (empty → no
    # additional judge call).
    llm = _StubLLM([
        "INSUFFICIENT_EVIDENCE\nJ: tangential",
        "INSUFFICIENT_EVIDENCE\nJ: still tangential",
        "INSUFFICIENT_EVIDENCE\nJ: nope",
        "",
    ])
    v, issued = _verifier_with_search(store, llm, sr)

    claim = {
        "pattern": "relational",
        "predicate": "capital_of",
        "slots": {
            "subject": "Donald Trump",
            "relation": "capital_of",
            "object": "United States",
        },
        "polarity": 1,
        "source_text": "Donald Trump capital of United States",
    }
    result = v.verify(claim)
    assert result.outcome.value == "inconclusive"
    # Three viable judges (capped at MAX_JUDGE_RETRIES) + one
    # reformulation call = 4. Empty reformulation skipped the extra
    # judge.
    assert len(llm.rewrite_calls) == 4


def test_comparative_detected_event_emitted(store):
    """When detection fires AND a turn_id is provided, a
    `comparative_detected` pipeline event lands in the store."""
    snips = [Snippet("a", "x", "u1"), Snippet("b", "y", "u2")]
    sr = {
        "list of World War II losses": snips,
    }
    llm = _StubLLM(["SUPPORTED\nJ: ok"])
    v, _ = _verifier_with_search(store, llm, sr)

    turn_id = store.insert_turn("assistant", "")
    claim = {
        "pattern": "relational",
        "predicate": "had_heaviest_losses",
        "slots": {
            "subject": "Soviet Union",
            "relation": "had_heaviest_losses",
            "object": "World War II",
        },
        "polarity": 1,
        "source_text": "the Soviet Union's losses by far the heaviest of any nation in WWII",
    }
    v.verify(claim, source_turn_id=turn_id)
    events = store.get_pipeline_events(turn_id)
    detected = [e for e in events if e["stage"] == "comparative_detected"]
    assert len(detected) == 1
    data = detected[0]["data"]
    assert data["superlative"] == "heaviest"
    assert data["subject"] == "Soviet Union"


def test_judge_retry_event_emitted_on_inconclusive_advance(store):
    """When the verifier steps from one viable attempt to the next
    after an inconclusive verdict, a `judge_retry_after_inconclusive`
    event lands."""
    snips_a = [Snippet("a", "x", "u1"), Snippet("b", "y", "u2")]
    snips_b = [Snippet("c", "z", "u3"), Snippet("d", "w", "u4")]
    sr = {
        "list of World War II losses": snips_a,
        "losses of World War II by country": snips_b,
    }
    llm = _StubLLM([
        "INSUFFICIENT_EVIDENCE\nJ: tangential",
        "SUPPORTED\nJ: USSR top",
    ])
    v, _ = _verifier_with_search(store, llm, sr)

    turn_id = store.insert_turn("assistant", "")
    claim = {
        "pattern": "relational",
        "predicate": "had_heaviest_losses",
        "slots": {
            "subject": "Soviet Union",
            "relation": "had_heaviest_losses",
            "object": "World War II",
        },
        "polarity": 1,
        "source_text": "the Soviet Union's losses by far the heaviest of any nation in WWII",
    }
    v.verify(claim, source_turn_id=turn_id)
    events = store.get_pipeline_events(turn_id)
    retries = [e for e in events if e["stage"] == "judge_retry_after_inconclusive"]
    assert len(retries) == 1
    assert retries[0]["data"]["verdict"] == "INSUFFICIENT_EVIDENCE"
