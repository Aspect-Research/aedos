"""v0.16.1 WS3 Step 1: traced compound-statement rollup
(`Aggregator.compose_statement_verdict`).

The monotone AND conjunction that collapsed a compound statement ("X and Y")
into one verdict used to live as an inline boolean in the benchmark runner
(`_strip_chain_flag` + contradicted-wins / verified-iff-all / else-abstain).
WS3 Step 1 MOVES it into the aggregator as a real TRACED operation:

  * verdict-NEUTRAL — bit-for-bit identical to the old inline boolean over the
    full six-way verdict set (each conjunct's dual designation
    `*_given_assertion` collapses to its base first, exactly as
    `_strip_chain_flag` did);
  * the rollup now carries a statement-level `JustificationTrace` whose
    `ProvenanceTerm` is an `op="and"` node AND-composing the per-claim
    sub-traces (the op="and" path that existed in trace.py but was never
    constructed), so it carries a real retraction footprint (the union of the
    conjuncts' source rows) instead of just a boolean.

These tests pin the verdict equivalence (the headline cases the spec lists,
plus an exhaustive sweep over n=1,2 to prove bit-for-bit equality with the old
boolean), the op="and" provenance composition, and that the benchmark runner
routes through the aggregator method with unchanged metrics on a mock result
set.
"""

from __future__ import annotations

import itertools

import pytest

