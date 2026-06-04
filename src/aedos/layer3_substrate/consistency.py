from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..audit.log import log_event

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


def _parse_types(raw) -> list:
    """Parse a stored *_entity_types JSON column into a Q-id list; [] on
    None/empty/malformed."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return val if isinstance(val, list) else []


def _role_types(sq_raw, sub_types, obj_types):
    """Map a row's (subject_types, object_types) onto the KB statement roles per
    its slot_to_qualifier direction. Returns
    (statement_subject_types, statement_value_types), or None when the map is
    qualifier-keyed / uninterpretable. Standard → (subject, object); inverse →
    (object, subject) (the subject slot lands on statement_value)."""
    if sq_raw is None:
        sq = None
    elif isinstance(sq_raw, str):
        try:
            sq = json.loads(sq_raw)
        except (json.JSONDecodeError, TypeError):
            return None
    else:
        sq = sq_raw
    if not sq:
        return (sub_types, obj_types)  # absent map == standard
    if not isinstance(sq, dict):
        return None
    subj, obj = sq.get("subject"), sq.get("object")
    if subj in (None, "statement_subject") and obj in (None, "statement_value"):
        return (sub_types, obj_types)
    if subj == "statement_value" and obj == "statement_subject":
        return (obj_types, sub_types)
    return None  # qualifier-keyed / uninterpretable


def _overlap(a, b) -> bool:
    """True if two Q-id lists share at least one element."""
    if not a or not b:
        return False
    return bool(set(a) & set(b))


class ConsistencyChecker:
    def __init__(
        self,
        db,
        retraction_propagator=None,
        config: Optional[dict] = None,
        circuit_breaker_threshold: Optional[int] = None,
    ) -> None:
        """Circuit-breaker threshold resolves in priority order:

          1. Explicit `circuit_breaker_threshold` kwarg — used by
             `build_pipeline` to thread `Config.circuit_breaker_threshold`.
          2. Legacy `config={"circuit_breaker_threshold": N}` dict —
             preserved for back-compat with tests.
          3. Architecture default (`_DEFAULT_CIRCUIT_BREAKER_THRESHOLD`).
        """
        self._db = db
        self._retraction_propagator = retraction_propagator
        cfg = config or {}
        self._threshold = (
            circuit_breaker_threshold
            if circuit_breaker_threshold is not None
            else cfg.get("circuit_breaker_threshold", _DEFAULT_CIRCUIT_BREAKER_THRESHOLD)
        )

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

        # v0.16.3 Batch B (piece 2): a PINNED row is operator-authoritative and is
        # never retracted. If EITHER side of the conflict is pinned, skip the whole
        # resolution (retract neither) — retracting only the un-pinned peer would
        # leave an asymmetric state where a pinned row's legitimate counterpart is
        # silently dropped. Do NOT bump the circuit breaker on a pin-skip (it is not
        # an escalating substrate fault — it is a deliberate operator override); log
        # it so the mismatch is still visible. Piece 3's coherence-aware
        # _is_inverse_mapping prevents legitimate inverse pairs from being flagged
        # here in the first place; this is the durable backstop.
        if self._conflict_touches_pinned(conflict):
            log_event(
                self._db,
                event_type="pin_conflict_skipped",
                event_subject=self._question_signature(conflict),
                event_data={
                    "table": conflict.table,
                    "inconsistency_class": conflict.inconsistency_class,
                    "row_a_id": conflict.row_a_id,
                    "row_b_id": conflict.row_b_id,
                },
            )
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

        log_event(
            self._db,
            event_type="consistency_violation",
            event_subject=sig,
            event_data={
                "table": conflict.table,
                "inconsistency_class": conflict.inconsistency_class,
                "row_a_id": conflict.row_a_id,
                "row_b_id": conflict.row_b_id,
            },
        )

    def _conflict_touches_pinned(self, conflict: ConsistencyResult) -> bool:
        """True if either conflicting row is a pinned predicate_translation row.
        Only predicate_translation carries the `pinned` column; for any other
        table (or a pre-migration DB without the column) this returns False so
        behavior is unchanged."""
        if conflict.table != "predicate_translation":
            return False
        ids = [r for r in (conflict.row_a_id, conflict.row_b_id) if r is not None]
        if not ids:
            return False
        placeholders = ",".join("?" for _ in ids)
        try:
            row = self._db.execute(
                f"SELECT 1 FROM predicate_translation "
                f"WHERE id IN ({placeholders}) AND pinned=1 LIMIT 1",
                ids,
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        return row is not None

    # ------------------------------------------------------------------
    # Check per table
    # ------------------------------------------------------------------

    def _check_predicate_translation_row(self, row_id: int) -> ConsistencyResult:
        row = self._db.execute(
            "SELECT aedos_predicate, kb_namespace, kb_property, slot_to_qualifier, "
            "subject_entity_types, object_entity_types "
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
            "SELECT id, aedos_predicate, slot_to_qualifier, "
            "subject_entity_types, object_entity_types FROM predicate_translation "
            "WHERE id != ? AND kb_namespace=? AND kb_property=? AND retracted_at IS NULL",
            (row_id, ns, prop),
        ).fetchall()

        for c in conflicts:
            if c["slot_to_qualifier"] != sq:
                # N5 + v0.16.3 Batch B (piece 3): inverse predicates (capital_of /
                # has_capital) legitimately map to the same KB property with
                # subject/object swapped. The PURELY STRUCTURAL swap test waved
                # through ANY swap — including an INCOHERENT one (the bare `capital`
                # bug: subject typed country but an inverse map that sends the
                # country to the city-typed statement_value). Now: a structural
                # swap is exempt ONLY when it is COHERENT — the entity-type that
                # each row lands on the KB statement_subject role agrees across the
                # pair (and matches the property's P2302 subject/value constraint
                # when the ontology is available). An INCOHERENT swap is a real
                # conflict and falls through. When type information is missing on
                # either row, coherence cannot be assessed → fall open to the prior
                # structural skip (conservative: never invent a conflict from
                # absent data — the untyped capital_of/has_capital seeds stay
                # exempt exactly as before).
                if _is_inverse_mapping(sq, c["slot_to_qualifier"]):
                    coherent = self._inverse_pair_coherence(prop, ns, row, c)
                    if coherent is not False:
                        # True (coherent) or None (indeterminate) → exempt, as before.
                        continue
                    # coherent is False → an incoherent swap → fall through to conflict.
                # Skip conflicts where either
                # row's slot_to_qualifier is NULL. A NULL sq on a kb-mapped
                # predicate is a malformed runtime-oracle entry; treating it
                # as a peer to a properly-formed sq map causes the
                # transitive_equivalence_violation rule to retract BOTH rows,
                # including the well-formed seed/peer. The malformed row's
                # absence of sq means it can't be used for KB lookups anyway;
                # let it persist harmlessly rather than poison its peers.
                if sq is None or c["slot_to_qualifier"] is None:
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

    def _inverse_pair_coherence(self, prop, ns, row_a, row_b) -> Optional[bool]:
        """v0.16.3 Batch B (piece 3): is a same-property, swapped-slot_to_qualifier
        pair an INTERNALLY COHERENT inverse?

        Returns True (coherent — exempt), False (incoherent — a real conflict), or
        None (indeterminate — fall open to the prior structural exemption).

        SOUNDNESS (adversarial-review fix): a conflict is declared ONLY on POSITIVE
        cross-role CONTRADICTION — a type that row A places on the KB
        statement_subject role is one that row B places on the statement_VALUE role
        (or vice versa). That is a genuine disagreement about which entity-type is
        the statement subject. Mere NON-OVERLAP of the two rows' role-types is NOT a
        conflict: two correct inverse predicates routinely type the same role with
        equivalent-but-distinct QIDs (country Q6256 vs sovereign-state Q3624078), and
        a stored type may be a SUBCLASS of the other row's / the property's
        constraint class. Treating non-overlap as incoherence manufactured a §3.2
        false-conflict that retracted BOTH legitimate rows — so we never do it.

        The bare-`capital` bug: capital (inverse) places country on
        statement_VALUE and city on statement_SUBJECT; its standard peer has_capital
        places country on statement_subject and city on statement_value. So
        capital's statement_subject types (city) OVERLAP has_capital's
        statement_value types (city) → positive contradiction → incoherent. The
        legitimate capital_of/has_capital pair places country on statement_subject
        in BOTH and city on statement_value in BOTH → no cross-role overlap →
        coherent (exempt), even with differing country/city QIDs."""
        a_sub = _parse_types(row_a["subject_entity_types"])
        a_obj = _parse_types(row_a["object_entity_types"])
        b_sub = _parse_types(row_b["subject_entity_types"])
        b_obj = _parse_types(row_b["object_entity_types"])
        # Need types on BOTH rows to assess coherence; otherwise indeterminate.
        if not (a_sub or a_obj) or not (b_sub or b_obj):
            return None

        a_roles = _role_types(row_a["slot_to_qualifier"], a_sub, a_obj)
        b_roles = _role_types(row_b["slot_to_qualifier"], b_sub, b_obj)
        if a_roles is None or b_roles is None:
            return None
        a_ss, a_sv = a_roles
        b_ss, b_sv = b_roles

        # POSITIVE, ROLE-DISCRIMINATING cross-role contradiction only: a type that A
        # places on statement_subject, B places on statement_value AND NOT also on
        # statement_subject (so the type genuinely discriminates the two roles).
        # The bare-`capital` bug: city is on capital's statement_subject and on
        # has_capital's statement_value but NOT has_capital's statement_subject →
        # incoherent. A SYMMETRIC property (spouse/sibling: human on every role in
        # both rows) overlaps cross-role too, but the type is on BOTH roles → not
        # discriminating → exempt (a symmetric predicate's direction is agnostic, so
        # a swapped pair is legitimate). Non-overlap alone is never a conflict
        # (equivalent-but-distinct vocabularies / subclasses).
        ss_contradiction = _overlap(a_ss, b_sv) and not _overlap(a_ss, b_ss)
        sv_contradiction = _overlap(a_sv, b_ss) and not _overlap(a_sv, b_sv)
        if ss_contradiction or sv_contradiction:
            return False
        return True

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

            if unresolvable:
                log_event(
                    self._db,
                    event_type="circuit_breaker_triggered",
                    event_subject=signature,
                    event_data={"cycle_count": new_count, "threshold": self._threshold},
                )

    def is_unresolvable(self, conflict: ConsistencyResult) -> bool:
        """Return True if the circuit breaker has fired for this conflict's question."""
        sig = self._question_signature(conflict)
        row = self._db.execute(
            "SELECT unresolvable FROM consistency_circuit_breaker WHERE question_signature=?",
            (sig,),
        ).fetchone()
        return bool(row and row["unresolvable"])
