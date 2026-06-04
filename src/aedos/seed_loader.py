"""Seed-pack loader for Aedos.

The seed pack at `seeds/predicate_translation.json` carries hand-curated
predicate metadata (routing_hint, kb_property, slot_to_qualifier,
cardinality, entity-type filters, rationale) that production deployments
expect to consult when the extractor produces a known predicate.
Auto-loading at DB-open time lets the in-vocabulary
verification path use these seeded rows instead of cold-start LLM
consultations.

This module exposes:

  - DEFAULT_SEED_FILE          Path to the canonical seed JSON.
  - load_seeds_into_connection Load every entry into an open sqlite3
                               connection; idempotent via INSERT OR
                               REPLACE; returns the count of rows
                               processed.

Idempotency: re-running against an already-seeded DB replaces each row
matching (aedos_predicate, kb_namespace) with the fresh JSON value. The
caller controls *whether* to load
(`create_schema(load_seeds=True)` loads only when the table is empty, so
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

# v0.16 WS6 T1: `kb_interval` is the interval-endpoint routing hint (parallels
# the runtime-only `kb_quantitative` hint). A `*_started` / `*_ended` predicate
# grounds against the P580 (start time) / P582 (end time) qualifier on its base
# relation's KB statement, via the walker's interval resolver — not the generic
# value-compare path. Admitted here so the new seed rows load.
_VALID_ROUTING_HINTS = {
    "user_authoritative", "kb_resolvable", "python", "abstain", "kb_interval",
}


def _synthesize_bindings_json(entry: dict) -> str | None:
    """v0.16.1 WS2: when a seed entry declares `candidate_kb_properties`, build
    the multi-property `bindings` JSON the substrate's binding loop arbitrates
    over (the scalar columns alone read-synthesize ONLY the single primary
    binding). Returns None when the entry has no candidates, so every other seed
    row keeps `bindings IS NULL` and read-synthesizes one legacy_scalar binding
    from its scalar columns exactly as before.

    bindings[0] is ALWAYS the primary property (mirroring the scalar columns).
    Each candidate property becomes an EXTRA binding marked
    `value_type_gated=True` and `single_valued=False`: its POSITIVE grounding is
    fail-closed type-gated in the verifier (a confirmed occupation/profession
    object only) and it can never CONTRADICT. The candidate's value-type
    constraint comes from `candidate_object_entity_types[<pid>]` in the seed —
    knowledge stays in the seed, not Python. Example (the copula fix):
    instance_of seeds candidate_kb_properties=["P106"] with
    candidate_object_entity_types={"P106": ["Q12737077","Q28640"]} →
    bindings = [P31 (primary), P106 (gated occupation)]."""
    candidates = entry.get("candidate_kb_properties")
    if not candidates or not isinstance(candidates, list):
        return None
    primary_prop = entry.get("kb_property")
    if not primary_prop:
        return None
    kb_namespace = entry.get("kb_namespace")
    slot_to_qualifier = entry.get("slot_to_qualifier")
    subject_types = entry.get("subject_entity_types") or None
    object_types = entry.get("object_entity_types") or None
    candidate_obj_types = entry.get("candidate_object_entity_types") or {}

    bindings: list[dict] = [
        {
            "kb_namespace": kb_namespace,
            "kb_property": primary_prop,
            "slot_to_qualifier": slot_to_qualifier,
            "single_valued": bool(int(entry.get("single_valued", 0) or 0)),
            "subject_entity_types": subject_types,
            "object_entity_types": object_types,
            "source": "legacy_scalar",
            "rank": 1.0,
            "value_type_gated": False,
        }
    ]
    seen = {primary_prop}
    for pid in candidates:
        if not isinstance(pid, str) or not pid or pid in seen:
            continue
        seen.add(pid)
        bindings.append(
            {
                "kb_namespace": kb_namespace,
                "kb_property": pid,
                # Candidate properties share the standard subject->statement
                # direction; reuse the row's slot map.
                "slot_to_qualifier": slot_to_qualifier,
                # Never license a CONTRADICTION from a candidate (a wrong
                # occupation must abstain, never contradict).
                "single_valued": False,
                "subject_entity_types": subject_types,
                # The value-type constraint that fail-closed-gates the positive
                # path: only an object subsumed by one of these classes verifies.
                "object_entity_types": candidate_obj_types.get(pid) or None,
                "source": "candidate",
                "rank": 0.4,
                "value_type_gated": True,
            }
        )
    return json.dumps(bindings)


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
        # v0.16.1 WS2: synthesize the multi-property `bindings` column when the
        # seed declares `candidate_kb_properties` (else NULL → read-synthesize
        # the single legacy_scalar binding, unchanged).
        bindings_json = _synthesize_bindings_json(entry)
        # v0.16.1 WS3b: premise -> Python channel. A seed may declare
        # `premise_properties` (slot -> KB property to fetch as a premise) for a
        # routing_hint='python' comparison predicate; NULL/omit preserves the
        # no-fetch default.
        premise_properties = entry.get("premise_properties") or None
        premise_properties_json = (
            json.dumps(premise_properties) if premise_properties else None
        )
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
                 reason, created_at, used_count, last_consulted_at, retracted_at,
                 bindings, premise_properties)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?)
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
                bindings_json,
                premise_properties_json,
            ),
        )
        loaded += 1
    conn.commit()
    return loaded
