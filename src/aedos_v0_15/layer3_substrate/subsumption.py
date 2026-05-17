from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from ..audit.log import log_event
from ..llm.client import LLMClient

_NOW = lambda: datetime.now(timezone.utc).isoformat()

SUBSUMPTION_TOOL: dict[str, Any] = {
    "name": "generate_subsumption_verdict",
    "description": "Determine whether entity_a is subsumed by entity_b (or vice versa) under the given relation type.",
    "input_schema": {
        "type": "object",
        "required": ["verdict", "reason"],
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["a_subsumed_by_b", "b_subsumed_by_a", "equivalent", "unrelated"],
                "description": (
                    "a_subsumed_by_b: A is an instance/subtype of B. "
                    "b_subsumed_by_a: B is an instance/subtype of A. "
                    "equivalent: A and B are the same type. "
                    "unrelated: no subsumption relationship."
                ),
            },
            "reason": {
                "type": "string",
                "description": "1-2 sentence justification for the verdict.",
            },
        },
    },
}


@dataclass
class EntityRef:
    namespace: str  # 'wikidata' | 'aedos'
    identifier: str


class SubsumptionVerdictType(str, Enum):
    A_SUBSUMED_BY_B = "a_subsumed_by_b"
    B_SUBSUMED_BY_A = "b_subsumed_by_a"
    EQUIVALENT = "equivalent"
    UNRELATED = "unrelated"


@dataclass
class SubsumptionVerdict:
    verdict: SubsumptionVerdictType
    source: str  # 'kb' | 'substrate' | 'llm_generated'
    reason: str
    row_id: Optional[int] = None
    traversal_chain: list[str] = field(default_factory=list)


class SubsumptionOracleError(Exception):
    pass


class SubsumptionOracle:
    def __init__(
        self,
        db: sqlite3.Connection,
        llm_client: LLMClient,
        kb_protocol=None,
        audit_log=None,
    ) -> None:
        self._db = db
        self._llm = llm_client
        self._kb = kb_protocol
        self._audit = audit_log

    def consult(
        self, entity_a: EntityRef, entity_b: EntityRef, relation_type: str
    ) -> SubsumptionVerdict:
        """Three-priority resolution: KB-mediated → substrate row → LLM generation."""
        # Priority 1: KB-mediated (both entities are Wikidata Q-numbers)
        if (
            self._kb is not None
            and entity_a.namespace == "wikidata"
            and entity_b.namespace == "wikidata"
        ):
            kb_result = self._kb.subsumption(entity_a.identifier, entity_b.identifier, relation_type)
            return SubsumptionVerdict(
                verdict=SubsumptionVerdictType(kb_result.verdict),
                source="kb",
                reason=f"KB traversal via {kb_result.establishing_property}",
                traversal_chain=kb_result.traversal_chain,
            )

        # Priority 2: Substrate row lookup
        row = self._fetch(entity_a, entity_b, relation_type)
        if row is not None:
            self._touch(row["id"])
            return SubsumptionVerdict(
                verdict=SubsumptionVerdictType(row["verdict"]),
                source="substrate",
                reason=row["reason"],
                row_id=row["id"],
            )

        # Priority 3: LLM generation
        return self._generate_and_store(entity_a, entity_b, relation_type)

    def retract(self, row_id: int, reason: str) -> None:
        now = _NOW()
        self._db.execute(
            "UPDATE subsumption SET retracted_at=?, retraction_reason=? WHERE id=?",
            (now, reason, row_id),
        )
        self._db.commit()
        if self._audit is not None:
            log_event(
                self._db,
                event_type="row_retracted",
                event_subject=f"subsumption:{row_id}",
                event_data={"reason": reason},
            )

    def query_neighbors(
        self, entity_a: EntityRef, relation_type: str
    ) -> list[SubsumptionVerdict]:
        """Return all non-retracted substrate rows involving entity_a."""
        rows = self._db.execute(
            """SELECT * FROM subsumption
               WHERE (entity_a_namespace=? AND entity_a_identifier=?)
               AND relation_type=? AND retracted_at IS NULL""",
            (entity_a.namespace, entity_a.identifier, relation_type),
        ).fetchall()
        return [
            SubsumptionVerdict(
                verdict=SubsumptionVerdictType(r["verdict"]),
                source="substrate",
                reason=r["reason"],
                row_id=r["id"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _fetch(
        self, entity_a: EntityRef, entity_b: EntityRef, relation_type: str
    ) -> Optional[dict]:
        row = self._db.execute(
            """SELECT * FROM subsumption
               WHERE entity_a_namespace=? AND entity_a_identifier=?
               AND entity_b_namespace=? AND entity_b_identifier=?
               AND relation_type=? AND retracted_at IS NULL
               ORDER BY id LIMIT 1""",
            (entity_a.namespace, entity_a.identifier, entity_b.namespace, entity_b.identifier, relation_type),
        ).fetchone()
        return dict(row) if row is not None else None

    def _touch(self, row_id: int) -> None:
        self._db.execute(
            "UPDATE subsumption SET used_count=used_count+1, last_consulted_at=? WHERE id=?",
            (_NOW(), row_id),
        )
        self._db.commit()

    def _generate_and_store(
        self, entity_a: EntityRef, entity_b: EntityRef, relation_type: str
    ) -> SubsumptionVerdict:
        prompt = (
            f"Determine the subsumption relationship between '{entity_a.identifier}' (namespace: {entity_a.namespace}) "
            f"and '{entity_b.identifier}' (namespace: {entity_b.namespace}) "
            f"under relation_type '{relation_type}'."
        )
        try:
            result = self._llm.extract_with_tool(
                system="You are a knowledge-base reasoning assistant.",
                user_message=prompt,
                tool=SUBSUMPTION_TOOL,
                purpose="subsumption_generation",
            )
        except Exception as exc:
            raise SubsumptionOracleError(f"LLM generation failed: {exc}") from exc

        verdict_str = result.get("verdict", "unrelated")
        reason = result.get("reason", "")
        now = _NOW()

        self._db.execute(
            """INSERT OR REPLACE INTO subsumption
               (entity_a_namespace, entity_a_identifier, entity_b_namespace, entity_b_identifier,
                relation_type, verdict, source, reason, created_at, used_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (entity_a.namespace, entity_a.identifier, entity_b.namespace, entity_b.identifier,
             relation_type, verdict_str, "llm_generated", reason, now),
        )
        self._db.commit()
        row_id: int = self._db.execute("SELECT last_insert_rowid()").fetchone()[0]

        if self._audit is not None:
            log_event(
                self._db,
                event_type="row_created",
                event_subject=f"subsumption:{row_id}",
                event_data={
                    "entity_a": f"{entity_a.namespace}:{entity_a.identifier}",
                    "entity_b": f"{entity_b.namespace}:{entity_b.identifier}",
                    "verdict": verdict_str,
                },
            )

        return SubsumptionVerdict(
            verdict=SubsumptionVerdictType(verdict_str),
            source="llm_generated",
            reason=reason,
            row_id=row_id,
        )
