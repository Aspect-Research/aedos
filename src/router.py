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
from src.pattern_registry import PatternRegistry as PredicateRegistry  # v0.3 alias; renamed in §4
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

# Confidence levels.
CONF_USER_ASSERTED = 0.95
CONF_PYTHON_VERIFIED = 0.99
CONF_PYTHON_CORRECTION = 0.99
CONF_STORE_VERIFIED = 0.95
CONF_STORE_CORRECTION = 0.95
CONF_PENDING_IMPLEMENTATION = 0.4  # retrieval failed / no user assertion / etc.
CONF_UNVERIFIABLE_IN_PRINCIPLE = 0.3
CONF_ROUTING_ANOMALY = 0.2

# v0.1 alias kept so external callers don't break:
CONF_UNVERIFIED = CONF_PENDING_IMPLEMENTATION


class RoutingOutcome(str, Enum):
    # User-turn outcomes
    USER_STORED = "user_stored"
    USER_DUPLICATE = "user_duplicate"
    USER_CONTRADICTED_PRIOR = "user_contradicted_prior"
    # Model-turn outcomes
    VERIFIED = "verified"
    CONTRADICTED = "contradicted"
    UNVERIFIED = "unverified"  # generic "we couldn't verify"
    UNVERIFIABLE_IN_PRINCIPLE = "unverifiable_in_principle"
    ROUTING_ANOMALY = "routing_anomaly"


def _is_user_subject(subject: str) -> bool:
    return subject.strip().lower() in {"user", "me", "i"}


