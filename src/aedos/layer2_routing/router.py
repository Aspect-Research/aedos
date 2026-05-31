from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..layer1_extraction.extractor import Claim
from ..layer3_substrate.predicate_translation import PredicateMetadata, PredicateTranslation, PredicateTranslationError
from .validator import Validator


@dataclass
class RoutingDecision:
    route: str  # user_authoritative | python | kb_resolvable | abstain | anomaly
    predicate_metadata: Optional[PredicateMetadata] = None
    anomaly_reason: Optional[str] = None
    stub: bool = False  # True when route is kb_resolvable or python (not yet verified)


class Router:
    def __init__(
        self,
        predicate_translation: PredicateTranslation,
        validator: Validator,
    ) -> None:
        self._oracle = predicate_translation
        self._validator = validator

    def route(self, claim: Claim) -> RoutingDecision:
        """Determine the verification route for a claim."""
        try:
            meta = self._oracle.consult(claim.predicate)
        except PredicateTranslationError as exc:
            return RoutingDecision(
                route="abstain",
                anomaly_reason=f"predicate_translation_failed: {exc.cause}",
            )

        validation = self._validator.validate(claim, meta)
        if not validation.passed:
            return RoutingDecision(
                route="anomaly",
                predicate_metadata=meta,
                anomaly_reason=validation.anomaly_reason,
            )

        routing_hint = meta.routing_hint
        stub = routing_hint in ("kb_resolvable", "python")

        return RoutingDecision(
            route=routing_hint,
            predicate_metadata=meta,
            stub=stub,
        )
