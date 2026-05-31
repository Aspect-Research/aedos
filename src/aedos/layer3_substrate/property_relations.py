"""v0.16 WS1 — Wikidata property-ontology cache (PropertyRelations).

`PropertyRelations.fetch(prop)` returns the structured P2302 constraint set +
P1647/P1696/P1659 relations for a Wikidata property, used by the substrate to
BUILD `PredicateBinding`s (constrained subject/value types, single-value flag)
and to discover sibling/inverse properties.

Cache-then-generate: a fresh (`ttl_days`) set of `property_relations` rows is
returned directly; otherwise the KB is queried (via `kb_protocol`), the result
is cached into `property_relations`, and returned.

FAIL-OPEN throughout: any DB or KB error — and the common case of a property
with no recorded constraints — returns an EMPTY `PropertyOntology`. Discovery
is additive enrichment; an empty ontology falls the caller back to the
oracle's primary binding (current behavior). Nothing here raises.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..audit.log import log_event

_NOW = lambda: datetime.now(timezone.utc).isoformat()

# How the structured ontology fields map onto the `property_relations`
# table's `relation_type` column. The table stores one row per (relation_type,
# related_value); `single_value` is stored as a single marker row with a NULL
# related_value (its presence flags the property functional).
_RELATION_SUBJECT_TYPE = "subject_type_constraint"
_RELATION_VALUE_TYPE = "value_type_constraint"
_RELATION_INVERSE = "inverse"
_RELATION_SUBPROPERTY = "subproperty"
_RELATION_RELATED = "related"
_RELATION_SINGLE_VALUE = "single_value"

# Maps each list-valued ontology field to its (relation_type, source) pair.
_LIST_FIELD_RELATIONS = (
    ("subject_type_qids", _RELATION_SUBJECT_TYPE, "wikidata_p2302"),
    ("value_type_qids", _RELATION_VALUE_TYPE, "wikidata_p2302"),
    ("inverse_pids", _RELATION_INVERSE, "wikidata_p1696"),
    ("subproperty_pids", _RELATION_SUBPROPERTY, "wikidata_p1647"),
    ("related_pids", _RELATION_RELATED, "wikidata_p1659"),
)


@dataclass
class PropertyOntology:
    """Structured P2302 constraints + sibling/inverse relations for one KB
    property. Empty (all-empty lists, single_valued=False) means 'the ontology
    cannot constrain this property' — the discovery fall-open case."""

    subject_type_qids: list[str] = field(default_factory=list)
    value_type_qids: list[str] = field(default_factory=list)
    inverse_pids: list[str] = field(default_factory=list)
    subproperty_pids: list[str] = field(default_factory=list)
    related_pids: list[str] = field(default_factory=list)
    single_valued: bool = False

    def is_empty(self) -> bool:
        return not (
            self.subject_type_qids
            or self.value_type_qids
            or self.inverse_pids
            or self.subproperty_pids
            or self.related_pids
            or self.single_valued
        )