@dataclass
class Decision:
    claim: dict
    outcome: RoutingOutcome
    # Section 4: explicit verification_status + confidence on every decision so
    # the corrector can plan interventions without re-querying the fact store.
    verification_status: str = ""
    confidence: float = 0.0
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
            "verification_status": self.verification_status,
            "confidence": self.confidence,
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
            new_conf = self.store.boost_confidence(fid)
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.USER_DUPLICATE,
                verification_status="user_asserted",
                confidence=new_conf,
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
                verification_status="user_asserted",
                confidence=CONF_USER_ASSERTED,
                stored_fact_id=new_id,
                closed_fact_ids=closed,
                notes=[
                    f"user reversed prior assertion; closed {len(closed)} old fact(s)"
                ],
            )
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.USER_STORED,
            verification_status="user_asserted",
            confidence=CONF_USER_ASSERTED,
            stored_fact_id=new_id,
        )

    # ---- model claims ----------------------------------------------------

    def _route_model(self, claim: dict, source_turn_id: int) -> Decision:
        predicate_meta = self.registry.get(claim["predicate"])
        method = predicate_meta.verification_method

        # Routing anomaly: a user-authoritative predicate must have 'user' as
        # its subject. If the model asserted one about another entity, this is
        # almost always upstream extraction error, not a content claim.
        if method == "user_authoritative" and not _is_user_subject(claim["subject"]):
            return self._route_routing_anomaly(claim, source_turn_id)

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

    def _route_routing_anomaly(self, claim: dict, source_turn_id: int) -> Decision:
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.ROUTING_ANOMALY,
            verification_status="routing_anomaly",
            confidence=CONF_ROUTING_ANOMALY,
            stored_fact_id=self._store_model_fact(
                claim,
                source_turn_id,
                confidence=CONF_ROUTING_ANOMALY,
                verification_status="routing_anomaly",
            ),
            notes=[
                f"routing anomaly: user-authoritative predicate "
                f"{claim['predicate']!r} was asserted about non-user subject "
                f"{claim['subject']!r}; this almost always indicates an "
                f"upstream extractor error rather than a wrong content claim"
            ],
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
                verification_status="unverifiable_pending_implementation",
                confidence=CONF_PENDING_IMPLEMENTATION,
                stored_fact_id=self._store_model_fact(
                    claim,
                    source_turn_id,
                    confidence=CONF_PENDING_IMPLEMENTATION,
                    verification_status="unverifiable_pending_implementation",
                ),
                notes=[f"python verifier {verifier_name} raised {type(e).__name__}: {e}"],
            )

        if result.outcome is VerificationOutcome.VERIFIED:
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.VERIFIED,
                verification_status="verified",
                confidence=CONF_PYTHON_VERIFIED,
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
                verification_status="contradicted",
                confidence=CONF_PYTHON_CORRECTION,
                stored_fact_id=corrected_fact_id,
                verifier_result=result,
                correction={
                    "original_object": claim["object"],
                    "corrected_object": correction_object,
                    "explanation": result.explanation,
                    "source_text": claim.get("source_text", ""),
                },
            )

        # inconclusive: the predicate is python-verifiable in principle, but
        # this particular input shape couldn't be parsed. Mark as pending.
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.UNVERIFIED,
            verification_status="unverifiable_pending_implementation",
            confidence=CONF_PENDING_IMPLEMENTATION,
            stored_fact_id=self._store_model_fact(
                claim,
                source_turn_id,
                confidence=CONF_PENDING_IMPLEMENTATION,
                verification_status="unverifiable_pending_implementation",
            ),
            verifier_result=result,
            notes=[f"python verifier inconclusive: {result.explanation}"],
        )

    def _route_store(self, claim: dict, source_turn_id: int) -> Decision:
        result = store_lookup_verify(claim, self.store)

        if result.outcome is StoreLookupOutcome.MATCH:
            assert result.matching_fact is not None and result.matching_fact.id is not None
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
            assert result.contradicting_fact is not None
            cf = result.contradicting_fact
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.CONTRADICTED,
                verification_status="contradicted",
                confidence=CONF_STORE_CORRECTION,
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

        # MISS: the user hasn't said this. Mark as pending — we'd verify if we
        # had ground truth, we just don't yet.
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.UNVERIFIED,
            verification_status="unverifiable_pending_implementation",
            confidence=CONF_PENDING_IMPLEMENTATION,
            stored_fact_id=self._store_model_fact(
                claim,
                source_turn_id,
                confidence=CONF_PENDING_IMPLEMENTATION,
                verification_status="unverifiable_pending_implementation",
            ),
            notes=[
                "model asserted a user-authoritative fact the user hasn't stated; "
                "stored low-confidence pending user assertion"
            ],
        )

    def _route_retrieval(self, claim: dict, source_turn_id: int) -> Decision:
        if self.retrieval_verifier is None:
            # No verifier configured — treat as pending until one is wired in.
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.UNVERIFIED,
                verification_status="unverifiable_pending_implementation",
                confidence=CONF_PENDING_IMPLEMENTATION,
                stored_fact_id=self._store_model_fact(
                    claim,
                    source_turn_id,
                    confidence=CONF_PENDING_IMPLEMENTATION,
                    verification_status="unverifiable_pending_implementation",
                ),
                notes=["no RetrievalVerifier configured on Router"],
            )

        result = self.retrieval_verifier.verify(claim)

        if result.outcome is VerificationOutcome.VERIFIED:
            return Decision(
                claim=claim,
                outcome=RoutingOutcome.VERIFIED,
                verification_status="verified",
                confidence=CONF_STORE_VERIFIED,
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
                verification_status="contradicted",
                confidence=CONF_PYTHON_CORRECTION,
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

        # INCONCLUSIVE — retrieval errored, returned nothing, judge said
        # insufficient_evidence, or judge output couldn't be parsed. All of
        # these are "verifiable in principle, just not by this run".
        return Decision(
            claim=claim,
            outcome=RoutingOutcome.UNVERIFIED,
            verification_status="unverifiable_pending_implementation",
            confidence=CONF_PENDING_IMPLEMENTATION,
            stored_fact_id=self._store_model_fact(
                claim,
                source_turn_id,
                confidence=CONF_PENDING_IMPLEMENTATION,
                verification_status="unverifiable_pending_implementation",
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
            outcome=RoutingOutcome.UNVERIFIABLE_IN_PRINCIPLE,
            verification_status="unverifiable_in_principle",
            confidence=CONF_UNVERIFIABLE_IN_PRINCIPLE,
            stored_fact_id=self._store_model_fact(
                claim,
                source_turn_id,
                confidence=CONF_UNVERIFIABLE_IN_PRINCIPLE,
                verification_status="unverifiable_in_principle",
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
