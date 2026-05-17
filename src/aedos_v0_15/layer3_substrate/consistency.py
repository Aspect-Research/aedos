from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

_DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 3


@dataclass
class ConsistencyResult:
    status: str  # "pass" | "conflict"
    inconsistency_class: Optional[str] = None
    row_a_id: Optional[int] = None
    row_b_id: Optional[int] = None
    table: Optional[str] = None
    details: dict = field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_inverse_mapping(sq_a_raw, sq_b_raw) -> bool:
    """True if two slot_to_qualifier maps are exact subject/object inversions
    of each other (N5).

    Inverse predicates — e.g. ``capital_of`` and ``has_capital``, both mapped
    to Wikidata P36 — legitimately map to the same KB property with subject and
    object swapped. The ``transitive_equivalence_violation`` rule must not flag
    such a pair as a conflict. Two maps qualify as inverses when A's subject is
    B's object, A's object is B's subject, and every other key is identical.
    Any other form of divergence on the same KB property remains a conflict.
    """
    if sq_a_raw is None or sq_b_raw is None:
        return False
    try:
        a = json.loads(sq_a_raw) if isinstance(sq_a_raw, str) else sq_a_raw
        b = json.loads(sq_b_raw) if isinstance(sq_b_raw, str) else sq_b_raw
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    if a.keys() != b.keys():
        return False
    if "subject" not in a or "object" not in a:
        return False
    # subject/object must be a clean swap...
    if a.get("subject") != b.get("object") or a.get("object") != b.get("subject"):
        return False
    # ...and every other key must be identical.
    return all(a[k] == b[k] for k in a if k not in ("subject", "object"))


