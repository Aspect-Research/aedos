from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..layer1_extraction.extractor import Claim
from ..layer3_substrate import Substrate
from ..layer3_substrate.subsumption import EntityRef
from ..layer4_sources.kb_verifier import KBVerdictType
from ..layer4_sources.kb_protocol import KBProtocol, LocalContext
from ..layer5_result.trace import JustificationTrace, TraceEdge, TraceNode


# Phase H D5: per-relation KB neighbor properties. Mirrors
# `_SUBSUMPTION_PROPERTIES` in `kb_wikidata.py` for is_a/part_of, plus
# P17 (country) on part_of for country-level grounding (e.g. Williams
# College P17 → United States; useful for "X is in the United States"
# style claims when the substrate's subsumption oracle is cold).
_D5_NEIGHBOR_PROPS_BY_RELATION: dict[str, tuple[str, ...]] = {
    "is_a": ("P31", "P279"),
    "part_of": ("P131", "P361", "P17"),
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class VerificationContext:
    current_time: str
    asserting_party: str
    # Phase H D47: the full input text the extractor was originally called
    # with, threaded request-scoped so the resolver / normalizer can use it
    # for Stage 2 disambiguation context. Optional — callers that don't
    # have a meaningful source text (direct-resolver corpus runners,
    # ad-hoc tests) pass None and Stage 2's abstention bias fires hard.
    source_text: Optional[str] = None


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
        config: Optional[dict] = None,
        walker_wall_clock_seconds: Optional[float] = None,
        walker_max_llm_calls: Optional[int] = None,
        walker_max_depth: Optional[int] = None,
        kb: Optional[KBProtocol] = None,
    ) -> None:
        """Resource budgets resolve in priority order:

          1. Explicit kwarg (`walker_wall_clock_seconds`, etc.) — used
             by `build_pipeline` to thread `Config` fields through.
          2. Legacy `config` dict (`config={"max_depth": N}`) — kept
             for back-compat with tests that construct the walker
             directly.
          3. Architecture defaults (`_DEFAULT_MAX_DEPTH` etc.).

        Per F3 §5.1: the kwarg path is the new wiring; the dict path
        is preserved so existing tests don't churn.
        """
        self._tier_u = tier_u
        self._kb_verifier = kb_verifier
        self._python_verifier = python_verifier
        self._substrate = substrate
        self._config = config or {}
        self._max_depth = (
            walker_max_depth
            if walker_max_depth is not None
            else self._config.get("max_depth", _DEFAULT_MAX_DEPTH)
        )
        self._default_wall_clock_seconds = walker_wall_clock_seconds
        self._default_max_llm_calls = walker_max_llm_calls
        # Phase H D5: the KB adapter is threaded explicitly so the walker
        # can call `enumerate_neighbors` directly. None disables the D5
        # fallback (back-compat for test paths that construct the walker
        # without a KB).
        self._kb = kb

    def walk(
        self,
        claim: Claim,
        context: VerificationContext,
        budget: Optional[WalkerBudget] = None,
    ) -> WalkResult:
        if budget is None:
            # Build a budget from the Walker's config-driven defaults
            # (F3 §5.1). Each field falls back to the dataclass default
            # if not explicitly configured.
            kwargs: dict[str, Any] = {}
            if self._default_wall_clock_seconds is not None:
                kwargs["wall_clock_seconds"] = self._default_wall_clock_seconds
            if self._default_max_llm_calls is not None:
                kwargs["max_llm_calls"] = self._default_max_llm_calls
            budget = WalkerBudget(**kwargs)

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
            kb_result = self._kb_verifier.verify(
                node,
                current_time=context.current_time,
                source_text=context.source_text,
            )
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

        # Python verifier (F-042: gated on routing_hint=="python" per architecture
        # §6.5 step 3: "Python verification if the route is Python." Before this
        # gate, the walker invoked the Python verifier unconditionally — and for
        # subjective / preference / opinion claims the live LLM-driven verifier
        # cheerfully wrote `return False`, producing `contradicted` instead of
        # `no_grounding_found`. That was a §3.2 soundness violation; see
        # docs/v0.16_planning.md D40 for the structural-test follow-up and D41
        # for the mock-fixture-discipline finding the bug surfaced.
        if (
            self._python_verifier is not None
            and self._predicate_routing(node.predicate) == "python"
        ):
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

    def _predicate_routing(self, predicate: str) -> Optional[str]:
        """Routing hint for `predicate` per the predicate translation oracle.
        Returns None when the consult fails — the walker treats an unknown
        routing as non-python (the conservative call: a wrong None costs a
        false abstain when the predicate should have routed to Python; a
        wrong 'python' would re-introduce F-042's false-contradiction class).
        """
        try:
            meta = self._substrate.predicate_translation.consult(predicate)
            return meta.routing_hint
        except Exception:
            return None

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

        Phase H D5: when the substrate's subsumption oracle produces no
        expansion for a relation_type (no cached rows match), the walker
        falls back to live KB neighbor enumeration via
        `_expand_via_kb_neighbors`. Cheapest-path-first per D5 design
        Decision 5.
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

            sub_produced: list[Claim] = []
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
                    sub_produced.append(new_node)
            expanded.extend(sub_produced)

            # Phase H D5 fallback: substrate oracle had nothing for this
            # relation_type → try live KB neighbor enumeration. Only fires
            # when the distribution gate is open (`directions` non-empty)
            # and substrate produced nothing.
            if not sub_produced:
                kb_produced = self._expand_via_kb_neighbors(
                    node, relation_type, directions, dist.verdict, trace
                )
                expanded.extend(kb_produced)

        return expanded, llm_delta

    def _expand_via_kb_neighbors(
        self,
        node: Claim,
        relation_type: str,
        directions: set[str],
        distribution_verdict,
        trace: JustificationTrace,
    ) -> list[Claim]:
        """Phase H D5 + D51: enumerate KB neighbors of `node`'s slot
        entities and emit expanded claims with the slot substituted by
        each neighbor.

        Fires as a fallback when `_expand_via_substrate` produced no
        expansion for `relation_type` (per D5 design Decision 5 —
        cheapest-path-first).

        Direction mapping (D5 + D51):
          - `"parent"` ∈ directions (distributes_down, both): call
            `enumerate_neighbors(direction="outgoing")` — yields E's
            parents (entities E points to via P31/P361/P131/P17/P279).
            Walker substitutes E with one of its parents.
          - `"child"` ∈ directions (distributes_up, both): call
            `enumerate_neighbors(direction="incoming")` — yields E's
            children (entities pointing to E). Walker substitutes E
            with one of its children. D51 (2026-05-24).

        Both directions fire when distribution is `both`; the trace
        records each direction separately so Phase 10.5 attribution
        can distinguish.

        Audit shape: each emitted expansion gets a
        `kb_neighbor_enumeration` trace edge with the source slot value,
        resolved Q-id, neighbor Q-id, KB property used, the
        distribution-verdict that authorized the traversal, and the
        direction (`"parent"` or `"child"`). The `_live_neighbors` call
        itself writes a `kb_live_neighbors` audit-log event with
        `direction` recorded.

        Fail-open shape: any failure in resolution, KB call, or parsing
        returns no expansion for the affected slot; the walker continues
        with whatever expansions other slots produced. Never raises.
        """
        if self._kb is None:
            return []
        properties = list(_D5_NEIGHBOR_PROPS_BY_RELATION.get(relation_type, ()))
        if not properties:
            return []

        # Map walker-direction → KB enumerate-neighbors-direction. The two
        # vocabularies use different words for symmetric concepts (the
        # walker thinks in terms of taxonomy direction; the KB call thinks
        # in terms of edge direction). For each walker-direction the
        # operator selected, do one enumeration call.
        kb_calls: list[tuple[str, str]] = []  # (walker_dir, kb_dir)
        if "parent" in directions:
            kb_calls.append(("parent", "outgoing"))
        if "child" in directions:
            kb_calls.append(("child", "incoming"))
        if not kb_calls:
            return []

        verdict_label = (
            distribution_verdict.value
            if hasattr(distribution_verdict, "value")
            else str(distribution_verdict)
        )

        expanded: list[Claim] = []
        for slot in ("subject", "object"):
            slot_val = node.subject if slot == "subject" else node.object
            if not slot_val:
                continue

            # Resolve the slot's surface form to a KB Q-id. Reuses the
            # substrate's EntityResolver — same caching, same D47 normalization,
            # same per-purpose LLM routing as KBVerifier.
            try:
                lc = LocalContext(
                    predicate=node.predicate,
                    slot_position=slot,
                    asserting_party=node.asserting_party,
                    source_text=node.source_text,
                    claim_subject=node.subject,
                    claim_predicate=node.predicate,
                    claim_object=node.object,
                    claim_id=node.claim_id,
                )
                candidates = self._substrate.resolver.resolve(slot_val, lc)
            except Exception:
                continue
            if not candidates:
                continue
            entity_qid = candidates[0].kb_identifier
            if not entity_qid or not entity_qid.startswith("Q"):
                continue

            for walker_dir, kb_dir in kb_calls:
                try:
                    neighbors_by_prop = self._kb.enumerate_neighbors(
                        entity_qid, properties, direction=kb_dir,
                    )
                except Exception:
                    continue

                for prop_id, neighbor_qids in neighbors_by_prop.items():
                    for neighbor_qid in neighbor_qids:
                        if slot == "subject":
                            new_node = _claim_from_parts(node, subject=neighbor_qid)
                        else:
                            new_node = _claim_from_parts(node, object_val=neighbor_qid)
                        trace.edges.append(TraceEdge(
                            edge_type="kb_neighbor_enumeration",
                            source=TraceNode("claim", {slot: slot_val}),
                            target=TraceNode("claim", {slot: neighbor_qid}),
                            metadata={
                                "relation_type": relation_type,
                                "direction": walker_dir,  # "parent" or "child"
                                "distribution": verdict_label,
                                "kb_property": prop_id,
                                "subject_qid": entity_qid,
                                "polarity": node.polarity,
                            },
                        ))
                        expanded.append(new_node)

        return expanded
