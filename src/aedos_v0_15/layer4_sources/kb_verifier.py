from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from ..layer1_extraction.extractor import Claim
from ..layer1_extraction.temporal import BEFORE_PRESENT
from ..layer3_substrate.predicate_translation import PredicateTranslation, PredicateTranslationError
from ..layer3_substrate.resolver import EntityResolver
from .kb_protocol import KBEntityID, KBProtocol, LocalContext, Statement

_NOW = lambda: datetime.now(timezone.utc).isoformat()


class KBVerdictType(str, Enum):
    VERIFIED = "verified"
    CONTRADICTED = "contradicted"
    NO_MATCH = "no_match"
    NO_KB_PATH = "no_kb_path"


@dataclass
class KBVerdict:
    verdict: KBVerdictType
    matched_statement: Optional[Statement] = None
    subject_kb_id: Optional[KBEntityID] = None
    trace: dict = field(default_factory=dict)


class KBVerifier:
    def __init__(
        self,
        kb_protocol: KBProtocol,
        entity_resolver: EntityResolver,
        predicate_translation: PredicateTranslation,
        audit_log=None,
    ) -> None:
        self._kb = kb_protocol
        self._resolver = entity_resolver
        self._pt = predicate_translation
        self._audit = audit_log

    def verify(self, claim: Claim, current_time: Optional[str] = None) -> KBVerdict:
        """Full KB verification: resolve → translate → lookup → scope compare.

        Honors claim polarity (C1): a negated claim inverts the KB's positive-
        content verdict. Resolves the object entity, not just the subject (M4),
        and only treats a value mismatch as a contradiction for functional
        (single_valued) predicates (M4).
        """
        if current_time is None:
            current_time = _NOW()

        # Step 1: get predicate metadata
        try:
            meta = self._pt.consult(claim.predicate)
        except PredicateTranslationError:
            return KBVerdict(verdict=KBVerdictType.NO_KB_PATH, trace={"reason": "predicate_translation_failed"})

        if meta.routing_hint != "kb_resolvable" or not meta.kb_property:
            return KBVerdict(verdict=KBVerdictType.NO_KB_PATH, trace={"reason": "not_kb_resolvable"})

        # Step 2: resolve subject entity
        subject_ctx = LocalContext(
            predicate=claim.predicate,
            slot_position="subject",
            asserting_party=claim.asserting_party,
        )
        subject_id = self._resolver.select(
            self._resolver.resolve(claim.subject, subject_ctx), subject_ctx
        )
        if subject_id is None:
            return KBVerdict(
                verdict=KBVerdictType.NO_MATCH,
                trace={"reason": "subject_resolution_failed", "reference": claim.subject},
            )

        # Step 3: resolve the object entity when the predicate's object slot is
        # an entity (M4). Falls back to the raw string for literal comparison.
        object_val = claim.object
        object_resolved = False
        if meta.object_type == "entity":
            object_ctx = LocalContext(
                predicate=claim.predicate,
                slot_position="object",
                asserting_party=claim.asserting_party,
            )
            resolved_object = self._resolver.select(
                self._resolver.resolve(claim.object, object_ctx), object_ctx
            )
            if resolved_object is not None:
                object_val = resolved_object
                object_resolved = True

        # Step 4: look up KB statements for (subject, kb_property)
        statements = self._kb.lookup_statements(subject_id, meta.kb_property)
        if not statements:
            # NO_MATCH is polarity-invariant — absence of evidence is not evidence.
            return KBVerdict(
                verdict=KBVerdictType.NO_MATCH,
                subject_kb_id=subject_id,
                trace={"reason": "no_statements_found", "entity": subject_id, "property": meta.kb_property},
            )

        # Step 5: verdict for the claim's *positive* content (polarity-agnostic).
        pos_verdict, statement = self._compare_positive(
            statements, claim, object_val, meta, current_time
        )

        # Step 6: apply claim polarity (C1). A negated claim asserts the triple
        # is false, so a KB-supported triple makes it CONTRADICTED, and vice versa.
        final_verdict = _apply_polarity(pos_verdict, claim.polarity)

        return KBVerdict(
            verdict=final_verdict,
            matched_statement=statement,
            subject_kb_id=subject_id,
            trace={
                "entity": subject_id,
                "property": meta.kb_property,
                "object_value": object_val,
                "object_resolved": object_resolved,
                "polarity": claim.polarity,
                "positive_verdict": pos_verdict.value,
                "single_valued": meta.single_valued,
            },
        )

    def _compare_positive(
        self,
        statements: list[Statement],
        claim: Claim,
        object_val,
        meta,
        current_time: str,
    ) -> tuple[KBVerdictType, Optional[Statement]]:
        """Verdict for the claim's positive content, ignoring polarity.

        A value match on a scope-compatible statement is VERIFIED. A
        scope-compatible statement whose value does not match is a CONTRADICTED
        only for a functional (single_valued) predicate — for a multi-valued
        predicate the KB simply holds other values and the claim's value may
        also be true, so the result is NO_MATCH.
        """
        scope_mismatch: Optional[Statement] = None
        for stmt in statements:
            if not _scope_compatible(stmt, claim, current_time):
                continue
            if _value_matches(stmt.value, object_val):
                return KBVerdictType.VERIFIED, stmt
            if scope_mismatch is None:
                scope_mismatch = stmt

        if scope_mismatch is not None and meta.single_valued:
            return KBVerdictType.CONTRADICTED, scope_mismatch
        return KBVerdictType.NO_MATCH, None


def _apply_polarity(pos_verdict: KBVerdictType, polarity: int) -> KBVerdictType:
    """Apply claim polarity to a positive-content verdict (C1).

    For an asserted claim (polarity 1) the verdict is unchanged. For a negated
    claim (polarity 0) a KB-verified positive triple makes the negation
    CONTRADICTED and a KB-contradicted positive triple makes it VERIFIED.
    NO_MATCH carries no polarity information and is unchanged.
    """
    if polarity == 1:
        return pos_verdict
    if pos_verdict == KBVerdictType.VERIFIED:
        return KBVerdictType.CONTRADICTED
    if pos_verdict == KBVerdictType.CONTRADICTED:
        return KBVerdictType.VERIFIED
    return pos_verdict


def _value_matches(kb_value, claim_object: str) -> bool:
    """Loose equality: Q-number match or case-insensitive string match."""
    if kb_value is None:
        return False
    kb_str = str(kb_value).strip()
    claim_str = claim_object.strip()
    return kb_str.lower() == claim_str.lower()


def _scope_compatible(stmt: Statement, claim: Claim, current_time: str) -> bool:
    """
    Return True if the statement's qualifier scope is compatible with the claim's temporal scope.
    If statement has no P580/P582 qualifiers, it is assumed always-valid.
    If claim has no scope, any statement is compatible.
    """
    stmt_from = stmt.qualifiers.get("P580")
    stmt_until = stmt.qualifiers.get("P582")

    # No qualifier on statement → always valid
    if not stmt_from and not stmt_until:
        return True

    # Claim has explicit valid_from → must not precede statement start
    if claim.valid_from and stmt_from:
        if claim.valid_from < stmt_from:
            return False

    # Claim has explicit valid_until → must not exceed statement end
    if claim.valid_until and claim.valid_until != BEFORE_PRESENT and stmt_until:
        if claim.valid_until > stmt_until:
            return False

    return True
