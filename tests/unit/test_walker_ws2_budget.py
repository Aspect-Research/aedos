"""v0.16 WS2 §5: cost-bound regression for removing the depth==0 KB-neighbor cap.

The depth==0 cap on KB-neighbor enumeration was the D51 18-min / OOM blowup
guard. WS2 removes it to enable bidirectional/forward search, replacing it with
(a) `_verify_chain`'s admission-narrowing, (b) the bounded premise-forward
frontier, and (c) an explicit per-walk fanout budget sampled WITHIN the frontier
loop. This file pins (c): un-capping must NOT reintroduce the multiplicative
fanout blowup — the walker must abstain via the fanout budget rather than
exploding a single depth's frontier.

This is a SRC-soundness pin authored alongside the cap removal (the cost bound
must land together with the un-cap per the WS2 soundness guard); it lives in a
dedicated file so it does not collide with the composition test-agent's
walker-behavior files.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock
from datetime import datetime, timezone

from aedos.database import open_memory_db
from aedos.layer1_extraction.extractor import Claim
from aedos.layer1_extraction.triage import TriageDecision
from aedos.layer3_substrate import Substrate
from aedos.layer3_substrate.predicate_distribution import PredicateDistributionOracle
from aedos.layer3_substrate.predicate_translation import PredicateTranslation
from aedos.layer3_substrate.resolver import EntityResolver
from aedos.layer3_substrate.subsumption import SubsumptionOracle
from aedos.layer4_sources.kb_protocol import (
    ResolutionCandidate,
    SubsumptionResult,
    TransitivePathResult,
)
from aedos.layer4_sources.kb_verifier import KBVerdictType, KBVerdict, KBVerifier
from aedos.layer4_sources.python_verifier import PythonVerifier
from aedos.layer4_sources.tier_u import LookupResult, TierU
from aedos.layer4_sources.walker import VerificationContext, Walker, WalkerBudget
from aedos.llm.client import LLMClient


class _TierU:
    def lookup(self, claim, current_time=None, exclude_row_ids=None):
        return LookupResult(found=False)

    def lookup_object_conflict(self, claim, current_time=None):
        return LookupResult(found=False)


class _NoMatchKBVerifier:
    def verify(self, claim, current_time=None, source_text=None):
        return KBVerdict(verdict=KBVerdictType.NO_MATCH)


def _claim(subject="Asa", predicate="lives_in", object_val="X", polarity=1):
    return Claim(
        claim_id="c1", subject=subject, predicate=predicate, object=object_val,
        polarity=polarity, source_text="t", asserting_party="u",
        triage_decision=TriageDecision.VERIFY,
    )


def _ctx():
    return VerificationContext(
        current_time=datetime.now(timezone.utc).isoformat(), asserting_party="u"
    )


def _blowup_substrate():
    """Substrate that forces KB-neighbor enumeration at every node:
    find_neighbors empty (so the KB fallback always fires), distribution
    `both` (both directions enumerate), resolver always resolves to a Q-id."""
    pd = MagicMock()
    dv = MagicMock()
    dv.verdict.value = "both"
    dv.was_cached = True
    pd.consult.return_value = dv
    sub = MagicMock()
    sub.find_neighbors.return_value = []
    resolver = MagicMock()
    resolver.resolve.return_value = [ResolutionCandidate(kb_identifier="Q1", score=1.0)]
    # Realistic predicate metadata for the walker's oracle reads
    # (routing_hint / user_subject_required). A bare MagicMock would expose a
    # truthy `user_subject_required`, spuriously tripping the walker's
    # user_subject_required anomaly guard before KB enumeration runs.
    pt = MagicMock()
    pt_meta = MagicMock()
    pt_meta.routing_hint = "kb_resolvable"
    pt_meta.user_subject_required = False
    pt.consult.return_value = pt_meta
    return Substrate(
        resolver=resolver,
        predicate_translation=pt,
        subsumption=sub,
        predicate_distribution=pd,
    )


def _blowup_kb():
    """KB whose enumeration returns 20 DISTINCT fresh children per property
    every call — the D51 multiplicative-fanout worst case (the `visited` set
    cannot dedupe distinct Q-ids)."""
    counter = {"n": 0}
    kb = MagicMock()

    # v0.16.1 WS5c: CORE passes the opaque relation_type; resolve the P-id set
    # here (matching kb_wikidata._NEIGHBOR_PROPERTIES_BY_RELATION) so the
    # fanout worst case is preserved.
    _NEIGHBOR_PROPS = {"is_a": ("P31", "P279"), "part_of": ("P131", "P361", "P17")}

    def fake_enum(entity, properties=None, direction="outgoing", relation_type=None):
        props = tuple(properties) if properties else _NEIGHBOR_PROPS.get(relation_type, ())
        out = {}
        for p in props:
            ids = []
            for _ in range(20):
                counter["n"] += 1
                ids.append(f"Q{counter['n']}")
            out[p] = ids
        return out

    kb.enumerate_neighbors.side_effect = fake_enum
    kb.verify_transitive_path.return_value = TransitivePathResult(holds=True)
    return kb


class TestDepthCapRemovedDoesNotBlowBudget:
    def test_uncapped_enumeration_abstains_via_probe_budget(self):
        """Without the depth==0 cap, a substrate that returns no neighbors and a
        KB that returns 20 fresh children per property would fan out
        multiplicatively across depth (OOM in the worst case). The per-walk
        KB-neighbor PROBE budget (max_kb_neighbor_probes, default 48) catches it
        FIRST — tighter than the admitted-only fanout budget (max_frontier_
        expansions=2000), because the probe budget counts EVERY candidate probed
        (admitted or rejected), which is the real cost (one SPARQL ASK each). The
        walk abstains with `budget_kb_neighbor_probes` rather than exploding. (The
        fanout budget remains a backstop for the substrate/premise-forward
        expansion arms, which the probe counter does not gate.)"""
        walker = Walker(
            tier_u=_TierU(),
            kb_verifier=_NoMatchKBVerifier(),
            python_verifier=None,
            substrate=_blowup_substrate(),
            kb=_blowup_kb(),
            walker_max_depth=4,
        )
        budget = WalkerBudget(
            wall_clock_seconds=30.0, max_llm_calls=100, max_frontier_expansions=2000
        )
        t0 = time.monotonic()
        result = walker.walk(_claim(), _ctx(), budget=budget)
        elapsed = time.monotonic() - t0

        assert result.verdict == "no_grounding_found"
        assert result.abstention_reason == "budget_kb_neighbor_probes"
        assert result.trace.walk_metadata.get("budget_exceeded") == "kb_neighbor_probes"
        # The whole point: bounded, fast, no blowup. A real OOM/18-min blowup
        # would never reach this assertion; 5s is a generous ceiling for the
        # bounded path.
        assert elapsed < 5.0

    def test_probe_budget_dedupes_repeated_neighbor_qids(self):
        """The per-walk `seen` dedupe collapses re-probing of the SAME neighbor
        QID across slots/directions/depths (famous containers like Q30/Q142
        recur). A KB that returns the same small fixed set every call must NOT
        burn the probe budget on duplicates — the walk abstains fast and does
        NOT time out on the wall clock."""
        kb = MagicMock()
        _NEIGHBOR_PROPS = {"is_a": ("P31", "P279"), "part_of": ("P131", "P361", "P17")}

        def fixed_enum(entity, properties=None, direction="outgoing", relation_type=None):
            props = tuple(properties) if properties else _NEIGHBOR_PROPS.get(relation_type, ())
            # Same two QIDs every call — the dedupe must collapse them.
            return {p: ["Q30", "Q142"] for p in props}

        kb.enumerate_neighbors.side_effect = fixed_enum
        kb.verify_transitive_path.return_value = TransitivePathResult(holds=True)
        walker = Walker(
            tier_u=_TierU(),
            kb_verifier=_NoMatchKBVerifier(),
            python_verifier=None,
            substrate=_blowup_substrate(),
            kb=kb,
            walker_max_depth=4,
        )
        budget = WalkerBudget(wall_clock_seconds=30.0, max_llm_calls=100)
        t0 = time.monotonic()
        result = walker.walk(_claim(), _ctx(), budget=budget)
        elapsed = time.monotonic() - t0

        assert result.verdict == "no_grounding_found"
        assert result.abstention_reason != "budget_wall_clock"  # did not time out
        assert elapsed < 5.0

    def test_total_expansions_bounded_by_budget(self):
        """The walker stops accumulating candidates once cumulative discovery
        crosses the fanout budget — the frontier never grows unboundedly."""
        kb = _blowup_kb()
        walker = Walker(
            tier_u=_TierU(),
            kb_verifier=_NoMatchKBVerifier(),
            python_verifier=None,
            substrate=_blowup_substrate(),
            kb=kb,
            walker_max_depth=4,
        )
        budget = WalkerBudget(
            wall_clock_seconds=30.0, max_llm_calls=1000, max_frontier_expansions=500
        )
        walker.walk(_claim(), _ctx(), budget=budget)
        # Enumeration fires a bounded number of times — the fanout check breaks
        # the frontier loop well before the multiplicative explosion. (At
        # depth 0 a single node yields up to 2 slots x 2 relation_types x 2
        # directions x 5 props x 20 = 800 candidates, already over the 500
        # bound, so the walk abstains at depth 0 — a handful of enum calls.)
        assert kb.enumerate_neighbors.call_count < 100


# ---------------------------------------------------------------------------
# §3.2 gate->ranker soundness proof: a `neither` predicate is now EXPLORED
# (the distribution gate is demoted to a ranker) but its substitution is
# REJECTED at verify time, so the OUTCOME is the same as the old gate
# (no_grounding_found) — reached soundly, not by a discovery-time skip.
# ---------------------------------------------------------------------------

class _NeitherTransport:
    def extract_with_tool(self, *a, purpose=None, **kw):
        if purpose == "substrate:predicate_distribution":
            return {"verdict": "neither", "reason": "test"}
        if purpose == "substrate:subsumption":
            return {"verdict": "unrelated", "reason": "test"}
        return {
            "object_type": "entity", "user_subject_required": 0,
            "distinct_slots": None, "routing_hint": "kb_resolvable",
            "kb_namespace": "wikidata", "kb_property": "P39",
            "slot_to_qualifier": None, "single_valued": 0, "reason": "test",
        }

    def chat(self, *a, **kw):
        return ""


class _MockKB:
    def resolve_entity(self, r, lc):
        return []

    def lookup_statements(self, e, p):
        return []

    def subsumption(self, a, b, rt):
        return SubsumptionResult(verdict="unrelated")


def _seed_sub(db, a, b, relation_type, verdict="a_subsumed_by_b"):
    db.execute(
        """INSERT INTO subsumption
           (entity_a_namespace, entity_a_identifier, entity_b_namespace, entity_b_identifier,
            relation_type, verdict, source, reason, created_at)
           VALUES ('aedos', ?, 'aedos', ?, ?, ?, 'substrate', 'seed', '2026-01-01T00:00:00')""",
        (a, b, relation_type, verdict),
    )
    db.commit()


def _seed_dist(db, predicate, relation_type, verdict, polarity=1):
    db.execute(
        """INSERT INTO predicate_distribution
           (aedos_predicate, polarity, relation_type, verdict, reason, created_at)
           VALUES (?, ?, ?, ?, 'seed', '2026-01-01T00:00:00')""",
        (predicate, polarity, relation_type, verdict),
    )
    db.commit()


class TestNeitherExploredButRejected:
    def test_neither_is_a_explored_but_verify_rejects(self):
        """`prefers x is_a` has distribution verdict `neither`. The is_a edge
        golden_retriever is_a dog genuinely HOLDS structurally, so the OLD gate
        would have skipped the relation outright. Now the relation is EXPLORED
        (find_neighbors runs), the structural edge is real, but `_verify_chain`
        REJECTS the substitution because the kind-entailment authority
        (distribution=neither) says `prefers` does not transfer across is_a.
        OUTCOME: no_grounding_found, with NO surviving subsumption_traversal
        edge — proving soundness moved to verify time without false-verifying."""
        db = open_memory_db()
        client = LLMClient(_transport=_NeitherTransport())
        kb = _MockKB()
        pt = PredicateTranslation(db=db, llm_client=client)
        resolver = EntityResolver(kb_protocol=kb, db=db)
        sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
        pd = PredicateDistributionOracle(db=db, llm_client=client)
        substrate = Substrate(
            resolver=resolver, predicate_translation=pt,
            subsumption=sub, predicate_distribution=pd,
        )
        tier_u = TierU(db=db, predicate_translation=pt)
        kbv = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
        walker = Walker(
            tier_u=tier_u, kb_verifier=kbv,
            python_verifier=PythonVerifier(), substrate=substrate,
        )

        tier_u.write(
            Claim(claim_id="p", subject="Asa", predicate="prefers",
                  object="golden_retriever", polarity=1, source_text="t",
                  asserting_party="u", triage_decision=TriageDecision.VERIFY),
            status="externally_verified",
        )
        _seed_sub(db, "golden_retriever", "dog", "is_a")  # the edge DOES hold
        _seed_dist(db, "prefers", "is_a", "neither")  # but prefers does not distribute

        goal = Claim(
            claim_id="g", subject="Asa", predicate="prefers", object="dog",
            polarity=1, source_text="t", asserting_party="u",
            triage_decision=TriageDecision.VERIFY,
        )
        result = walker.walk(goal, _ctx())

        assert result.verdict == "no_grounding_found"
        # No substitution survived to a verdict — verify-time rejection, not a
        # gate skip. (Discovery may run; the admitted edge set is empty.)
        assert "subsumption_traversal" not in [
            e.edge_type for e in result.trace.edges
        ]