class PropertyRelations:
    """Cache-then-generate accessor for a Wikidata property's ontology.

    Reads/writes the `property_relations` table (created by WS8 in
    `database.py`). `kb_protocol` must expose `fetch_property_ontology(prop)`
    (the v0.16 WS1 KBProtocol addition); it is consulted only on a cache miss
    or stale rows, and is called via `getattr` so a stub adapter without the
    method simply yields an empty ontology.
    """

    def __init__(
        self,
        db: sqlite3.Connection,
        kb_protocol,
        *,
        ttl_days: int = 30,
    ) -> None:
        self._db = db
        self._kb = kb_protocol
        self._ttl_days = ttl_days

    def fetch(
        self, kb_property: str, kb_namespace: str = "wikidata"
    ) -> PropertyOntology:
        """Return the cached ontology if fresh; else query the KB, cache, and
        return. FAIL-OPEN: any error returns an empty `PropertyOntology`."""
        if not kb_property:
            return PropertyOntology()
        try:
            cached = self._read_cache(kb_property, kb_namespace)
        except Exception:
            cached = None
        if cached is not None:
            return cached

        ontology_dict = self._query_kb(kb_property)
        ontology = _dict_to_ontology(ontology_dict)
        # Cache even an empty result so a long-tail property the ontology can't
        # constrain isn't re-queried on every consult within the TTL window.
        try:
            self._write_cache(kb_property, kb_namespace, ontology)
        except Exception:
            pass  # caching is best-effort; never let it break discovery
        return ontology

    # ------------------------------------------------------------------
    # KB query (fail-open)
    # ------------------------------------------------------------------

    def _query_kb(self, kb_property: str) -> dict:
        fetch = getattr(self._kb, "fetch_property_ontology", None)
        if fetch is None:
            return {}
        try:
            result = fetch(kb_property)
        except Exception:
            return {}
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------
    # Cache read / write
    # ------------------------------------------------------------------

    def _read_cache(
        self, kb_property: str, kb_namespace: str
    ) -> Optional[PropertyOntology]:
        """Return the cached ontology if non-retracted, non-stale rows exist;
        else None (signaling 'query the KB'). A property that genuinely has no
        constraints is cached as a single sentinel row (relation_type='empty'),
        so its presence distinguishes 'known-empty' from 'never fetched'."""
        rows = self._db.execute(
            "SELECT relation_type, related_value, created_at "
            "FROM property_relations "
            "WHERE kb_namespace=? AND kb_property=? AND retracted_at IS NULL",
            (kb_namespace, kb_property),
        ).fetchall()
        if not rows:
            return None
        # Freshness: if the newest row is older than the TTL, treat as a miss
        # and re-query (the KB ontology may have changed).
        if self._stale(rows):
            return None
        ontology = PropertyOntology()
        for relation_type, related_value, _created in rows:
            if relation_type == _RELATION_SINGLE_VALUE:
                ontology.single_valued = True
            elif relation_type == "empty":
                continue  # sentinel: known-empty, no fields to populate
            else:
                bucket = _bucket_for_relation(ontology, relation_type)
                if bucket is not None and related_value and related_value not in bucket:
                    bucket.append(related_value)
        self._touch(kb_namespace, kb_property)
        return ontology

    def _stale(self, rows) -> bool:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._ttl_days)
        newest: Optional[datetime] = None
        for _rt, _rv, created_at in rows:
            parsed = _parse_iso(created_at)
            if parsed is not None and (newest is None or parsed > newest):
                newest = parsed
        if newest is None:
            return True
        return newest < cutoff

    def _write_cache(
        self, kb_property: str, kb_namespace: str, ontology: PropertyOntology
    ) -> None:
        now = _NOW()
        # Replace any prior rows for this property so a re-fetch reflects the
        # current ontology (idempotent; UNIQUE constraint also guards dupes).
        self._db.execute(
            "DELETE FROM property_relations WHERE kb_namespace=? AND kb_property=?",
            (kb_namespace, kb_property),
        )
        rows: list[tuple] = []
        for attr, relation_type, source in _LIST_FIELD_RELATIONS:
            for value in getattr(ontology, attr):
                rows.append((relation_type, value, source))
        if ontology.single_valued:
            rows.append((_RELATION_SINGLE_VALUE, None, "wikidata_p2302"))
        if not rows:
            # Sentinel marks the property as known-empty so we don't re-query
            # within the TTL window (avoids hammering a long-tail property).
            rows.append(("empty", None, "wikidata_p2302"))
        for relation_type, related_value, source in rows:
            self._db.execute(
                "INSERT OR IGNORE INTO property_relations "
                "(kb_namespace, kb_property, relation_type, related_value, "
                " source, created_at, used_count) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (kb_namespace, kb_property, relation_type, related_value, source, now),
            )
        self._db.commit()
        self._log(
            "kb_property_relations_cached",
            kb_property,
            {
                "kb_namespace": kb_namespace,
                "subject_type_qids": ontology.subject_type_qids,
                "value_type_qids": ontology.value_type_qids,
                "inverse_pids": ontology.inverse_pids,
                "subproperty_pids": ontology.subproperty_pids,
                "related_pids": ontology.related_pids,
                "single_valued": ontology.single_valued,
            },
        )

    def _touch(self, kb_namespace: str, kb_property: str) -> None:
        try:
            self._db.execute(
                "UPDATE property_relations "
                "SET last_consulted_at=?, used_count=used_count+1 "
                "WHERE kb_namespace=? AND kb_property=? AND retracted_at IS NULL",
                (_NOW(), kb_namespace, kb_property),
            )
            self._db.commit()
        except Exception:
            pass

    def _log(self, event_type: str, event_subject: str, event_data: dict) -> None:
        try:
            log_event(
                self._db,
                event_type=event_type,
                event_subject=event_subject,
                event_data=event_data,
            )
        except Exception:
            pass


# ----------------------------------------------------------------------
# Module helpers
# ----------------------------------------------------------------------


def _dict_to_ontology(d: dict) -> PropertyOntology:
    """Build a `PropertyOntology` from the dict shape the KB adapter returns,
    defensively (missing/wrong-typed keys default to empty)."""
    if not isinstance(d, dict):
        return PropertyOntology()

    def _list(key: str) -> list[str]:
        v = d.get(key)
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []

    return PropertyOntology(
        subject_type_qids=_list("subject_type_qids"),
        value_type_qids=_list("value_type_qids"),
        inverse_pids=_list("inverse_pids"),
        subproperty_pids=_list("subproperty_pids"),
        related_pids=_list("related_pids"),
        single_valued=bool(d.get("single_valued", False)),
    )


def _bucket_for_relation(
    ontology: PropertyOntology, relation_type: str
) -> Optional[list]:
    return {
        _RELATION_SUBJECT_TYPE: ontology.subject_type_qids,
        _RELATION_VALUE_TYPE: ontology.value_type_qids,
        _RELATION_INVERSE: ontology.inverse_pids,
        _RELATION_SUBPROPERTY: ontology.subproperty_pids,
        _RELATION_RELATED: ontology.related_pids,
    }.get(relation_type)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
