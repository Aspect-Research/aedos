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
            trace.chain_includes_assertion = True

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
            if flipped_status == "asserted_unverified":
                trace.chain_includes_assertion = True
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
                    },
                ))
                if oc_status == "asserted_unverified":
                    trace.chain_includes_assertion = True
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
            # assertion-conditional.
            trace.chain_includes_assertion = True
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
            verdict = self._verify_kb_quantitative(node, context, trace)
            if verdict is not None:
                trace.source_breakdown["kb"] = trace.source_breakdown.get("kb", 0) + 1
                return verdict, "kb_quantitative", 0, {
                    "source": "kb_quantitative",
                    "predicate": node.predicate,
                }

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
                grounding = {
                    "source": "kb",
                    "entity": kb_result.subject_kb_id,
                    "kb_property": kb_result.trace.get("kb_property"),
                    "lookup_inverted": kb_result.trace.get("lookup_inverted"),
                }
                return "verified", "kb", 0, grounding
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
                grounding = {
                    "source": "kb",
                    "entity": kb_result.subject_kb_id,
                    "kb_property": kb_result.trace.get("kb_property"),
                    "verdict": "contradicted",
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
                grounding = {
                    "source": "python",
                    "output": str(getattr(py_result, "output", "")),
                    "verdict": py_result.verdict,
                }
                return py_result.verdict, "python", 0, grounding

        return None, "", 0, {}

    def _verify_kb_quantitative(self, claim: Claim, context: VerificationContext, trace) -> Optional[str]:
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
        return _apply_polarity_str(verdict, claim.polarity)

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
            # and substrate produced nothing. Capped to depth==0 (the
            # initial frontier) — without this cap, the D51 reverse-direction
            # fanout (~20 entities per call × 5 properties × 2 slots × 2
            # relations × per-depth multiplication) blew past 18-min per-case
            # wall-clock in the first D51 diagnostic. KB-enumerated
            # children become Q-id-keyed claims that the walker still
            # explores via Tier U / KB verifier; further KB enumeration
            # on those Q-id claims rarely yields useful premises and
            # multiplies cost catastrophically.
            if not sub_produced and depth == 0:
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
