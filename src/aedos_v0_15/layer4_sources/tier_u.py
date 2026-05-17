from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..audit.log import log_event
from ..layer1_extraction.extractor import Claim
from ..layer1_extraction.temporal import BEFORE_PRESENT
from ..layer3_substrate.predicate_translation import PredicateTranslation, PredicateTranslationError

_NOW = lambda: datetime.now(timezone.utc).isoformat()


@dataclass
class WriteResult:
    row_id: int
    was_idempotent: bool = False
    contradiction_closed: bool = False
    closed_row_id: Optional[int] = None


@dataclass
class LookupResult:
    found: bool
    rows: list[dict] = field(default_factory=list)
    stage: int = 0  # 1=literal, 2=entity-resolution, 3=predicate-translation
    historical_only: bool = False  # True when only historical rows were found


class TierU:
    def __init__(
        self,
        db: sqlite3.Connection,
        audit_log=None,
        entity_resolver=None,  # stub in Phase 3; wired in Phase 4
        predicate_translation: Optional[PredicateTranslation] = None,
    ) -> None:
        self._db = db
        self._audit = audit_log
        self._resolver = entity_resolver
        self._oracle = predicate_translation

    def write(self, claim: Claim, source_context: Optional[dict] = None) -> WriteResult:
        """Write claim to Tier U. Idempotent on matching content; closes contradictions."""
        now = _NOW()
        source_ctx_json = json.dumps(source_context) if source_context else None

        # Idempotency: exact match on asserting_party + subject + predicate + object + polarity
        existing = self._db.execute(
            """SELECT id FROM tier_u
               WHERE asserting_party=? AND subject=? AND predicate=? AND object=?
               AND polarity=? AND retracted_at IS NULL
               ORDER BY id LIMIT 1""",
            (claim.asserting_party, claim.subject, claim.predicate,
             claim.object, claim.polarity),
        ).fetchone()
        if existing is not None:
            return WriteResult(row_id=existing["id"], was_idempotent=True)

        # Contradiction check: same (asserting_party, subject, predicate) but conflicting content
        conflict = self._db.execute(
            """SELECT id, asserted_at FROM tier_u
               WHERE asserting_party=? AND subject=? AND predicate=?
               AND (object != ? OR polarity != ?) AND retracted_at IS NULL
               ORDER BY id LIMIT 1""",
            (claim.asserting_party, claim.subject, claim.predicate,
             claim.object, claim.polarity),
        ).fetchone()

        contradiction_closed = False
        closed_row_id: Optional[int] = None
        if conflict is not None:
            # Close the contradicted row by setting valid_until to now
            self._db.execute(
                "UPDATE tier_u SET valid_until=? WHERE id=?",
                (now, conflict["id"]),
            )
            contradiction_closed = True
            closed_row_id = conflict["id"]

        self._db.execute(
            """INSERT INTO tier_u
               (asserting_party, subject, predicate, object, polarity,
                valid_from, valid_until, valid_during_ref,
                source_text, source_context, asserted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                claim.asserting_party,
                claim.subject,
                claim.predicate,
                claim.object,
                claim.polarity,
                claim.valid_from,
                claim.valid_until,
                claim.valid_during_ref,
                claim.source_text,
                source_ctx_json,
                now,
            ),
        )
        self._db.commit()
        row_id: int = self._db.execute("SELECT last_insert_rowid()").fetchone()[0]

        if self._audit is not None:
            log_event(
                self._db,
                event_type="row_created",
                event_subject=f"tier_u:{row_id}",
                event_data={
                    "asserting_party": claim.asserting_party,
                    "predicate": claim.predicate,
                    "contradiction_closed": contradiction_closed,
                },
            )

        return WriteResult(
            row_id=row_id,
            was_idempotent=False,
            contradiction_closed=contradiction_closed,
            closed_row_id=closed_row_id,
        )

    def lookup(
        self,
        claim: Claim,
        current_time: Optional[str] = None,
    ) -> LookupResult:
        """Three-stage lookup against Tier U rows."""
        if current_time is None:
            current_time = _NOW()

        # Stage 1: literal match
        result = self._stage1(claim, current_time)
        if result.found:
            return result

        # Stage 2: entity-resolution broadening (stub in Phase 3)
        if self._resolver is not None:
            result = self._stage2(claim, current_time)
            if result.found:
                return result

        # Stage 3: predicate-translation broadening
        if self._oracle is not None:
            result = self._stage3(claim, current_time)
            if result.found:
                return result

        # Check if there are only historical matches
        hist = self._stage1_historical(claim)
        if hist:
            return LookupResult(found=False, rows=hist, stage=1, historical_only=True)

        return LookupResult(found=False)

    def retract(self, row_id: int, reason: str) -> None:
        """Retract a Tier U row."""
        now = _NOW()
        self._db.execute(
            "UPDATE tier_u SET retracted_at=?, retraction_reason=? WHERE id=?",
            (now, reason, row_id),
        )
        self._db.commit()
        if self._audit is not None:
            log_event(
                self._db,
                event_type="row_retracted",
                event_subject=f"tier_u:{row_id}",
                event_data={"reason": reason},
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _stage1(self, claim: Claim, current_time: str) -> LookupResult:
        rows = self._query_current(
            claim.asserting_party, claim.subject, claim.predicate,
            claim.object, claim.polarity, current_time,
        )
        if rows:
            return LookupResult(found=True, rows=rows, stage=1)
        return LookupResult(found=False)

    def _stage1_historical(self, claim: Claim) -> list[dict]:
        rows = self._db.execute(
            """SELECT * FROM tier_u
               WHERE asserting_party=? AND subject=? AND predicate=? AND object=?
               AND polarity=? AND retracted_at IS NULL
               AND (valid_until IS NOT NULL OR valid_until=?)""",
            (
                claim.asserting_party, claim.subject, claim.predicate,
                claim.object, claim.polarity, BEFORE_PRESENT,
            ),
        ).fetchall()
        return [dict(r) for r in rows]

    def _stage2(self, claim: Claim, current_time: str) -> LookupResult:
        """Entity-resolution broadening — stub; not implemented until Phase 4."""
        return LookupResult(found=False)

    def _stage3(self, claim: Claim, current_time: str) -> LookupResult:
        """Predicate-translation broadening via oracle neighbors."""
        try:
            neighbors = self._oracle.query_neighbors(claim.predicate)
        except PredicateTranslationError:
            return LookupResult(found=False)
        for neighbor in neighbors:
            if neighbor.retracted_at is not None:
                continue
            rows = self._query_current(
                claim.asserting_party, claim.subject, neighbor.aedos_predicate,
                claim.object, claim.polarity, current_time,
            )
            if rows:
                return LookupResult(found=True, rows=rows, stage=3)
        return LookupResult(found=False)

    def _query_current(
        self,
        asserting_party: str,
        subject: str,
        predicate: str,
        object_val: str,
        polarity: int,
        current_time: str,
    ) -> list[dict]:
        """Return non-retracted, currently-valid rows matching all given fields."""
        rows = self._db.execute(
            """SELECT * FROM tier_u
               WHERE asserting_party=? AND subject=? AND predicate=? AND object=?
               AND polarity=? AND retracted_at IS NULL
               AND (valid_until IS NULL OR (valid_until != ? AND valid_until > ?))""",
            (asserting_party, subject, predicate, object_val, polarity,
             BEFORE_PRESENT, current_time),
        ).fetchall()
        return [dict(r) for r in rows]
