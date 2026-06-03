from __future__ import annotations

import threading
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


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class VerificationContext:
    current_time: str
    asserting_party: str
    # The full input text the extractor was originally called
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
    # multiplicatively across depth (the 18-min / OOM blowup). The
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


# Helpers for the kb_quantitative path.
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
    """True if the object is a descriptive class reference rather than a
    specific entity name.

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


def _vague_class_head(object_value: str) -> Optional[str]:
    """Extract the bare CLASS-noun head from a vague-class object phrase, or
    None if no head can be isolated.

    "a town in the United States"          -> "town"
    "a state that borders New York"        -> "state"
    "an institution founded before 1800"   -> "institution"
    "some river"                           -> "river"

    The head is the noun phrase BETWEEN the indefinite-article prefix and the
    first restrictive modifier (a preposition like "in"/"of"/"that"/relative
    clause). The walker resolves THIS head to a KB class Q-id and confirms the
    subject's instance/subclass path to it — the qualifier ("in the United
    States") is intentionally dropped: the §3.2-sound check is "subject is_a
    <class>" via P31/P279 authority, and a positive path can only HOLD when the
    membership is real. The qualifier is NOT used to broaden a positive (it
    could only tighten one, and the KB path already enforces the tightening
    through the resolved class).
    """
    if not object_value:
        return None
    obj = object_value.strip()
    if not obj:
        return None
    lower = obj.lower()
    head = None
    for prefix in _VAGUE_CLASS_PREFIXES:
        if lower.startswith(prefix):
            head = obj[len(prefix):]
            break
    if head is None:
        return None
    head = head.strip()
    if not head:
        return None
    # Cut at the first restrictive modifier: a preposition or relative-clause
    # marker. Leaves the bare class-noun head ("town", "state", "institution").
    cut_markers = (
        " that ", " which ", " where ", " whose ", " who ",
        " in ", " of ", " on ", " at ", " from ", " with ", " near ",
        " founded ", " located ", " bordering ", " borders ", " using ",
    )
    head_lower = head.lower()
    cut = len(head)
    for marker in cut_markers:
        idx = head_lower.find(marker)
        if idx != -1 and idx < cut:
            cut = idx
    head = head[:cut].strip()
    return head or None


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
        valid_from_ref=template.valid_from_ref,
        valid_until_ref=template.valid_until_ref,
    )


def _apply_assertion_designation(base_verdict: str, trace: JustificationTrace) -> str:
    """Convert a base verdict to its
    `_given_assertion` dual designation when the chain composition
    includes an asserted-unverified premise (or when the walk was
    pre-flagged for a `user_authoritative` claim).

    Imports lazily inside the function to avoid a circular import
    (aggregator imports from trace; walker imports trace; if walker
    imports aggregator at module load we get a cycle).
    """
    if not trace.chain_includes_assertion:
        return base_verdict
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

        The kwarg path is the new wiring; the dict path
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
        # The KB adapter is threaded explicitly so the walker
        # can call `enumerate_neighbors` directly. None disables the
        # KB-neighbor fallback (back-compat for test paths that construct the walker
        # without a KB).
        self._kb = kb
        # v0.16 WS3 §3D: the bounded nogood cache for _nogood_vetoes. When None
        # the helper falls back to the kb_verifier's cache (then no-op), so the
        # explicit wiring here and the fallback both work.
        self._exception_cache = exception_cache
        # v0.16.2 Phase C: the two per-walk mutable flags below live in
        # thread-local storage so the SAME Walker can verify claims concurrently
        # (one thread per claim) without one walk's flags leaking into another's.
        # `_user_authoritative_walk` gates whether KB grounding runs, so a cross-
        # walk race there would be a §3.2 hazard — thread-local removes it.
        # Exposed via same-named properties so all existing read/write sites are
        # unchanged. (KBVerifier is stateless per verify(); the resolver's own
        # per-resolve state is likewise thread-local — see resolver.py.)
        self._tls = threading.local()

    @property
    def _excluded_tier_u_row_ids(self) -> set:
        return getattr(self._tls, "excluded_tier_u_row_ids", set())

    @_excluded_tier_u_row_ids.setter
    def _excluded_tier_u_row_ids(self, value: set) -> None:
        self._tls.excluded_tier_u_row_ids = value

    @property
    def _user_authoritative_walk(self) -> bool:
        return getattr(self._tls, "user_authoritative_walk", False)

    @_user_authoritative_walk.setter
    def _user_authoritative_walk(self, value: bool) -> None:
        self._tls.user_authoritative_walk = value

    def walk(
        self,
        claim: Claim,
        context: VerificationContext,
        budget: Optional[WalkerBudget] = None,
        excluded_tier_u_row_ids: Optional[set[int]] = None,
    ) -> WalkResult:
        """Verify `claim` against the three typed sources of belief.

        `excluded_tier_u_row_ids`
        lets the caller suppress specific Tier U rows from the lookup —
        used by the promote-then-walk corpus runner / chat-wrapper to
        prevent the walker from matching the claim's own freshly-promoted
        asserted_unverified row at Stage 1. With the row filtered out,
        the polarity-conflict and object-conflict belief-revision paths
        in `_direct_lookup` become reachable for cases like
        "Asa is not a student" where the corpus
        relies on the walker finding the prior of opposite polarity.
        Empty / None means no filtering (idempotent
        promotions don't get filtered because the row pre-dates this
        walk's promotion attempt — those cases the walker should still
        match).
        """
        self._excluded_tier_u_row_ids: set[int] = excluded_tier_u_row_ids or set()
        if budget is None:
            # Build a budget from the Walker's config-driven defaults.
            # Each field falls back to the dataclass default
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
        # reason the extractor dropped them). subject_absent_from_source
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

        # Per-walk routing flag. When set, external (KB / Python) grounding is
        # structurally unreachable for this walk's claims — the claim is the
        # user's own (user_authoritative predicate, or a stipulated
        # user-persona subject), so it grounds only in Tier U and every verdict
        # is conditional on the user's assertion. See _try_external_grounding.
        self._user_authoritative_walk = False

        # v0.16.1 WS5b: a persona-subject claim is the asking user, not a KB
        # entity. When the claim's subject is a stipulated user identity for
        # this asserting_party (tier_u.has_identity), route the claim
        # user_authoritative exactly as a user_authoritative-predicate claim:
        # set the chain flag (verdict family becomes *_given_assertion) AND make
        # external grounding structurally unreachable, so the entity resolver
        # never sees the persona name (it would otherwise misresolve "Asa" →
        # "Asa, King of Judah" and emit a §3.2 false verified/contradicted on a
        # negation like "Asa is not in France"). This replaces the deleted
        # _is_persona_subject KB-skip guard with a route at walk entry.
        # Defensive: a mock TierU in a unit test may not implement
        # has_identity; treat a missing method / failure as "not a persona"
        # (the conservative direction — at worst a missing route, never a
        # false verdict).
        _has_identity = getattr(self._tier_u, "has_identity", None)
        try:
            persona_subject = bool(
                _has_identity(context.asserting_party, claim.subject)
            ) if _has_identity is not None else False
        except Exception:
            persona_subject = False

        route = self._predicate_routing(claim.predicate)

        # user_subject_required anomaly guard (v0.16.1 WS5b, relocated from the
        # deleted Layer-2 Validator). A predicate flagged user_subject_required
        # (e.g. `prefers`, `believes`) asserted about a subject that is NEITHER
        # the asserting party itself NOR a stipulated user persona is
        # malformed — a first-person predicate attributed to a third party.
        # FAIL-CLOSED: short-circuit to a base no_grounding_found / anomaly
        # abstain BEFORE any Tier U / KB / Python lookup, so it can only ever
        # become an abstain, never a verdict (it must not reach the entity
        # resolver to misresolve + false-contradict).
        if (
            self._predicate_user_subject_required(claim.predicate)
            and claim.subject != context.asserting_party
            and not persona_subject
        ):
            trace.walk_metadata["short_circuit"] = "user_subject_required"
            trace.polarity_trace = []
            return WalkResult(
                verdict="no_grounding_found",
                trace=trace,
                abstention_reason="user_subject_required",
                budget_consumption=BudgetConsumption(wall_clock_ms=0.0, llm_calls=0),
            )

        # For predicates routed
        # `user_authoritative` (or a persona-subject claim), external grounding
        # is structurally
        # unreachable — no KB property maps to `prefers` / `believes` /
        # similar, and Python cannot compute first-person facts. Every
        # verdict on such a claim is therefore conditional on user
        # assertion. Pre-set the chain flag so the verdict family is
        # always `*_given_assertion` (verified / contradicted /
        # abstained), even when no Tier U premise is present (the
        # abstained_given_assertion case). See design doc §"User_authoritative
        # verdict semantics".
        if route == "user_authoritative" or persona_subject:
            self._user_authoritative_walk = True
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
                # worst case). Breaking mid-frontier stops accumulation
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

        # C2-FC1 (§3.2): never emit a CONTRADICTED verdict for a claim whose
        # SUBJECT is a vague/indefinite reference ("a university", "a company").
        # Such a subject denotes an EXISTENTIAL, not a specific entity: no
        # grounding path — direct lookup OR a multi-hop discovery substitution
        # that resolves the subject to one arbitrary KB entity — can soundly
        # REFUTE it. "A university founded before 1800" is TRUE (such
        # universities exist) even when the arbitrarily-resolved university was
        # founded in 2001. Suppress to abstain. This is the SUBJECT-slot analog
        # of the vague-OBJECT object-conflict guard in _direct_lookup; it sits at
        # the single verdict chokepoint so it covers BOTH the direct and the
        # discovered-substitution contradiction paths. Only contradiction is
        # suppressed — an (existentially-true) verified verdict is left intact.
        # (csu_003: the multi-hop "Asa works at a university that was founded
        # before 1800" splits off a dangling "a university" subject whose
        # intended referent is Asa's employer.)
        if current_verdict == "contradicted" and _is_vague_class_object(claim.subject):
            trace.walk_metadata["vague_subject_contradiction_suppressed"] = claim.subject
            return WalkResult(
                verdict=_apply_assertion_designation("no_grounding_found", trace),
                trace=trace,
                abstention_reason="vague_subject_existential",
                budget_consumption=consumption,
            )

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

    def _record_python_premise_term(self, trace, premise_literals, *, assertion):
        """v0.16.1 WS3b (gate 3): record the premise -> Python derivation as a
        single AND-alternative on the provenance term. The python computation
        and EVERY fetched premise row are conjoined (`op='and'`) — a verdict
        derived this way holds only if the python literal AND all premise rows
        survive, so the composed verdict's retraction footprint is the union of
        all premise rows. The python literal carries `assertion=assertion`
        (gate a): when any premise rests on an asserted_unverified row the whole
        derivation is assertion-conditional, so the DERIVED
        chain_includes_assertion fires and the walk's final verdict becomes
        *_given_assertion. When there are no fetched premises (premise_literals
        empty), this falls back to a plain python OR-literal — exactly the prior
        behavior."""
        from ..layer5_result.trace import ProvenanceLiteral, ProvenanceTerm
        python_lit = ProvenanceTerm.lit(ProvenanceLiteral(
            source="python", assertion=assertion,
        ))
        if not premise_literals:
            # No fetched premises -> the historical plain-python literal.
            trace.provenance.add_alternative(python_lit)
            return
        and_children = [python_lit]
        for lit in premise_literals:
            and_children.append(ProvenanceTerm.lit(lit))
        trace.provenance.add_alternative(
            ProvenanceTerm(op="and", children=and_children)
        )

    def _gather_python_premises(
        self, claim: Claim, context: VerificationContext
    ) -> Optional[tuple[dict, list, bool]]:
        """v0.16.1 WS3b: gather a BOUNDED, GROUNDED premise dict for a
        routing_hint='python' comparison predicate, driven by the predicate's
        `premise_properties` metadata (slot -> KB property). Returns:

          * ([], [], False) when the predicate declares NO premise_properties
            (the common case) — behave EXACTLY as today (no fetch).
          * (premises, literals, assertion) on success: `premises` is the dict
            threaded into PythonVerifier.verify (keyed by slot name, each
            {'value', 'source', 'kb_property'}); `literals` are the
            ProvenanceLiteral rows for the AND-term; `assertion` is True iff any
            premise rests on an asserted_unverified Tier-U row.
          * None (gate b — FAIL CLOSED) when a DECLARED premise slot could not
            be resolved to a Q-id or its KB property had no usable value. The
            caller abstains; the verifier is NOT invoked with a fabricated input.

        WHICH property to fetch comes ONLY from `meta.premise_properties` (the
        oracle/seed) — there is NO hardcoded predicate->property table in Python.
        The fetch reuses the same resolver + lookup_statements path as
        _verify_kb_quantitative / _gather_interval (KB premises are externally
        grounded -> assertion=False). Bounded to the two entity slots the
        metadata may name (subject / object)."""
        from ..layer5_result.trace import ProvenanceLiteral

        try:
            meta = self._substrate.predicate_translation.consult(claim.predicate)
        except Exception:
            # Cannot read metadata -> behave as today (no premise fetch). A
            # consult miss must not block the plain (premise-less) python path.
            return ({}, [], False)

        premise_map = getattr(meta, "premise_properties", None)
        if not premise_map or not isinstance(premise_map, dict):
            # No declared premises -> exact prior behavior (no fetch).
            return ({}, [], False)

        # The resolver path is required to fetch a premise; if it is unwired we
        # cannot ground a DECLARED premise -> fail closed.
        if (
            self._kb_verifier is None
            or not hasattr(self._kb_verifier, "_resolver")
            or self._kb is None
        ):
            return None
        resolver = self._kb_verifier._resolver

        slot_values = {"subject": claim.subject, "object": claim.object}
        premises: dict = {}
        literals: list = []
        assertion = False

        for slot, kb_property in premise_map.items():
            if slot not in slot_values or not kb_property:
                # The metadata named a slot we cannot bind (e.g. a literal slot
                # the oracle should have omitted). Skip it — only entity slots
                # carry fetchable premises; a literal operand stays in the
                # claim's own slot text the generated code already sees.
                continue
            slot_surface = slot_values[slot]
            if not slot_surface:
                # A declared entity slot is empty -> cannot ground -> fail closed.
                return None

            lookup_ctx = LocalContext(
                predicate=claim.predicate,
                slot_position=slot,
                asserting_party=claim.asserting_party,
                source_text=context.source_text,
                claim_subject=claim.subject,
                claim_predicate=claim.predicate,
                claim_object=claim.object,
                claim_id=claim.claim_id,
            )
            try:
                resolved = resolver.select(
                    resolver.resolve(slot_surface, lookup_ctx), lookup_ctx
                )
            except Exception:
                return None
            if resolved is None:
                # Gate b: a declared premise slot did not resolve -> abstain.
                return None

            try:
                statements = self._kb.lookup_statements(resolved, kb_property)
            except Exception:
                return None
            value = self._premise_value_from_statements(statements)
            if value is None:
                # Gate b: the KB carries no usable value for the declared
                # premise property -> abstain (no fabricated input).
                return None

            premises[slot] = {
                "value": value,
                "source": "kb",
                "kb_property": kb_property,
                "entity": resolved,
            }
            # KB premises are externally grounded (assertion=False). The
            # row_id is None (live KB statement, no cached substrate row), but
            # the literal still participates in the AND-term footprint.
            literals.append(ProvenanceLiteral(
                source="kb", table=None, row_id=None,
                status=None, assertion=False,
            ))

        if not premises:
            # premise_properties named only slots we could not bind (all
            # literal) -> no fetch needed; behave as the plain python path.
            return ({}, [], False)
        return (premises, literals, assertion)

    @staticmethod
    def _premise_value_from_statements(statements: list) -> Optional[str]:
        """Pick a single usable premise value from KB statements, or None.

        Prefers a `preferred`-ranked statement; else, when all non-deprecated
        values agree, returns that value; conflicting values with no preferred
        discriminator -> None (fail-closed, never pick arbitrarily). Date values
        are normalized to their 4-digit year via _normalize_date_value so the
        generated comparison code receives a clean comparable token; non-date
        values pass through as their string form."""
        if not statements:
            return None
        preferred = None
        seen: set[str] = set()
        for stmt in statements:
            if getattr(stmt, "rank", "normal") == "deprecated":
                continue
            raw = stmt.value
            if raw is None:
                continue
            year = _normalize_date_value(str(raw))
            token = year if year is not None else str(raw).strip()
            if not token:
                continue
            if getattr(stmt, "rank", "normal") == "preferred" and preferred is None:
                preferred = token
            seen.add(token)
        if preferred is not None:
            return preferred
        if len(seen) == 1:
            return next(iter(seen))
        # Zero usable values or conflicting values with no preferred -> abstain.
        return None

    def _direct_lookup(
        self, node: Claim, context: VerificationContext, trace: JustificationTrace
    ) -> tuple[Optional[str], str, int]:
        """Returns (verdict_or_None, source, llm_calls_used).

        Status-aware Tier U handling.

        Tier U match flow:
          - `externally_verified` row → plain verified, no chain flag.
          - `asserted_unverified` row:
            * `user_authoritative` route: short-circuit
              → verified; chain flag was already set at walk start so
              the final verdict becomes `verified_given_assertion`.
            * any other route: try KB/Python for external
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

        # Check belief-revision
        # paths against PRIORS first, before Stage 1. Excluding
        # the walk's own promoted row from Stage 1
        # breaks cases (e.g. "Asa lives in Williamstown")
        # where the promoted row IS the legitimate grounding and the
        # walker should match it to return verified_given_assertion.
        #
        # The restructured flow: first look for a CONFLICTING prior
        # (opposite polarity OR different object on a functional
        # predicate) WITH the walk's own promotion excluded; if found,
        # belief-revision fires and returns `contradicted`. If no
        # conflict, fall through to normal Stage 1 (no exclusion) so the
        # walker can match its own promotion as the in-vocabulary
        # grounding source. The literal lookup and external grounding
        # follow as before.
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

        # v0.16.1 WS3 Step 0: vague-class instance check. When the claim's
        # OBJECT is a vague descriptive class ("a town in the United States"),
        # the walker normally abstains (it cannot match a free-text class as a
        # literal). Try a SOUND positive grounding: resolve the class to a KB
        # Q-id and confirm the SUBJECT's instance/subclass path to it via the
        # `is_a` (P31|P279+) transitive authority the walker already trusts.
        # Fires ONLY on a definite positive; on any miss / uncertainty it
        # returns None and falls through to the existing logic below (which
        # still abstains on the vague class — sound, never a cold LLM positive).
        if node.polarity == 1 and _is_vague_class_object(node.object):
            vague_verdict = self._verify_vague_class_instance(node, trace)
            if vague_verdict == "verified":
                return "verified", "kb", 0

        # Object-conflict belief revision: a functional
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
                # Skip the
                # object-conflict contradicted return when the claim's
                # object is a vague descriptive phrase (indefinite-
                # article noun-phrase like "a town in the US", "a state
                # that borders NY"). The specific Tier U premise may be
                # an INSTANCE of the claim's CLASS — Williamstown is "a
                # town in the United States" — so the conflict is a
                # subsumption candidate, not a contradiction. The walker
                # cannot resolve free-text class references to KB Q-IDs
                # cheaply at this site (no class-instance oracle),
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
        # match its own promotion here — the grounding path where a
        # promoted assertion is the legitimate grounding source.
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
            # WS3 §3B: record the Stage-1 verified premise in the
            # provenance term so it is retractable. assertion=False here; the
            # external-grounding fallthrough below records an assertion literal
            # when no external grounding upgrades the asserted_unverified row.
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
                # For user_authoritative predicates the chain flag was
                # already set at walk start; do not attempt KB/Python
                # (structurally unreachable).
                return "verified", "tier_u", 0

            # Try external grounding for an upgrade
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

        # (Belief-revision paths are checked BEFORE Stage 1 — see the
        # belief-revision block at the top of this method.
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

        The grounding_chain dict is the
        structured detail consumed by `mark_externally_verified`'s
        audit event — KB statement coordinates or Python execution
        identity.
        """
        # External (KB / Python) grounding is structurally unreachable for a
        # user_authoritative walk — set at walk entry when the predicate routes
        # user_authoritative OR the claim's subject is a stipulated user
        # persona (tier_u.has_identity). For a persona subject this is the
        # critical §3.2 guard: otherwise the entity resolver may resolve the
        # persona name (e.g. "Asa") to an unrelated Wikidata entity (Asa, King
        # of Judah; or a different person named Asa), and the kb_verifier's
        # polarity-aware branch would emit verified/contradicted on facts about
        # that wrong entity — a false-contradiction on a negation like "Asa is
        # not in France". The persona's true verification is in Tier U; KB is
        # the wrong source. (Replaces the deleted _is_persona_subject KB-skip
        # guard with the walk-entry user_authoritative route.)
        if getattr(self, "_user_authoritative_walk", False):
            return None, "", 0, {}

        # kb_quantitative comparison path.
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
                # WS3: the resolver's entity_resolution_cache row the KB
                # statement is keyed on — the retractable dependency of this
                # KB verdict (None for mock/transient resolvers).
                cache_row_id = kb_result.trace.get("resolution_cache_row_id")
                trace.edges.append(TraceEdge(
                    edge_type="premise_lookup",
                    source=trace.root,
                    target=TraceNode("kb_statement", {"entity": kb_result.subject_kb_id}),
                    metadata={
                        "source": "kb", "verdict": "verified",
                        # Surface the lookup direction on the result-level
                        # trace so debugging can see inverted lookups.
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

        # Python verifier: gated on routing_hint=="python" per architecture
        # §6.5 step 3: "Python verification if the route is Python." Without this
        # gate, the walker invokes the Python verifier unconditionally — and for
        # subjective / preference / opinion claims the live LLM-driven verifier
        # cheerfully writes `return False`, producing `contradicted` instead of
        # `no_grounding_found`. That is a §3.2 soundness violation.
        if (
            self._python_verifier is not None
            and self._predicate_routing(node.predicate) == "python"
        ):
            # v0.16.1 WS3b: premise -> Python channel. When the predicate's
            # metadata declares `premise_properties` (slot -> KB property), the
            # walker resolves the named entity slots and fetches those facts as
            # PREMISES the generated code computes over (e.g. born_before needs
            # both people's P569 birth years). `_gather_python_premises` is
            # FAIL-CLOSED: it returns None to signal "a declared premise could
            # not be grounded" (gate b — abstain, never fabricate). It returns
            # a (premises, literals, assertion) triple otherwise; an empty
            # premises dict (no premise_properties declared) reproduces today's
            # behavior exactly. `assertion` is True iff ANY premise rests on an
            # asserted_unverified Tier-U row (gate a — forces the chain-flag).
            gathered = self._gather_python_premises(node, context)
            if gathered is None:
                # Gate b: a declared premise was unresolvable/ungrounded ->
                # abstain on this path. Fall through (no terminal verdict here).
                return None, "", 0, {}
            premises, premise_literals, premise_assertion = gathered

            # Back-compat: when there are NO fetched premises (the common case —
            # the predicate declared no premise_properties), call verify(node)
            # with the exact prior 1-arg shape so existing verifiers/mocks that
            # do not yet accept the optional `premises` kwarg keep working. Only
            # the genuinely premise-bearing comparison path uses the new kwarg.
            if premises:
                py_result = self._python_verifier.verify(node, premises=premises)
            else:
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
                    metadata={
                        "source": "python",
                        "verdict": py_result.verdict,
                        # Observability: which premises fed the computation.
                        "premise_count": len(premise_literals),
                        "premise_includes_assertion": premise_assertion,
                    },
                ))
                # Gate 3 (provenance AND-term): the Python verdict rests on the
                # CONJUNCTION of the python computation AND every fetched premise
                # row. Record them as ONE and-alternative so the composed
                # verdict's retraction footprint includes every premise row.
                # Gate a: if any premise literal is assertion-conditional, the
                # python literal carries assertion=True too, so the derived
                # chain_includes_assertion fires and the final verdict becomes
                # *_given_assertion (never a laundered plain verify/contradict).
                self._record_python_premise_term(
                    trace, premise_literals, assertion=premise_assertion
                )
                grounding = {
                    "source": "python",
                    "output": str(getattr(py_result, "output", "")),
                    "verdict": py_result.verdict,
                    "premise_count": len(premise_literals),
                }
                return py_result.verdict, "python", 0, grounding

        return None, "", 0, {}

    def _verify_kb_quantitative(self, claim: Claim, context: VerificationContext, trace) -> Optional[tuple[str, dict]]:
        """Verify a kb_quantitative claim by
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
        # resolver (which wires up the Wikipedia normalizer for
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
        # uses (the kb_verifier's resolver wires the Wikipedia normalizer).
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
        # DATA-MODEL NOTE — a kb_interval predicate that maps to a STATEMENT-VALUED
        # date property (P571 inception / P576 dissolution) gets NO start/end from
        # this arm: _interval_from_statements reads the P580/P582 *qualifiers* off
        # the base statement, but for P571/P576 the date is the statement VALUE
        # itself, not a P580/P582 qualifier. v0.16.1 WS4 dropped the dead
        # status_started/status_ended seed rows that exercised exactly this dead
        # arm; the canonical groundings are Tier U (below) or the founded_in_year
        # (P571) / dissolved_in_year (P576) date-in-object predicates on the normal
        # KB path. A runtime-generated row that ever routes a statement-valued date
        # property here still yields nothing — fail-closed by construction.

        # ALSO gather Tier U endpoint rows for the same predicate (the
        # *_started/_ended Tier U fact participates alongside the KB qualifier).
        tier_u_interval = self._tier_u_endpoint(claim, context)

        # Merge: KB is authoritative; Tier U fills an endpoint the KB left open.
        # A conflict (both known, different values) is NOT silently reconciled —
        # _verify_interval_endpoint compares against the KB value, and the Tier U
        # value only fills a gap. (Keeping the merge additive avoids fabricating
        # an interval neither source asserts.)
        merged = kb_interval or Interval()
        if tier_u_interval is not None:
            if not merged.start_known and tier_u_interval.start_known:
                merged.start = tier_u_interval.start
                merged.start_known = True
            if not merged.end_known and tier_u_interval.end_known:
                merged.end = tier_u_interval.end
                merged.end_known = True
            # A pure Tier-U interval inherits its uniqueness (the single-distinct-
            # date invariant enforced in _tier_u_endpoint), so the contradiction
            # gate in _verify_interval_endpoint is reachable for a uniquely-valued
            # Tier-U-only endpoint. When the KB contributed the interval it stays
            # authoritative and a Tier-U gap-fill must not flip its unique flag.
            if kb_interval is None:
                merged.unique = tier_u_interval.unique
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

        # v0.16.1 WS5c: the start/end qualifier keys come from the KB adapter
        # (the authority that populates Statement.qualifiers), not a hardcoded
        # walker P-id. Fail-safe to the historical (P580, P582) default for
        # adapters predating the accessor (behavior-neutral).
        start_key, end_key = self._interval_qualifier_keys()

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
            self._iso_or_none(s.qualifiers.get(start_key))
            for s in chosen
        }
        starts.discard(None)
        if len(starts) > 1:
            # Conflicting starts, no single preferred discriminator → abstain.
            return None

        ends_raw = [s.qualifiers.get(end_key) for s in chosen]
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

    def _interval_qualifier_keys(self) -> tuple[str, str]:
        """v0.16.1 WS5c: the (start, end) temporal interval-qualifier keys to
        read off `Statement.qualifiers`, sourced from the KB adapter (the
        authority that populates them) via the optional KBProtocol
        `interval_qualifier_keys` accessor. Falls back to the historical
        (P580, P582) for adapters predating the accessor — behavior-neutral,
        since interval grounding only runs against a live adapter that provides
        it (`_gather_interval` returns None when `self._kb is None`)."""
        accessor = getattr(self._kb, "interval_qualifier_keys", None)
        if callable(accessor):
            try:
                keys = accessor()
            except Exception:
                keys = None
            if isinstance(keys, (tuple, list)) and len(keys) == 2:
                return keys[0], keys[1]
        return ("P580", "P582")

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
        # unique=True: the single-distinct-date invariant enforced one line
        # above (len(dates) == 1) GUARANTEES this endpoint is uniquely valued,
        # so a Tier-U endpoint may contradict symmetrically with the KB path.
        # Without this the contradiction-branch gate `if not interval.unique`
        # over-abstains for a uniquely-valued Tier-U endpoint (forward-defensive
        # — all 8 endpoint seeds are single_valued=0 today, branch unreachable).
        if claim.predicate.endswith("_started"):
            return Interval(start=date, start_known=True, unique=True)
        if claim.predicate.endswith("_ended"):
            return Interval(end=date, end_known=True, unique=True)
        return None

    # v0.16.1 WS4: the three-valued holds-at-T primitive (_interval_holds_at,
    # spec §B.2) was REMOVED. It had NO verdict-path consumer — its only intended
    # caller is the deferred event-relative resolver (item 7 Stage 2): "did X hold
    # relation R at time T". Wiring it to any present-day base-relation check would
    # risk a false-contradict (returning 'false' for an interval that has ended,
    # when a tenseless/historical claim makes no T assertion), turning current
    # verifies into abstains or worse. Per the operator directive (default-to-
    # remove when wiring carries false-contradict risk), the inert primitive and
    # its unit tests were dropped; endpoint grounding stays on
    # _verify_interval_endpoint's year-aware equality compare below. It returns
    # with Stage 2 when a sound holds-at-T consumer actually exists.

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
        # v0.16.1 WS5c: the start/end qualifier P-ids come from the KB adapter
        # (the authority that populates Statement.qualifiers), not a hardcoded
        # walker P-id. _started -> start qualifier, _ended -> end qualifier.
        start_key, end_key = self._interval_qualifier_keys()
        pred = claim.predicate
        if pred.endswith("_started"):
            qual_prop = start_key
        elif pred.endswith("_ended"):
            qual_prop = end_key
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

        kb_date = interval.start if qual_prop == start_key else interval.end
        endpoint_known = interval.start_known if qual_prop == start_key else interval.end_known
        if not endpoint_known or kb_date is None:
            # The KB records no value for this endpoint (open end / unknown
            # start) — abstain rather than contradict (absence is not evidence).
            return None

        # Capture the retractable resolution-cache row id the subject resolution
        # touched (WS3) — the dependency a correction would retract.
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

    def _predicate_user_subject_required(self, predicate: str) -> bool:
        """Whether `predicate` requires the claim subject to be the asserting
        user (a first-person predicate such as `prefers` / `believes`) per the
        predicate translation oracle. Relocated from the deleted Layer-2
        Validator. Returns False on a consult failure — fail-OPEN on the GUARD
        itself so an oracle miss never *blocks* a checkworthy claim; the guard
        only ever turns a confirmed first-person-about-a-third-party claim into
        an abstain (it cannot manufacture a verdict)."""
        try:
            meta = self._substrate.predicate_translation.consult(predicate)
            return bool(meta.user_subject_required)
        except Exception:
            return False

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

        Composes the (now un-gated)
        subsumption-neighbor expansion with the new premise-forward frontier,
        proposing candidate substitution claims WITHOUT the distribution gate
        foreclosing relations. Each candidate is admitted to the returned
        frontier only if `_verify_chain` confirms the taxonomy/transitive edge
        in a source (§3.2 never-false-verify: soundness lives at verify time).

        The walker does not emit a predicate-equivalence expansion edge: an
        equivalent predicate shares the same `kb_property`, so its KB lookup is
        identical to the original's, and `TierU.lookup` stage 3 already
        broadens by the same `predicate_translation` oracle.

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
            # REMOVED. The 18-min blowup was a multiplicative-fanout
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
        # `allow_llm` governs the §6 fall-through consult below. It stays True
        # (full KB->substrate->LLM) when the KB was UNAVAILABLE, but is forced
        # False on a DEFINITE KB negative: a cold LLM guess must never fabricate
        # a positive over an authoritative KB negative (§3.2), yet a sound
        # substrate row (operator-seeded / discovered — trust ordering:
        # substrate > KB > LLM) may still confirm the step.
        allow_llm = True
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
                    # The KB authoritatively answered (no fail-open error).
                    # A definite hold confirms the edge immediately. A definite
                    # non-hold is a NEGATIVE answer: do NOT return False
                    # outright (that would discard a sound Priority-2 substrate
                    # row — e.g. a seeded `Williamstown part_of Massachusetts`
                    # when Wikidata's part_of closure is incomplete). Instead
                    # fall through to the substrate consult in LLM-EXCLUDED mode
                    # (allow_llm=False) so only a real substrate/KB row can
                    # confirm — a cold LLM positive is never admitted over a KB
                    # negative (§3.2 never-false-verify; trust: substrate > KB).
                    if tp.holds:
                        return True
                    allow_llm = False
                # KB unavailable (no result / fail-open error) — fall through
                # to the FULL substrate/LLM consult on the aedos surface forms.

        # 2. Substrate consult (KB -> substrate -> LLM, §6) on aedos surface
        # forms. On a definite KB negative (allow_llm=False) the LLM tier is
        # suppressed inside consult, so only a substrate row can confirm.
        try:
            verdict = self._substrate.subsumption.consult(
                EntityRef("aedos", child_surface),
                EntityRef("aedos", parent_surface),
                relation_type,
                allow_llm=allow_llm,
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

    def _verify_vague_class_instance(
        self,
        node: Claim,
        trace: JustificationTrace,
    ) -> Optional[str]:
        """v0.16.1 WS3 Step 0: SOUND class-instance check for a vague-class
        object.

        When a claim's OBJECT is a vague descriptive class ("a town in the
        United States", "a state that borders New York") the walker cannot
        match it as a literal entity, so today it abstains. This adds a
        positive grounding path that fires ONLY on a confirmed class
        membership, using the SAME subsumption authority the walker already
        trusts (`verify_transitive_path` over `is_a` = P31|P279+):

          1. Extract the bare class-noun head from the vague object.
          2. Resolve it to a KB CLASS Q-id (via the substrate resolver, the
             same resolver `_resolve_qid` uses).
          3. Resolve the claim's SUBJECT to a Q-id.
          4. Ask the KB whether `subject is_a class` holds transitively.
          5. Return "verified" ONLY on a DEFINITE positive (holds=True,
             error=None). On a non-resolution, a definite negative, an
             uncertain/fail-open KB answer, or any exception, return None so
             the caller KEEPS ABSTAINING — that is sound (§3.2). A cold LLM
             positive is NEVER admitted: the only authority consulted is the
             KB transitive path.

        Returns "verified" on confirmed membership, else None (caller falls
        through to the existing object-conflict / Stage-1 / external-grounding
        logic, all of which still apply — this only ADDS a verify, it never
        suppresses any other path).
        """
        # Subject-less or KB-less walks cannot run the check; abstain.
        if self._kb is None or not node.subject:
            return None
        head = _vague_class_head(node.object)
        if not head:
            return None
        # Resolve the class head and the subject to Q-ids. The class head is
        # resolved in the object slot, the subject in the subject slot, exactly
        # as `_resolve_qid` is used elsewhere.
        class_qid = self._resolve_qid(node, head, "object")
        if not class_qid:
            return None  # vague class did not resolve to a Q-id -> abstain
        subject_qid = self._resolve_qid(node, node.subject, "subject")
        if not subject_qid:
            return None
        # Nogood veto FIRST (entailment-safety): a cached "does NOT hold"
        # forecloses the edge without a network round-trip.
        if self._nogood_vetoes(subject_qid, class_qid, "is_a"):
            return None
        # KB authority: does `subject is_a class` hold transitively (P31|P279+)?
        try:
            tp = self._kb.verify_transitive_path(
                subject_qid, class_qid, None, relation_type="is_a"
            )
        except Exception:
            return None
        # Verify ONLY on a DEFINITE positive. A definite negative, a fail-open
        # error, or a None result all KEEP ABSTAINING (sound — never guess).
        if tp is None or getattr(tp, "error", None) is not None:
            return None
        if not getattr(tp, "holds", False):
            return None
        trace.source_breakdown["kb"] = trace.source_breakdown.get("kb", 0) + 1
        trace.edges.append(TraceEdge(
            edge_type="premise_lookup",
            source=trace.root,
            target=TraceNode("kb_statement", {"entity": node.subject}),
            metadata={
                "source": "kb",
                "verdict": "verified",
                "relation_type": "is_a",
                "grounding": "vague_class_instance",
                "class_head": head,
                "subject_qid": subject_qid,
                "class_qid": class_qid,
                "establishing_property": getattr(tp, "establishing_property", None),
                "polarity": node.polarity,
            },
        ))
        self._record_premise(trace, source="kb", assertion=False)
        return "verified"

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
        """Enumerate KB neighbors of `node`'s slot entities
        and emit expanded claims with the slot substituted by each neighbor.

        Fires as the DISCOVERY enumerator when `find_neighbors` produced no
        substrate expansion for `relation_type` (cheapest-path-first). v0.16
        WS2 §3/§5: the distribution verdict is a RANKER (not a gate) and the
        depth==0 cap is gone — BOTH directions (parent via outgoing, child via
        incoming) are now enumerated regardless of the distribution verdict;
        `preferred` only ORDERS the calls (preferred direction first). Like the
        substrate-neighbor candidates, KB-enumerated neighbors are routed
        through `_verify_chain`: the single enumeration hop is
        structural evidence the neighbor EXISTS, but NOT that the substitution
        is ENTAILED — an is_a `neither` predicate or an unentailed downward hop
        must be rejected by the same gate. The neighbor is a Q-id, so the gate's
        `_resolve_qid` passes it through and the transitive-path/is_a
        distribution check adjudicates it; the per-walk fanout budget (§5)
        bounds the cost the removed depth cap once guarded. The trace records
        each direction (`"parent"` / `"child"`) and the KB property used.

        Direction mapping:
          - `"parent"`: `enumerate_neighbors(direction="outgoing")` — yields E's
            parents (entities E points to via the relation's property set).
          - `"child"`: `enumerate_neighbors(direction="incoming")` — yields E's
            children (entities pointing to E).

        Fail-open: any failure in resolution, KB call, or parsing returns no
        expansion for the affected slot; never raises.
        """
        if self._kb is None:
            return []
        # v0.16.1 WS5c: the relation -> KB-property mapping moved INTO the
        # adapter's enumerate_neighbors. CORE passes the opaque relation_type
        # (no P-id naming above the seam); the adapter resolves the property
        # set. The walker only ever drives the two taxonomic/containment
        # relations — guard to those so an unrecognized relation produces no
        # KB expansion (preserving the former unknown-relation early-return).
        if relation_type not in ("is_a", "part_of"):
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
            # substrate's EntityResolver — same caching, same normalization,
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
                        entity_qid, direction=kb_dir, relation_type=relation_type,
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