class ConsistencyChecker:
    def __init__(
        self,
        db,
        audit_log=None,
        retraction_propagator=None,
        config: Optional[dict] = None,
    ) -> None:
        self._db = db
        self._audit = audit_log
        self._retraction_propagator = retraction_propagator
        cfg = config or {}
        self._threshold = cfg.get("circuit_breaker_threshold", _DEFAULT_CIRCUIT_BREAKER_THRESHOLD)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_on_write(self, table: str, row_id: int) -> ConsistencyResult:
        """Check a newly-written row against its neighbors."""
        if table == "predicate_translation":
            return self._check_predicate_translation_row(row_id)
        if table == "subsumption":
            return self._check_subsumption_row(row_id)
        if table == "predicate_distribution":
            return self._check_distribution_row(row_id)
        return ConsistencyResult(status="pass")

    def check_periodic(self) -> list[ConsistencyResult]:
        """Batch scan over all substrate tables."""
        results: list[ConsistencyResult] = []
        results.extend(self._scan_predicate_translation())
        results.extend(self._scan_subsumption())
        results.extend(self._scan_predicate_distribution())
        return [r for r in results if r.status == "conflict"]

    def resolve_conflict(self, conflict: ConsistencyResult) -> None:
        """Retract both rows; update circuit breaker; log."""
        if conflict.status != "conflict":
            return

        now = _now_iso()

        for row_id in (conflict.row_a_id, conflict.row_b_id):
            if row_id is None:
                continue
            self._db.execute(
                f"UPDATE {conflict.table} SET retracted_at=?, retraction_reason=? WHERE id=?",
                (now, f"consistency_check:{conflict.inconsistency_class}", row_id),
            )
        self._db.commit()

        # Architecture 5.4 step 2: verdicts whose justification traces include
        # either retracted row are propagated for re-derivation.
        if self._retraction_propagator is not None:
            for row_id in (conflict.row_a_id, conflict.row_b_id):
                if row_id is not None:
                    self._retraction_propagator.propagate_retraction(conflict.table, row_id)

        sig = self._question_signature(conflict)
        self._increment_circuit_breaker(sig, now)

        if self._audit:
            self._audit.log(
                event_type="consistency_violation",
                event_subject=sig,
                event_data=json.dumps({
                    "table": conflict.table,
                    "inconsistency_class": conflict.inconsistency_class,
                    "row_a_id": conflict.row_a_id,
                    "row_b_id": conflict.row_b_id,
                }),
            )

    # ------------------------------------------------------------------
    # Check per table
    # ------------------------------------------------------------------

    def _check_predicate_translation_row(self, row_id: int) -> ConsistencyResult:
        row = self._db.execute(
            "SELECT aedos_predicate, kb_namespace, kb_property, slot_to_qualifier "
            "FROM predicate_translation WHERE id=? AND retracted_at IS NULL",
            (row_id,),
        ).fetchone()
        if not row:
            return ConsistencyResult(status="pass")

        pred, ns, prop, sq = row["aedos_predicate"], row["kb_namespace"], row["kb_property"], row["slot_to_qualifier"]
        if prop is None:
            return ConsistencyResult(status="pass")

        # Conflict: a DIFFERENT predicate maps to the same (kb_namespace, kb_property) with a different
        # slot_to_qualifier. Two predicates with incompatible mappings to the same KB property.
        conflicts = self._db.execute(
            "SELECT id, aedos_predicate, slot_to_qualifier FROM predicate_translation "
            "WHERE id != ? AND kb_namespace=? AND kb_property=? AND retracted_at IS NULL",
            (row_id, ns, prop),
        ).fetchall()

        for c in conflicts:
            if c["slot_to_qualifier"] != sq:
                # N5: inverse predicates (capital_of / has_capital) map to the
                # same KB property with subject/object swapped. That is a
                # legitimate inverse pair, not an incompatible mapping — skip it.
                if _is_inverse_mapping(sq, c["slot_to_qualifier"]):
                    continue
                return ConsistencyResult(
                    status="conflict",
                    inconsistency_class="transitive_equivalence_violation",
                    row_a_id=row_id,
                    row_b_id=c["id"],
                    table="predicate_translation",
                    details={"predicate_a": pred, "predicate_b": c["aedos_predicate"], "kb_property": prop},
                )

        return ConsistencyResult(status="pass")

    def _check_subsumption_row(self, row_id: int) -> ConsistencyResult:
        row = self._db.execute(
            "SELECT entity_a_namespace, entity_a_identifier, entity_b_namespace, entity_b_identifier, "
            "relation_type, verdict FROM subsumption WHERE id=? AND retracted_at IS NULL",
            (row_id,),
        ).fetchone()
        if not row:
            return ConsistencyResult(status="pass")

        conflicts = self._db.execute(
            "SELECT id, verdict FROM subsumption "
            "WHERE id != ? AND entity_a_namespace=? AND entity_a_identifier=? "
            "AND entity_b_namespace=? AND entity_b_identifier=? AND relation_type=? "
            "AND verdict != ? AND retracted_at IS NULL",
            (row_id, row["entity_a_namespace"], row["entity_a_identifier"],
             row["entity_b_namespace"], row["entity_b_identifier"],
             row["relation_type"], row["verdict"]),
        ).fetchall()

        if conflicts:
            return ConsistencyResult(
                status="conflict",
                inconsistency_class="contradicting_subsumption",
                row_a_id=row_id,
                row_b_id=conflicts[0]["id"],
                table="subsumption",
                details={
                    "entity_a": f"{row['entity_a_namespace']}:{row['entity_a_identifier']}",
                    "entity_b": f"{row['entity_b_namespace']}:{row['entity_b_identifier']}",
                    "relation_type": row["relation_type"],
                },
            )

        return ConsistencyResult(status="pass")

    def _check_distribution_row(self, row_id: int) -> ConsistencyResult:
        row = self._db.execute(
            "SELECT aedos_predicate, polarity, relation_type, verdict "
            "FROM predicate_distribution WHERE id=? AND retracted_at IS NULL",
            (row_id,),
        ).fetchone()
        if not row:
            return ConsistencyResult(status="pass")

        conflicts = self._db.execute(
            "SELECT id, verdict FROM predicate_distribution "
            "WHERE id != ? AND aedos_predicate=? AND polarity=? AND relation_type=? "
            "AND verdict != ? AND retracted_at IS NULL",
            (row_id, row["aedos_predicate"], row["polarity"], row["relation_type"], row["verdict"]),
        ).fetchall()

        if conflicts:
            return ConsistencyResult(
                status="conflict",
                inconsistency_class="conflicting_distribution",
                row_a_id=row_id,
                row_b_id=conflicts[0]["id"],
                table="predicate_distribution",
                details={
                    "predicate": row["aedos_predicate"],
                    "polarity": row["polarity"],
                    "relation_type": row["relation_type"],
                },
            )

        return ConsistencyResult(status="pass")

    # ------------------------------------------------------------------
    # Periodic scan (dedup by pair)
    # ------------------------------------------------------------------

    def _scan_predicate_translation(self) -> list[ConsistencyResult]:
        rows = self._db.execute(
            "SELECT id FROM predicate_translation WHERE retracted_at IS NULL"
        ).fetchall()
        seen: set[tuple] = set()
        results = []
        for r in rows:
            result = self._check_predicate_translation_row(r["id"])
            if result.status == "conflict":
                pair = (min(result.row_a_id, result.row_b_id), max(result.row_a_id, result.row_b_id))
                if pair not in seen:
                    seen.add(pair)
                    results.append(result)
        return results

    def _scan_subsumption(self) -> list[ConsistencyResult]:
        rows = self._db.execute(
            "SELECT id FROM subsumption WHERE retracted_at IS NULL"
        ).fetchall()
        seen: set[tuple] = set()
        results = []
        for r in rows:
            result = self._check_subsumption_row(r["id"])
            if result.status == "conflict":
                pair = (min(result.row_a_id, result.row_b_id), max(result.row_a_id, result.row_b_id))
                if pair not in seen:
                    seen.add(pair)
                    results.append(result)
        return results

    def _scan_predicate_distribution(self) -> list[ConsistencyResult]:
        rows = self._db.execute(
            "SELECT id FROM predicate_distribution WHERE retracted_at IS NULL"
        ).fetchall()
        seen: set[tuple] = set()
        results = []
        for r in rows:
            result = self._check_distribution_row(r["id"])
            if result.status == "conflict":
                pair = (min(result.row_a_id, result.row_b_id), max(result.row_a_id, result.row_b_id))
                if pair not in seen:
                    seen.add(pair)
                    results.append(result)
        return results

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    def _question_signature(self, conflict: ConsistencyResult) -> str:
        details_str = ":".join(f"{k}={v}" for k, v in sorted(conflict.details.items()))
        return f"{conflict.table}:{conflict.inconsistency_class}:{details_str}"

    def _increment_circuit_breaker(self, signature: str, now: str) -> None:
        existing = self._db.execute(
            "SELECT id, cycle_count FROM consistency_circuit_breaker WHERE question_signature=?",
            (signature,),
        ).fetchone()

        if existing is None:
            self._db.execute(
                "INSERT INTO consistency_circuit_breaker "
                "(question_signature, cycle_count, last_triggered_at, unresolvable) VALUES (?,?,?,?)",
                (signature, 1, now, 0),
            )
            self._db.commit()
        else:
            new_count = existing["cycle_count"] + 1
            unresolvable = 1 if new_count >= self._threshold else 0
            unresolvable_reason = "repeated_conflict" if unresolvable else None
            self._db.execute(
                "UPDATE consistency_circuit_breaker "
                "SET cycle_count=?, last_triggered_at=?, unresolvable=?, unresolvable_reason=? "
                "WHERE question_signature=?",
                (new_count, now, unresolvable, unresolvable_reason, signature),
            )
            self._db.commit()

            if unresolvable and self._audit:
                self._audit.log(
                    event_type="circuit_breaker_triggered",
                    event_subject=signature,
                    event_data=json.dumps({"cycle_count": new_count, "threshold": self._threshold}),
                )

    def is_unresolvable(self, conflict: ConsistencyResult) -> bool:
        """Return True if the circuit breaker has fired for this conflict's question."""
        sig = self._question_signature(conflict)
        row = self._db.execute(
            "SELECT unresolvable FROM consistency_circuit_breaker WHERE question_signature=?",
            (sig,),
        ).fetchone()
        return bool(row and row["unresolvable"])
