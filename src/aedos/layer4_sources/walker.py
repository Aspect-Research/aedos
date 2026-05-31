from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..layer1_extraction.extractor import Claim
from ..layer1_extraction.temporal import BEFORE_PRESENT
from ..layer3_substrate import Substrate
from ..layer3_substrate.subsumption import EntityRef
from ..layer4_sources.kb_verifier import (
    KBVerdictType,
    _normalize_date_value,
    _value_matches,
)
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
    # v0.16 WS2 §5: per-walk fanout bound on discovery candidates. The depth==0
    # cap on KB-neighbor enumeration is removed to enable bidirectional/forward
    # search, but un-capped blind incoming enumeration fans out
    # multiplicatively across depth (the D51 18-min / OOM blowup). The
    # wall-clock budget is only sampled at depth boundaries, so a single
    # depth's frontier can explode before the next check; this bound is sampled
    # WITHIN the frontier loop (every node's discovery) so the walker abstains
    # the moment cumulative expansion crosses the bound — making the budget the
    # true cost bound. Permissive default (config-tunable per contract §0.11
    # decision 6); seeds + premise-forward keep real walks far below it.
    max_frontier_expansions: int = 2000


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


@dataclass
class Interval:
    """v0.16 WS6 T1: a bounded interval gathered from KB statement qualifiers
    (P580 start / P582 end) and/or Tier U *_started/_ended facts.

    `start` / `end` are ISO-date strings (YYYY-MM-DD or YYYY), compared
    lexically (valid for zero-padded ISO). `start_known` / `end_known` are the
    three-valued-logic discriminators: an UNKNOWN endpoint is NOT the same as a
    known endpoint at some sentinel. An open end (end_known=False) means the
    relation is ongoing (`holds_at` returns true once start<=T). BEFORE_PRESENT
    maps to end_known=False (an unspecified past end never forces a false)."""
    start: Optional[str] = None
    end: Optional[str] = None
    start_known: bool = False
    end_known: bool = False
    # Round-1 robustness follow-up (WS6, defense-in-depth): True iff this
    # interval was built from a UNIQUELY-identified base statement (a single
    # candidate, or a single preferred-ranked statement), rather than from an
    # arbitrary collapse of ALL the subject's statements. Only a unique interval
    # may license a *_started/_ended CONTRADICTED verdict — collapsing several
    # statements that merely happen to AGREE on a start must never contradict an
    # asserted endpoint (a future single_valued endpoint binding could otherwise
    # false-contradict). Forward-defensive: all 8 endpoint seeds are
    # single_valued=0 today, so no contradiction path is reachable yet.
    unique: bool = False


