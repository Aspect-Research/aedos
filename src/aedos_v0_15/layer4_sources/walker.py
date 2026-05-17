from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from ..layer1_extraction.extractor import Claim
from ..layer3_substrate import Substrate
from ..layer3_substrate.subsumption import EntityRef
from ..layer4_sources.kb_verifier import KBVerdictType
from ..layer4_sources.kb_protocol import LocalContext
from ..layer5_result.trace import JustificationTrace, TraceEdge, TraceNode


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class VerificationContext:
    current_time: str
    asserting_party: str


@dataclass
class WalkerBudget:
    wall_clock_seconds: float = 30.0
    max_llm_calls: int = 10


@dataclass
class BudgetConsumption:
    wall_clock_ms: float = 0.0
    llm_calls: int = 0


@dataclass
class WalkResult:
    verdict: str  # verified | contradicted | no_grounding_found
    trace: JustificationTrace
    abstention_reason: Optional[str] = None
    budget_consumption: BudgetConsumption = field(default_factory=BudgetConsumption)


class BudgetExceeded(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------

_DEFAULT_MAX_DEPTH = 4


def _claim_key(claim: Claim) -> str:
    return f"{claim.asserting_party}|{claim.subject}|{claim.predicate}|{claim.object}|{claim.polarity}"


def _claim_from_parts(
    template: Claim, subject: str = None, predicate: str = None, object_val: str = None, polarity: int = None
) -> Claim:
    """Return a modified copy of template claim with given overrides."""
    from ..layer1_extraction.triage import TriageDecision
    return Claim(
        claim_id=template.claim_id,
        subject=subject if subject is not None else template.subject,
        predicate=predicate if predicate is not None else template.predicate,
        object=object_val if object_val is not None else template.object,
        polarity=polarity if polarity is not None else template.polarity,
        source_text=template.source_text,
        asserting_party=template.asserting_party,
        triage_decision=TriageDecision.VERIFY,
        valid_from=template.valid_from,
        valid_until=template.valid_until,
        valid_during_ref=template.valid_during_ref,
    )


class Walker:
    def __init__(
        self,
        tier_u,
        kb_verifier,
        python_verifier,
        substrate: Substrate,
        audit_log=None,
        config: Optional[dict] = None,
    ) -> None:
        self._tier_u = tier_u
        self._kb_verifier = kb_verifier
        self._python_verifier = python_verifier
        self._substrate = substrate
        self._audit = audit_log
        self._config = config or {}
        self._max_depth = self._config.get("max_depth", _DEFAULT_MAX_DEPTH)

    def walk(
        self,
        claim: Claim,
        context: VerificationContext,
        budget: Optional[WalkerBudget] = None,
    ) -> WalkResult:
        if budget is None:
            budget = WalkerBudget()

        start_time = time.monotonic()
        llm_calls = 0
        root_node = TraceNode(node_type="claim", content={
            "subject": claim.subject, "predicate": claim.predicate,
            "object": claim.object, "polarity": claim.polarity,
        })
        trace = JustificationTrace(
            root=root_node,
            source_breakdown={"tier_u": 0, "kb": 0, "python": 0},
        )
        polarity_trace: list[int] = [claim.polarity]

        frontier: list[Claim] = [claim]
        visited: dict[str, Claim] = {}
        depth = 0
        current_verdict: Optional[str] = None

        while frontier and depth < self._max_depth:
            # Budget check
            elapsed = time.monotonic() - start_time
            if elapsed > budget.wall_clock_seconds:
                consumption = BudgetConsumption(wall_clock_ms=elapsed * 1000, llm_calls=llm_calls)
                trace.walk_metadata.update({"depth_reached": depth, "budget_exceeded": "wall_clock"})
                trace.polarity_trace = polarity_trace
                return WalkResult(
                    verdict="no_grounding_found",
                    trace=trace,
                    abstention_reason="budget_wall_clock",
                    budget_consumption=consumption,
                )
            if llm_calls >= budget.max_llm_calls:
                consumption = BudgetConsumption(wall_clock_ms=elapsed * 1000, llm_calls=llm_calls)
                trace.walk_metadata.update({"depth_reached": depth, "budget_exceeded": "llm_calls"})
                trace.polarity_trace = polarity_trace
                return WalkResult(
                    verdict="no_grounding_found",
                    trace=trace,
                    abstention_reason="budget_llm_calls",
                    budget_consumption=consumption,
                )

            next_frontier: list[Claim] = []
            for node in frontier:
                key = _claim_key(node)
                if key in visited:
                    continue
                visited[key] = node

                # Direct premise lookup
                verdict, lookup_source, llm_delta = self._direct_lookup(node, context, trace)
                llm_calls += llm_delta

                if verdict is not None:
                    # Handle conflicting verdicts
                    if current_verdict is None:
                        current_verdict = verdict
                    elif current_verdict != verdict:
                        # Conflict: contradicted wins
                        current_verdict = "contradicted"
                        trace.walk_metadata["conflict"] = True
                    # Terminal on contradiction; on verified keep walking for conflicts
                    if current_verdict == "contradicted":
                        break

                    # If verified, still add to trace but we'll return it below
                    if current_verdict == "verified":
                        break

                # Expand via substrate
                expanded, llm_delta = self._expand_via_substrate(node, trace, depth)
                llm_calls += llm_delta
                next_frontier.extend(expanded)

            if current_verdict in ("verified", "contradicted"):
                break

            frontier = next_frontier
            depth += 1

        elapsed = time.monotonic() - start_time
        consumption = BudgetConsumption(wall_clock_ms=elapsed * 1000, llm_calls=llm_calls)
        trace.walk_metadata.update({"depth_reached": depth, "llm_calls": llm_calls})
        trace.polarity_trace = polarity_trace

        if current_verdict is not None:
            return WalkResult(verdict=current_verdict, trace=trace, budget_consumption=consumption)

        return WalkResult(
            verdict="no_grounding_found",
            trace=trace,
            abstention_reason="depth_exhausted",
            budget_consumption=consumption,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _direct_lookup(
        self, node: Claim, context: VerificationContext, trace: JustificationTrace
    ) -> tuple[Optional[str], str, int]:
        """Returns (verdict_or_None, source, llm_calls_used)."""
        llm_delta = 0

        # Tier U lookup
        tier_u_result = self._tier_u.lookup(node, current_time=context.current_time)
        if tier_u_result.found:
            trace.source_breakdown["tier_u"] = trace.source_breakdown.get("tier_u", 0) + 1
            trace.edges.append(TraceEdge(
                edge_type="premise_lookup",
                source=trace.root,
                target=TraceNode("tier_u_row", {"subject": node.subject, "predicate": node.predicate}),
                metadata={"source": "tier_u", "polarity": node.polarity},
            ))
            return "verified" if node.polarity == 1 else "contradicted", "tier_u", 0
        if tier_u_result.historical_only:
            # Historical match means claim was true at some point, counts as partial evidence
            # but does NOT ground a present-tense claim → skip
            pass

        # KB verification
        if self._kb_verifier is not None:
            kb_result = self._kb_verifier.verify(node, current_time=context.current_time)
            if kb_result.verdict == KBVerdictType.VERIFIED:
                trace.source_breakdown["kb"] = trace.source_breakdown.get("kb", 0) + 1
                trace.edges.append(TraceEdge(
                    edge_type="premise_lookup",
                    source=trace.root,
                    target=TraceNode("kb_statement", {"entity": kb_result.subject_kb_id}),
                    metadata={"source": "kb", "verdict": "verified"},
                ))
                return "verified", "kb", 0
            elif kb_result.verdict == KBVerdictType.CONTRADICTED:
                trace.edges.append(TraceEdge(
                    edge_type="premise_lookup",
                    source=trace.root,
                    target=TraceNode("kb_statement", {"entity": kb_result.subject_kb_id}),
                    metadata={"source": "kb", "verdict": "contradicted"},
                ))
                return "contradicted", "kb", 0

        # Python verifier (stubbed until Phase 7)
        if self._python_verifier is not None:
            py_result = self._python_verifier.verify(node)
            if getattr(py_result, "terminal", False):
                trace.source_breakdown["python"] = trace.source_breakdown.get("python", 0) + 1
                return py_result.verdict, "python", 0

        return None, "", 0

    def _expand_via_substrate(
        self, node: Claim, trace: JustificationTrace, depth: int
    ) -> tuple[list[Claim], int]:
        """Expand node into equivalent/subsumed claims. Returns (new_nodes, llm_calls)."""
        expanded: list[Claim] = []
        llm_delta = 0

        # Predicate-equivalence substitution via predicate_translation neighbors
        try:
            neighbors = self._substrate.predicate_translation.query_neighbors(node.predicate)
            for meta in neighbors:
                if meta.retracted_at is not None:
                    continue
                new_node = _claim_from_parts(node, predicate=meta.aedos_predicate)
                trace.edges.append(TraceEdge(
                    edge_type="predicate_equivalence",
                    source=TraceNode("claim", {"predicate": node.predicate}),
                    target=TraceNode("claim", {"predicate": meta.aedos_predicate}),
                    metadata={"kb_property": meta.kb_property},
                ))
                expanded.append(new_node)
        except Exception:
            pass

        # Distribution-gated subsumption traversal
        for slot in ["subject", "object"]:
            slot_val = node.subject if slot == "subject" else node.object
            for relation_type in ["is_a", "part_of"]:
                try:
                    dist = self._substrate.predicate_distribution.consult(
                        node.predicate, node.polarity, relation_type
                    )
                    llm_delta += (0 if dist.was_cached else 1)
                except Exception:
                    continue

                if dist.verdict.value == "neither":
                    continue  # gate closed

                # Find subsumption neighbors for this slot
                entity_ref = EntityRef(namespace="aedos", identifier=slot_val)
                try:
                    sub_neighbors = self._substrate.subsumption.query_neighbors(entity_ref, relation_type)
                    for sub in sub_neighbors:
                        # Determine target identifier from the neighbor verdict
                        # query_neighbors returns verdicts involving entity_ref as entity_a
                        # We need to find the entity_b identifier from the DB
                        # For now, skip actual entity lookup in walker — this is exercised via mocked substrate
                        pass
                except Exception:
                    pass

        return expanded, llm_delta
