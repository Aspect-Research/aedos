from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from ..audit.log import log_event
from ..llm.client import LLMClient

_NOW = lambda: datetime.now(timezone.utc).isoformat()

PREDICATE_DISTRIBUTION_TOOL: dict[str, Any] = {
    "name": "generate_distribution_verdict",
    "description": (
        "Determine whether a predicate distributes up, down, both, or neither "
        "over a subsumption relation of the given type."
    ),
    "input_schema": {
        "type": "object",
        "required": ["verdict", "reason"],
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["distributes_up", "distributes_down", "both", "neither"],
                "description": (
                    "distributes_up: if P(X) and X is_a/part_of Y then P(Y). "
                    "distributes_down: if P(Y) and X is_a/part_of Y then P(X). "
                    "both: distributes in both directions. "
                    "neither: does not distribute."
                ),
            },
            "reason": {
                "type": "string",
                "description": "1-2 sentence justification.",
            },
        },
    },
}


class DistributionVerdictType(str, Enum):
    DISTRIBUTES_UP = "distributes_up"
    DISTRIBUTES_DOWN = "distributes_down"
    BOTH = "both"
    NEITHER = "neither"


@dataclass
class DistributionVerdict:
    verdict: DistributionVerdictType
    reason: str
    row_id: Optional[int] = None
    was_cached: bool = False


class PredicateDistributionError(Exception):
    pass


class PredicateDistributionOracle:
    def __init__(
        self,
        db: sqlite3.Connection,
        llm_client: LLMClient,
        consistency_checker=None,
    ) -> None:
        self._db = db
        self._llm = llm_client
        self._consistency = consistency_checker

    def consult(
        self, predicate: str, polarity: int, relation_type: str
    ) -> DistributionVerdict:
        """Lookup-first; LLM cold-cache generation on miss."""
        row = self._fetch(predicate, polarity, relation_type)
        if row is not None:
            self._touch(row["id"])
            return DistributionVerdict(
                verdict=DistributionVerdictType(row["verdict"]),
                reason=row["reason"],
                row_id=row["id"],
                was_cached=True,
            )
        return self._generate_and_store(predicate, polarity, relation_type)

    def retract(self, row_id: int, reason: str) -> None:
        now = _NOW()
        self._db.execute(
            "UPDATE predicate_distribution SET retracted_at=?, retraction_reason=? WHERE id=?",
            (now, reason, row_id),
        )
        self._db.commit()
        log_event(
            self._db,
            event_type="row_retracted",
            event_subject=f"predicate_distribution:{row_id}",
            event_data={"reason": reason},
        )

    def query_neighbors(
        self, predicate: str, relation_type: str
    ) -> list[DistributionVerdict]:
        """Return all non-retracted rows for this predicate across polarities."""
        rows = self._db.execute(
            """SELECT * FROM predicate_distribution
               WHERE aedos_predicate=? AND relation_type=? AND retracted_at IS NULL""",
            (predicate, relation_type),
        ).fetchall()
        return [
            DistributionVerdict(
                verdict=DistributionVerdictType(r["verdict"]),
                reason=r["reason"],
                row_id=r["id"],
                was_cached=True,
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _fetch(
        self, predicate: str, polarity: int, relation_type: str
    ) -> Optional[dict]:
        row = self._db.execute(
            """SELECT * FROM predicate_distribution
               WHERE aedos_predicate=? AND polarity=? AND relation_type=?
               AND retracted_at IS NULL
               ORDER BY id LIMIT 1""",
            (predicate, polarity, relation_type),
        ).fetchone()
        return dict(row) if row is not None else None

    def _touch(self, row_id: int) -> None:
        self._db.execute(
            "UPDATE predicate_distribution SET used_count=used_count+1, last_consulted_at=? WHERE id=?",
            (_NOW(), row_id),
        )
        self._db.commit()

    def _generate_and_store(
        self, predicate: str, polarity: int, relation_type: str
    ) -> DistributionVerdict:
        polarity_label = "asserted" if polarity == 1 else "negated"
        # v0.16 WS2 §3b: trimmed to neutral verdict definitions. The former
        # "AUTHORITATIVE RUBRIC" (pinned lives_in/mortal/prefers framings) plus
        # the polarity default were hand-seeded knowledge — a hardcoded-mapping
        # smell that pre-committed the oracle to the corpus's verdicts rather
        # than letting it reason from the predicate's semantics. The rubric
        # was a band-aid for an under-discovered substrate; v0.16 removes the
        # gate role (distribution is now only a RANKER for part_of and the
        # kind-entailment authority for is_a, with structural soundness enforced
        # at verify time in the walker), so the oracle no longer needs to be
        # over-confident. Only the four verdict definitions remain.
        prompt = (
            f"Determine how the predicate '{predicate}' distributes over a "
            f"'{relation_type}' subsumption relation (one of is_a or part_of) "
            f"under polarity {polarity} ({polarity_label}).\n\n"
            f"VERDICT DEFINITIONS:\n"
            f"  distributes_up:   if P(X) and X {relation_type} Y, then P(Y).\n"
            f"  distributes_down: if P(Y) and X {relation_type} Y, then P(X).\n"
            f"  both:             distributes in both directions.\n"
            f"  neither:          does not distribute in either direction."
        )
        try:
            result = self._llm.extract_with_tool(
                system="You are a knowledge-base reasoning assistant specializing in predicate logic.",
                user_message=prompt,
                tool=PREDICATE_DISTRIBUTION_TOOL,
                purpose="substrate:predicate_distribution",
            )
        except Exception as exc:
            raise PredicateDistributionError(f"LLM generation failed: {exc}") from exc

        verdict_str = result.get("verdict", "neither")
        reason = result.get("reason", "")
        now = _NOW()

        self._db.execute(
            """INSERT OR REPLACE INTO predicate_distribution
               (aedos_predicate, polarity, relation_type, verdict, reason, created_at, used_count)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (predicate, polarity, relation_type, verdict_str, reason, now),
        )
        self._db.commit()
        row_id: int = self._db.execute("SELECT last_insert_rowid()").fetchone()[0]

        log_event(
            self._db,
            event_type="row_created",
            event_subject=f"predicate_distribution:{row_id}",
            event_data={
                "predicate": predicate,
                "polarity": polarity,
                "relation_type": relation_type,
                "verdict": verdict_str,
            },
        )

        # Substrate-internal consistency check on write (architecture 5.4).
        if self._consistency is not None:
            _result = self._consistency.check_on_write("predicate_distribution", row_id)
            if _result.status == "conflict":
                self._consistency.resolve_conflict(_result)

        return DistributionVerdict(
            verdict=DistributionVerdictType(verdict_str),
            reason=reason,
            row_id=row_id,
            was_cached=False,
        )
