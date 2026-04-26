"""Verification router (v0.4 — code-generated python).

Dispatches each extracted fact (pattern + predicate + slots) to the
correct verifier. The verification method comes from the PATTERN, with
two routing details:

  - ``predicate_overrides`` on a pattern can pin specific predicates to
    a non-default method (e.g. ``relational.reverse_of`` → python).
  - ``python`` invokes the v0.4 code-generation pipeline (triage →
    neutral prompt → code → sandbox → comparator). When triage decides
    the claim isn't python-resolvable, the router falls back to the
    pattern's first non-python rule that matches.

Other v0.3 behavior (routing anomaly opt-in, retrieval inconclusive vs
failed split, store_lookup for user-authoritative claims by the model)
is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from src.fact_store import Fact, FactStore
from src.llm_client import LLMClient
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

# v0.2 alias kept for any external callers:
CONF_UNVERIFIED = CONF_PENDING_IMPLEMENTATION


class RoutingOutcome(str, Enum):
    USER_STORED = "user_stored"
    USER_DUPLICATE = "user_duplicate"
    USER_CONTRADICTED_PRIOR = "user_contradicted_prior"
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


def _is_user(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in {"user", "me", "i"}


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
    # v0.4: ``code_gen_result`` carries the full code-generation trace
    # (triage, prompt, code, execution, comparison). The legacy
    # ``verifier_result`` field is kept None.
    code_gen_result: Optional[dict] = None
    retrieval_result: Optional[RetrievalResult] = None
    correction: Optional[dict] = None
    notes: list[str] = field(default_factory=list)
    anomaly_slot: Optional[dict] = None  # {slot, expected, actual} for routing anomalies

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
        }


class Router:
    def __init__(
        self,
        store: FactStore,
        registry: PatternRegistry,
        retrieval_verifier: RetrievalVerifier | None = None,
        code_gen_verifier: CodeGenerationVerifier | None = None,
        llm: LLMClient | None = None,
    ):
        self.store = store
        self.registry = registry
        self.retrieval_verifier = retrieval_verifier
        # If no code_gen_verifier was supplied but an LLM was, build one
        # automatically. Tests that don't need code generation can leave
        # both None — patterns that require python will surface a clear
        # error instead of silently misbehaving.
        if code_gen_verifier is None and llm is not None:
            code_gen_verifier = CodeGenerationVerifier(store=store, llm=llm)
        self.code_gen_verifier = code_gen_verifier

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
            pattern.name, claim["predicate"], key_slots, polarity
        )
        closed: list[int] = []
        for f in opposite:
            assert f.id is not None
            self.store.close_fact(f.id)
            closed.append(f.id)

        new_id = self._store(claim, source_turn_id, asserted_by="user",
                             confidence=CONF_USER_ASSERTED,
                             verification_status="user_asserted")

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
        slots = claim.get("slots", {})

        # Routing anomaly: pattern opted in to flag non-user agents.
        anomaly = self._maybe_anomaly(pattern, slots)
        if anomaly is not None:
            return self._route_routing_anomaly(claim, source_turn_id, anomaly)

        method = pattern.resolve_method(slots, predicate=claim["predicate"])
        return self._dispatch_method(claim, pattern, source_turn_id, method)

    def _dispatch_method(
        self, claim: dict, pattern: Pattern, source_turn_id: int, method: str,
    ) -> Decision:
        if method == "python":
            return self._route_python(claim, pattern, source_turn_id)
        if method == "user_authoritative":
            return self._route_store(claim, pattern, source_turn_id)
        if method == "store_lookup":
            return self._route_store(claim, pattern, source_turn_id)
        if method == "retrieval":
            return self._route_retrieval(claim, source_turn_id)
        if method == "unverifiable":
            return self._route_unverifiable(claim, source_turn_id)
        raise RuntimeError(f"router has no handler for verification_method={method!r}")

    def _maybe_anomaly(self, pattern: Pattern, slots: dict) -> dict | None:
        """Return {slot, expected, actual} if this is a routing anomaly, else None."""
        if not pattern.flag_non_user_as_anomaly:
            return None
        # The anomaly trigger is "the user-authoritative branch's `when`
        # condition isn't met". Find that branch.
        for rule in pattern.verification_rules:
            if rule.method == "user_authoritative" and rule.when:
                slot_name, expected = next(iter(rule.when.items()))
                actual = slots.get(slot_name)
                if not _is_user(actual) and not (
                    isinstance(actual, str) and actual.strip().lower() == str(expected).strip().lower()
                ):
                    return {"slot": slot_name, "expected": expected, "actual": actual}
        return None

    # ---- per-method handlers -------------------------------------------

    def _route_python(self, claim: dict, pattern: Pattern, source_turn_id: int) -> Decision:
        if self.code_gen_verifier is None:
            # No code-gen verifier configured — surface as pending so the
            # trace shows the misconfiguration rather than crashing.
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

        result = self.code_gen_verifier.verify(claim, source_turn_id=source_turn_id)

        if result.status == "not_python_verifiable":
            # Triage said deterministic python can't resolve this. Fall
            # through to the pattern's first non-python rule that matches.
            fallback = pattern.fallback_method(claim.get("slots", {}))
            decision = self._dispatch_method(claim, pattern, source_turn_id, fallback)
            decision.code_gen_result = result.to_dict()
            decision.notes.append(
                f"python triage said not verifiable: {result.explanation}; "
                f"fell back to {fallback}"
            )
            return decision

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

        # comparison_error / code_execution_failed → pending
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
            claim, self.store, key_slot_names=KEY_SLOTS_BY_PATTERN.get(pattern.name, [])
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

    def _route_retrieval(self, claim: dict, source_turn_id: int) -> Decision:
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

        # INCONCLUSIVE — split per Section 6 (handled fully there).
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
            notes=["pattern is unverifiable by design for this slot configuration"],
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
        """Project the computed value into the right slot for a contradiction.

        Mirrors the per-pattern claim_value extractor. The router writes
        the corrected fact to the store, so it needs to know which slot
        to overwrite with ``result.actual_value``.
        """
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
                # Lift slot temporal scope onto the fact's columns when present.
                valid_from=str(slots["valid_from"]) if slots.get("valid_from") else None,
                valid_until=str(slots["valid_until"]) if slots.get("valid_until") else None,
                source_turn_id=source_turn_id,
                source_text=claim.get("source_text"),
            )
        )