from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer4_sources.walker import BudgetConsumption, WalkResult
from aedos.layer5_result.aggregator import (
    ALL_VERDICTS,
    Aggregator,
    StatementVerdict,
    base_verdict_of,
)
from aedos.layer5_result.trace import (
    JustificationTrace,
    ProvenanceLiteral,
    ProvenanceTerm,
    TraceNode,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _walk_result(verdict: str, *, prov_literal: ProvenanceLiteral | None = None) -> WalkResult:
    """A WalkResult carrying `verdict` and a per-claim trace. When
    `prov_literal` is given, the trace's provenance term wraps it (so the
    AND-rollup has a real child to compose and a real source row to union)."""
    trace = JustificationTrace(root=TraceNode("claim"))
    if prov_literal is not None:
        trace.provenance.add_alternative(ProvenanceTerm.lit(prov_literal))
    return WalkResult(
        verdict=verdict,
        trace=trace,
        budget_consumption=BudgetConsumption(wall_clock_ms=10.0, llm_calls=1),
    )


def _old_inline_rollup(verdicts: list[str]) -> str:
    """The ORIGINAL inline boolean the benchmark runner applied, reconstructed
    here as the oracle. It strips each conjunct's chain flag to its base
    verdict, then: empty -> abstain; single -> that base; any contradicted ->
    contradicted; all verified -> verified; else -> no_grounding_found.

    This is the exact pre-WS3 semantics the aggregator must reproduce."""
    bases = [base_verdict_of(v) for v in verdicts]
    if not bases:
        return "no_grounding_found"
    if len(bases) == 1:
        return bases[0]
    if "contradicted" in bases:
        return "contradicted"
    if all(b == "verified" for b in bases):
        return "verified"
    return "no_grounding_found"


# ---------------------------------------------------------------------------
# Headline verdict-equivalence cases (the ones the spec enumerates)
# ---------------------------------------------------------------------------

class TestCompoundRollupHeadlineCases:
    def test_verified_and_verified_is_verified(self):
        agg = Aggregator()
        sv = agg.compose_statement_verdict(
            [_walk_result("verified"), _walk_result("verified")]
        )
        assert isinstance(sv, StatementVerdict)
        assert sv.verdict == "verified"

    def test_verified_and_contradicted_is_contradicted(self):
        agg = Aggregator()
        sv = agg.compose_statement_verdict(
            [_walk_result("verified"), _walk_result("contradicted")]
        )
        assert sv.verdict == "contradicted"

    def test_verified_and_abstain_is_abstain(self):
        agg = Aggregator()
        sv = agg.compose_statement_verdict(
            [_walk_result("verified"), _walk_result("no_grounding_found")]
        )
        assert sv.verdict == "no_grounding_found"

    def test_single_claim_passes_through(self):
        agg = Aggregator()
        for v in ("verified", "contradicted", "no_grounding_found"):
            sv = agg.compose_statement_verdict([_walk_result(v)])
            assert sv.verdict == v, f"single {v!r} should pass through"

    def test_single_dual_designation_collapses_to_base(self):
        # A lone `verified_given_assertion` collapses to its base `verified`,
        # exactly as the old `_strip_chain_flag` did.
        agg = Aggregator()
        sv = agg.compose_statement_verdict([_walk_result("verified_given_assertion")])
        assert sv.verdict == "verified"

    def test_empty_is_abstain(self):
        agg = Aggregator()
        sv = agg.compose_statement_verdict([])
        assert sv.verdict == "no_grounding_found"

    def test_contradicted_wins_over_abstain(self):
        agg = Aggregator()
        sv = agg.compose_statement_verdict(
            [_walk_result("no_grounding_found"), _walk_result("contradicted")]
        )
        assert sv.verdict == "contradicted"


# ---------------------------------------------------------------------------
# Exhaustive verdict equivalence vs the old inline boolean
# ---------------------------------------------------------------------------

class TestCompoundRollupEquivalence:
    def test_matches_old_boolean_for_n1_and_n2(self):
        """Over the FULL six-way verdict set, the aggregator's composed verdict
        equals the old inline boolean for every combination of one and two
        conjuncts — zero mismatches. Proves the move is bit-for-bit
        verdict-neutral (including the dual-designation collapse)."""
        agg = Aggregator()
        mismatches = []
        # n = 1 and n = 2 cover the single-passthrough branch and the
        # multi-conjunct conjunction branch (contradicted-wins / all-verified /
        # else-abstain); the implementation report extends this to n=3 (258
        # combos) — n<=2 is the load-bearing coverage at the test layer.
        for n in (1, 2):
            for combo in itertools.product(ALL_VERDICTS, repeat=n):
                results = [_walk_result(v) for v in combo]
                got = agg.compose_statement_verdict(results).verdict
                expected = _old_inline_rollup(list(combo))
                if got != expected:
                    mismatches.append((combo, got, expected))
        assert not mismatches, f"verdict drift vs old boolean: {mismatches[:10]}"

    def test_composed_verdict_is_always_base(self):
        # The rollup never emits a `*_given_assertion` verdict — it composes
        # base verdicts (matching the old boolean's post-strip output).
        agg = Aggregator()
        for combo in itertools.product(ALL_VERDICTS, repeat=2):
            sv = agg.compose_statement_verdict([_walk_result(v) for v in combo])
            assert sv.verdict in ("verified", "contradicted", "no_grounding_found")

    def test_per_claim_verdicts_recorded_as_bases(self):
        agg = Aggregator()
        sv = agg.compose_statement_verdict(
            [_walk_result("verified_given_assertion"), _walk_result("contradicted")]
        )
        # The conjunct base verdicts are recorded for observability.
        assert sv.per_claim_verdicts == ["verified", "contradicted"]


# ---------------------------------------------------------------------------
# AND-composed provenance term over the sub-traces
# ---------------------------------------------------------------------------

class TestCompoundRollupTrace:
    def test_provenance_term_is_and_over_subtraces(self):
        """The statement trace's provenance term is an op="and" node whose
        children are the per-claim conjunct provenance terms (the op="and" path
        that existed but was never constructed)."""
        agg = Aggregator()
        r1 = _walk_result(
            "verified",
            prov_literal=ProvenanceLiteral(source="kb", table="entity_resolution_cache", row_id=1),
        )
        r2 = _walk_result(
            "verified",
            prov_literal=ProvenanceLiteral(source="tier_u", table="tier_u", row_id=2),
        )
        sv = agg.compose_statement_verdict([r1, r2], source_text="X is a town and Y is a city")

        assert sv.trace.provenance.op == "and"
        assert len(sv.trace.provenance.children) == 2
        # The AND node unions the conjuncts' retraction footprints.
        rows = set(sv.trace.provenance.source_rows())
        assert ("entity_resolution_cache", 1) in rows
        assert ("tier_u", 2) in rows

    def test_and_term_unions_source_rows(self):
        # _extract_source_rows (the propagator feed) reads the term as the
        # single source of truth, so the rollup's retraction footprint is the
        # union of the conjuncts'.
        from aedos.layer5_result.aggregator import _extract_source_rows
        agg = Aggregator()
        r1 = _walk_result(
            "verified",
            prov_literal=ProvenanceLiteral(source="tier_u", table="tier_u", row_id=11),
        )
        r2 = _walk_result(
            "verified",
            prov_literal=ProvenanceLiteral(source="kb", table="entity_resolution_cache", row_id=22),
        )
        sv = agg.compose_statement_verdict([r1, r2])
        assert set(_extract_source_rows(sv.trace)) == {
            ("tier_u", 11), ("entity_resolution_cache", 22),
        }

    def test_includes_assertion_is_monotone_or(self):
        # A single assertion-conditional conjunct flags the whole rollup
        # (monotone-OR over the conjuncts' literals).
        agg = Aggregator()
        r1 = _walk_result(
            "verified",
            prov_literal=ProvenanceLiteral(source="kb", assertion=False),
        )
        r2 = _walk_result(
            "verified",
            prov_literal=ProvenanceLiteral(source="tier_u", assertion=True),
        )
        sv = agg.compose_statement_verdict([r1, r2])
        assert sv.trace.provenance.includes_assertion() is True
        assert sv.trace.chain_includes_assertion is True

    def test_source_text_recorded_in_walk_metadata(self):
        agg = Aggregator()
        sv = agg.compose_statement_verdict(
            [_walk_result("verified"), _walk_result("verified")],
            source_text="Paris is a city and France is a country",
        )
        assert sv.trace.walk_metadata.get("source_text") == (
            "Paris is a city and France is a country"
        )
        rollup = sv.trace.walk_metadata.get("rollup")
        assert rollup is not None
        assert rollup["op"] == "and"
        assert rollup["composed_verdict"] == "verified"

    def test_root_is_statement_node(self):
        agg = Aggregator()
        sv = agg.compose_statement_verdict(
            [_walk_result("verified")], source_text="X"
        )
        assert sv.trace.root.node_type == "statement"


# ---------------------------------------------------------------------------
# Benchmark wiring: AedosRunner routes through compose_statement_verdict
# ---------------------------------------------------------------------------

class _FakeExtractor:
    """Extracts one Claim per surface in `claims_for_text`, keyed on the
    statement text, so a compound statement yields multiple conjunct claims."""

    def __init__(self, n_claims: int):
        self._n = n_claims

    def extract(self, text, ctx):
        return [
            Claim(
                claim_id=f"c{i}",
                subject=f"S{i}",
                predicate="is",
                object="O",
                polarity=1,
                source_text=text,
                asserting_party="benchmark",
                triage_decision=TriageDecision.VERIFY,
            )
            for i in range(self._n)
        ]


class _FakeWalker:
    """Returns a pre-scripted verdict per claim (by claim_id order)."""

    def __init__(self, verdicts: list[str]):
        self._verdicts = verdicts
        self._i = 0

    def walk(self, claim, ctx):
        v = self._verdicts[self._i]
        self._i += 1
        return _walk_result(v)


class TestBenchmarkRunnerWiring:
    def _run(self, statement, conjunct_verdicts):
        from tests.evaluation.benchmark import AedosRunner, BenchmarkCase
        agg = Aggregator()
        runner = AedosRunner(
            pipeline=(
                _FakeExtractor(len(conjunct_verdicts)),
                _FakeWalker(conjunct_verdicts),
                agg,
            )
        )
        case = BenchmarkCase(
            case_id="t1",
            statement=statement,
            ground_truth="verified",
            failure_mode="cross_source_unification",
            notes="",
        )
        return runner.run_case(case)

    def test_runner_uses_aggregator_rollup_verified(self):
        # Two verified conjuncts -> the runner's verdict is the aggregator's
        # composed `verified` (not an inline boolean).
        result = self._run("A and B", ["verified", "verified"])
        assert result.verdict == "verified"

    def test_runner_rollup_contradicted_wins(self):
        result = self._run("A and B", ["verified", "contradicted"])
        assert result.verdict == "contradicted"

    def test_runner_rollup_abstains_on_mixed(self):
        result = self._run("A and B", ["verified", "no_grounding_found"])
        assert result.verdict == "no_grounding_found"

    def test_runner_verdict_matches_old_boolean_metrics(self):
        """The runner's verdict on a mock result set is exactly what the old
        inline boolean would have produced — so benchmark metrics are unchanged
        by the move to the aggregator. Swept over representative conjunct mixes."""
        mixes = [
            ["verified"],
            ["contradicted"],
            ["no_grounding_found"],
            ["verified", "verified"],
            ["verified", "contradicted"],
            ["verified", "no_grounding_found"],
            ["verified_given_assertion", "verified"],
            ["contradicted_given_assertion", "verified"],
            ["verified", "verified", "verified"],
            ["verified", "verified", "contradicted"],
        ]
        for mix in mixes:
            result = self._run("A and B", list(mix))
            assert result.verdict == _old_inline_rollup(mix), (
                f"runner verdict drift for {mix!r}: "
                f"got {result.verdict!r}, expected {_old_inline_rollup(mix)!r}"
            )

    def test_runner_still_calls_aggregate_for_side_effects(self):
        # The runner still calls aggregate(...) (its metadata/propagator
        # side-effects are preserved); compose_statement_verdict is the
        # ADDITIONAL traced rollup. We assert both are invoked.
        from tests.evaluation.benchmark import AedosRunner, BenchmarkCase
        from unittest.mock import MagicMock

        agg = Aggregator()
        agg.aggregate = MagicMock(wraps=agg.aggregate)
        agg.compose_statement_verdict = MagicMock(wraps=agg.compose_statement_verdict)
        runner = AedosRunner(
            pipeline=(_FakeExtractor(2), _FakeWalker(["verified", "verified"]), agg)
        )
        case = BenchmarkCase("t1", "A and B", "verified", "cross_source_unification", "")
        result = runner.run_case(case)
        assert result.verdict == "verified"
        agg.aggregate.assert_called_once()
        agg.compose_statement_verdict.assert_called_once()
        # The compose call received the per-claim walk results and the statement.
        _, kwargs = agg.compose_statement_verdict.call_args
        assert kwargs.get("source_text") == "A and B"
