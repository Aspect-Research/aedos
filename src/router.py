"""Verification router.

Takes each extracted claim plus its origin (user or assistant) and applies
the storage + verification policy from the design spec. Returns a
``VerificationDecision`` per claim — the pipeline uses that to log events,
update the turn, and (for contradictions) build the correction prompt.

The router is the single place that decides what ``asserted_by``,
``verification_status``, and ``confidence`` get written to the store. Every
other component just feeds it claims and consumes the decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from src.fact_store import Fact, FactStore
from src.predicate_registry import PredicateRegistry
from src.verifiers.python_verifiers import (
    VerificationOutcome,
    VerificationResult,
    get_verifier,
)
from src.verifiers.retrieval_verifier import RetrievalResult, RetrievalVerifier
from src.verifiers.store_verifier import (
    StoreLookupOutcome,
    store_lookup_verify,
)

# Confidence levels per the spec.
CONF_USER_ASSERTED = 0.95
CONF_PYTHON_VERIFIED = 0.99
CONF_PYTHON_CORRECTION = 0.99
CONF_STORE_VERIFIED = 0.95
CONF_STORE_CORRECTION = 0.95
CONF_UNVERIFIED = 0.5
CONF_RETRIEVAL_STUB = 0.4
CONF_UNVERIFIABLE = 0.3


class RoutingOutcome(str, Enum):
    # User-turn outcomes
    USER_STORED = "user_stored"
    USER_DUPLICATE = "user_duplicate"
    USER_CONTRADICTED_PRIOR = "user_contradicted_prior"
    # Model-turn outcomes
    VERIFIED = "verified"
    CONTRADICTED = "contradicted"
    UNVERIFIED = "unverified"
    RETRIEVAL_STUB = "retrieval_stub"
    UNVERIFIABLE_FLAGGED = "unverifiable_flagged"


@dataclass
class Decision:
    claim: dict
    outcome: RoutingOutcome
    stored_fact_id: Optional[int] = None
    boosted_fact_id: Optional[int] = None
    closed_fact_ids: list[int] = field(default_factory=list)
    contradicting_fact_id: Optional[int] = None
    matching_fact_id: Optional[int] = None
    verifier_result: Optional[VerificationResult] = None
    retrieval_result: Optional[RetrievalResult] = None  # set when retrieval ran
    correction: Optional[dict] = None  # {original_object, corrected_object, explanation}
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "claim": self.claim,
            "outcome": self.outcome.value,
            "stored_fact_id": self.stored_fact_id,
            "boosted_fact_id": self.boosted_fact_id,
            "closed_fact_ids": self.closed_fact_ids,
            "contradicting_fact_id": self.contradicting_fact_id,
            "matching_fact_id": self.matching_fact_id,
            "verifier_result": (
                self.verifier_result.to_dict() if self.verifier_result else None
            ),
            "retrieval_result": (
                self.retrieval_result.to_dict() if self.retrieval_result else None
            ),
            "correction": self.correction,
            "notes": self.notes,
        }


class Router:
    def __init__(
        self,
        store: FactStore,
        registry: PredicateRegistry,
        retrieval_verifier: RetrievalVerifier | None = None,
    ):
        self.store = store
        self.registry = registry
        self.retrieval_verifier = retrieval_verifier

    # ---- entry point -----------------------------------------------------

    def route(self, claim: dict, origin: str, source_turn_id: int) -> Decision:
        if origin not in ("user", "model"):
            raise ValueError(f"origin must be 'user' or 'model', got {origin!r}")
        if not self.registry.has(claim["predicate"]):
            raise ValueError(
                f"unknown predicate {claim['predicate']!r} — extractor should have filtered it"
            )

        if origin == "user":
            return self._route_user(claim, source_turn_id)
        return self._route_model(claim, source_turn_id)

    # ---- user claims -----------------------------------------------------

    def _route_user(self, claim: dict, source_turn_id: int) -> Decision:
        subject = claim["subject"]
        predicate = claim["predicate"]
        obj = claim["object"]
        polarity = int(claim["polarity"])

        # Same fact already asserted? Boost, don't duplicate.
        existing = self.store.find_currently_valid(subject, predicate, obj, polarity)
        if existing:
            fid = existing[0].id
            assert fid is not None
            self.store.boost_confidence(fid)
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.USER_DUPLICATE,
                boosted_fact_id=fid,
                notes=[f"user repeated an already-known fact (id={fid})"],
            )

        # Opposite-polarity fact currently valid? Close it, then store the new.
        opposite = self.store.find_contradictions(subject, predicate, obj, polarity)
        closed: list[int] = []
        for f in opposite:
            assert f.id is not None
            self.store.close_fact(f.id)
            closed.append(f.id)

        new_id = self.store.insert_fact(
            Fact(
                subject=subject,
                predicate=predicate,
                object=obj,
                object_type=claim["object_type"],
                polarity=polarity,
                confidence=CONF_USER_ASSERTED,
                asserted_by="user",
                verification_status="user_asserted",
                source_turn_id=source_turn_id,
                source_text=claim.get("source_text"),
            )
        )

        if closed:
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.USER_CONTRADICTED_PRIOR,
                stored_fact_id=new_id,
                closed_fact_ids=closed,
                notes=[
                    f"user reversed prior assertion; closed {len(closed)} old fact(s)"
                ],
            )
        return Decision(
            claim=claim, outcome=RoutingOutcome.USER_STORED, stored_fact_id=new_id
        )

    # ---- model claims ----------------------------------------------------

    def _route_model(self, claim: dict, source_turn_id: int) -> Decision:
        predicate_meta = self.registry.get(claim["predicate"])
        method = predicate_meta.verification_method

        if method == "python":
            return self._route_python(claim, source_turn_id, predicate_meta.python_verifier)
        if method in ("store_lookup", "user_authoritative"):
            # user_authoritative claims from the MODEL are verified via store
            # lookup against what the user previously asserted.
            return self._route_store(claim, source_turn_id)
        if method == "retrieval":
            return self._route_retrieval(claim, source_turn_id)
        if method == "unverifiable":
            return self._route_unverifiable(claim, source_turn_id)

        raise RuntimeError(
            f"router has no handler for verification_method={method!r}"
        )

    def _route_python(
        self, claim: dict, source_turn_id: int, verifier_name: str | None
    ) -> Decision:
        assert verifier_name, "registry should have enforced this"
        verifier = get_verifier(verifier_name)
        try:
            result = verifier(claim)
        except Exception as e:  # fail loudly but don't crash the pipeline
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.UNVERIFIED,
                stored_fact_id=self._store_model_fact(
                    claim,
                    source_turn_id,
                    confidence=CONF_UNVERIFIED,
                    verification_status="unverified",
                ),
                notes=[f"python verifier {verifier_name} raised {type(e).__name__}: {e}"],
            )

        if result.outcome is VerificationOutcome.VERIFIED:
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.VERIFIED,
                stored_fact_id=self._store_model_fact(
                    claim,
                    source_turn_id,
                    confidence=CONF_PYTHON_VERIFIED,
                    verification_status="verified",
                ),
                verifier_result=result,
            )

        if result.outcome is VerificationOutcome.CONTRADICTED:
            # Store the correction (not the wrong claim) as a verified fact.
            correction_object = (
                str(result.actual_value) if result.actual_value is not None else claim["object"]
            )
            corrected_fact_id = self.store.insert_fact(
                Fact(
                    subject=claim["subject"],
                    predicate=claim["predicate"],
                    object=correction_object,
                    object_type=claim["object_type"],
                    polarity=1,  # the correction is a positive claim about reality
                    confidence=CONF_PYTHON_CORRECTION,
                    asserted_by="python_verifier",
                    verification_status="verified",
                    source_turn_id=source_turn_id,
                    source_text=claim.get("source_text"),
                )
            )
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.CONTRADICTED,
                stored_fact_id=corrected_fact_id,
                verifier_result=result,
                correction={
                    "original_object": claim["object"],
                    "corrected_object": correction_object,
                    "explanation": result.explanation,
                    "source_text": claim.get("source_text", ""),
                },
            )

        # inconclusive
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.UNVERIFIED,
            stored_fact_id=self._store_model_fact(
                claim,
                source_turn_id,
                confidence=CONF_UNVERIFIED,
                verification_status="unverified",
            ),
            verifier_result=result,
            notes=[f"python verifier inconclusive: {result.explanation}"],
        )

    def _route_store(self, claim: dict, source_turn_id: int) -> Decision:
        result = store_lookup_verify(claim, self.store)

        if result.outcome is StoreLookupOutcome.MATCH:
            assert result.matching_fact is not None and result.matching_fact.id is not None
            self.store.boost_confidence(result.matching_fact.id)
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.VERIFIED,
                boosted_fact_id=result.matching_fact.id,
                matching_fact_id=result.matching_fact.id,
                notes=["model claim matched a stored user-asserted fact"],
            )

        if result.outcome is StoreLookupOutcome.CONTRADICTION:
            assert result.contradicting_fact is not None
            cf = result.contradicting_fact
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.CONTRADICTED,
                contradicting_fact_id=cf.id,
                correction={
                    "original_object": claim["object"],
                    "corrected_object": cf.object,
                    "original_polarity": int(claim["polarity"]),
                    "corrected_polarity": cf.polarity,
                    "explanation": (
                        f"the user previously asserted "
                        f"({cf.subject}, {cf.predicate}, {cf.object}, "
                        f"polarity={cf.polarity})"
                    ),
                    "source_text": claim.get("source_text", ""),
                },
            )

        # MISS: store as unverified, don't fabricate the user's preference.
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.UNVERIFIED,
            stored_fact_id=self._store_model_fact(
                claim,
                source_turn_id,
                confidence=CONF_UNVERIFIED,
                verification_status="unverified",
            ),
            notes=[
                "model asserted a user-authoritative fact the user hasn't stated; "
                "stored low-confidence"
            ],
        )

    def _route_retrieval(self, claim: dict, source_turn_id: int) -> Decision:
        if self.retrieval_verifier is None:
            # No verifier configured — fall through to unverified-with-low-confidence,
            # the v0.1 behavior. Tests that don't care about retrieval can omit it.
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.RETRIEVAL_STUB,
                stored_fact_id=self._store_model_fact(
                    claim,
                    source_turn_id,
                    confidence=CONF_RETRIEVAL_STUB,
                    verification_status="unverified",
                ),
                notes=["no RetrievalVerifier configured on Router"],
            )

        result = self.retrieval_verifier.verify(claim)

        if result.outcome is VerificationOutcome.VERIFIED:
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.VERIFIED,
                stored_fact_id=self._store_model_fact(
                    claim,
                    source_turn_id,
                    confidence=CONF_STORE_VERIFIED,
                    verification_status="verified",
                ),
                retrieval_result=result,
            )

        if result.outcome is VerificationOutcome.CONTRADICTED:
            # We don't necessarily have a clean "corrected_object" — the judge
            # only confirmed the claim is wrong. Surface the verdict text as
            # the correction explanation.
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.CONTRADICTED,
                stored_fact_id=self._store_model_fact(
                    claim,
                    source_turn_id,
                    confidence=CONF_PYTHON_CORRECTION,
                    verification_status="contradicted",
                ),
                retrieval_result=result,
                correction={
                    "original_object": claim["object"],
                    "corrected_object": (
                        result.actual_value
                        if result.actual_value is not None
                        else "(see judge justification)"
                    ),
                    "explanation": result.explanation
                    or (result.verdict.justification if result.verdict else ""),
                    "source_text": claim.get("source_text", ""),
                },
            )

        # INCONCLUSIVE — retrieval failed, no results, judge unsure, etc.
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.UNVERIFIED,
            stored_fact_id=self._store_model_fact(
                claim,
                source_turn_id,
                confidence=CONF_RETRIEVAL_STUB,
                verification_status="unverified",
            ),
            retrieval_result=result,
            notes=[
                f"retrieval inconclusive: {result.error_flag or 'insufficient_evidence'}: "
                f"{result.explanation}"
            ],
        )

    def _route_unverifiable(self, claim: dict, source_turn_id: int) -> Decision:
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.UNVERIFIABLE_FLAGGED,
            stored_fact_id=self._store_model_fact(
                claim,
                source_turn_id,
                confidence=CONF_UNVERIFIABLE,
                verification_status="unverified",
            ),
            notes=["predicate is unverifiable by design"],
        )

    # ---- helpers --------------------------------------------------------

    def _store_model_fact(
        self, claim: dict, source_turn_id: int, confidence: float, verification_status: str
    ) -> int:
        return self.store.insert_fact(
            Fact(
                subject=claim["subject"],
                predicate=claim["predicate"],
                object=claim["object"],
                object_type=claim["object_type"],
                polarity=int(claim["polarity"]),
                confidence=confidence,
                asserted_by="model",
                verification_status=verification_status,
                source_turn_id=source_turn_id,
                source_text=claim.get("source_text"),
            )
        )