class BudgetExceeded(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------

_DEFAULT_MAX_DEPTH = 4


_VAGUE_CLASS_PREFIXES = ("a ", "an ", "some ", "any ")
_VAGUE_CLASS_RELCLAUSE = (" that ", " which ", " where ", " whose ", " who ")


# Phase 10.5 Step 6 Tier A2: helpers for the kb_quantitative path.
import re as _re_quant

_QUANT_NUMBER_RE = _re_quant.compile(
    r"([-+]?\d[\d,]*\.?\d*)\s*(million|billion|thousand|hundred|m|bn|k)?",
    _re_quant.IGNORECASE,
)
_QUANT_MULTIPLIERS = {
    "thousand": 1_000, "k": 1_000,
    "million": 1_000_000, "m": 1_000_000,
    "billion": 1_000_000_000, "bn": 1_000_000_000,
    "hundred": 100,
}


def _parse_quantity(s: str) -> Optional[float]:
    """Parse the leading numeric quantity from a string. Handles
    "60 million", "1.5 billion", "67000000", "1,000,000", etc.
    Returns None if no quantity can be extracted."""
    if not s:
        return None
    text = str(s).strip()
    m = _QUANT_NUMBER_RE.search(text)
    if not m:
        return None
    num_str = m.group(1).replace(",", "")
    unit = (m.group(2) or "").lower()
    try:
        value = float(num_str)
    except ValueError:
        return None
    multiplier = _QUANT_MULTIPLIERS.get(unit, 1)
    return value * multiplier


def _apply_polarity_str(verdict: str, polarity: int) -> str:
    """Polarity-invert a string verdict. Used by the kb_quantitative
    path (which returns string verdicts) — parallels kb_verifier's
    _apply_polarity for KBVerdictType."""
    if polarity == 1:
        return verdict
    if verdict == "verified":
        return "contradicted"
    if verdict == "contradicted":
        return "verified"
    return verdict


def _is_vague_class_object(object_value: str) -> bool:
    """Phase 10.5 Step 6 sub-cause F follow-on: True if the object is a
    descriptive class reference rather than a specific entity name.

    Triggers on:
      - Indefinite-article prefix: 'a town in the United States',
        'an institution founded before 1800'.
      - Relative-clause structure: 'a state that borders New York',
        'a country which uses the Euro'.

    Used by the walker's object-conflict path to skip the contradicted
    return when the claim's object is a class — the specific Tier U
    premise may instantiate the class (Williamstown IS a town in the
    US), so the conflict is a subsumption candidate, not a refutation.
    """
    if not object_value:
        return False
    obj = object_value.strip()
    if not obj:
        return False
    lower = obj.lower()
    for prefix in _VAGUE_CLASS_PREFIXES:
        if lower.startswith(prefix):
            return True
    for relclause in _VAGUE_CLASS_RELCLAUSE:
        if relclause in lower:
            return True
    return False


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


def _apply_assertion_designation(base_verdict: str, trace: JustificationTrace) -> str:
    """Phase H Cluster 2 step 3: convert a base verdict to its
    `_given_assertion` dual designation when the chain composition
    includes an asserted-unverified premise (or when the walk was
    pre-flagged for a `user_authoritative` claim).

    Imports lazily inside the function to avoid a circular import
    (aggregator imports from trace; walker imports trace; if walker
    imports aggregator at module load we get a cycle).
    """
    if not trace.chain_includes_assertion:
        return base_verdict
    from ..layer5_result.aggregator import _BASE_OF_DUAL  # noqa: F401  (sanity import)
    # Inverse of _BASE_OF_DUAL — base verdict → dual designation.
    mapping = {
        "verified": "verified_given_assertion",
        "contradicted": "contradicted_given_assertion",
        "no_grounding_found": "abstained_given_assertion",
    }
    return mapping.get(base_verdict, base_verdict)


def _distribution_directions(verdict) -> set[str]:
    """Preferred neighbor directions (ranking hint), not a gate.

    v0.16 WS2 §3: distribution is demoted from a GATE to a RANKER. This maps
    a verdict to the directions the predicate is EXPECTED to distribute over;
    `_discover_chains` uses the set to ORDER candidates (preferred-first), NOT
    to skip a relation. Soundness moved to `_verify_chain` — a `neither`
    verdict (empty set) no longer forecloses the relation; it deprioritizes it.

    distributes_up   (P(X) and X R Y => P(Y)): to verify P(E), descend to children.
    distributes_down (P(Y) and X R Y => P(X)): to verify P(E), ascend to parents.
    both: either direction. neither: no preference (empty — ranks last).
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
        exception_cache=None,
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
        # v0.16 WS3 §3D: the bounded nogood cache for _nogood_vetoes. When None
        # the helper falls back to the kb_verifier's cache (then no-op), so the
        # explicit wiring here and the fallback both work.
        self._exception_cache = exception_cache

    def walk(
        self,
        claim: Claim,
        context: VerificationContext,
        budget: Optional[WalkerBudget] = None,
        excluded_tier_u_row_ids: Optional[set[int]] = None,
    ) -> WalkResult:
        """Verify `claim` against the three typed sources of belief.

        Phase H Cluster 3 step 7 (2026-05-26): `excluded_tier_u_row_ids`
        lets the caller suppress specific Tier U rows from the lookup —
        used by the promote-then-walk corpus runner / chat-wrapper to
        prevent the walker from matching the claim's own freshly-promoted
        asserted_unverified row at Stage 1. With the row filtered out,
        the polarity-conflict and object-conflict belief-revision paths
        in `_direct_lookup` become reachable for cases like
        `der_revision_003` ("Asa is not a student") where the corpus
        relies on the walker finding the prior of opposite polarity.
        Empty / None means no filtering (pre-step-7 behavior; idempotent
        promotions don't get filtered because the row pre-dates this
        walk's promotion attempt — those cases the walker should still
        match).
        """
        self._excluded_tier_u_row_ids: set[int] = excluded_tier_u_row_ids or set()
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

        # v0.16 WS4 (4b): a claim carrying an extraction-layer abstention_reason
        # is malformed or not-checkworthy. Short-circuit to no_grounding_found
        # BEFORE any Tier U / KB / Python / LLM lookup. The self_referential /
        # predicate_eq_object reasons are the §3.2-soundness-critical ones: their
        # malformed triples would, if looked up, risk a false contradiction (the
        # v0.15 reason the extractor dropped them). subject_absent_from_source
        # and not_checkworthy are also returned here (nothing to ground). This
        # is the pre-lookup guard — placed at walk entry, before the frontier
        # loop and before any _direct_lookup. Base no_grounding_found (NOT
        # _apply_assertion_designation): the user_authoritative chain-flag set
        # happens after this guard, so it is irrelevant here.
        if claim.abstention_reason:
            trace.walk_metadata["short_circuit"] = claim.abstention_reason
            trace.polarity_trace = []
            return WalkResult(
                verdict="no_grounding_found",
                trace=trace,
                abstention_reason=claim.abstention_reason,
                budget_consumption=BudgetConsumption(wall_clock_ms=0.0, llm_calls=0),
            )

        # Phase H Cluster 2 step 3 (Q-UserAuth): for predicates routed
        # `user_authoritative`, external grounding is structurally
        # unreachable — no KB property maps to `prefers` / `believes` /
        # similar, and Python cannot compute first-person facts. Every
        # verdict on such a claim is therefore conditional on user
        # assertion. Pre-set the chain flag so the verdict family is
        # always `*_given_assertion` (verified / contradicted /
        # abstained), even when no Tier U premise is present (the
        # abstained_given_assertion case). See design doc §"User_authoritative
        # verdict semantics".
        if self._predicate_routing(claim.predicate) == "user_authoritative":
            self._record_premise(trace, source="tier_u", assertion=True)

        frontier: list[Claim] = [claim]
        visited: dict[str, Claim] = {}
        depth = 0
        current_verdict: Optional[str] = None
        # v0.16 WS2 §5: cumulative discovery-expansion counter. Sampled WITHIN
        # the frontier loop so an un-capped KB-enumeration fanout (the removed
        # depth==0 cap's failure mode) can't explode a single depth's frontier
        # past the budget before the depth-boundary wall-clock check fires.
        total_expansions = 0
        fanout_exceeded = False

        while frontier and depth < self._max_depth:
            # Budget check
            elapsed = time.monotonic() - start_time
            if elapsed > budget.wall_clock_seconds:
                consumption = BudgetConsumption(wall_clock_ms=elapsed * 1000, llm_calls=llm_calls)
                trace.walk_metadata.update({"depth_reached": depth, "budget_exceeded": "wall_clock"})
                trace.polarity_trace = polarity_trace
                return WalkResult(
                    verdict=_apply_assertion_designation("no_grounding_found", trace),
                    trace=trace,
                    abstention_reason="budget_wall_clock",
                    budget_consumption=consumption,
                )
            if llm_calls >= budget.max_llm_calls:
                consumption = BudgetConsumption(wall_clock_ms=elapsed * 1000, llm_calls=llm_calls)
                trace.walk_metadata.update({"depth_reached": depth, "budget_exceeded": "llm_calls"})
                trace.polarity_trace = polarity_trace
                return WalkResult(
                    verdict=_apply_assertion_designation("no_grounding_found", trace),
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

                # v0.16 WS2 §2: discover liberally, verify soundly. Discovery
                # proposes candidate substitutions (subsumption neighbors +
                # premise-forward) without the distribution gate; _verify_chain
                # (called inside _discover_chains per candidate) admits a
                # candidate only if the taxonomy/transitive edge is confirmed
                # in a source. Soundness lives entirely at verify time (§3.2).
                expanded, llm_delta = self._discover_chains(node, trace, depth)
                llm_calls += llm_delta
                next_frontier.extend(expanded)
                total_expansions += len(expanded)

                # v0.16 WS2 §5 fanout bound: abstain the moment cumulative
                # discovery crosses the budget. This is the cost bound that
                # replaces the removed depth==0 cap — without it, blind
                # incoming enumeration fans out multiplicatively (OOM in the
                # D51 worst case). Breaking mid-frontier stops accumulation
                # before the next node enumerates.
                if total_expansions > budget.max_frontier_expansions:
                    fanout_exceeded = True
                    break

            if fanout_exceeded:
                elapsed = time.monotonic() - start_time
                consumption = BudgetConsumption(wall_clock_ms=elapsed * 1000, llm_calls=llm_calls)
                trace.walk_metadata.update(
                    {"depth_reached": depth, "budget_exceeded": "fanout"}
                )
                trace.polarity_trace = polarity_trace
                return WalkResult(
                    verdict=_apply_assertion_designation("no_grounding_found", trace),
                    trace=trace,
                    abstention_reason="budget_fanout",
                    budget_consumption=consumption,
                )

            if current_verdict in ("verified", "contradicted"):
                break

            frontier = next_frontier
            depth += 1

        elapsed = time.monotonic() - start_time
        consumption = BudgetConsumption(wall_clock_ms=elapsed * 1000, llm_calls=llm_calls)
        trace.walk_metadata.update({"depth_reached": depth, "llm_calls": llm_calls})
        trace.polarity_trace = polarity_trace

        if current_verdict is not None:
            return WalkResult(
                verdict=_apply_assertion_designation(current_verdict, trace),
                trace=trace,
                budget_consumption=consumption,
            )

        return WalkResult(
            verdict=_apply_assertion_designation("no_grounding_found", trace),
            trace=trace,
            abstention_reason="depth_exhausted",
            budget_consumption=consumption,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _record_premise(self, trace, *, source, table=None, row_id=None,
                        status=None, assertion=False):
        """WS3 §3B: append one grounding premise to the trace's provenance
        term as a fresh OR-alternative. Each grounded premise found in a walk
        is an independent way the verdict could hold (OR); a future multi-hop
        composition ANDs hops within one alternative. Centralizes literal
        construction so every grounding site contributes provenance, and so
        the DERIVED chain_includes_assertion (monotone-OR over literal
        .assertion flags) reproduces the legacy boolean exactly."""
        from ..layer5_result.trace import ProvenanceLiteral, ProvenanceTerm
        trace.provenance.add_alternative(
            ProvenanceTerm.lit(ProvenanceLiteral(
                source=source, table=table, row_id=row_id,
                status=status, assertion=assertion,
            ))
        )

    def _direct_lookup(
        self, node: Claim, context: VerificationContext, trace: JustificationTrace
    ) -> tuple[Optional[str], str, int]:
        """Returns (verdict_or_None, source, llm_calls_used).

        Phase H Cluster 2 step 3: status-aware Tier U handling.

        Tier U match flow:
          - `externally_verified` row → plain verified, no chain flag.
          - `asserted_unverified` row:
            * `user_authoritative` route (Q-UserAuth): short-circuit
              → verified; chain flag was already set at walk start so
              the final verdict becomes `verified_given_assertion`.
            * any other route (Q-Lookup α): try KB/Python for external
              grounding. If KB or Python verifies the same claim, call
              `tier_u.mark_externally_verified` to upgrade the row and
              return plain verified WITHOUT setting the chain flag
              (this walk's verdict is externally grounded). If neither
              external source verifies, set the chain flag and return
              verified (final verdict becomes `verified_given_assertion`).

        Belief-revision paths (polarity_conflict, object_conflict)
        likewise read the contradicting row's status:
          - externally_verified contradicting row → plain contradicted.
          - asserted_unverified contradicting row → contradicted with
            chain flag set → final `contradicted_given_assertion`.
        """
        llm_delta = 0

        # Phase H Cluster 3 step 7 fixup (2026-05-26): check belief-revision
        # paths against PRIORS first, before Stage 1. Pre-fixup this code
        # excluded the walk's own promoted row from Stage 1 — but that
        # broke R3 cases (`der_multihop_001` "Asa lives in Williamstown")
        # where the promoted row IS the legitimate grounding and the
        # walker should match it to return verified_given_assertion.
        #
        # The restructured flow: first look for a CONFLICTING prior
        # (opposite polarity OR different object on a functional
        # predicate) WITH the walk's own promotion excluded; if found,
        # belief-revision fires and returns `contradicted`. If no
        # conflict, fall through to normal Stage 1 (no exclusion) so the
        # walker can match its own promotion as the in-vocabulary
        # grounding source. Q-Lookup α and external grounding follow as
        # before.
        excluded = getattr(self, "_excluded_tier_u_row_ids", None) or None

        # Polarity-conflict belief revision (architecture 8.1): a
        # currently-valid non-self prior of opposite polarity contradicts
        # the claim. Exclusion of own promotion prevents the walker from
        # missing the prior when the promote-then-walk pattern has
        # already written a row of the claim's polarity.
        flipped = _claim_from_parts(node, polarity=1 - node.polarity)
        flipped_result = self._tier_u.lookup(
            flipped,
            current_time=context.current_time,
            exclude_row_ids=excluded,
        )
        if flipped_result.found:
            flipped_row = flipped_result.rows[0] if flipped_result.rows else {}
            flipped_status = flipped_row.get("status", "asserted_unverified")
            trace.source_breakdown["tier_u"] = trace.source_breakdown.get("tier_u", 0) + 1
            trace.edges.append(TraceEdge(
                edge_type="premise_lookup",
                source=trace.root,
                target=TraceNode("tier_u_row", {"subject": node.subject, "predicate": node.predicate}),
                metadata={
                    "source": "tier_u", "polarity": flipped.polarity, "verdict": "contradicted",
                    "tier_u_row_id": flipped_row.get("id"),
                    "belief_revision": "polarity_conflict",
                    "premise_status": flipped_status,
                },
            ))
            self._record_premise(
                trace, source="tier_u", table="tier_u",
                row_id=flipped_row.get("id"), status=flipped_status,
                assertion=(flipped_status == "asserted_unverified"),
            )
            return "contradicted", "tier_u", 0

        # Object-conflict belief revision (D16): a functional
        # (single_valued) predicate admits at most one object value per
        # subject. lookup_object_conflict already filters to DIFFERENT
        # objects so it can't return the walk's own promotion — no
        # exclusion needed.
        if node.polarity == 1:
            oc_result = self._tier_u.lookup_object_conflict(
                node, current_time=context.current_time
            )
            if (
                oc_result.found
                and self._predicate_is_functional(node.predicate)
                # Phase 10.5 Step 6 sub-cause F follow-on: skip the
                # object-conflict contradicted return when the claim's
                # object is a vague descriptive phrase (indefinite-
                # article noun-phrase like "a town in the US", "a state
                # that borders NY"). The specific Tier U premise may be
                # an INSTANCE of the claim's CLASS — Williamstown is "a
                # town in the United States" — so the conflict is a
                # subsumption candidate, not a contradiction. The walker
                # cannot resolve free-text class references to KB Q-IDs
                # cheaply at this site (no class-instance oracle in v0.15),
                # so the §3.2 soundness invariant calls for abstaining
                # rather than emitting a §3.2-violating false contradiction.
                and not _is_vague_class_object(node.object)
            ):
                oc_row = oc_result.rows[0] if oc_result.rows else {}
                oc_status = oc_row.get("status", "asserted_unverified")
                trace.source_breakdown["tier_u"] = trace.source_breakdown.get("tier_u", 0) + 1
                trace.edges.append(TraceEdge(
                    edge_type="premise_lookup",
                    source=trace.root,
                    target=TraceNode("tier_u_row", {"subject": node.subject, "predicate": node.predicate}),
                    metadata={
                        "source": "tier_u", "polarity": node.polarity, "verdict": "contradicted",
                        "tier_u_row_id": oc_row.get("id"),
                        "belief_revision": "object_conflict",
                        "premise_status": oc_status,
                        # WS5(a.3): a functional-predicate object conflict has a
                        # distinct contradicting value — the conflicting Tier U
                        # row's object ("the source indicates {object} instead").
                        "contradicting_value": oc_row.get("object"),
                        "contradicting_value_type": "literal",
                    },
                ))
                self._record_premise(
                    trace, source="tier_u", table="tier_u",
                    row_id=oc_row.get("id"), status=oc_status,
                    assertion=(oc_status == "asserted_unverified"),
                )
                return "contradicted", "tier_u", 0

        # Stage 1 literal Tier U lookup. Now that belief-revision paths
        # against priors have already been checked, the walker is free to
        # match its own promotion here — this is the Q-UserAuth /
        # Q-Lookup α grounding path for R3 cases.
        tier_u_result = self._tier_u.lookup(
            node,
            current_time=context.current_time,
        )
        if tier_u_result.found:
            # Defensive: a mock TierU may report `found=True` without a
            # populated rows list. Fall back to the asserted_unverified
            # default (conservative — never under-flags a verdict).
            row = tier_u_result.rows[0] if tier_u_result.rows else {}
            row_status = row.get("status", "asserted_unverified")
            row_id = row.get("id")
            trace.source_breakdown["tier_u"] = trace.source_breakdown.get("tier_u", 0) + 1
            trace.edges.append(TraceEdge(
                edge_type="premise_lookup",
                source=trace.root,
                target=TraceNode("tier_u_row", {"subject": node.subject, "predicate": node.predicate}),
                metadata={
                    "source": "tier_u", "polarity": node.polarity, "verdict": "verified",
                    "tier_u_row_id": row_id,
                    "premise_status": row_status,
                },
            ))
            # WS3 §3B Edit 4: record the Stage-1 verified premise in the
            # provenance term so it is retractable. assertion=False here; the
            # Q-Lookup-α fallthrough below records an assertion literal when no
            # external grounding upgrades the asserted_unverified row.
            self._record_premise(
                trace, source="tier_u", table="tier_u",
                row_id=row_id, status=row_status, assertion=False,
            )
            # TierU._stage1 matches polarity exactly: a `found` hit is an
            # assertion of the SAME polarity as the claim, hence verified —
            # including a negated claim grounded in a negated Tier U row.

            if row_status == "externally_verified":
                # Established external knowledge; verdict is externally grounded.
                return "verified", "tier_u", 0

            # asserted_unverified path.
            route = self._predicate_routing(node.predicate)
            if route == "user_authoritative":
                # Q-UserAuth: chain flag was already set at walk start;
                # do not attempt KB/Python (structurally unreachable).
                return "verified", "tier_u", 0

            # Q-Lookup α: try external grounding for an upgrade
            # opportunity. KB first, Python second (matches §6.5 order
            # for non-Tier-U lookups). On success, upgrade the row and
            # return plain verified — the chain flag is NOT set
            # because the verdict is externally grounded.
            upgrade_verdict, upgrade_source, upgrade_llm_delta, grounding_chain = \
                self._try_external_grounding(node, context, trace)
            llm_delta += upgrade_llm_delta
            if upgrade_verdict == "verified":
                if row_id is not None:
                    self._tier_u.mark_externally_verified(
                        row_id,
                        grounding_chain=grounding_chain,
                        verdict_produced="verified",
                    )
                return "verified", f"tier_u→{upgrade_source}_upgrade", llm_delta
            # No external grounding available; chain stays
            # assertion-conditional. Mark the Stage-1 premise literal
            # assertion-conditional by appending an assertion literal
            # (OR semantics: includes_assertion() now True).
            self._record_premise(
                trace, source="tier_u", table="tier_u",
                row_id=row_id, status="asserted_unverified", assertion=True,
            )
            return "verified", "tier_u", llm_delta

        if tier_u_result.historical_only:
            # Historical match means claim was true at some point, counts as partial evidence
            # but does NOT ground a present-tense claim → skip
            pass

        # (Belief-revision paths moved BEFORE Stage 1 — see the
        # Cluster 3 step 7 fixup block at the top of this method.
        # Reaching here means there is no current Tier U premise of
        # either polarity / no functional-predicate object_conflict.)

        # No Tier U premise. Try KB / Python for standalone external
        # grounding (the §6.5 fallthrough — no upgrade scenario here,
        # there is no Tier U row to upgrade).
        external_verdict, external_source, external_llm_delta, _ = \
            self._try_external_grounding(node, context, trace)
        llm_delta += external_llm_delta
        if external_verdict is not None:
            return external_verdict, external_source, llm_delta

        return None, "", llm_delta

    def _try_external_grounding(
        self,
        node: Claim,
        context: VerificationContext,
        trace: JustificationTrace,
    ) -> tuple[Optional[str], str, int, dict]:
        """Try KB then (if route is python) Python verification for
        external grounding of `node`. Returns
        `(verdict_or_None, source, llm_delta, grounding_chain_dict)`.

        Phase H Cluster 2 step 3: factored out of `_direct_lookup` so
        the Q-Lookup-α upgrade path can share the same logic as the
        standalone-fallthrough path. The grounding_chain dict is the
        structured detail consumed by `mark_externally_verified`'s
        audit event — KB statement coordinates or Python execution
        identity, per the operator's audit-detail confirmation in
        step 1.
        """
        # Phase 10.5 Step 6 Batch 8+: skip KB verification when the
        # claim's subject is a known user persona stipulated in Tier U
        # (via a "user identity X" row). Otherwise the entity resolver
        # may resolve the persona name (e.g. "Asa") to an unrelated
        # Wikidata entity (Asa, King of Judah; or a different person
        # named Asa entirely), and the kb_verifier's polarity-aware
        # branch will emit verified/contradicted on facts about that
        # wrong entity. For negation claims ("Asa is not in France")
        # this produces a §3.2 false-contradiction when the misresolved
        # entity happens to be in France. The persona's true
        # verification is in Tier U; KB is the wrong source.
        if self._is_persona_subject(node):
            return None, "", 0, {}

        # Phase 10.5 Step 6 Tier A2: kb_quantitative comparison path.
        # When the predicate's routing_hint is "kb_quantitative", the
        # claim asserts a numeric comparison against a KB-derivable
        # quantity (e.g. "France has more than 60 million people" →
        # P1082 (population) > 60000000). The walker queries KB, parses
        # the claim's object as a threshold, and compares.
        if self._predicate_routing(node.predicate) == "kb_quantitative":
            quant = self._verify_kb_quantitative(node, context, trace)
            if quant is not None:
                verdict, detail = quant
                trace.source_breakdown["kb"] = trace.source_breakdown.get("kb", 0) + 1
                self._record_premise(trace, source="kb", assertion=False)
                # WS5(a.2): the kb_quantitative path previously appended no
                # trace edge — add one carrying the comparison detail
                # (kb_value/threshold/comparator) so the walk is observable,
                # and on contradiction the KB value IS the contradicting value
                # ("the source indicates 67000000").
                edge_md = {
                    "source": "kb_quantitative",
                    "verdict": verdict,
                    "predicate": node.predicate,
                    "kb_value": detail.get("kb_value"),
                    "threshold": detail.get("threshold"),
                    "comparator": detail.get("comparator"),
                    "kb_property": detail.get("kb_property"),
                }
                if verdict == "contradicted":
                    edge_md["contradicting_value"] = detail.get("kb_value")
                    edge_md["contradicting_value_type"] = "quantity"
                trace.edges.append(TraceEdge(
                    edge_type="premise_lookup",
                    source=trace.root,
                    target=TraceNode("kb_statement", {"entity": node.subject}),
                    metadata=edge_md,
                ))
                grounding = {
                    "source": "kb_quantitative",
                    "predicate": node.predicate,
                    "kb_value": detail.get("kb_value"),
                    "threshold": detail.get("threshold"),
                }
                if verdict == "contradicted":
                    grounding["contradicting_value"] = detail.get("kb_value")
                    grounding["contradicting_value_type"] = "quantity"
                return verdict, "kb_quantitative", 0, grounding

        # v0.16 WS6 T1: kb_interval endpoint path. A `*_started` / `*_ended`
        # predicate asserts the START or END date of an interval-bearing
        # relation (employment/membership/role). The walker resolves the
        # subject, looks up the base-relation KB statement, reads the matching
        # P580 (start) / P582 (end) QUALIFIER, and compares the claim's asserted
        # year/date against it. Placed BEFORE the generic kb_verifier branch
        # (mirrors the kb_quantitative placement) because the verifier's generic
        # value-compare path cannot interpret the qualifier-keyed slot map. The
        # resolver FAILS CLOSED (returns None) on any resolution/KB error,
        # ambiguity, or unknown endpoint — never fabricates a verdict.
        if self._predicate_routing(node.predicate) == "kb_interval":
            iv = self._verify_interval_endpoint(node, context, trace)
            if iv is not None:
                # _verify_interval_endpoint emits its own trace edge + provenance
                # premise; return its verdict + grounding chain directly.
                verdict, grounding = iv
                return verdict, "kb_interval", 0, grounding
            # None => abstain on this path; fall through (there is no generic
            # value-compare path for a qualifier-keyed object, so this returns
            # no_grounding_found unless another source grounds it).

        # KB verification
        if self._kb_verifier is not None:
            kb_result = self._kb_verifier.verify(
                node,
                current_time=context.current_time,
                source_text=context.source_text,
            )
            if kb_result.verdict == KBVerdictType.VERIFIED:
                trace.source_breakdown["kb"] = trace.source_breakdown.get("kb", 0) + 1
                # WS3 D13: the resolver's entity_resolution_cache row the KB
                # statement is keyed on — the retractable dependency of this
                # KB verdict (None for mock/transient resolvers).
                cache_row_id = kb_result.trace.get("resolution_cache_row_id")
                trace.edges.append(TraceEdge(
                    edge_type="premise_lookup",
                    source=trace.root,
                    target=TraceNode("kb_statement", {"entity": kb_result.subject_kb_id}),
                    metadata={
                        "source": "kb", "verdict": "verified",
                        # R1: surface the D19 lookup direction on the result-level
                        # trace so Phase 10.5 debugging can see inverted lookups.
                        "lookup_inverted": kb_result.trace.get("lookup_inverted"),
                        "entity_resolution_cache_row_id": cache_row_id,
                    },
                ))
                self._record_premise(
                    trace, source="kb", table="entity_resolution_cache",
                    row_id=cache_row_id, assertion=False,
                )
                grounding = {
                    "source": "kb",
                    "entity": kb_result.subject_kb_id,
                    "kb_property": kb_result.trace.get("kb_property"),
                    "lookup_inverted": kb_result.trace.get("lookup_inverted"),
                }
                return "verified", "kb", 0, grounding
            elif kb_result.verdict == KBVerdictType.CONTRADICTED:
                cache_row_id = kb_result.trace.get("resolution_cache_row_id")
                # WS5(a): the contradicting KB value is the matched
                # statement's value (the verifier also surfaces it on
                # kb_result.trace['contradicting_value']). On the no-statements
                # subsumption-fallback CONTRADICTED path matched_statement is
                # None, so guard for None — that path carries no distinct
                # "instead" value and falls back to the generic correction.
                matched = kb_result.matched_statement
                cv_raw = getattr(matched, "value", None) if matched is not None else None
                cv_type = getattr(matched, "value_type", None) if matched is not None else None
                if cv_raw is None:
                    cv_raw = kb_result.trace.get("contradicting_value")
                trace.edges.append(TraceEdge(
                    edge_type="premise_lookup",
                    source=trace.root,
                    target=TraceNode("kb_statement", {"entity": kb_result.subject_kb_id}),
                    metadata={
                        "source": "kb", "verdict": "contradicted",
                        "lookup_inverted": kb_result.trace.get("lookup_inverted"),
                        "entity_resolution_cache_row_id": cache_row_id,
                        # WS5(a): carry the contradicting KB value so the
                        # aggregator can populate ClaimVerdict.contradicting_value
                        # and the chat-wrapper can emit "the source indicates X".
                        "contradicting_value": cv_raw,
                        "contradicting_value_type": cv_type,
                        "kb_property": kb_result.trace.get("kb_property"),
                    },
                ))
                self._record_premise(
                    trace, source="kb", table="entity_resolution_cache",
                    row_id=cache_row_id, assertion=False,
                )
                grounding = {
                    "source": "kb",
                    "entity": kb_result.subject_kb_id,
                    "kb_property": kb_result.trace.get("kb_property"),
                    "verdict": "contradicted",
                    "contradicting_value": cv_raw,
                    "contradicting_value_type": cv_type,
                }
                return "contradicted", "kb", 0, grounding

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
                self._record_premise(trace, source="python", assertion=False)
                grounding = {
                    "source": "python",
                    "output": str(getattr(py_result, "output", "")),
                    "verdict": py_result.verdict,
                }
                return py_result.verdict, "python", 0, grounding

        return None, "", 0, {}

    def _verify_kb_quantitative(self, claim: Claim, context: VerificationContext, trace) -> Optional[tuple[str, dict]]:
        """Phase 10.5 Step 6 Tier A2: verify a kb_quantitative claim by
        querying KB for the subject's value of the predicate's
        kb_property and comparing against the claim's object as a
        numeric threshold.

        Predicates with routing_hint=kb_quantitative encode the
        comparator in their name (population_greater_than vs
        population_less_than). The kb_property is the canonical
        Wikidata property (P1082 for population, etc.).

        Object parsing: extracts the largest numeric value from the
        object string, handling abbreviated units like "60 million"
        / "1.5 billion". Returns None (no terminal verdict, walker
        falls through to next stage) on parse failures or KB lookup
        failures — §3.2 soundness, never fabricate a verdict on
        uncertainty.
        """
        # Parse threshold from claim.object
        threshold = _parse_quantity(claim.object)
        if threshold is None:
            return None

        # Determine comparator from predicate name
        pred = claim.predicate.lower()
        if "greater_than" in pred or "more_than" in pred or "above" in pred:
            comparator = "gt"
        elif "less_than" in pred or "below" in pred or "fewer_than" in pred:
            comparator = "lt"
        else:
            return None

        # Look up predicate metadata (already cached at this point)
        try:
            meta = self._substrate.predicate_translation.consult(claim.predicate)
        except Exception:
            return None
        if not meta.kb_property:
            return None

        # Resolve the subject to a KB Q-id via the kb_verifier's
        # resolver (which wires up the Wikipedia normalizer for D47
        # robustness). Fail closed if the resolver is unwired.
        if self._kb_verifier is None or not hasattr(self._kb_verifier, "_resolver"):
            return None
        resolver = self._kb_verifier._resolver
        lookup_ctx = LocalContext(
            predicate=claim.predicate,
            slot_position="subject",
            asserting_party=claim.asserting_party,
            source_text=context.source_text,
            claim_subject=claim.subject,
            claim_predicate=claim.predicate,
            claim_object=claim.object,
            claim_id=claim.claim_id,
        )
        resolved = resolver.select(
            resolver.resolve(claim.subject, lookup_ctx), lookup_ctx
        )
        if resolved is None:
            return None

        # Fetch KB statements for the property
        try:
            statements = self._kb.lookup_statements(resolved, meta.kb_property)
        except Exception:
            return None
        if not statements:
            return None

        # Take the most-recent / preferred value. P1082 (population) is
        # multi-valued historically — Wikidata stores a series of point-
        # in-time-qualified statements (e.g. "100M in 1990", "67M in 2024").
        # Strategy:
        #   1. Prefer the "preferred"-ranked statement if any exists
        #      (Wikidata marks the current/canonical value as preferred).
        #   2. Otherwise take the maximum across non-deprecated values —
        #      population generally grows over time so the largest value
        #      is typically the most recent; this is also the most
        #      conservative answer for the comparator semantics
        #      ("greater than 60M"): a recent value of 67M correctly
        #      verifies, an older 1900 value would not.
        candidate_values = []
        preferred_value = None
        for stmt in statements:
            if getattr(stmt, "rank", "normal") == "deprecated":
                continue
            v = _parse_quantity(str(stmt.value))
            if v is None:
                continue
            if getattr(stmt, "rank", "normal") == "preferred" and preferred_value is None:
                preferred_value = v
            candidate_values.append(v)
        if not candidate_values:
            return None
        kb_value = preferred_value if preferred_value is not None else max(candidate_values)

        if comparator == "gt":
            verified = kb_value > threshold
        else:  # lt
            verified = kb_value < threshold
        verdict = "verified" if verified else "contradicted"
        # WS5(a.2): return the comparison detail alongside the verdict so the
        # caller can append an observability edge and, on contradiction,
        # surface kb_value as the contradicting value.
        detail = {
            "kb_value": kb_value,
            "threshold": threshold,
            "comparator": comparator,
            "kb_property": meta.kb_property,
        }
        return _apply_polarity_str(verdict, claim.polarity), detail

    # ------------------------------------------------------------------
    # v0.16 WS6 T1: interval-from-events resolver (holds-at-T)
    #
    # Wikidata data-model limits this resolver respects (spec §F):
    #   * P580 (start) / P582 (end) are QUALIFIERS on a base-relation
    #     statement (P108 employer / P463 member-of / P39 position), NOT
    #     statements themselves — they cannot be looked up directly, only
    #     read off the base statement. This is why the *_started/_ended
    #     predicates route HERE (the resolver) rather than the generic
    #     kb_verifier value-compare path, whose _lookup_targets cannot
    #     interpret a `qualifier:Pxxx` object slot.
    #   * No event-relative bounds exist in the data model — only absolute
    #     dates. "before/after <event>" stays on the claim's valid_during_ref
    #     (extractor Rule 16); this resolver only reads absolute qualifier dates.
    #   * Day precision is the finest the parser keeps (_parse_time_value
    #     truncates to YYYY-MM-DD); many P580/P582 are year-precision only —
    #     the year-aware _value_matches / _normalize_date_value handle that.
    # ------------------------------------------------------------------

    def _gather_interval(
        self, claim: Claim, base_property: str, context: VerificationContext
    ) -> Optional[Interval]:
        """Resolve the subject's KB Q-id, look up `base_property` statements,
        and gather the P580/P582 qualifiers off the statement that matches the
        claim's org/object — plus any Tier U *_started/_ended endpoint rows for
        the same (asserting_party, subject, base-object).

        Returns an Interval, or None on any resolution / KB failure or ambiguity
        (§3.2 fail-closed: a None propagates to abstain, never a verdict). When
        multiple statements carry CONFLICTING starts for the matched org, prefer
        a `preferred`-ranked statement; if none is preferred, abstain (None) —
        we do NOT max dates (a max would fabricate an interval the KB does not
        assert)."""
        # Resolve the subject via the SAME resolver pattern _verify_kb_quantitative
        # uses (the kb_verifier's resolver wires the D47 Wikipedia normalizer).
        if self._kb_verifier is None or not hasattr(self._kb_verifier, "_resolver"):
            return None
        if self._kb is None:
            return None
        resolver = self._kb_verifier._resolver
        lookup_ctx = LocalContext(
            predicate=claim.predicate,
            slot_position="subject",
            asserting_party=claim.asserting_party,
            source_text=context.source_text,
            claim_subject=claim.subject,
            claim_predicate=claim.predicate,
            claim_object=claim.object,
            claim_id=claim.claim_id,
        )
        try:
            resolved = resolver.select(
                resolver.resolve(claim.subject, lookup_ctx), lookup_ctx
            )
        except Exception:
            return None
        if resolved is None:
            return None

        try:
            statements = self._kb.lookup_statements(resolved, base_property)
        except Exception:
            return None

        # An endpoint claim (`<relation>_started/_ended(subject, year)`) carries
        # only the DATE in its object slot — it never carries the org/value the
        # base statement points at (Claim has no `object_org` field). So there is
        # nothing to narrow the candidate set by: the interval is built from the
        # subject's full set of base statements, and _interval_from_statements
        # itself grounds against the UNIQUE-or-agreeing-start interval (a single
        # candidate, or a preferred-unique, or agreeing starts; conflicting
        # starts with no preferred discriminator -> abstain). The prior
        # org-narrowing branch read getattr(claim, 'object_org', None), which was
        # always None, so it was dead code (round-1 robustness follow-up: removed).
        kb_interval = self._interval_from_statements(statements)
        # DORMANT-MECHANISM NOTE (round-1 follow-up) — status_started/status_ended
        # (kb_property P571 inception / P576 dissolution): the KB arm here is
        # intentionally INERT for these two. _interval_from_statements reads the
        # P580/P582 *qualifiers* off the base statement, but for P571/P576 the
        # date is the statement VALUE itself, not a P580/P582 qualifier, so this
        # call yields no start/end for them. That is by design: status endpoints
        # ground via Tier U (below) or via the founded_in_year (P571) /
        # dissolved_in_year (P576) date-in-object predicates on the normal KB
        # path — NOT through this qualifier-gathering arm.

        # ALSO gather Tier U endpoint rows for the same predicate (the
        # *_started/_ended Tier U fact participates alongside the KB qualifier).
        tier_u_interval = self._tier_u_endpoint(claim, context)

        # Merge: KB is authoritative; Tier U fills an endpoint the KB left open.
        # A conflict (both known, different values) is NOT silently reconciled —
        # _interval_holds_at / _verify_interval_endpoint compare against the KB
        # value, and the Tier U value only fills a gap. (Keeping the merge
        # additive avoids fabricating an interval neither source asserts.)
        merged = kb_interval or Interval()
        if tier_u_interval is not None:
            if not merged.start_known and tier_u_interval.start_known:
                merged.start = tier_u_interval.start
                merged.start_known = True
            if not merged.end_known and tier_u_interval.end_known:
                merged.end = tier_u_interval.end
                merged.end_known = True
        if kb_interval is None and tier_u_interval is None:
            return None
        return merged

    def _interval_from_statements(
        self, statements: list
    ) -> Optional[Interval]:
        """Build an Interval from candidate base-relation statements' P580/P582
        qualifiers. Fail-closed on a conflicting start across statements unless
        exactly one is `preferred`-ranked.

        BEFORE_PRESENT and a missing/empty P582 both map to end_known=False
        (open / ongoing). A present P582 sets end_known=True."""
        if not statements:
            return None

        # Prefer a single `preferred`-ranked statement when present (Wikidata
        # marks the canonical/current value preferred). Else require the starts
        # to agree; conflicting starts with no preferred ranking → abstain.
        preferred = [s for s in statements if getattr(s, "rank", "normal") == "preferred"]
        if len(preferred) == 1:
            chosen = [preferred[0]]
        elif len(preferred) > 1:
            # Multiple preferred with potentially different starts — only safe
            # when their starts agree; else abstain.
            chosen = preferred
        else:
            chosen = statements

        # Round-1 robustness follow-up (WS6, defense-in-depth): the interval is
        # UNIQUELY identified iff it rests on a single base statement OR a single
        # preferred-ranked one. An interval built by collapsing several
        # statements that merely AGREE on a start is NOT unique — it must not
        # license a *_started/_ended contradiction (see _verify_interval_endpoint).
        unique = len(statements) == 1 or len(preferred) == 1

        starts = {
            self._iso_or_none(s.qualifiers.get("P580"))
            for s in chosen
        }
        starts.discard(None)
        if len(starts) > 1:
            # Conflicting starts, no single preferred discriminator → abstain.
            return None

        ends_raw = [s.qualifiers.get("P582") for s in chosen]
        ends = {self._iso_or_none(e) for e in ends_raw}
        ends.discard(None)
        # A genuine open end (some statement has NO P582) keeps the interval
        # open even if another statement records an end (ongoing dominates).
        any_open_end = any(
            (not e) or e == BEFORE_PRESENT for e in ends_raw
        )

        start = next(iter(starts)) if starts else None
        if len(ends) == 1 and not any_open_end:
            end = next(iter(ends))
            end_known = True
        elif len(ends) > 1:
            # Conflicting ends with no preferred discriminator → treat as open
            # rather than fabricate a single end.
            end = None
            end_known = False
        else:
            end = None
            end_known = False

        return Interval(
            start=start,
            end=end,
            start_known=start is not None,
            end_known=end_known,
            unique=unique,
        )

    @staticmethod
    def _iso_or_none(value) -> Optional[str]:
        """Normalize a qualifier value to a comparable ISO string, or None.
        BEFORE_PRESENT (the extractor's implicit-past end sentinel) maps to None
        so it is treated as an OPEN end (never forces a false holds_at)."""
        if value is None:
            return None
        if value == BEFORE_PRESENT:
            return None
        s = str(value).strip()
        return s or None

    def _tier_u_endpoint(
        self, claim: Claim, context: VerificationContext
    ) -> Optional[Interval]:
        """Gather a Tier U *_started/_ended endpoint fact for this claim into a
        one-sided Interval. The endpoint kind comes from the predicate suffix.
        Fail-open: any lookup failure returns None."""
        try:
            result = self._tier_u.lookup(claim, current_time=context.current_time)
        except Exception:
            return None
        if not getattr(result, "found", False):
            return None
        rows = getattr(result, "rows", None) or []
        if not rows:
            return None
        # Round-1 robustness follow-up (WS6): mirror the KB conflicting-start
        # abstention (_interval_from_statements). Instead of trusting an
        # arbitrary rows[0], collect the DISTINCT normalized endpoint dates
        # across ALL rows; if more than one distinct non-None date is present
        # the Tier U facts disagree on this endpoint — abstain (None) rather
        # than pick a row by accident. Symmetric with the KB path; removes the
        # arbitrary-row dependence.
        dates = {self._iso_or_none(r.get("object")) for r in rows}
        dates.discard(None)
        if len(dates) != 1:
            # Zero distinct dates (all None) -> nothing to ground; more than one
            # -> conflicting Tier U endpoints. Either way, abstain.
            return None
        date = next(iter(dates))
        if claim.predicate.endswith("_started"):
            return Interval(start=date, start_known=True)
        if claim.predicate.endswith("_ended"):
            return Interval(end=date, end_known=True)
        return None

    def _interval_holds_at(self, interval: Interval, T: str) -> str:
        """Three-valued holds-at-T table (spec §B.2). Lexical ISO compare —
        valid for zero-padded YYYY-MM-DD / YYYY strings.

        DORMANT-MECHANISM NOTE (round-1 follow-up): this is a forward-looking
        primitive (covered by unit tests) NOT yet consumed by any verdict path
        — the holds-at-T base-relation-scope consumer is deferred. Endpoint
        grounding today uses _verify_interval_endpoint's year-aware equality
        compare, not this holds-at scoping. Kept inert/tested for that future.

        Returns 'true' | 'false' | 'unknown'.

          start_known and start > T                         -> false (not begun)
          end_known and end < T                             -> false (already ended)
          start_known and end_known and start<=T<=end       -> true
          start_known, end UNKNOWN (open=ongoing), start<=T -> true
          start UNKNOWN, end_known, T<=end                  -> unknown
          both unknown                                      -> unknown
        """
        if not T:
            return "unknown"
        if interval.start_known and interval.start is not None and interval.start > T:
            return "false"
        if interval.end_known and interval.end is not None and interval.end < T:
            return "false"
        if (
            interval.start_known
            and interval.end_known
            and interval.start is not None
            and interval.end is not None
            and interval.start <= T <= interval.end
        ):
            return "true"
        if (
            interval.start_known
            and not interval.end_known
            and interval.start is not None
            and interval.start <= T
        ):
            return "true"
        # start unknown (end_known or not) → cannot place T within the interval.
        return "unknown"

    def _verify_interval_endpoint(
        self, claim: Claim, context: VerificationContext, trace
    ) -> Optional[tuple[str, dict]]:
        """Verdict for a *_started / *_ended endpoint claim (spec §B.3).

        endpoint kind: `_started` -> P580 (start), `_ended` -> P582 (end).
        The claim's object is the asserted year/date; compare it against the KB
        qualifier date via the year-aware _value_matches. Match -> verified; a
        single_valued/functional mismatch with a value-type-gate-satisfying
        resolved date -> contradicted; else None (abstain).

        FAIL-CLOSED: returns None on any resolution/KB error, ambiguity, or
        unknown endpoint (§3.2). Returns (verdict, grounding_chain) on a
        terminal verdict, having emitted a kb_statement trace edge + provenance
        premise. Carries contradicting_value (WS5) on a contradiction and a
        retractable resolution-cache row id (WS3) when one was touched."""
        pred = claim.predicate
        if pred.endswith("_started"):
            qual_prop = "P580"
        elif pred.endswith("_ended"):
            qual_prop = "P582"
        else:
            return None

        if not claim.object:
            return None

        # Read the base KB property + single_valued off predicate metadata
        # (NO hardcoded Python table — the knowledge lives in the seed/oracle).
        try:
            meta = self._substrate.predicate_translation.consult(pred)
        except Exception:
            return None
        base_property = meta.kb_property
        if not base_property:
            return None

        interval = self._gather_interval(claim, base_property, context)
        if interval is None:
            return None

        kb_date = interval.start if qual_prop == "P580" else interval.end
        endpoint_known = interval.start_known if qual_prop == "P580" else interval.end_known
        if not endpoint_known or kb_date is None:
            # The KB records no value for this endpoint (open end / unknown
            # start) — abstain rather than contradict (absence is not evidence).
            return None

        # Capture the retractable resolution-cache row id the subject resolution
        # touched (WS3 D13) — the dependency a correction would retract.
        _last_row_id = getattr(self._kb_verifier._resolver, "last_cache_row_id", None)
        cache_row_id = _last_row_id() if callable(_last_row_id) else None

        # Year-aware compare (reuses the kb_verifier helper): the claim object
        # is the asserted year/date, kb_date is YYYY-MM-DD / YYYY.
        matches = _value_matches(kb_date, claim.object)

        edge_md = {
            "source": "kb_interval",
            "qualifier": qual_prop,
            "endpoint_value": kb_date,
            "predicate": pred,
            "kb_property": base_property,
            "claim_object": claim.object,
        }

        if matches:
            verdict = _apply_polarity_str("verified", claim.polarity)
            edge_md["verdict"] = verdict
            trace.source_breakdown["kb"] = trace.source_breakdown.get("kb", 0) + 1
            trace.edges.append(TraceEdge(
                edge_type="premise_lookup",
                source=trace.root,
                target=TraceNode("kb_statement", {"entity": claim.subject}),
                metadata=edge_md,
            ))
            self._record_premise(
                trace, source="kb", table="entity_resolution_cache",
                row_id=cache_row_id, assertion=False,
            )
            grounding = {
                "source": "kb_interval",
                "predicate": pred,
                "qualifier": qual_prop,
                "endpoint_value": kb_date,
                "kb_property": base_property,
            }
            return verdict, grounding

        # Mismatch. Only a functional (single_valued) endpoint with a resolved
        # date value-type that satisfies the value-type gate may contradict;
        # else abstain (§3.2 — multi-valued endpoints just hold other values).
        if not meta.single_valued:
            return None
        # Round-1 robustness follow-up (WS6, defense-in-depth): a CONTRADICTED
        # endpoint verdict is licensed ONLY when the interval was built from a
        # UNIQUELY-identified statement (single candidate / preferred-unique),
        # never from an arbitrary collapse of all the subject's statements that
        # merely happened to agree on a start. Otherwise a future single_valued
        # endpoint binding could false-contradict against a value that is just
        # one of several the KB holds. Abstain when the interval is non-unique.
        # (All 8 endpoint seeds are single_valued=0 today, so this branch is
        # unreachable now — it is forward-defensive.)
        if not interval.unique:
            return None
        # Value-type gate: the KB endpoint must normalize to a date for a
        # `time` object_type predicate to contradict (mirrors kb_verifier's
        # _contradiction_value_type_ok for object_type 'time' -> {date,literal}).
        if _normalize_date_value(kb_date) is None:
            return None

        verdict = _apply_polarity_str("contradicted", claim.polarity)
        edge_md["verdict"] = verdict
        if verdict == "contradicted":
            edge_md["contradicting_value"] = kb_date
            edge_md["contradicting_value_type"] = "time"
        trace.source_breakdown["kb"] = trace.source_breakdown.get("kb", 0) + 1
        trace.edges.append(TraceEdge(
            edge_type="premise_lookup",
            source=trace.root,
            target=TraceNode("kb_statement", {"entity": claim.subject}),
            metadata=edge_md,
        ))
        self._record_premise(
            trace, source="kb", table="entity_resolution_cache",
            row_id=cache_row_id, assertion=False,
        )
        grounding = {
            "source": "kb_interval",
            "predicate": pred,
            "qualifier": qual_prop,
            "endpoint_value": kb_date,
            "kb_property": base_property,
            "verdict": verdict,
        }
        if verdict == "contradicted":
            grounding["contradicting_value"] = kb_date
            grounding["contradicting_value_type"] = "time"
        return verdict, grounding

    def _is_persona_subject(self, claim: Claim) -> bool:
        """Phase 10.5 Step 6 Batch 8+: True when the claim's subject is
        a known user persona stipulated in Tier U via a `user identity X`
        row. Used by `_direct_lookup` to skip KB verification — the
        entity resolver would otherwise resolve the persona name to an
        unrelated Wikidata entity (e.g. "Asa" → Asa, King of Judah) and
        the kb_verifier's polarity-aware branch would emit
        verified/contradicted on facts about that wrong entity.

        Specifically catches: rows of the shape
        `(asserting_party, 'user', 'identity', X, polarity=1)` where X
        matches the claim's subject. The asserting_party check is the
        claim's asserting_party so the persona is scoped to who's
        asking — different parties can stipulate different personas.
        """
        if not claim.subject:
            return False
        # Defensive: a mock TierU might not expose this attribute.
        try:
            db = self._tier_u._db  # type: ignore[attr-defined]
        except AttributeError:
            return False
        try:
            row = db.execute(
                """SELECT 1 FROM tier_u
                   WHERE asserting_party=? AND subject='user'
                     AND predicate='identity' AND object=?
                     AND polarity=1 AND retracted_at IS NULL""",
                (claim.asserting_party, claim.subject),
            ).fetchone()
        except Exception:
            return False
        return row is not None

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

    def _discover_chains(
        self, node: Claim, trace: JustificationTrace, depth: int
    ) -> tuple[list[Claim], int]:
        """v0.16 WS2 §2: LIBERAL chain discovery + SOUND per-edge verification.

        Replaces `_expand_via_substrate`. Composes the (now un-gated)
        subsumption-neighbor expansion with the new premise-forward frontier,
        proposing candidate substitution claims WITHOUT the distribution gate
        foreclosing relations. Each candidate is admitted to the returned
        frontier only if `_verify_chain` confirms the taxonomy/transitive edge
        in a source (§3.2 never-false-verify: soundness lives at verify time).

        The walker does not emit a predicate-equivalence expansion edge: an
        equivalent predicate shares the same `kb_property`, so its KB lookup is
        identical to the original's, and `TierU.lookup` stage 3 already
        broadens by the same `predicate_translation` oracle (D7).

        Discovery sources (no gate; distribution demoted to RANKER per §3):
          1. Subsumption-neighbor expansion. For each relation_type, consult
             predicate_distribution to learn the PREFERRED direction (ranking
             hint only — `neither` no longer skips the relation), gather
             substrate `find_neighbors` AND (now un-capped, §5) KB
             `enumerate_neighbors` candidates, ORDER preferred-direction
             neighbors first, and admit each via `_verify_chain`.
          2. Premise-forward expansion (`_expand_from_premises`, §4): seed a
             forward frontier from Tier U facts about the goal's subject and
             expand via bounded OUTGOING KB edges to meet the goal's object.

        Trace edges carry the existing keys PLUS a `discovery_source`
        observability key (`"subsumption_neighbor"` | `"premise_forward"`).
        """
        expanded: list[Claim] = []
        llm_delta = 0

        for relation_type in ("is_a", "part_of"):
            try:
                dist = self._substrate.predicate_distribution.consult(
                    node.predicate, node.polarity, relation_type
                )
                llm_delta += (0 if dist.was_cached else 1)
            except Exception:
                continue

            # v0.16 WS2 §3: distribution is a RANKER, not a gate. `neither` no
            # longer forecloses the relation — it deprioritizes it (empty
            # preferred set ranks every direction last). Soundness is enforced
            # downstream in _verify_chain (the transitive-path/substrate edge
            # check), so a wrong `neither` can no longer cause a false-abstain
            # by skipping a genuinely-entailing chain, and a wrong non-`neither`
            # can no longer admit an unentailed chain.
            preferred = _distribution_directions(dist.verdict)
            verdict_label = (
                dist.verdict.value if hasattr(dist.verdict, "value")
                else str(dist.verdict)
            )

            sub_produced: list[Claim] = []
            for slot in ("subject", "object"):
                slot_val = node.subject if slot == "subject" else node.object
                if not slot_val:
                    continue
                entity_ref = EntityRef(namespace="aedos", identifier=slot_val)
                try:
                    sub_neighbors = self._substrate.subsumption.find_neighbors(
                        entity_ref, relation_type
                    )
                except Exception:
                    continue
                # Rank: preferred-direction neighbors first (ranking hint),
                # but KEEP all of them (no direction gate, §3).
                ranked = sorted(
                    sub_neighbors,
                    key=lambda s: 0 if s.direction in preferred else 1,
                )
                for sub in ranked:
                    new_id = sub.entity.identifier
                    # SOUND admission: confirm the taxonomy edge before
                    # substituting. _verify_chain routes child->parent through
                    # the KB transitive primitive (Q-ids) or the substrate
                    # consult (aedos surface forms), or — for intensional is_a
                    # with no KB path — the distribution kind-entailment verdict.
                    if not self._verify_chain(
                        node, sub.entity.identifier, sub.direction,
                        relation_type, slot, dist.verdict, trace,
                    ):
                        continue
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
                            "distribution": verdict_label,
                            "subsumption_row_id": sub.row_id,
                            "discovery_source": "subsumption_neighbor",
                            "polarity": node.polarity,
                        },
                    ))
                    # WS3 single-source-of-truth: record the subsumption row as
                    # a provenance premise so the dep lives in the provenance
                    # TERM (not only in edge metadata). _extract_source_rows
                    # short-circuits on provenance.source_rows(), so a dep that
                    # is only in metadata is dropped from the term AND from the
                    # retraction footprint. The term is the single source of
                    # truth — the edge metadata is observability only.
                    self._record_premise(
                        trace, source="subsumption",
                        table="subsumption", row_id=sub.row_id,
                    )
                    sub_produced.append(new_node)
            expanded.extend(sub_produced)

            # v0.16 WS2 §5: the depth==0 cap on KB-neighbor enumeration is
            # REMOVED. The D51 18-min blowup was a multiplicative-fanout
            # problem; it is now bounded structurally rather than by a depth
            # cap: (a) _verify_chain admits only CONFIRMED edges to the
            # frontier (collapsing the un-verified-substitution fanout the cap
            # was guarding against), (b) the premise-forward frontier (§4)
            # seeds expansion from a bounded Tier U premise set rather than
            # blind taxonomy enumeration, and (c) the existing
            # _DEFAULT_NEIGHBOR_REVERSE_LIMIT=20 + the walk loop's max_depth +
            # wall-clock/LLM budget remain as the cost bounds. Removing the cap
            # is required for the bidirectional/forward search (contract item e).
            if not sub_produced:
                kb_produced = self._expand_via_kb_neighbors(
                    node, relation_type, preferred, dist.verdict, trace
                )
                expanded.extend(kb_produced)

        # Discovery source 2: premise-forward frontier (§4).
        try:
            premise_forward = self._expand_from_premises(node, trace)
            expanded.extend(premise_forward)
        except Exception:
            pass

        return expanded, llm_delta

    def _verify_chain(
        self,
        node: Claim,
        neighbor_id: str,
        direction: str,
        relation_type: str,
        slot: str,
        distribution_verdict,
        trace: JustificationTrace,
    ) -> bool:
        """v0.16 WS2 §2/§3.2/§6: the SOUND per-edge admission check.

        Where `_discover_chains` proposes liberally, this gate admits a
        substitution ONLY IF the taxonomy/transitive edge actually holds in a
        source — preserving the §3.2 never-false-verify invariant now that the
        distribution gate is demoted to a ranker. A `neither` predicate (e.g.
        `prefers × is_a`) is still explored, but rejected here, so the OUTCOME
        is identical to the old gate (no_grounding_found) for a SOUND reason
        (verify-time rejection, not discovery-time skip).

        Duality note (§2b): this handles the SLOT-SUBSTITUTION case (the goal
        slot is a taxonomic ancestor/descendant of a grounded premise's slot).
        The verifier-internal SPARQL ASK (`kb_verifier._subsumption_upgrades`)
        handles the dual SUBJECT-FIXED/value-subsumption case (a KB statement
        value is more specific than the claimed value). They are duals, not
        duplicates, and must not be merged.

        Two-axis soundness (contract item g — the predicate_distribution split):
          - STRUCTURAL EDGE (does the taxonomy hop hold in a source?): confirmed
            by the KB transitive primitive when both endpoints resolve to Q-ids,
            else by `SubsumptionOracle.consult` (KB -> substrate -> LLM, §6) on
            the aedos surface forms. For find_neighbors-derived candidates the
            substrate row already exists, so consult resolves to that row (the
            sound evidence) without an LLM call; only a genuinely-absent edge
            reaches the LLM last-resort authority.
          - ENTAILMENT VALIDITY (does the PREDICATE transfer across that hop?):
            * `part_of` containment is a GRAPH property: locative-containment
              predicates distribute over part_of, so a confirmed structural edge
              IS sufficient and the distribution verdict is a pure RANKER here.
            * `is_a` kind-entailment is a PREDICATE-SEMANTICS property: no KB
              transitive path expresses "mortal distributes_down", so the
              distribution oracle is the kind-entailment AUTHORITY — a confirmed
              is_a structural edge is admitted ONLY IF the distribution verdict
              is non-`neither`. This is exactly why `prefers × is_a` (verdict
              `neither`) is REJECTED even though golden_retriever is_a dog holds
              structurally: the edge is real, but `prefers` does not distribute.

        The §3.2 never-false-verify invariant lives entirely here: with the
        distribution gate demoted to a ranker, a `neither` predicate is still
        EXPLORED but its substitution is REJECTED (verify-time), giving the same
        OUTCOME as the old gate for a sound reason.

        The WS3 nogood cache is consulted FIRST (entailment-safety): a nogood
        flagging "this path does NOT hold" vetoes the edge before any
        confirmation. Optional / fail-open no-op until WS3 wires the cache.
        """
        slot_val = node.subject if slot == "subject" else node.object

        # Orient the edge child -> parent.
        if direction == "parent":
            child_surface, parent_surface = slot_val, neighbor_id
        else:  # "child": the neighbor is subsumed by the slot value
            child_surface, parent_surface = neighbor_id, slot_val

        # is_a kind-entailment authority gate: the distribution oracle decides
        # whether the predicate transfers across an is_a hop at all. A `neither`
        # verdict forecloses is_a substitution regardless of a real structural
        # edge (the `prefers × is_a` case). part_of containment is a graph
        # property — distribution does not gate it (pure ranker, §3b).
        if relation_type == "is_a":
            dv = (
                distribution_verdict.value
                if hasattr(distribution_verdict, "value")
                else distribution_verdict
            )
            if not dv or dv == "neither":
                return False

        # --- Structural edge confirmation ---
        # 1. KB transitive-path (sound, when both endpoints resolve to Q-ids).
        if self._kb is not None:
            child_qid = self._resolve_qid(node, child_surface, slot)
            parent_qid = self._resolve_qid(node, parent_surface, slot)
            if child_qid and parent_qid:
                # WS3 nogood cache consult FIRST (entailment-safety): a nogood
                # vetoes a path flagged "does NOT hold". Optional — fail-open
                # no-op until WS3 wires the cache. A True veto rejects the edge.
                if self._nogood_vetoes(child_qid, parent_qid, relation_type):
                    return False
                try:
                    tp = self._kb.verify_transitive_path(
                        child_qid, parent_qid, None, relation_type=relation_type
                    )
                except Exception:
                    tp = None
                if tp is not None and getattr(tp, "error", None) is None:
                    # The KB authoritatively answered (no fail-open error):
                    # honor BOTH its verdicts. A definite hold confirms the
                    # edge; a definite non-hold is a NEGATIVE answer — return
                    # False here rather than falling through to the
                    # substrate/LLM consult, which could fabricate a positive
                    # (Priority-3) over an authoritative KB negative (§3.2:
                    # never false-verify). Only an UNAVAILABLE answer (tp is
                    # None, or tp.error set => fail-open) may fall through.
                    return bool(tp.holds)
                # KB unavailable (no result / fail-open error) — fall through
                # to the substrate/LLM consult on the aedos surface forms.

        # 2. Substrate consult (KB -> substrate -> LLM, §6) on aedos surface forms.
        try:
            verdict = self._substrate.subsumption.consult(
                EntityRef("aedos", child_surface),
                EntityRef("aedos", parent_surface),
                relation_type,
            )
            v = verdict.verdict
            v = v.value if hasattr(v, "value") else v
            # child -> parent edge: consistent verdicts are "child subsumed by
            # parent" (a=child, b=parent => a_subsumed_by_b) or equivalent.
            if v in ("a_subsumed_by_b", "equivalent"):
                return True
        except Exception:
            pass

        return False

    def _resolve_qid(self, node: Claim, surface: str, slot: str) -> Optional[str]:
        """Resolve a surface form to a KB Q-id via the substrate resolver.
        Returns the Q-id string (starting with 'Q') or None. Fail-open: any
        resolution failure / non-Q candidate returns None (the caller falls
        back to the substrate consult path). Never raises."""
        if not surface:
            return None
        # An already-resolved Q-id (e.g. a KB-enumerated neighbor) passes
        # through without a redundant resolver round-trip.
        if surface.startswith("Q") and surface[1:].isdigit():
            return surface
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
            candidates = self._substrate.resolver.resolve(surface, lc)
        except Exception:
            return None
        if not candidates:
            return None
        qid = candidates[0].kb_identifier
        if qid and qid.startswith("Q"):
            return qid
        return None

    def _nogood_vetoes(
        self, child_qid: str, parent_qid: str, relation_type: str
    ) -> bool:
        """v0.16 WS3 hook (entailment-safety): consult the bounded nogood
        cache (substrate_exceptions) for a flag that this transitive path does
        NOT hold for (relation_type, child -> parent). Until WS3 wires the
        `SubstrateExceptionCache`, the lookup is an additive fail-open no-op
        (always returns False — no veto). The walker accesses the optional
        cache via `getattr` so this works before WS3 lands and after, with no
        signature change here."""
        cache = getattr(self, "_exception_cache", None)
        if cache is None:
            cache = getattr(self._kb_verifier, "_exception_cache", None)
        if cache is None:
            return False
        try:
            return bool(
                cache.is_nogood(
                    relation_type=relation_type,
                    source_identifier=child_qid,
                    target_identifier=parent_qid,
                )
            )
        except Exception:
            return False

    def _expand_from_premises(
        self, node: Claim, trace: JustificationTrace
    ) -> list[Claim]:
        """v0.16 WS2 §4: premise-forward frontier.

        Seed a forward frontier from Tier U facts about the goal's subject and
        expand via bounded OUTGOING KB edges, meeting the goal's object.

        For goal P(S, O): the walker already has Tier U premises about S with a
        DIFFERENT object O' (surfaced by `lookup_object_conflict`). Premise-
        forward resolves O' to a Q-id, confirms the goal's object O is reachable
        from O' via the part_of transitive path
        (`verify_transitive_path(O'_qid, O_qid, part_of)`), and — when it grounds
        — emits a `premise_forward` trace edge and the substituted claim P(S, O')
        as a candidate (which the walk loop re-looks-up against the grounding
        Tier U premise). This is the BIDIRECTIONAL meet: subsumption-neighbor
        discovery expands DOWN from the goal object; premise-forward expands UP
        from the premise object; they meet in the middle. It replaces the
        depth==0 cap (§5) as the cost-control mechanism (the forward frontier is
        seeded by a small set of premises, not blind enumeration).

        Fail-open: any resolution/KB failure returns no candidate for that
        premise; never raises. Bounded by the OUTGOING (un-LIMIT'd, naturally
        small) fanout of `verify_transitive_path` and the walk loop's max_depth.
        """
        if self._kb is None:
            return []
        if node.polarity != 1 or not node.object:
            return []

        try:
            oc_result = self._tier_u.lookup_object_conflict(node)
        except Exception:
            return []
        if not oc_result.found:
            return []

        # Soundness gate: the substitution P(S,O') ⊢ P(S,O) over `O' part_of O`
        # is valid ONLY IF the PREDICATE distributes UP over part_of (the
        # definition: P(X) and X part_of Y => P(Y); here X=O' the child place,
        # Y=O the ancestor). Consult predicate_distribution the same way the
        # is_a arm of _verify_chain does, and admit ONLY a distributes_up/both
        # verdict. A `neither` or down-only verdict FORECLOSES the substitution
        # (a non-distributing place predicate must not ride a part_of edge to a
        # false). Fail-closed (abstain) on any absent/uncertain verdict — §3.2
        # never-false-verify.
        try:
            dist = self._substrate.predicate_distribution.consult(
                node.predicate, node.polarity, "part_of"
            )
        except Exception:
            return []
        dv = dist.verdict.value if hasattr(dist.verdict, "value") else dist.verdict
        if dv not in ("distributes_up", "both"):
            return []

        goal_qid = self._resolve_qid(node, node.object, "object")
        if not goal_qid:
            return []

        expanded: list[Claim] = []
        for row in oc_result.rows:
            premise_obj = row.get("object")
            if not premise_obj:
                continue
            premise_qid = self._resolve_qid(node, premise_obj, "object")
            if not premise_qid:
                continue
            # Premise object O' reaches goal object O via part_of? (O' is the
            # more specific place; O the ancestor — Williamstown -> US.)
            try:
                tp = self._kb.verify_transitive_path(
                    premise_qid, goal_qid, None, relation_type="part_of"
                )
            except Exception:
                continue
            if tp is None or not getattr(tp, "holds", False):
                continue
            # Grounded: the goal is P(S, O), the premise is P(S, O'), and
            # O' part_of O. Substitute the goal object with the premise object
            # so the walk loop re-looks-up the grounded premise claim.
            new_node = _claim_from_parts(node, object_val=premise_obj)
            trace.edges.append(TraceEdge(
                edge_type="premise_forward",
                source=TraceNode("claim", {"object": node.object}),
                target=TraceNode("claim", {"object": premise_obj}),
                metadata={
                    "relation_type": "part_of",
                    "direction": "child",
                    "discovery_source": "premise_forward",
                    "premise_object_qid": premise_qid,
                    "goal_object_qid": goal_qid,
                    "tier_u_row_id": row.get("id"),
                    "establishing_property": getattr(tp, "establishing_property", None),
                    "polarity": node.polarity,
                },
            ))
            expanded.append(new_node)

        return expanded

    def _expand_via_kb_neighbors(
        self,
        node: Claim,
        relation_type: str,
        preferred: set[str],
        distribution_verdict,
        trace: JustificationTrace,
    ) -> list[Claim]:
        """Phase H D5 + D51: enumerate KB neighbors of `node`'s slot entities
        and emit expanded claims with the slot substituted by each neighbor.

        Fires as the DISCOVERY enumerator when `find_neighbors` produced no
        substrate expansion for `relation_type` (cheapest-path-first). v0.16
        WS2 §3/§5: the distribution verdict is a RANKER (not a gate) and the
        depth==0 cap is gone — BOTH directions (parent via outgoing, child via
        incoming) are now enumerated regardless of the distribution verdict;
        `preferred` only ORDERS the calls (preferred direction first). Like the
        substrate-neighbor candidates, KB-enumerated neighbors are routed
        through `_verify_chain` (SS3 symmetry): the single enumeration hop is
        structural evidence the neighbor EXISTS, but NOT that the substitution
        is ENTAILED — an is_a `neither` predicate or an unentailed downward hop
        must be rejected by the same gate. The neighbor is a Q-id, so the gate's
        `_resolve_qid` passes it through and the transitive-path/is_a
        distribution check adjudicates it; the per-walk fanout budget (§5)
        bounds the cost the removed depth cap once guarded. The trace records
        each direction (`"parent"` / `"child"`) and the KB property used.

        Direction mapping (D5 + D51):
          - `"parent"`: `enumerate_neighbors(direction="outgoing")` — yields E's
            parents (entities E points to via the relation's property set).
          - `"child"`: `enumerate_neighbors(direction="incoming")` — yields E's
            children (entities pointing to E). D51 (2026-05-24).

        Fail-open: any failure in resolution, KB call, or parsing returns no
        expansion for the affected slot; never raises.
        """
        if self._kb is None:
            return []
        properties = list(_D5_NEIGHBOR_PROPS_BY_RELATION.get(relation_type, ()))
        if not properties:
            return []

        # v0.16 WS2 §3: both directions fire (gate removed); `preferred` ranks
        # the enumeration order (the distribution-preferred direction first).
        all_calls = [("parent", "outgoing"), ("child", "incoming")]  # (walker_dir, kb_dir)
        kb_calls = sorted(all_calls, key=lambda c: 0 if c[0] in preferred else 1)

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
                        # v0.16 soundness (SS3 symmetry): route every KB-enum
                        # candidate through the SAME entailment gate the
                        # find_neighbors arm uses. The single enumeration hop
                        # is structural evidence the neighbor EXISTS, but it is
                        # not evidence the SUBSTITUTION is entailed: an is_a
                        # `neither` predicate, or an unentailed downward hop,
                        # must be rejected here exactly as for substrate
                        # neighbors. The neighbor is a Q-id, so _resolve_qid
                        # passes it through and the transitive-path/is_a
                        # distribution gate adjudicates it. Skip on rejection.
                        if not self._verify_chain(
                            node, neighbor_qid, walker_dir,
                            relation_type, slot, distribution_verdict, trace,
                        ):
                            continue
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
                                "discovery_source": "kb_neighbor_enumeration",
                                "polarity": node.polarity,
                            },
                        ))
                        expanded.append(new_node)

        return expanded
