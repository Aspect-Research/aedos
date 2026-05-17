from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from ..audit.log import log_event
from ..llm.client import LLMClient

_NOW = lambda: datetime.now(timezone.utc).isoformat()

PREDICATE_METADATA_TOOL: dict[str, Any] = {
    "name": "generate_predicate_metadata",
    "description": (
        "Produce structured metadata for the given Aedos predicate. "
        "Choose routing_hint conservatively: prefer abstain over a speculative kb_resolvable."
    ),
    "input_schema": {
        "type": "object",
        "required": ["object_type", "user_subject_required", "routing_hint", "reason"],
        "properties": {
            "object_type": {
                "type": "string",
                "enum": ["entity", "quantity", "time", "proposition", "entity_list"],
                "description": "The type of value the object slot holds.",
            },
            "user_subject_required": {
                "type": "integer",
                "enum": [0, 1],
                "description": "1 if the subject must be the asserting party (e.g., prefers, believes).",
            },
            "distinct_slots": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "Slot pairs that must differ, e.g. ['subject', 'object'].",
            },
            "routing_hint": {
                "type": "string",
                "enum": ["user_authoritative", "python", "kb_resolvable", "abstain"],
            },
            "kb_namespace": {
                "type": ["string", "null"],
                "description": "KB namespace, e.g. 'wikidata'. Null when not kb_resolvable.",
            },
            "kb_property": {
                "type": ["string", "null"],
                "description": "KB property identifier, e.g. 'P39'. Null when not kb_resolvable.",
            },
            "slot_to_qualifier": {
                "type": ["object", "null"],
                "description": "JSON mapping Aedos slot names to KB qualifier P-numbers.",
            },
            "single_valued": {
                "type": "integer",
                "enum": [0, 1],
                "description": (
                    "1 if the predicate is functional/single-valued — a subject "
                    "has at most one true object (e.g. place_of_birth, date_of_death). "
                    "0 if multi-valued (e.g. position_held, occupation, award_received). "
                    "Only a functional predicate licenses a KB contradiction from a "
                    "non-matching value."
                ),
            },
            "reason": {
                "type": "string",
                "description": "1-2 sentence justification for the routing and mapping choices.",
            },
        },
    },
}

_GENERATION_SYSTEM_PROMPT = """\
You are a knowledge-representation expert helping to build a claim-verification system.
Given an Aedos predicate (a canonical snake_case relational predicate), produce its metadata:

object_type options:
  entity       — the object is a named entity (person, place, org, concept)
  quantity     — the object is a number with optional unit
  time         — the object is a date, time, or duration
  proposition  — the object is a nested claim
  entity_list  — the object is a list of entities

routing_hint options:
  user_authoritative — the asserting party is the ground truth (preference, belief, opinion)
  python             — reducible to deterministic computation (arithmetic, date math)
  kb_resolvable      — maps to a structured knowledge base property (Wikidata)
  abstain            — no authoritative source of belief; cannot be verified

When in doubt, choose abstain over kb_resolvable. Only choose kb_resolvable when you are
confident the predicate maps to a real Wikidata property.

single_valued: set 1 only for functional predicates where a subject has at most one
true object (place_of_birth, date_of_death, capital). Set 0 for multi-valued predicates
(position_held, occupation, award_received, member_of). When unsure, choose 0 — a wrong
single_valued=1 produces a false contradiction.
"""


@dataclass
class PredicateMetadata:
    id: int
    aedos_predicate: str
    object_type: str
    user_subject_required: bool
    distinct_slots: Optional[list[str]]
    routing_hint: str
    kb_namespace: Optional[str]
    kb_property: Optional[str]
    slot_to_qualifier: Optional[dict]
    reason: str
    created_at: str
    last_consulted_at: Optional[str] = None
    used_count: int = 0
    retracted_at: Optional[str] = None
    retraction_reason: Optional[str] = None
    single_valued: bool = False  # functional predicate: licenses KB contradiction


class PredicateTranslationError(Exception):
    def __init__(self, predicate: str, cause: str, details: str = ""):
        super().__init__(f"predicate_translation failed for {predicate!r}: {cause}. {details}")
        self.predicate = predicate
        self.cause = cause
        self.details = details


