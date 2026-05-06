"""Verification dispatcher.

Per-claim LLM router (``src/llm_router.py``) decides which method to
use; this file is a thin shim that calls that router, logs the
decision, and dispatches to the matching verifier.

Methods the LLM router can return:

  - ``python``                            — code-generation pipeline
  - ``python_with_canonical_constants``   — code-generation with cross-check
  - ``retrieval``                         — search + judge
  - ``user_authoritative``                — store lookup (user is GT)
  - ``unverifiable``                      — no method applies

User-origin claims still go through the original boost / contradiction /
store path; the LLM router only runs for model-origin claims.

Routing anomaly: a small sanity check that catches upstream EXTRACTOR
errors. If the extractor binds an attitude- or preference-class claim
to a non-user agent, that's almost always a slot-binding bug rather
than a coherent claim about a third party.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from src.fact_store import DEFAULT_USER_ID, Fact, FactStore
from src.llm_client import LLMClient
from src.llm_router import ROUTING_METHODS, RoutingDecision, route_claim
from src.pattern_registry import Pattern, PatternRegistry
from src.router.constants import (
    KEY_SLOTS_BY_PATTERN,
    UNIQUE_VALUE_SLOTS,
    USER_SUBJECT_PATTERNS,
    confidence_from_counts,
    is_user,
    unique_value_slots_enabled,
)

# v0.13: confidence for outcomes that don't produce a stored fact
# (no counts to derive from). Equivalent to ``confidence_from_counts(0, 0)``.
# Inlined as a module constant for readability at Decision-construction
# sites — the value is just the uniform-prior expectation.
_NO_EVIDENCE_CONFIDENCE = 0.5
from src.router.types import Decision, RoutingOutcome
from src.verifiers.code_generation import (
    CodeGenerationVerifier,
    CodeGenVerificationResult,
)
from src.verifiers.retrieval_verifier import RetrievalVerifier
from src.verifiers.store_verifier import (
    StoreLookupOutcome,
    store_lookup_verify,
)


RoutingFn = Callable[[dict], RoutingDecision]


class Router:
    def __init__(
        self,
        store: FactStore,
        registry: PatternRegistry,
        *,
        llm: LLMClient | None = None,
        routing_fn: RoutingFn | None = None,
        retrieval_verifier: RetrievalVerifier | None = None,
        code_gen_verifier: CodeGenerationVerifier | None = None,
        user_id: str = DEFAULT_USER_ID,
        session_id: str | None = None,
        cache_gate: Any = None,
    ):
        self.store = store
        self.registry = registry
        self.llm = llm
        self.user_id = user_id
        # v0.7.14: when set, _route_user stamps session-scoped user
        # assertions with this id (microtheory layer). None preserves
        # the existing cross-session behavior. Pipeline assigns post-init.
        self.session_id = session_id
        # If neither a routing_fn nor an llm is provided, model-origin
        # routing fails loudly. User-origin routing doesn't need either.
        if routing_fn is None and llm is not None:
            routing_fn = lambda claim, _llm=llm: route_claim(claim, _llm)
        self.routing_fn = routing_fn

        self.retrieval_verifier = retrieval_verifier
        if code_gen_verifier is None and llm is not None:
            code_gen_verifier = CodeGenerationVerifier(store=store, llm=llm)
        self.code_gen_verifier = code_gen_verifier
        # CacheGate (v0.6 refactor) — single owner of cache lookup +
        # write. None = no caching. Pipeline assigns this per-turn so
        # the gate can stash classify state across the route() calls.
        self._cache_gate = cache_gate
        # v0.10.0 — origin currently being routed. Set by route() at
        # entry, read by every dispatch helper as the asserted_by value
        # when storing facts. Default "model" preserves existing
        # behavior for direct dispatch-helper calls (e.g. tests). The
        # parallel verify in v0.9.0 only parallelizes model-origin
        # routing, so a single shared instance attribute is race-free
        # in practice — every concurrent route() carries the same
        # origin value.
        self._origin_for_storage: str = "model"

    # ---- entry point ---------------------------------------------------

    def route(self, claim: dict, origin: str, source_turn_id: int) -> Decision:
        if origin not in ("user", "model"):
            raise ValueError(f"origin must be 'user' or 'model', got {origin!r}")
        pattern_name = claim.get("pattern")
        if not isinstance(pattern_name, str) or not self.registry.has(pattern_name):
            raise ValueError(
                f"unknown pattern {pattern_name!r} on claim — extractor should have filtered"
            )
        pattern = self.registry.get(pattern_name)

        # v0.10.0: thread the origin through the dispatch helpers as
        # the storage author. Save/restore so re-entrant calls (e.g.
        # tests calling route() multiple times) don't leak state.
        prior_origin = self._origin_for_storage
        self._origin_for_storage = origin
        try:
            if origin == "user":
                return self._route_user(claim, pattern, source_turn_id)
            return self._route_model(claim, pattern, source_turn_id)
        finally:
            self._origin_for_storage = prior_origin

    # ---- user-origin claims --------------------------------------------

    def _route_user(self, claim: dict, pattern: Pattern, source_turn_id: int) -> Decision:
        # v0.10.0: per-claim-class user trust. The user is authoritative
        # ONLY about themselves — preferences, beliefs, self-location,
        # personal relationships. World claims the user makes ("Cairo
        # is 9:56 AM", "Trump is the 47th president") get verified
        # through the same stack as model claims; the user being
        # wrong doesn't make a checkable fact true.
        from src.router.constants import is_self_attribute
        if not is_self_attribute(claim):
            return self._route_user_world_claim(claim, pattern, source_turn_id)

        slots = claim.get("slots", {})
        polarity = int(claim["polarity"])
        key_slots = self._key_slots(pattern, slots)

        existing = self.store.find_currently_valid(
            pattern.name, predicate=claim["predicate"],
            slot_match=key_slots, polarity=polarity,
            user_id=self.user_id,
        )
        if existing:
            fid = existing[0].id
            assert fid is not None
            new_conf = self.store.boost_confidence(fid)
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.USER_DUPLICATE,
                verification_status="user_asserted",
                confidence=new_conf,
                boosted_fact_id=fid,
                notes=[
                    f"user repeated an already-known fact (id={fid}, "
                    f"reinforcement_count={(existing[0].reinforcement_count or 0) + 1})"
                ],
            )

        opposite = self.store.find_contradictions(
            pattern.name, claim["predicate"], key_slots, polarity,
            user_id=self.user_id,
        )
        closed: list[int] = []
        for f in opposite:
            assert f.id is not None
            self.store.close_fact(f.id)
            closed.append(f.id)

        prior_self_contradictions: list[dict] = []
        if unique_value_slots_enabled():
            prior_self_contradictions = self._find_unique_value_conflicts(
                pattern.name, claim["predicate"], slots, polarity,
            )

        # Brand-new user-asserted fact: counts are (0, 0).
        fresh_conf = confidence_from_counts(0, 0)
        new_id = self._store(claim, source_turn_id, asserted_by="user",
                             confidence=fresh_conf,
                             verification_status="user_asserted")

        if prior_self_contradictions:
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.USER_CONTRADICTED_SELF,
                verification_status="user_asserted",
                confidence=fresh_conf,
                stored_fact_id=new_id,
                closed_fact_ids=closed,
                notes=[
                    f"user contradicting themselves on a unique-per-entity "
                    f"slot ({len(prior_self_contradictions)} prior fact(s) "
                    f"with a different value); both stored — operator "
                    f"intervention recommended"
                ],
            )

        if closed:
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.USER_CONTRADICTED_PRIOR,
                verification_status="user_asserted",
                confidence=fresh_conf,
                stored_fact_id=new_id,
                closed_fact_ids=closed,
                notes=[f"user reversed prior assertion; closed {len(closed)} old fact(s)"],
            )
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.USER_STORED,
            verification_status="user_asserted",
            confidence=fresh_conf,
            stored_fact_id=new_id,
        )

    def _route_user_world_claim(
        self, claim: dict, pattern: Pattern, source_turn_id: int,
    ) -> Decision:
        """v0.10.0 — user-stated claim that is NOT a self-attribute.

        The user said something about the world: a current time, a
        timezone offset, who-is-the-president, a population, etc.
        These get verified the same way model claims do — same LLM
        router, same verifier dispatch, same outcomes — but the
        resulting fact is stored with ``asserted_by="user"`` so the
        chat-prompt builder knows the user (not the model) is the
        source. The Decision's ``verification_status`` carries the
        verifier's verdict, which the corrector and chat prompt use
        to decide whether to gently surface a contradiction.

        Implementation note: this reuses the model-side dispatch via
        ``_route_model`` because the verification logic is identical.
        ``self._origin_for_storage`` was set to "user" by ``route()``,
        so every internal ``_store(..., asserted_by=self._origin_for_storage)``
        writes the row as user-asserted. The Decision's
        ``user_world_claim`` flag is set so downstream consumers
        (chat-prompt builder, corrector, UI) can treat it differently
        from a sacrosanct user assertion."""
        decision = self._route_model(claim, pattern, source_turn_id)
        decision.user_world_claim = True
        return decision

    # ---- model-origin claims -------------------------------------------

    def _route_model(self, claim: dict, pattern: Pattern, source_turn_id: int) -> Decision:
        slots = claim.get("slots", {})
        anomaly = self._maybe_anomaly(pattern, slots)
        if anomaly is not None:
            decision = self._route_routing_anomaly(claim, source_turn_id, anomaly)
            self._log_routing_decision(decision, source_turn_id, routing=None)
            return decision

        if self.routing_fn is None:
            raise RuntimeError(
                "Router needs an llm or a routing_fn to handle model-origin claims"
            )
        routing = self.routing_fn(claim)

        decision = self._dispatch_method(claim, pattern, source_turn_id, routing)
        decision.routing_decision = routing.to_dict()
        self._log_routing_decision(decision, source_turn_id, routing=routing)
        return decision

    def _log_routing_decision(
        self, decision: Decision, source_turn_id: int,
        routing: RoutingDecision | None,
    ) -> None:
        try:
            self.store.insert_pipeline_event(
                source_turn_id,
                "routing_decision",
                {
                    "claim": decision.claim,
                    "decision": routing.to_dict() if routing else None,
                    "outcome": decision.outcome.value,
                    "verification_status": decision.verification_status,
                    "anomaly_slot": decision.anomaly_slot,
                },
            )
        except Exception:
            pass  # Logging must never crash routing.

    def _dispatch_method(
        self,
        claim: dict,
        pattern: Pattern,
        source_turn_id: int,
        routing: RoutingDecision,
    ) -> Decision:
        method = routing.method
        if method == "python":
            return self._route_python(
                claim, source_turn_id, use_canonical_constants=False,
            )
        if method == "python_with_canonical_constants":
            return self._route_python(
                claim, source_turn_id, use_canonical_constants=True,
            )
        if method == "user_authoritative":
            return self._route_store(claim, pattern, source_turn_id)
        if method == "retrieval":
            return self._route_retrieval(
                claim, source_turn_id, query_hint=routing.retrieval_query_hint,
            )
        if method == "unverifiable":
            return self._route_unverifiable(claim, source_turn_id)
        raise RuntimeError(
            f"router has no handler for routing method {method!r}; "
            f"valid methods are {ROUTING_METHODS}"
        )

    def _maybe_anomaly(self, pattern: Pattern, slots: dict) -> dict | None:
        slot_name = USER_SUBJECT_PATTERNS.get(pattern.name)
        if slot_name is None:
            return None
        actual = slots.get(slot_name)
        if is_user(actual):
            return None
        return {"slot": slot_name, "expected": "user", "actual": actual}

    # ---- per-method handlers -------------------------------------------

    def _route_python(
        self, claim: dict, source_turn_id: int,
        *, use_canonical_constants: bool,
    ) -> Decision:
        if self.code_gen_verifier is None:
            fresh_conf = confidence_from_counts(0, 0)
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.UNVERIFIED,
                verification_status="unverifiable_pending_implementation",
                confidence=fresh_conf,
                stored_fact_id=self._store(
                    claim, source_turn_id, asserted_by=self._origin_for_storage,
                    confidence=fresh_conf,
                    verification_status="unverifiable_pending_implementation",
                ),
                notes=["python method routed but no CodeGenerationVerifier configured"],
            )

        if use_canonical_constants:
            result = self.code_gen_verifier.verify_with_cross_check(
                claim, source_turn_id=source_turn_id,
            )
        else:
            result = self.code_gen_verifier.verify(
                claim, source_turn_id=source_turn_id,
            )

        if result.status == "verified":
            fact_id, final_conf, _ = self._store_or_boost_model_fact(
                claim, source_turn_id, verification_status="verified",
            )
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.VERIFIED,
                verification_status="verified",
                confidence=final_conf,
                stored_fact_id=fact_id,
                code_gen_result=result.to_dict(),
            )

        if result.status == "contradicted":
            corrected_slots = _apply_correction_to_slots(claim, result)
            corrected_claim = dict(claim)
            corrected_claim["slots"] = corrected_slots
            # v0.10.0: when the original claim came from the user
            # (user-stated world claim), also store the user's wrong
            # version with verification_status="contradicted" so the
            # chat-prompt builder + UI can surface "the user said X
            # but verification produced Y". Model-side keeps the
            # legacy behavior — we only persist the corrected value.
            if self._origin_for_storage == "user":
                self._store(
                    claim, source_turn_id, asserted_by="user",
                    confidence=confidence_from_counts(0, 0),
                    verification_status="contradicted",
                )
            corrected_conf = confidence_from_counts(0, 0)
            corrected_id = self._store(
                corrected_claim, source_turn_id, asserted_by="python_verifier",
                confidence=corrected_conf, verification_status="verified",
            )
            original_value = _extract_displayed_claim_value(claim)
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.CONTRADICTED,
                verification_status="contradicted",
                confidence=corrected_conf,
                stored_fact_id=corrected_id,
                code_gen_result=result.to_dict(),
                correction={
                    "original_object": original_value,
                    "corrected_object": result.actual_value,
                    "explanation": result.explanation,
                    "source_text": claim.get("source_text", ""),
                },
            )

        # comparison_error / code_execution_failed / canonical_constants_disagreement
        notes = [
            f"code generation produced status {result.status!r}: {result.explanation}"
        ]
        fresh_conf = confidence_from_counts(0, 0)
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.UNVERIFIED,
            verification_status="unverifiable_pending_implementation",
            confidence=fresh_conf,
            stored_fact_id=self._store(
                claim, source_turn_id, asserted_by=self._origin_for_storage,
                confidence=fresh_conf,
                verification_status="unverifiable_pending_implementation",
            ),
            code_gen_result=result.to_dict(),
            notes=notes,
        )

    def _route_store(self, claim: dict, pattern: Pattern, source_turn_id: int) -> Decision:
        result = store_lookup_verify(
            claim, self.store,
            key_slot_names=KEY_SLOTS_BY_PATTERN.get(pattern.name, []),
            user_id=self.user_id,
        )
        if result.outcome is StoreLookupOutcome.MATCH:
            assert result.matching_fact and result.matching_fact.id is not None
            new_conf = self.store.boost_confidence(result.matching_fact.id)
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.VERIFIED,
                verification_status="verified",
                confidence=new_conf,
                boosted_fact_id=result.matching_fact.id,
                matching_fact_id=result.matching_fact.id,
                notes=["model claim matched a stored user-asserted fact"],
            )
        if result.outcome is StoreLookupOutcome.CONTRADICTION:
            cf = result.contradicting_fact
            assert cf is not None
            # The contradicting fact is a stored user-asserted prior;
            # its existing reinforcement_count is the count signal.
            cf_conf = confidence_from_counts(
                cf.reinforcement_count or 0, 0,
            )
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.CONTRADICTED,
                verification_status="contradicted",
                confidence=cf_conf,
                contradicting_fact_id=cf.id,
                correction={
                    "original_object": claim.get("slots"),
                    "corrected_object": cf.slots,
                    "explanation": (
                        f"the user previously asserted "
                        f"({cf.pattern}, {cf.predicate}, {cf.slots}, "
                        f"polarity={cf.polarity})"
                    ),
                    "source_text": claim.get("source_text", ""),
                },
            )
        fresh_conf = confidence_from_counts(0, 0)
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.UNVERIFIED,
            verification_status="unverifiable_pending_implementation",
            confidence=fresh_conf,
            stored_fact_id=self._store(
                claim, source_turn_id, asserted_by=self._origin_for_storage,
                confidence=fresh_conf,
                verification_status="unverifiable_pending_implementation",
            ),
            notes=[
                "model asserted what would be a user-authoritative fact, "
                "but the user hasn't stated it"
            ],
        )

    def _maybe_cache_hit(
        self, claim: dict, source_turn_id: int,
    ) -> Decision | None:
        """Try the cache via CacheGate. On hit, hand off to
        _build_cache_hit_decision. Used by _route_retrieval as the
        defense-in-depth check (the Pipeline-level tiered short-
        circuit normally catches this first as of v0.7.14)."""
        if self._cache_gate is None:
            return None
        identity_slots = KEY_SLOTS_BY_PATTERN.get(claim.get("pattern", ""), [])
        hit = self._cache_gate.maybe_hit(
            claim, identity_slots, turn_id=source_turn_id,
        )
        if hit is None:
            return None
        return self._build_cache_hit_decision(claim, hit, source_turn_id)

    def _build_cache_hit_decision(
        self, claim: dict, hit, source_turn_id: int,
    ) -> Decision:
        """Build a Decision from a cache hit. Extracted so the
        Pipeline-level tiered short-circuit (v0.7.14) can call it
        directly with a hit obtained via maybe_hit(require_eligible=False).
        Carries cache-as-evidence flow-through + earned-trust-curve
        confidence."""
        cached = hit.verdict
        key = hit.matched_key or cached.canonical_key

        # v0.7.10 cache-as-evidence: the cached `evidence` dict is the
        # full RetrievalResult.to_dict() that the verifier stored
        # originally — snippets, attempts, verdict, justification.
        # Surface it on the Decision so the corrector hedges with the
        # real justification (not "from cache") and the per-claim
        # Decision-detail UI shows the actual snippets that backed
        # this verdict, marked as cached.
        cached_retrieval_result = _cache_evidence_to_retrieval_result(
            cached.evidence, key, hit
        )

        # v0.13: confidence is purely the cache entry's count history.
        # No path priors, no LLM-emitted judge confidence — just the
        # frequentist Beta(1,1) posterior from refresh_count and
        # contradiction_count.
        adjusted_conf = confidence_from_counts(
            refresh_count=cached.refresh_count or 0,
            contradiction_count=cached.contradiction_count or 0,
        )

        # Find or boost the corresponding model-asserted fact instead
        # of inserting a duplicate on every cache hit (was a real
        # accumulation bug pre-v0.7.13).
        verification_status = (
            "verified" if cached.verdict == "verified"
            else "contradicted" if cached.verdict == "contradicted"
            else "retrieval_inconclusive"
        )
        fact_id, _, reinforcement_count = self._store_or_boost_model_fact(
            claim, source_turn_id,
            verification_status=verification_status,
        )
        notes_extra = (
            f" · reinforced ×{cached.refresh_count + 1}"
            if (cached.refresh_count or 0) > 0
            else ""
        )
        if (cached.contradiction_count or 0) > 0:
            notes_extra += f" · prior flips: {cached.contradiction_count}"

        if cached.verdict == "verified":
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.VERIFIED,
                verification_status="verified",
                confidence=adjusted_conf,
                stored_fact_id=fact_id,
                notes=[
                    f"served from cache (key={key!r}, hit_count={cached.hit_count})"
                    + notes_extra
                ],
                served_from_cache=True,
                retrieval_result=cached_retrieval_result,
            )
        if cached.verdict == "contradicted":
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.CONTRADICTED,
                verification_status="contradicted",
                confidence=adjusted_conf,
                stored_fact_id=fact_id,
                notes=[f"served from cache (key={key!r})" + notes_extra],
                correction={
                    "original_object": claim.get("slots"),
                    "corrected_object": (cached.evidence or {}).get(
                        "actual_value"),
                    "explanation": (cached.evidence or {}).get(
                        "explanation", "from cache"),
                    "source_text": claim.get("source_text", ""),
                },
                served_from_cache=True,
                retrieval_result=cached_retrieval_result,
            )
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.UNVERIFIED,
            verification_status="retrieval_inconclusive",
            confidence=adjusted_conf,
            stored_fact_id=fact_id,
            notes=[
                f"served from cache (inconclusive, key={key!r})" + notes_extra
            ],
            served_from_cache=True,
            retrieval_result=cached_retrieval_result,
        )

    def _route_retrieval(
        self, claim: dict, source_turn_id: int,
        *, query_hint: str | None = None,
    ) -> Decision:
        if self.retrieval_verifier is None:
            fresh_conf = confidence_from_counts(0, 0)
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.UNVERIFIED,
                verification_status="retrieval_failed",
                confidence=fresh_conf,
                stored_fact_id=self._store(
                    claim, source_turn_id, asserted_by=self._origin_for_storage,
                    confidence=fresh_conf,
                    verification_status="retrieval_failed",
                ),
                notes=["no RetrievalVerifier configured on Router"],
            )

        from src.verifiers.types import VerificationOutcome

        # v0.6 — Tier 2 cache lookup BEFORE retrieval. CacheGate (set
        # by Pipeline per-turn) owns lookup + event emission.
        cached = self._maybe_cache_hit(claim, source_turn_id)
        if cached is not None:
            return cached

        result = self.retrieval_verifier.verify(claim, source_turn_id=source_turn_id)

        if result.outcome is VerificationOutcome.VERIFIED:
            fact_id, final_conf, _ = self._store_or_boost_model_fact(
                claim, source_turn_id, verification_status="verified",
            )
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.VERIFIED,
                verification_status="verified",
                confidence=final_conf,
                stored_fact_id=fact_id,
                retrieval_result=result,
            )
        if result.outcome is VerificationOutcome.CONTRADICTED:
            fact_id, final_conf, _ = self._store_or_boost_model_fact(
                claim, source_turn_id, verification_status="contradicted",
            )
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.CONTRADICTED,
                verification_status="contradicted",
                confidence=final_conf,
                stored_fact_id=fact_id,
                retrieval_result=result,
                correction={
                    "original_object": claim.get("slots"),
                    "corrected_object": result.actual_value,
                    "explanation": result.explanation
                    or (result.verdict.justification if result.verdict else ""),
                    "source_text": claim.get("source_text", ""),
                },
            )

        is_failed = (
            result.error_flag in {"retrieval_error", "no_results", "judge_error",
                                  "judge_parse_error", "retrieval_not_configured"}
        )
        status = "retrieval_failed" if is_failed else "retrieval_inconclusive"
        if is_failed:
            confidence = confidence_from_counts(0, 0)
            fact_id = self._store(
                claim, source_turn_id, asserted_by=self._origin_for_storage,
                confidence=confidence, verification_status=status,
            )
        else:
            fact_id, confidence, _ = self._store_or_boost_model_fact(
                claim, source_turn_id, verification_status=status,
            )

        return Decision(
            claim=claim,
            outcome=RoutingOutcome.UNVERIFIED,
            verification_status=status,
            confidence=confidence,
            stored_fact_id=fact_id,
            retrieval_result=result,
            notes=[
                f"retrieval {status}: "
                f"{result.error_flag or 'insufficient_evidence'} — "
                f"{result.explanation}"
            ],
        )

    def _route_unverifiable(self, claim: dict, source_turn_id: int) -> Decision:
        fresh_conf = confidence_from_counts(0, 0)
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.UNVERIFIABLE_IN_PRINCIPLE,
            verification_status="unverifiable_in_principle",
            confidence=fresh_conf,
            stored_fact_id=self._store(
                claim, source_turn_id, asserted_by=self._origin_for_storage,
                confidence=fresh_conf,
                verification_status="unverifiable_in_principle",
            ),
            notes=["LLM router determined no method applies"],
        )

    def _route_routing_anomaly(
        self, claim: dict, source_turn_id: int, anomaly: dict
    ) -> Decision:
        fresh_conf = confidence_from_counts(0, 0)
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.ROUTING_ANOMALY,
            verification_status="routing_anomaly",
            confidence=fresh_conf,
            stored_fact_id=self._store(
                claim, source_turn_id, asserted_by=self._origin_for_storage,
                confidence=fresh_conf, verification_status="routing_anomaly",
            ),
            anomaly_slot=anomaly,
            notes=[
                f"routing anomaly: pattern {claim['pattern']!r} expects "
                f"slot {anomaly['slot']!r}={anomaly['expected']!r} for the "
                f"user-authoritative branch, but got {anomaly['actual']!r}; "
                "this almost always indicates an upstream extractor error"
            ],
        )

    # ---- helpers --------------------------------------------------------

    def _find_unique_value_conflicts(
        self, pattern_name: str, predicate: str,
        slots: dict, polarity: int,
    ) -> list[dict]:
        """v0.6 prototype. Look up any prior facts where the identity-
        slot is the same but the value-slot is different."""
        conflicts: list[dict] = []
        for (p_name, pred, id_slot, val_slot), enabled in UNIQUE_VALUE_SLOTS.items():
            if not enabled:
                continue
            if p_name != pattern_name or pred != predicate:
                continue
            id_value = slots.get(id_slot)
            new_value = slots.get(val_slot)
            if id_value is None or new_value is None:
                continue
            prior = self.store.find_currently_valid(
                pattern_name, predicate=predicate,
                slot_match={id_slot: id_value},
                polarity=polarity,
                user_id=self.user_id,
            )
            for p_fact in prior:
                prior_val = p_fact.slots.get(val_slot)
                if prior_val is not None and str(prior_val) != str(new_value):
                    conflicts.append({
                        "prior_fact_id": p_fact.id,
                        "prior_value": prior_val,
                        "new_value": new_value,
                        "slot_name": val_slot,
                        "identity_slot": id_slot,
                    })
        return conflicts

    def _key_slots(self, pattern: Pattern, slots: dict) -> dict:
        names = KEY_SLOTS_BY_PATTERN.get(pattern.name, [])
        return {k: slots[k] for k in names if k in slots}

    def _store(
        self,
        claim: dict,
        source_turn_id: int,
        *,
        asserted_by: str,
        confidence: float,
        verification_status: str,
    ) -> int:
        slots = claim.get("slots") or {}
        # v0.7.14: stamp session_id on user assertions whose source
        # text carries a session marker ("for this conversation",
        # "let's say", etc.). Cross-session assertions stay session_id
        # NULL — preserves the existing "I like peanut butter" memory
        # model. Model-asserted facts always go cross-session for now;
        # the cache handles their per-session caching separately.
        from src.session_markers import is_session_scoped
        session_id = None
        if (asserted_by == "user"
            and self.session_id is not None
            and is_session_scoped(claim.get("source_text"))):
            session_id = self.session_id
        return self.store.insert_fact(
            Fact(
                pattern=claim["pattern"],
                predicate=claim["predicate"],
                slots=dict(slots),
                polarity=int(claim["polarity"]),
                confidence=confidence,
                asserted_by=asserted_by,
                verification_status=verification_status,
                valid_from=str(slots["valid_from"]) if slots.get("valid_from") else None,
                valid_until=str(slots["valid_until"]) if slots.get("valid_until") else None,
                source_turn_id=source_turn_id,
                source_text=claim.get("source_text"),
                user_id=self.user_id,
                session_id=session_id,
            )
        )

    def _store_or_boost_model_fact(
        self,
        claim: dict,
        source_turn_id: int,
        *,
        verification_status: str,
    ) -> tuple[int, float, int]:
        """v0.7.13: find an existing matching model-asserted fact and
        boost it instead of inserting a duplicate. Mirror of the
        user-asserted find-or-boost pattern in _route_user.

        Returns ``(fact_id, confidence, reinforcement_count)``.

        v0.13: confidence is purely a function of reinforcement_count.
        No path_prior or verifier_confidence parameters — those were
        the LLM-emitted/hardcoded inputs the frequentist refactor
        deleted. Same-shape lookup is still anchored on the pattern's
        identity slots; polarity match required (opposite polarity is
        a CONTRADICTION, not a reinforcement).
        """
        slots = claim.get("slots") or {}
        polarity = int(claim["polarity"])
        pattern_name = claim.get("pattern", "")
        identity_slot_names = KEY_SLOTS_BY_PATTERN.get(pattern_name, [])
        slot_match = {k: slots[k] for k in identity_slot_names if k in slots}

        existing = []
        if slot_match and pattern_name:
            try:
                existing = self.store.find_currently_valid(
                    pattern_name,
                    predicate=claim.get("predicate"),
                    slot_match=slot_match,
                    polarity=polarity,
                    user_id=self.user_id,
                )
            except Exception:
                existing = []

        # v0.10.0: same-origin reinforcement only. Model-asserted
        # facts boost on model re-assertion; user-stated world-claim
        # facts boost on user re-assertion. Cross-origin matches are
        # NOT treated as reinforcement — they go through the regular
        # contradiction/confirmation path so the Decision carries the
        # right semantics for the chat-prompt builder.
        existing = [f for f in existing
                    if f.asserted_by == self._origin_for_storage]

        if existing:
            target = existing[-1]
            assert target.id is not None
            new_conf = self.store.boost_confidence(target.id)
            new_count = (target.reinforcement_count or 0) + 1
            return target.id, new_conf, new_count

        # No matching fact yet — store fresh at the (0, 0) prior.
        fresh_conf = confidence_from_counts(0, 0)
        fact_id = self._store(
            claim, source_turn_id, asserted_by=self._origin_for_storage,
            confidence=fresh_conf, verification_status=verification_status,
        )
        return fact_id, fresh_conf, 0


# ---- module-level helpers ------------------------------------------


def _cache_evidence_to_retrieval_result(
    evidence: dict | None, matched_key: str, hit,
) -> dict | None:
    """Synthesize a retrieval_result-shaped dict from a cached
    verdict's evidence so the corrector + per-claim Decision UI see
    the original snippets and judge justification (not just
    "served from cache"). Adds a `served_from_cache` marker + the
    matched key so the trace makes the cache origin explicit.

    The evidence dict was originally `RetrievalResult.to_dict()` at
    the time of the first verification; we shape it back into the
    same form (snippets, attempts, verdict) and tack on the cache
    metadata. Keys missing from older entries (pre-v0.7.10) just
    end up absent from the synthesized dict — no crash.
    """
    if not evidence:
        return None
    out = dict(evidence)
    out["served_from_cache"] = True
    out["cached_key"] = matched_key
    out["cache_hit_count"] = getattr(hit.verdict, "hit_count", None)
    out["cache_evidence_hash"] = getattr(hit.verdict, "evidence_hash", None)
    out["cache_age_at_hit"] = getattr(hit.verdict, "cached_at", None)
    if getattr(hit, "score", None) is not None:
        out["cache_match_score"] = hit.score
    return out


def _apply_correction_to_slots(
    claim: dict, result: CodeGenVerificationResult
) -> dict:
    """Project the computed value into the right slot for a contradiction."""
    slots = dict(claim.get("slots") or {})
    pattern = claim.get("pattern")
    predicate = claim.get("predicate") or ""
    if pattern == "quantitative":
        slots["value"] = result.actual_value
    elif pattern == "relational" and predicate == "reverse_of":
        slots["subject"] = result.actual_value
    return slots


def _extract_displayed_claim_value(claim: dict) -> Any:
    """Pull the slot value most useful for the correction payload UI."""
    slots = claim.get("slots") or {}
    pattern = claim.get("pattern")
    predicate = claim.get("predicate") or ""
    if pattern == "quantitative":
        return slots.get("value")
    if pattern == "relational" and predicate == "reverse_of":
        return slots.get("subject")
    return slots
