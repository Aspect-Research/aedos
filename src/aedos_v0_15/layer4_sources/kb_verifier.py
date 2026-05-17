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
        """Full KB verification: resolve → translate → lookup → scope compare."""
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
        local_ctx = LocalContext(
            predicate=claim.predicate,
            slot_position="subject",
            asserting_party=claim.asserting_party,
        )
        subject_candidates = self._resolver.resolve(claim.subject, local_ctx)
        subject_id = self._resolver.select(subject_candidates, local_ctx)
        if subject_id is None:
            return KBVerdict(
                verdict=KBVerdictType.NO_MATCH,
                trace={"reason": "subject_resolution_failed", "reference": claim.subject},
            )

        # Step 3: look up KB statements for (subject, kb_property)
        statements = self._kb.lookup_statements(subject_id, meta.kb_property)
        if not statements:
            return KBVerdict(
                verdict=KBVerdictType.NO_MATCH,
                subject_kb_id=subject_id,
                trace={"reason": "no_statements_found", "entity": subject_id, "property": meta.kb_property},
            )

        # Step 4: compare each statement's value against claim.object + temporal scope
        object_val = claim.object
        contradicted_statement: Optional[Statement] = None

        for stmt in statements:
            if not _scope_compatible(stmt, claim, current_time):
                continue
            # Value comparison: statement.value should match claim object
            # For entity values, compare Q-numbers; for literals, string-compare
            if _value_matches(stmt.value, object_val):
                return KBVerdict(
                    verdict=KBVerdictType.VERIFIED,
                    matched_statement=stmt,
                    subject_kb_id=subject_id,
                    trace={"entity": subject_id, "property": meta.kb_property},
                )
            elif _scope_compatible(stmt, claim, current_time):
                # Same property, compatible scope, but different value → contradiction
                contradicted_statement = stmt

        if contradicted_statement is not None:
            return KBVerdict(
                verdict=KBVerdictType.CONTRADICTED,
                matched_statement=contradicted_statement,
                subject_kb_id=subject_id,
                trace={"entity": subject_id, "property": meta.kb_property, "kb_value": contradicted_statement.value},
            )

        return KBVerdict(
            verdict=KBVerdictType.NO_MATCH,
            subject_kb_id=subject_id,
            trace={"reason": "scope_mismatch_or_no_value_match"},
        )


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