class PredicateTranslation:
    def __init__(
        self,
        db: sqlite3.Connection,
        llm_client: LLMClient,
        audit_log=None,
    ) -> None:
        self._db = db
        self._llm = llm_client
        self._audit = audit_log

    def consult(
        self,
        aedos_predicate: str,
        kb_namespace: Optional[str] = None,
    ) -> PredicateMetadata:
        """Return predicate metadata from cache or generate it via LLM.

        Raises PredicateTranslationError if generation fails.
        """
        row = self._fetch(aedos_predicate)
        if row is not None:
            self._touch(row.id)
            return row
        return self._generate_and_store(aedos_predicate, kb_namespace)

    def retract(self, row_id: int, reason: str) -> None:
        """Retract a row. Sets retracted_at; does not delete."""
        now = _NOW()
        self._db.execute(
            "UPDATE predicate_translation SET retracted_at=?, retraction_reason=? WHERE id=?",
            (now, reason, row_id),
        )
        self._db.commit()
        if self._audit is not None:
            log_event(
                self._db,
                event_type="row_retracted",
                event_subject=f"predicate_translation:{row_id}",
                event_data={"reason": reason},
            )

    def query_neighbors(self, aedos_predicate: str) -> list[PredicateMetadata]:
        """Return rows whose kb_property matches the given predicate's kb_property."""
        subject = self._fetch(aedos_predicate)
        if subject is None or subject.kb_property is None:
            return []
        rows = self._db.execute(
            "SELECT * FROM predicate_translation WHERE kb_property=? AND aedos_predicate!=?",
            (subject.kb_property, aedos_predicate),
        ).fetchall()
        return [self._row_to_metadata(r) for r in rows]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch(self, aedos_predicate: str) -> Optional[PredicateMetadata]:
        """Return the first non-retracted row for the predicate, or None."""
        row = self._db.execute(
            "SELECT * FROM predicate_translation "
            "WHERE aedos_predicate=? AND retracted_at IS NULL "
            "ORDER BY id LIMIT 1",
            (aedos_predicate,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_metadata(row)

    def _touch(self, row_id: int) -> None:
        now = _NOW()
        self._db.execute(
            "UPDATE predicate_translation "
            "SET last_consulted_at=?, used_count=used_count+1 WHERE id=?",
            (now, row_id),
        )
        self._db.commit()

    def _generate_and_store(
        self, aedos_predicate: str, kb_namespace: Optional[str]
    ) -> PredicateMetadata:
        try:
            raw = self._llm.extract_with_tool(
                system=_GENERATION_SYSTEM_PROMPT,
                user_message=f'Generate metadata for the Aedos predicate: "{aedos_predicate}"',
                tool=PREDICATE_METADATA_TOOL,
                purpose="substrate:predicate_translation",
            )
        except Exception as exc:
            if self._audit is not None:
                log_event(
                    self._db,
                    event_type="row_generation_failed",
                    event_subject=f"predicate_translation:{aedos_predicate}",
                    event_data={"error": str(exc)},
                )
            raise PredicateTranslationError(
                aedos_predicate, "llm_call_failed", str(exc)
            ) from exc

        # Validate required fields
        for required in ("object_type", "routing_hint", "reason"):
            if not raw.get(required):
                if self._audit is not None:
                    log_event(
                        self._db,
                        event_type="row_generation_failed",
                        event_subject=f"predicate_translation:{aedos_predicate}",
                        event_data={"error": f"missing field: {required}"},
                    )
                raise PredicateTranslationError(
                    aedos_predicate,
                    "malformed_response",
                    f"missing required field: {required}",
                )

        now = _NOW()
        effective_kb_namespace = raw.get("kb_namespace") or kb_namespace
        distinct_slots_raw = raw.get("distinct_slots")
        slot_to_qualifier_raw = raw.get("slot_to_qualifier")

        # INSERT OR REPLACE handles the case where a retracted row exists for the same
        # (predicate, namespace) key — SQLite deletes the old row and inserts the new one.
        single_valued = int(raw.get("single_valued", 0) or 0)
        self._db.execute(
            """INSERT OR REPLACE INTO predicate_translation
               (aedos_predicate, object_type, user_subject_required, distinct_slots,
                routing_hint, kb_namespace, kb_property, slot_to_qualifier,
                single_valued, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                aedos_predicate,
                raw["object_type"],
                int(raw.get("user_subject_required", 0)),
                json.dumps(distinct_slots_raw) if distinct_slots_raw else None,
                raw["routing_hint"],
                effective_kb_namespace,
                raw.get("kb_property"),
                json.dumps(slot_to_qualifier_raw) if slot_to_qualifier_raw else None,
                single_valued,
                raw["reason"],
                now,
            ),
        )
        self._db.commit()
        row_id = self._db.execute("SELECT last_insert_rowid()").fetchone()[0]

        if self._audit is not None:
            log_event(
                self._db,
                event_type="row_created",
                event_subject=f"predicate_translation:{row_id}",
                event_data={
                    "aedos_predicate": aedos_predicate,
                    "routing_hint": raw["routing_hint"],
                    "kb_property": raw.get("kb_property"),
                },
            )

        return PredicateMetadata(
            id=row_id,
            aedos_predicate=aedos_predicate,
            object_type=raw["object_type"],
            user_subject_required=bool(int(raw.get("user_subject_required", 0))),
            distinct_slots=distinct_slots_raw,
            routing_hint=raw["routing_hint"],
            kb_namespace=effective_kb_namespace,
            kb_property=raw.get("kb_property"),
            slot_to_qualifier=slot_to_qualifier_raw,
            reason=raw["reason"],
            created_at=now,
            single_valued=bool(single_valued),
        )

    @staticmethod
    def _row_to_metadata(row: sqlite3.Row) -> PredicateMetadata:
        def _parse_json(val: Optional[str]) -> Any:
            if val is None:
                return None
            try:
                return json.loads(val)
            except (json.JSONDecodeError, TypeError):
                return None

        return PredicateMetadata(
            id=row["id"],
            aedos_predicate=row["aedos_predicate"],
            object_type=row["object_type"],
            user_subject_required=bool(row["user_subject_required"]),
            distinct_slots=_parse_json(row["distinct_slots"]),
            routing_hint=row["routing_hint"],
            kb_namespace=row["kb_namespace"],
            kb_property=row["kb_property"],
            slot_to_qualifier=_parse_json(row["slot_to_qualifier"]),
            reason=row["reason"],
            created_at=row["created_at"],
            last_consulted_at=row["last_consulted_at"],
            used_count=row["used_count"],
            retracted_at=row["retracted_at"],
            retraction_reason=row["retraction_reason"],
            single_valued=bool(row["single_valued"]),
        )
