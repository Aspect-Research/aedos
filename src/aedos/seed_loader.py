"""Seed-pack loader for Aedos v0.15.

The seed pack at `seeds/predicate_translation.json` carries hand-curated
predicate metadata (routing_hint, kb_property, slot_to_qualifier,
cardinality, entity-type filters, rationale) that production deployments
expect to consult when the extractor produces a known predicate. Phase H
Cluster 3 introduces auto-loading at DB-open time so the in-vocabulary
verification path uses these seeded rows instead of cold-start LLM
consultations.

This module exposes:

  - DEFAULT_SEED_FILE          Path to the canonical seed JSON.
  - load_seeds_into_connection Load every entry into an open sqlite3
                               connection; idempotent via INSERT OR
                               REPLACE; returns the count of rows
                               processed.

Idempotency: re-running against an already-seeded DB replaces each row
matching (aedos_predicate, kb_namespace) with the fresh JSON value. The
caller controls *whether* to load (Phase H Cluster 3:
`create_schema(load_seeds=True)` loads only when the table is empty, so
operator modifications and retractions persist across re-opens).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SEED_FILE: Path = (
    Path(__file__).resolve().parents[2] / "seeds" / "predicate_translation.json"
)

_REQUIRED_FIELDS = {
    "aedos_predicate",
    "object_type",
    "user_subject_required",
    "routing_hint",
    "kb_namespace",
    "kb_property",
    "slot_to_qualifier",
    "single_valued",
    "reason",
}

_VALID_ROUTING_HINTS = {"user_authoritative", "kb_resolvable", "python", "abstain"}


def _validate_entry(entry: dict, idx: int) -> None:
    missing = _REQUIRED_FIELDS - entry.keys()
    if missing:
        raise ValueError(f"Entry {idx}: missing fields {missing}")
    if entry["routing_hint"] not in _VALID_ROUTING_HINTS:
        raise ValueError(
            f"Entry {idx}: invalid routing_hint {entry['routing_hint']!r}"
        )
    if not entry.get("aedos_predicate"):
        raise ValueError(f"Entry {idx}: empty aedos_predicate")


def load_seeds_into_connection(
    conn: sqlite3.Connection,
    seed_file: Path | str | None = None,
) -> int:
    """Load every seed entry into `conn`. Idempotent; commits inside.

    Returns the number of entries processed. The caller is responsible
    for deciding whether to load (e.g., gated on table emptiness).
    """
    path = Path(seed_file) if seed_file is not None else DEFAULT_SEED_FILE
    seeds_data = json.loads(path.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).isoformat()
    loaded = 0
    for idx, entry in enumerate(seeds_data):
        _validate_entry(entry, idx)
        slot_json = (
            json.dumps(entry["slot_to_qualifier"])
            if entry["slot_to_qualifier"] is not None
            else None
        )
        subject_types = entry.get("subject_entity_types")
        object_types = entry.get("object_entity_types")
        subject_types_json = (
            json.dumps(subject_types) if subject_types else None
        )
        object_types_json = (
            json.dumps(object_types) if object_types else None
        )
        kb_namespace = entry.get("kb_namespace")
        # SQLite NULL != NULL in UNIQUE checks, so INSERT OR REPLACE
        # won't deduplicate rows where kb_namespace IS NULL. Delete
        # first instead.
        if kb_namespace is None:
            conn.execute(
                "DELETE FROM predicate_translation "
                "WHERE aedos_predicate = ? AND kb_namespace IS NULL",
                (entry["aedos_predicate"],),
            )
        conn.execute(
            """
            INSERT OR REPLACE INTO predicate_translation
                (aedos_predicate, object_type, user_subject_required, distinct_slots,
                 routing_hint, kb_namespace, kb_property, slot_to_qualifier,
                 single_valued, subject_entity_types, object_entity_types,
                 reason, created_at, used_count, last_consulted_at, retracted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL)
            """,
            (
                entry["aedos_predicate"],
                entry["object_type"],
                int(entry["user_subject_required"]),
                entry.get("distinct_slots"),
                entry["routing_hint"],
                kb_namespace,
                entry.get("kb_property"),
                slot_json,
                int(entry["single_valued"]),
                subject_types_json,
                object_types_json,
                entry.get("reason", ""),
                now,
            ),
        )
        loaded += 1
    conn.commit()
    return loaded
