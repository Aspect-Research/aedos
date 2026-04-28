"""Verification dispatcher (v0.5 — LLM-routed).

The pattern + predicate-override dispatch from v0.4 is gone. A per-claim
LLM router (``src/llm_router.py``) decides which method to use; this file
is a thin shim that calls that router, logs the decision, and dispatches
to the matching verifier.

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
than a coherent claim about a third party. The LLM router would route
such claims to ``unverifiable`` anyway, but flagging them prominently
alerts the operator to fix the extractor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from src.fact_store import DEFAULT_USER_ID, Fact, FactStore
from src.llm_client import LLMClient
from src.llm_router import ROUTING_METHODS, RoutingDecision, route_claim
from src.pattern_registry import Pattern, PatternRegistry
from src.verifiers.code_generation import (
    CodeGenerationVerifier,
    CodeGenVerificationResult,
)
from src.verifiers.retrieval_verifier import RetrievalResult, RetrievalVerifier
from src.verifiers.store_verifier import (
    StoreLookupOutcome,
    store_lookup_verify,
)

# Confidence levels.
CONF_USER_ASSERTED = 0.95
CONF_PYTHON_VERIFIED = 0.99
CONF_PYTHON_CORRECTION = 0.99
CONF_RETRIEVAL_VERIFIED = 0.95
CONF_RETRIEVAL_CORRECTION = 0.95
CONF_STORE_VERIFIED = 0.95
CONF_PENDING_IMPLEMENTATION = 0.4
CONF_RETRIEVAL_INCONCLUSIVE = 0.4
CONF_RETRIEVAL_FAILED = 0.4
CONF_UNVERIFIABLE_IN_PRINCIPLE = 0.3
CONF_ROUTING_ANOMALY = 0.2


class RoutingOutcome(str, Enum):
    USER_STORED = "user_stored"
    USER_DUPLICATE = "user_duplicate"
    USER_CONTRADICTED_PRIOR = "user_contradicted_prior"
    USER_CONTRADICTED_SELF = "user_contradicted_self"  # v0.6 prototype
    VERIFIED = "verified"
    CONTRADICTED = "contradicted"
    UNVERIFIED = "unverified"
    UNVERIFIABLE_IN_PRINCIPLE = "unverifiable_in_principle"
    ROUTING_ANOMALY = "routing_anomaly"


# Slots that define identity for each pattern's store-lookup key.
KEY_SLOTS_BY_PATTERN: dict[str, list[str]] = {
    "preference": ["agent", "object"],
    "propositional_attitude": ["agent", "proposition"],
    "spatial_temporal": ["entity", "location"],
    "categorical": ["entity", "category"],
    "role_assignment": ["agent", "role", "org"],
    "relational": ["subject", "object"],
    "quantitative": ["subject", "property"],
    "event": ["event_type", "occurred_at"],
}


# Patterns whose subject must be the user. If the extractor produced one
# of these patterns with a non-user agent, that's almost always an
# upstream slot-binding error — flag it as a routing anomaly. (v0.4 used
# a per-pattern YAML flag for this; v0.5 inlines the rule.)
_USER_SUBJECT_PATTERNS: dict[str, str] = {
    "preference": "agent",
    "propositional_attitude": "agent",
}


# v0.6 PROTOTYPE — unique-value-slot detection. Opt-in via env var.
#
# The default contradiction model in v0.3+ catches polarity flips
# (same key slots, opposite polarity). It does NOT catch "user said
# X about themselves in turn N, then says Y about themselves in turn
# M" when X and Y are different values on a slot that's biologically/
# definitionally unique per entity (one birthplace, one biological
# mother, one native language at birth).
#
# This map says: "for THIS pattern + THIS predicate + THIS key-slot
# combination, the value-slot is unique-per-entity." A new user
# assertion with the same key-slots but a different value-slot value
# is flagged as user_contradicted_self.
#
# Format: (pattern, predicate, identity_slot, value_slot) → True
#
# Empty by default — the operator opts in by setting the env var
# AEDOS_UNIQUE_VALUE_SLOTS=1. Even when enabled, the rule only applies
# to patterns explicitly listed here. Adding new entries is intended
# as a careful, considered design decision — see CLAUDE.md's
# "Bounded predicate vocabulary" invariant for the design philosophy.
_UNIQUE_VALUE_SLOTS: dict[tuple[str, str, str, str], bool] = {
    ("spatial_temporal", "was_born_in", "entity", "location"): True,
}


def _unique_value_slots_enabled() -> bool:
    """Reads the env var live so tests can monkeypatch."""
    import os
    return os.getenv("AEDOS_UNIQUE_VALUE_SLOTS") == "1"


def _is_user(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in {"user", "me", "i"}


RoutingFn = Callable[[dict], RoutingDecision]


@dataclass
class Decision:
    claim: dict
    outcome: RoutingOutcome
    verification_status: str = ""
    confidence: float = 0.0
    stored_fact_id: Optional[int] = None
    boosted_fact_id: Optional[int] = None
    closed_fact_ids: list[int] = field(default_factory=list)
    contradicting_fact_id: Optional[int] = None
    matching_fact_id: Optional[int] = None
    code_gen_result: Optional[dict] = None
    retrieval_result: Optional[RetrievalResult] = None
    correction: Optional[dict] = None
    notes: list[str] = field(default_factory=list)
    anomaly_slot: Optional[dict] = None  # {slot, expected, actual} for routing anomalies
    # v0.5: routing decision payload from the LLM router. Surfaces in the
    # trace UI as the leading section of every model-origin Decision.
    routing_decision: Optional[dict] = None
    # v0.6: True when the verdict came from the Tier 2 verification
    # cache (short-circuited the retrieval verifier). Lets the UI mark
    # cached claims distinctly without having to grep notes for the
    # "served from cache" string.
    served_from_cache: bool = False

    def to_dict(self) -> dict:
        return {
            "claim": self.claim,
            "outcome": self.outcome.value,
            "verification_status": self.verification_status,
            "confidence": self.confidence,
            "stored_fact_id": self.stored_fact_id,
            "boosted_fact_id": self.boosted_fact_id,
            "closed_fact_ids": self.closed_fact_ids,
            "contradicting_fact_id": self.contradicting_fact_id,
            "matching_fact_id": self.matching_fact_id,
            "code_gen_result": self.code_gen_result,
            "retrieval_result": (
                self.retrieval_result.to_dict() if self.retrieval_result else None
            ),
            "correction": self.correction,
            "notes": self.notes,
            "anomaly_slot": self.anomaly_slot,
            "routing_decision": self.routing_decision,
            "served_from_cache": self.served_from_cache,
        }


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
        # v0.6 — when set, the router checks the cache BEFORE invoking
        # the retrieval verifier. On hit (within TTL), short-circuits.
        # The lookup-eligibility check is the same scope+stability gate
        # the cache-write path uses, supplied by the Pipeline via
        # ``cache_eligible_keys`` (set of canonical_keys).
        verification_cache: Any = None,
        cache_eligible_keys: set[str] | None = None,
    ):
        self.store = store
        self.registry = registry
        self.llm = llm
        self.user_id = user_id
        # If neither a routing_fn nor an llm is provided, model-origin
        # routing fails loudly. User-origin routing doesn't need either.
        if routing_fn is None and llm is not None:
            routing_fn = lambda claim, _llm=llm: route_claim(claim, _llm)
        self.routing_fn = routing_fn

        self.retrieval_verifier = retrieval_verifier
        if code_gen_verifier is None and llm is not None:
            code_gen_verifier = CodeGenerationVerifier(store=store, llm=llm)
        self.code_gen_verifier = code_gen_verifier
        self._verification_cache = verification_cache
        self._cache_eligible_keys = cache_eligible_keys or set()

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

        if origin == "user":
            return self._route_user(claim, pattern, source_turn_id)
        return self._route_model(claim, pattern, source_turn_id)

    # ---- user-origin claims --------------------------------------------

    def _route_user(self, claim: dict, pattern: Pattern, source_turn_id: int) -> Decision:
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
                notes=[f"user repeated an already-known fact (id={fid})"],
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

        # v0.6 PROTOTYPE — unique-value-slot detection. If the operator
        # has opted in (AEDOS_UNIQUE_VALUE_SLOTS=1), check whether this
        # user assertion contradicts a prior assertion on a slot that
        # is unique-per-entity (e.g. birthplace). Same identity slot,
        # different value slot, same polarity → user_contradicted_self.
        prior_self_contradictions: list[dict] = []
        if _unique_value_slots_enabled():
            prior_self_contradictions = self._find_unique_value_conflicts(
                pattern.name, claim["predicate"], slots, polarity,
            )

        new_id = self._store(claim, source_turn_id, asserted_by="user",
                             confidence=CONF_USER_ASSERTED,
                             verification_status="user_asserted")

        if prior_self_contradictions:
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.USER_CONTRADICTED_SELF,
                verification_status="user_asserted",
                confidence=CONF_USER_ASSERTED,
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
                confidence=CONF_USER_ASSERTED,
                stored_fact_id=new_id,
                closed_fact_ids=closed,
                notes=[f"user reversed prior assertion; closed {len(closed)} old fact(s)"],
            )
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.USER_STORED,
            verification_status="user_asserted",
            confidence=CONF_USER_ASSERTED,
            stored_fact_id=new_id,
        )

    # ---- model-origin claims -------------------------------------------

    def _route_model(self, claim: dict, pattern: Pattern, source_turn_id: int) -> Decision:
        # Routing anomaly check (hardcoded in _USER_SUBJECT_PATTERNS — v0.5
        # replaced the per-pattern YAML opt-in). Catches upstream extractor
        # errors regardless of how the LLM router would have routed the claim.
        slots = claim.get("slots", {})
        anomaly = self._maybe_anomaly(pattern, slots)
        if anomaly is not None:
            decision = self._route_routing_anomaly(claim, source_turn_id, anomaly)
            self._log_routing_decision(decision, source_turn_id, routing=None)
            return decision

        # Ask the LLM router how to verify this claim.
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
        """Emit a ``routing_decision`` pipeline event so the trace UI can
        render the chosen method, reason, and confidence above the
        verification block.
        """
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
            # Logging must never crash routing.
            pass

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
        # Coercion in route_claim should prevent this; surface loudly.
        raise RuntimeError(
            f"router has no handler for routing method {method!r}; "
            f"valid methods are {ROUTING_METHODS}"
        )

    def _maybe_anomaly(self, pattern: Pattern, slots: dict) -> dict | None:
        """Return {slot, expected, actual} if this is a routing anomaly, else None.

        v0.5: applies to the patterns in ``_USER_SUBJECT_PATTERNS``.
        These patterns are ill-formed when the subject slot is a
        non-user agent — such claims are almost always extractor errors.
        """
        slot_name = _USER_SUBJECT_PATTERNS.get(pattern.name)
        if slot_name is None:
            return None
        actual = slots.get(slot_name)
        if _is_user(actual):
            return None
        return {"slot": slot_name, "expected": "user", "actual": actual}

    # ---- per-method handlers -------------------------------------------

    def _route_python(
        self, claim: dict, source_turn_id: int,
        *, use_canonical_constants: bool,
    ) -> Decision:
        if self.code_gen_verifier is None:
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.UNVERIFIED,
                verification_status="unverifiable_pending_implementation",
                confidence=CONF_PENDING_IMPLEMENTATION,
                stored_fact_id=self._store(
                    claim, source_turn_id, asserted_by="model",
                    confidence=CONF_PENDING_IMPLEMENTATION,
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

        # v0.5: triage is gone. The LLM router has already decided this is
        # python-verifiable; if the code can't actually compute the answer,
        # surfaces as ``code_execution_failed`` or ``comparison_error`` —
        # not ``not_python_verifiable``.
        if result.status == "verified":
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.VERIFIED,
                verification_status="verified",
                confidence=CONF_PYTHON_VERIFIED,
                stored_fact_id=self._store(
                    claim, source_turn_id, asserted_by="model",
                    confidence=CONF_PYTHON_VERIFIED, verification_status="verified",
                ),
                code_gen_result=result.to_dict(),
            )

        if result.status == "contradicted":
            corrected_slots = self._apply_correction_to_slots(claim, result)
            corrected_claim = dict(claim)
            corrected_claim["slots"] = corrected_slots
            corrected_id = self._store(
                corrected_claim, source_turn_id, asserted_by="python_verifier",
                confidence=CONF_PYTHON_CORRECTION, verification_status="verified",
            )
            original_value = self._extract_displayed_claim_value(claim)
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.CONTRADICTED,
                verification_status="contradicted",
                confidence=CONF_PYTHON_CORRECTION,
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
        # all map to pending. The trace carries the rich diagnostic.
        notes = [
            f"code generation produced status {result.status!r}: {result.explanation}"
        ]
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.UNVERIFIED,
            verification_status="unverifiable_pending_implementation",
            confidence=CONF_PENDING_IMPLEMENTATION,
            stored_fact_id=self._store(
                claim, source_turn_id, asserted_by="model",
                confidence=CONF_PENDING_IMPLEMENTATION,
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
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.CONTRADICTED,
                verification_status="contradicted",
                confidence=CONF_STORE_VERIFIED,
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
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.UNVERIFIED,
            verification_status="unverifiable_pending_implementation",
            confidence=CONF_PENDING_IMPLEMENTATION,
            stored_fact_id=self._store(
                claim, source_turn_id, asserted_by="model",
                confidence=CONF_PENDING_IMPLEMENTATION,
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
        """Look up the claim in the verification cache. On hit, return
        a Decision built from the cached verdict. On miss / no cache /
        not eligible, return None — caller falls through to retrieval.

        Two-stage lookup:
          1. Exact match on the canonical key.
          2. Semantic-shape match on miss: fetch entries with the same
             pattern + polarity + identity slots, Jaccard-rank predicate
             tokens, accept best > 0.7 as a semantic hit. Catches
             ``child_of`` ↔ ``son_of``-style synonymy that the stem
             normalizer in canonicalize_claim_key doesn't reach.
        """
        if self._verification_cache is None:
            return None
        from src.cache import canonicalize_claim_key
        key = canonicalize_claim_key(claim)
        if key not in self._cache_eligible_keys:
            # Not flagged as cache-eligible by Pipeline's scoping pass.
            return None
        try:
            cached = self._verification_cache.lookup(key)
        except Exception as exc:  # noqa: BLE001
            self.store.insert_pipeline_event(
                source_turn_id, "cache_lookup",
                {"canonical_key": key,
                 "error": f"{type(exc).__name__}: {exc}"},
            )
            return None
        if cached is None:
            # Exact miss — try semantic-shape lookup before falling
            # through to retrieval. Identity slots are pattern-specific.
            pattern_name = claim.get("pattern", "")
            identity_slots = KEY_SLOTS_BY_PATTERN.get(pattern_name, [])
            semantic_hit = None
            try:
                semantic_hit = self._verification_cache.semantic_lookup(
                    claim, identity_slots,
                )
            except Exception as exc:  # noqa: BLE001
                self.store.insert_pipeline_event(
                    source_turn_id, "cache_lookup",
                    {"canonical_key": key,
                     "error": (
                         f"semantic_lookup raised: "
                         f"{type(exc).__name__}: {exc}"
                     )},
                )
                semantic_hit = None
            if semantic_hit is None:
                self.store.insert_pipeline_event(
                    source_turn_id, "cache_lookup",
                    {"canonical_key": key, "result": "miss"},
                )
                return None
            # Semantic hit — promote to ``cached`` and log distinctly.
            cached = semantic_hit.verdict
            self.store.insert_pipeline_event(
                source_turn_id, "cache_lookup",
                {"canonical_key": key, "result": "semantic_hit",
                 "matched_key": semantic_hit.matched_key,
                 "score": round(semantic_hit.score, 3),
                 "verdict": cached.verdict,
                 "stability_class": cached.stability_class,
                 "hit_count": cached.hit_count,
                 "cached_at": cached.cached_at,
                 "expires_at": cached.expires_at},
            )
        else:
            # Exact hit. Log it.
            self.store.insert_pipeline_event(
                source_turn_id, "cache_lookup",
                {"canonical_key": key, "result": "hit",
                 "verdict": cached.verdict,
                 "stability_class": cached.stability_class,
                 "hit_count": cached.hit_count,
                 "cached_at": cached.cached_at,
                 "expires_at": cached.expires_at},
            )

        if cached.verdict == "verified":
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.VERIFIED,
                verification_status="verified",
                confidence=CONF_RETRIEVAL_VERIFIED,
                stored_fact_id=self._store(
                    claim, source_turn_id, asserted_by="model",
                    confidence=CONF_RETRIEVAL_VERIFIED,
                    verification_status="verified",
                ),
                notes=[f"served from cache (key={key!r}, "
                       f"hit_count={cached.hit_count})"],
                served_from_cache=True,
            )
        if cached.verdict == "contradicted":
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.CONTRADICTED,
                verification_status="contradicted",
                confidence=CONF_RETRIEVAL_CORRECTION,
                stored_fact_id=self._store(
                    claim, source_turn_id, asserted_by="model",
                    confidence=CONF_RETRIEVAL_CORRECTION,
                    verification_status="contradicted",
                ),
                notes=[f"served from cache (key={key!r})"],
                correction={
                    "original_object": claim.get("slots"),
                    "corrected_object": (cached.evidence or {}).get(
                        "actual_value"),
                    "explanation": (cached.evidence or {}).get(
                        "explanation", "from cache"),
                    "source_text": claim.get("source_text", ""),
                },
                served_from_cache=True,
            )
        # Cached inconclusive — still serve so we don't redo retrieval
        # on a known-tough claim.
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.UNVERIFIED,
            verification_status="retrieval_inconclusive",
            confidence=CONF_RETRIEVAL_INCONCLUSIVE,
            stored_fact_id=self._store(
                claim, source_turn_id, asserted_by="model",
                confidence=CONF_RETRIEVAL_INCONCLUSIVE,
                verification_status="retrieval_inconclusive",
            ),
            notes=[f"served from cache (inconclusive, key={key!r})"],
            served_from_cache=True,
        )

    def _route_retrieval(
        self, claim: dict, source_turn_id: int,
        *, query_hint: str | None = None,
    ) -> Decision:
        if self.retrieval_verifier is None:
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.UNVERIFIED,
                verification_status="retrieval_failed",
                confidence=CONF_RETRIEVAL_FAILED,
                stored_fact_id=self._store(
                    claim, source_turn_id, asserted_by="model",
                    confidence=CONF_RETRIEVAL_FAILED,
                    verification_status="retrieval_failed",
                ),
                notes=["no RetrievalVerifier configured on Router"],
            )

        from src.verifiers.types import VerificationOutcome

        # v0.6 — Tier 2 cache lookup BEFORE retrieval. The Pipeline
        # populated _cache_eligible_keys for claims that scoping marked
        # world_fact + stability marked non-volatile.
        cached = self._maybe_cache_hit(claim, source_turn_id)
        if cached is not None:
            return cached

        # Note: query_hint is logged on the routing_decision event but not
        # threaded into the retrieval verifier's query plan in v0.5; the
        # verifier still uses its own slot-aware query strategy. Threading
        # the hint through is a future improvement.
        result = self.retrieval_verifier.verify(claim, source_turn_id=source_turn_id)

        if result.outcome is VerificationOutcome.VERIFIED:
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.VERIFIED,
                verification_status="verified",
                confidence=CONF_RETRIEVAL_VERIFIED,
                stored_fact_id=self._store(
                    claim, source_turn_id, asserted_by="model",
                    confidence=CONF_RETRIEVAL_VERIFIED, verification_status="verified",
                ),
                retrieval_result=result,
            )
        if result.outcome is VerificationOutcome.CONTRADICTED:
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.CONTRADICTED,
                verification_status="contradicted",
                confidence=CONF_RETRIEVAL_CORRECTION,
                stored_fact_id=self._store(
                    claim, source_turn_id, asserted_by="model",
                    confidence=CONF_RETRIEVAL_CORRECTION,
                    verification_status="contradicted",
                ),
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
        confidence = CONF_RETRIEVAL_FAILED if is_failed else CONF_RETRIEVAL_INCONCLUSIVE

        return Decision(
            claim=claim,
            outcome=RoutingOutcome.UNVERIFIED,
            verification_status=status,
            confidence=confidence,
            stored_fact_id=self._store(
                claim, source_turn_id, asserted_by="model",
                confidence=confidence, verification_status=status,
            ),
            retrieval_result=result,
            notes=[
                f"retrieval {status}: "
                f"{result.error_flag or 'insufficient_evidence'} — "
                f"{result.explanation}"
            ],
        )

    def _route_unverifiable(self, claim: dict, source_turn_id: int) -> Decision:
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.UNVERIFIABLE_IN_PRINCIPLE,
            verification_status="unverifiable_in_principle",
            confidence=CONF_UNVERIFIABLE_IN_PRINCIPLE,
            stored_fact_id=self._store(
                claim, source_turn_id, asserted_by="model",
                confidence=CONF_UNVERIFIABLE_IN_PRINCIPLE,
                verification_status="unverifiable_in_principle",
            ),
            notes=["LLM router determined no method applies"],
        )

    def _route_routing_anomaly(
        self, claim: dict, source_turn_id: int, anomaly: dict
    ) -> Decision:
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.ROUTING_ANOMALY,
            verification_status="routing_anomaly",
            confidence=CONF_ROUTING_ANOMALY,
            stored_fact_id=self._store(
                claim, source_turn_id, asserted_by="model",
                confidence=CONF_ROUTING_ANOMALY, verification_status="routing_anomaly",
            ),
            anomaly_slot=anomaly,
            notes=[
                f"routing anomaly: pattern {claim['pattern']!r} expects "
                f"slot {anomaly['slot']!r}={anomaly['expected']!r} for the "
                f"user-authoritative branch, but got {anomaly['actual']!r}; "
                "this almost always indicates an upstream extractor error"
            ],
        )

    # ---- correction helpers --------------------------------------------

    def _apply_correction_to_slots(
        self, claim: dict, result: CodeGenVerificationResult
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

    def _extract_displayed_claim_value(self, claim: dict) -> Any:
        """Pull the slot value most useful for the correction payload UI."""
        slots = claim.get("slots") or {}
        pattern = claim.get("pattern")
        predicate = claim.get("predicate") or ""
        if pattern == "quantitative":
            return slots.get("value")
        if pattern == "relational" and predicate == "reverse_of":
            return slots.get("subject")
        return slots

    # ---- helpers --------------------------------------------------------

    def _find_unique_value_conflicts(
        self, pattern_name: str, predicate: str,
        slots: dict, polarity: int,
    ) -> list[dict]:
        """v0.6 prototype. Look up any prior facts where the identity-
        slot is the same but the value-slot is different. Returns a
        list of {prior_value, slot_name} dicts. Only checks pattern/
        predicate combinations explicitly in _UNIQUE_VALUE_SLOTS."""
        conflicts: list[dict] = []
        for (p_name, pred, id_slot, val_slot), enabled in _UNIQUE_VALUE_SLOTS.items():
            if not enabled:
                continue
            if p_name != pattern_name or pred != predicate:
                continue
            id_value = slots.get(id_slot)
            new_value = slots.get(val_slot)
            if id_value is None or new_value is None:
                continue
            # Find ALL currently-valid facts with same identity value,
            # any value-slot value, same polarity.
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
            )
        )
