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


def _distribution_directions(verdict) -> set[str]:
    """Neighbor directions the walker may traverse for a distribution verdict.

    distributes_up   (P(X) and X R Y => P(Y)): to verify P(E), descend to children.
    distributes_down (P(Y) and X R Y => P(X)): to verify P(E), ascend to parents.
    both: either direction. neither: gate closed.
    """
    v = verdict.value if hasattr(verdict, "value") else verdict
    if v == "distributes_up":
        return {"child"}
    if v == "distributes_down":
        return {"parent"}
    if v == "both":
        return {"child", "parent"}
    return set()


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
        polarity_trace: list[int] = []

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
                polarity_trace.append(node.polarity)

                # Direct premise lookup
                verdict, lookup_source, llm_delta = self._direct_lookup(node, context, trace)
                llm_calls += llm_delta

                if verdict is not None:
                    # Handle conflicting verdicts (architecture 6.4): contradiction wins.
                    if current_verdict is None:
                        current_verdict = verdict
                    elif current_verdict != verdict:
                        current_verdict = "contradicted"
                        trace.walk_metadata["conflict"] = True
                    if current_verdict == "contradicted":
                        break
                    # A grounded `verified` node needs no expansion; keep scanning
                    # the rest of this frontier so a conflicting verdict is caught.
                    continue

                # Expand via substrate (ungrounded nodes only)
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
                metadata={
                    "source": "tier_u", "polarity": node.polarity, "verdict": "verified",
                    "tier_u_row_id": tier_u_result.rows[0]["id"] if tier_u_result.rows else None,
                },
            ))
            # TierU._stage1 matches polarity exactly: a `found` hit is an
            # assertion of the SAME polarity as the claim, hence verified —
            # including a negated claim grounded in a negated Tier U row.
            return "verified", "tier_u", 0
        if tier_u_result.historical_only:
            # Historical match means claim was true at some point, counts as partial evidence
            # but does NOT ground a present-tense claim → skip
            pass

        # Belief revision (architecture 8.1): if the claim's exact negation is
        # asserted in Tier U — a currently-valid, non-retracted row of opposite
        # polarity for the same (party, subject, predicate, object) — the
        # authoritative prior contradicts the claim.
        flipped = _claim_from_parts(node, polarity=1 - node.polarity)
        flipped_result = self._tier_u.lookup(flipped, current_time=context.current_time)
        if flipped_result.found:
            trace.source_breakdown["tier_u"] = trace.source_breakdown.get("tier_u", 0) + 1
            trace.edges.append(TraceEdge(
                edge_type="premise_lookup",
                source=trace.root,
                target=TraceNode("tier_u_row", {"subject": node.subject, "predicate": node.predicate}),
                metadata={
                    "source": "tier_u", "polarity": flipped.polarity, "verdict": "contradicted",
                    "tier_u_row_id": flipped_result.rows[0]["id"] if flipped_result.rows else None,
                    "belief_revision": "polarity_conflict",
                },
            ))
            return "contradicted", "tier_u", 0

        # Object-conflict belief revision (D16): a functional (single_valued)
        # predicate admits at most one object value per subject. A
        # currently-valid Tier U row positively asserting a DIFFERENT object for
        # the same (party, subject, predicate) therefore contradicts a positive
        # claim — the asserting party already stipulated another value.
        # Multi-valued predicates do not fire this path (a different value is a
        # parallel assertion). Negated claims do not fire it either: Phase B
        # keeps the negated-claim direction conservative (fall through to
        # abstain) rather than deriving the negation from the functional prior.
        if node.polarity == 1:
            oc_result = self._tier_u.lookup_object_conflict(
                node, current_time=context.current_time
            )
            if oc_result.found and self._predicate_is_functional(node.predicate):
                trace.source_breakdown["tier_u"] = trace.source_breakdown.get("tier_u", 0) + 1
                trace.edges.append(TraceEdge(
                    edge_type="premise_lookup",
                    source=trace.root,
                    target=TraceNode("tier_u_row", {"subject": node.subject, "predicate": node.predicate}),
                    metadata={
                        "source": "tier_u", "polarity": node.polarity, "verdict": "contradicted",
                        "tier_u_row_id": oc_result.rows[0]["id"] if oc_result.rows else None,
                        "belief_revision": "object_conflict",
                    },
                ))
                return "contradicted", "tier_u", 0

        # KB verification
        if self._kb_verifier is not None:
            kb_result = self._kb_verifier.verify(node, current_time=context.current_time)
            if kb_result.verdict == KBVerdictType.VERIFIED:
                trace.source_breakdown["kb"] = trace.source_breakdown.get("kb", 0) + 1
                trace.edges.append(TraceEdge(
                    edge_type="premise_lookup",
                    source=trace.root,
                    target=TraceNode("kb_statement", {"entity": kb_result.subject_kb_id}),
                    metadata={
                        "source": "kb", "verdict": "verified",
                        # R1: surface the D19 lookup direction on the result-level
                        # trace so Phase 10.5 debugging can see inverted lookups.
                        "lookup_inverted": kb_result.trace.get("lookup_inverted"),
                    },
                ))
                return "verified", "kb", 0
            elif kb_result.verdict == KBVerdictType.CONTRADICTED:
                trace.edges.append(TraceEdge(
                    edge_type="premise_lookup",
                    source=trace.root,
                    target=TraceNode("kb_statement", {"entity": kb_result.subject_kb_id}),
                    metadata={
                        "source": "kb", "verdict": "contradicted",
                        "lookup_inverted": kb_result.trace.get("lookup_inverted"),
                    },
                ))
                return "contradicted", "kb", 0

        # Python verifier
        if self._python_verifier is not None:
            py_result = self._python_verifier.verify(node)
            if py_result.verdict != "no_terminal_result":
                trace.source_breakdown["python"] = trace.source_breakdown.get("python", 0) + 1
                trace.edges.append(TraceEdge(
                    edge_type="premise_lookup",
                    source=trace.root,
                    target=TraceNode("python_result", {
                        "code": getattr(py_result, "generated_code", ""),
                        "output": str(getattr(py_result, "output", "")),
                    }),
                    metadata={"source": "python", "verdict": py_result.verdict},
                ))
                return py_result.verdict, "python", 0

        return None, "", 0

    def _predicate_is_functional(self, predicate: str) -> bool:
        """Whether `predicate` is functional (single_valued) per predicate
        translation.

        In the assembled pipeline Layer 2 routing has already consulted the
        oracle for this predicate, so this `consult` is a cache hit (no LLM
        call) — `_direct_lookup` keeps reporting llm_delta=0, consistent with
        the KB-verifier path which also consults the oracle internally. A
        consult failure is treated as non-functional: a wrong 0 costs only a
        false abstain, a wrong 1 a false contradiction (architecture 5.2).
        """
        try:
            meta = self._substrate.predicate_translation.consult(predicate)
            return bool(meta.single_valued)
        except Exception:
            return False

    def _expand_via_substrate(
        self, node: Claim, trace: JustificationTrace, depth: int
    ) -> tuple[list[Claim], int]:
        """Expand node into subsumed claims. Returns (new_nodes, llm_calls).

        The walker does not emit a predicate-equivalence expansion edge: an
        equivalent predicate shares the same `kb_property`, so its KB lookup is
        identical to the original's, and `TierU.lookup` stage 3 already
        broadens by the same `predicate_translation` oracle. The edge was
        redundant (D7); only distribution-gated subsumption traversal remains.
        """
        expanded: list[Claim] = []
        llm_delta = 0

        # Distribution-gated subsumption traversal.
        # For goal claim P(E): consult predicate_distribution to learn whether
        # the predicate distributes over the relation; if so, substitute the
        # slot entity with a taxonomy neighbor and emit a subsumption_traversal
        # edge. distributes_up => descend to children; distributes_down =>
        # ascend to parents; neither => gate closed.
        for relation_type in ("is_a", "part_of"):
            try:
                dist = self._substrate.predicate_distribution.consult(
                    node.predicate, node.polarity, relation_type
                )
                llm_delta += (0 if dist.was_cached else 1)
            except Exception:
                continue

            directions = _distribution_directions(dist.verdict)
            if not directions:
                continue  # gate closed (neither)

            for slot in ("subject", "object"):
                slot_val = node.subject if slot == "subject" else node.object
                entity_ref = EntityRef(namespace="aedos", identifier=slot_val)
                try:
                    sub_neighbors = self._substrate.subsumption.find_neighbors(
                        entity_ref, relation_type
                    )
                except Exception:
                    continue
                for sub in sub_neighbors:
                    if sub.direction not in directions:
                        continue
                    new_id = sub.entity.identifier
                    if slot == "subject":
                        new_node = _claim_from_parts(node, subject=new_id)
                    else:
                        new_node = _claim_from_parts(node, object_val=new_id)
                    trace.edges.append(TraceEdge(
                        edge_type="subsumption_traversal",
                        source=TraceNode("claim", {slot: slot_val}),
                        target=TraceNode("claim", {slot: new_id}),
                        metadata={
                            "relation_type": relation_type,
                            "direction": sub.direction,
                            "distribution": dist.verdict.value,
                            "subsumption_row_id": sub.row_id,
                            "polarity": node.polarity,
                        },
                    ))
                    expanded.append(new_node)

        return expanded, llm_delta
