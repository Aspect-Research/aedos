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
    closed_row_ids: list[int] = field(default_factory=list)


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
        entity_resolver=None,  # stub in Phase 3; wired in Phase 4
        predicate_translation: Optional[PredicateTranslation] = None,
        wikipedia_normalizer=None,
    ) -> None:
        # `db` is required; audit events are written via log_event(db, ...)
        # unconditionally — the vestigial `audit_log` flag (a D8 leftover that
        # build_pipeline never set) was removed in Phase B (B3), matching the
        # A4 cleanup of consistency.py / retraction.py / contradiction_tracer.py.
        #
        # Phase H D47: `wikipedia_normalizer` (optional) lets TierU key
        # rows on the canonical Wikipedia form rather than the surface
        # form, so cross-utterance references to the same entity dedupe
        # to one row. The original surface forms are preserved in the
        # tier_u.subject_surface / object_surface columns. When None
        # (test paths that don't wire it), TierU behaves exactly as
        # before — subject/object are keyed on the literal Claim slots.
        self._db = db
        self._resolver = entity_resolver
        self._oracle = predicate_translation
        self._normalizer = wikipedia_normalizer

    def _normalize_slot(self, value: str, claim: Claim, slot: str) -> str:
        """Phase H D47: return the canonical Wikipedia form for a claim
        slot. No-op when no normalizer is wired or the value is empty;
        also skipped for the asserting party itself (first-person
        canonicalization output) and synthetic event ids."""
        if self._normalizer is None or not value:
            return value
        if claim.asserting_party and value == claim.asserting_party:
            return value
        if value.startswith("event_"):
            return value
        try:
            result = self._normalizer.normalize(
                surface_form=value,
                claim_subject=claim.subject,
                claim_predicate=claim.predicate,
                claim_object=claim.object,
                source_text=claim.source_text,
                slot_position=slot,
                claim_id=claim.claim_id,
            )
        except Exception:
            return value
        return result.normalized_form or value

    def write(self, claim: Claim, source_context: Optional[dict] = None) -> WriteResult:
        """Write claim to Tier U.

        Idempotent on matching content. A prior row is *closed* (its
        `valid_until` set to now) only when the new claim genuinely contradicts
        it (D16) — one of:

          (a) same object, opposite polarity — a direct negation; closes the
              prior regardless of the predicate's cardinality;
          (b) different object, both positive polarity, and the predicate is
              functional (single_valued) — the asserting party revised a
              single-valued slot.

        A different object on a *multi-valued* predicate is a parallel
        assertion — the prior stays open (e.g. two occupations, two hobbies).
        A different object at a different polarity (the contrastive-correction
        shape "X, not Y") is likewise compatible: the prior stays open.
        """
        now = _NOW()
        source_ctx_json = json.dumps(source_context) if source_context else None

        # Phase H D47: persist the canonical form in subject/object and the
        # surface form in subject_surface/object_surface. All downstream
        # keying (idempotency, negation, object-conflict) is on the
        # canonical form, so cross-utterance references to the same entity
        # collapse to one row. When the normalizer is not wired the
        # canonical form equals the surface form and behavior is unchanged.
        subject_canonical = self._normalize_slot(claim.subject, claim, "subject")
        object_canonical = self._normalize_slot(claim.object, claim, "object")

        # Idempotency: exact match on asserting_party + subject + predicate + object + polarity
        existing = self._db.execute(
            """SELECT id FROM tier_u
               WHERE asserting_party=? AND subject=? AND predicate=? AND object=?
               AND polarity=? AND retracted_at IS NULL
               ORDER BY id LIMIT 1""",
            (claim.asserting_party, subject_canonical, claim.predicate,
             object_canonical, claim.polarity),
        ).fetchone()
        if existing is not None:
            return WriteResult(row_id=existing["id"], was_idempotent=True)

        closed_row_ids: list[int] = []
        parallel_assertion = False

        # (a) Direct negation: a prior row with the SAME object at the opposite
        #     polarity. Closed regardless of predicate cardinality.
        negation_rows = self._db.execute(
            """SELECT id FROM tier_u
               WHERE asserting_party=? AND subject=? AND predicate=?
               AND object=? AND polarity=? AND retracted_at IS NULL""",
            (claim.asserting_party, subject_canonical, claim.predicate,
             object_canonical, 1 - claim.polarity),
        ).fetchall()
        closed_row_ids.extend(r["id"] for r in negation_rows)

        # (b) Functional object revision: prior positive rows asserting a
        #     DIFFERENT object. Only fires for a positive new claim and a
        #     functional predicate; on a multi-valued predicate the prior rows
        #     stay open as parallel assertions.
        if claim.polarity == 1:
            other_object_rows = self._db.execute(
                """SELECT id FROM tier_u
                   WHERE asserting_party=? AND subject=? AND predicate=?
                   AND object!=? AND polarity=1 AND retracted_at IS NULL""",
                (claim.asserting_party, subject_canonical, claim.predicate,
                 object_canonical),
            ).fetchall()
            if other_object_rows:
                if self._predicate_is_functional(claim.predicate):
                    closed_row_ids.extend(r["id"] for r in other_object_rows)
                else:
                    parallel_assertion = True

        for closed_id in closed_row_ids:
            self._db.execute(
                "UPDATE tier_u SET valid_until=? WHERE id=?", (now, closed_id)
            )

        self._db.execute(
            """INSERT INTO tier_u
               (asserting_party, subject, predicate, object, polarity,
                valid_from, valid_until, valid_during_ref,
                source_text, source_context, asserted_at,
                subject_surface, object_surface)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                claim.asserting_party,
                subject_canonical,
                claim.predicate,
                object_canonical,
                claim.polarity,
                claim.valid_from,
                claim.valid_until,
                claim.valid_during_ref,
                claim.source_text,
                source_ctx_json,
                now,
                claim.subject if subject_canonical != claim.subject else None,
                claim.object if object_canonical != claim.object else None,
            ),
        )
        self._db.commit()
        row_id: int = self._db.execute("SELECT last_insert_rowid()").fetchone()[0]

        contradiction_closed = bool(closed_row_ids)
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
        # Audit which case fired, so Phase 10.5 can tell a belief revision
        # (a closed prior) from a parallel assertion (a multi-valued addition).
        for closed_id in closed_row_ids:
            log_event(
                self._db,
                event_type="tier_u_row_closed",
                event_subject=f"tier_u:{closed_id}",
                event_data={
                    "closed_by_row_id": row_id,
                    "asserting_party": claim.asserting_party,
                    "predicate": claim.predicate,
                },
            )
        if parallel_assertion:
            log_event(
                self._db,
                event_type="tier_u_parallel_assertion",
                event_subject=f"tier_u:{row_id}",
                event_data={
                    "asserting_party": claim.asserting_party,
                    "predicate": claim.predicate,
                },
            )

        return WriteResult(
            row_id=row_id,
            was_idempotent=False,
            contradiction_closed=contradiction_closed,
            closed_row_ids=closed_row_ids,
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

    def lookup_object_conflict(
        self,
        claim: Claim,
        current_time: Optional[str] = None,
    ) -> LookupResult:
        """Find currently-valid, non-retracted, *positive* Tier U rows for the
        same (asserting_party, subject, predicate) whose object differs from the
        claim's.

        For a functional (single_valued) predicate such a row contradicts a
        positive claim — the asserting party already stipulated a different
        value for the slot. This is the object-conflict half of belief revision
        (D16); the caller (the walker) consults `single_valued` and decides.
        Multi-valued predicates do not conflict on an object difference.

        Only positive (polarity=1) rows are returned: a negative Tier U row
        `¬(S P O′)` about a different object O′ does not bear on a claim about
        O. Literal match only — no entity/predicate broadening.

        Phase H D47: subject + object are normalized to canonical Wikipedia
        form (when the normalizer is wired) before keying. A prior row
        asserting "Asa lives_in Boston" and a current claim "Asa lives_in
        Massachusetts" — wait, those are different canonicals; conflict
        legitimately fires. But "Asa lives_in Boston" vs. "Asa lives_in
        Boston, Massachusetts" — same canonical "Boston" after redirect —
        correctly dedupes through this path.
        """
        if current_time is None:
            current_time = _NOW()
        subject_canonical = self._normalize_slot(claim.subject, claim, "subject")
        object_canonical = self._normalize_slot(claim.object, claim, "object")
        rows = self._db.execute(
            """SELECT * FROM tier_u
               WHERE asserting_party=? AND subject=? AND predicate=?
               AND object != ? AND polarity=1 AND retracted_at IS NULL
               AND (valid_until IS NULL OR (valid_until != ? AND valid_until > ?))""",
            (claim.asserting_party, subject_canonical, claim.predicate,
             object_canonical, BEFORE_PRESENT, current_time),
        ).fetchall()
        rows = [dict(r) for r in rows]
        return LookupResult(found=bool(rows), rows=rows, stage=1)

    def retract(self, row_id: int, reason: str) -> None:
        """Retract a Tier U row."""
        now = _NOW()
        self._db.execute(
            "UPDATE tier_u SET retracted_at=?, retraction_reason=? WHERE id=?",
            (now, reason, row_id),
        )
        self._db.commit()
        log_event(
            self._db,
            event_type="row_retracted",
            event_subject=f"tier_u:{row_id}",
            event_data={"reason": reason},
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _predicate_is_functional(self, predicate: str) -> bool:
        """Whether `predicate` is functional (single_valued) per the predicate
        translation oracle.

        Treated as multi-valued — the architecture 5.2 conservative default —
        when no oracle is wired or the consult fails: a wrong 0 keeps a
        parallel row open (a false abstain at worst), whereas a wrong 1 would
        wrongly close a live row. In the assembled pipeline the predicate has
        already been routed by Layer 2, so this consult is a cache hit.
        """
        if self._oracle is None:
            return False
        try:
            return bool(self._oracle.consult(predicate).single_valued)
        except Exception:
            return False

    def _stage1(self, claim: Claim, current_time: str) -> LookupResult:
        subject_canonical = self._normalize_slot(claim.subject, claim, "subject")
        object_canonical = self._normalize_slot(claim.object, claim, "object")
        rows = self._query_current(
            claim.asserting_party, subject_canonical, claim.predicate,
            object_canonical, claim.polarity, current_time,
        )
        if rows:
            return LookupResult(found=True, rows=rows, stage=1)
        return LookupResult(found=False)

    def _stage1_historical(self, claim: Claim) -> list[dict]:
        subject_canonical = self._normalize_slot(claim.subject, claim, "subject")
        object_canonical = self._normalize_slot(claim.object, claim, "object")
        rows = self._db.execute(
            """SELECT * FROM tier_u
               WHERE asserting_party=? AND subject=? AND predicate=? AND object=?
               AND polarity=? AND retracted_at IS NULL
               AND (valid_until IS NOT NULL OR valid_until=?)""",
            (
                claim.asserting_party, subject_canonical, claim.predicate,
                object_canonical, claim.polarity, BEFORE_PRESENT,
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
        subject_canonical = self._normalize_slot(claim.subject, claim, "subject")
        object_canonical = self._normalize_slot(claim.object, claim, "object")
        for neighbor in neighbors:
            if neighbor.retracted_at is not None:
                continue
            rows = self._query_current(
                claim.asserting_party, subject_canonical, neighbor.aedos_predicate,
                object_canonical, claim.polarity, current_time,
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
