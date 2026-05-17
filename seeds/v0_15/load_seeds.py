"""
Load predicate translation seeds into a v0.15 database.

Usage:
    python seeds/v0_15/load_seeds.py [--db-path PATH]

Defaults to $AEDOS_DB_PATH or aedos_v0_15.db in the current directory.
The load is idempotent: existing rows with the same aedos_predicate are
replaced (INSERT OR REPLACE), preserving row semantics.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_SEEDS_FILE = Path(__file__).parent / "predicate_translation.json"
_SEED_VERSION_FILE = Path(__file__).parent / "SEED_VERSION.txt"

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


def load_seeds(db_path: str) -> int:
    seeds_data = json.loads(_SEEDS_FILE.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    loaded = 0
    try:
        for idx, entry in enumerate(seeds_data):
            _validate_entry(entry, idx)
            slot_json = (
                json.dumps(entry["slot_to_qualifier"])
                if entry["slot_to_qualifier"] is not None
                else None
            )
            kb_namespace = entry.get("kb_namespace")
            # SQLite NULL != NULL in UNIQUE checks, so INSERT OR REPLACE won't
            # deduplicate rows where kb_namespace IS NULL. Delete first instead.
            if kb_namespace is None:
                conn.execute(
                    "DELETE FROM predicate_translation WHERE aedos_predicate = ? AND kb_namespace IS NULL",
                    (entry["aedos_predicate"],),
                )
            conn.execute(
                """
                INSERT OR REPLACE INTO predicate_translation
                    (aedos_predicate, object_type, user_subject_required, distinct_slots,
                     routing_hint, kb_namespace, kb_property, slot_to_qualifier,
                     single_valued, reason, created_at, used_count, last_consulted_at, retracted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL)
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
                    entry.get("reason", ""),
                    now,
                ),
            )
            loaded += 1
        conn.commit()
    finally:
        conn.close()
    return loaded


def main() -> None:
    parser = argparse.ArgumentParser(description="Load Aedos v0.15 predicate seeds")
    parser.add_argument(
        "--db-path",
        default=os.environ.get("AEDOS_DB_PATH", "aedos_v0_15.db"),
        help="Path to the SQLite database (default: $AEDOS_DB_PATH or aedos_v0_15.db)",
    )
    args = parser.parse_args()

    version_info = _SEED_VERSION_FILE.read_text(encoding="utf-8").strip()
    print(f"Seed version info:\n{version_info}\n")

    n = load_seeds(args.db_path)
    print(f"Loaded {n} predicate translation seeds into {args.db_path}")


if __name__ == "__main__":
    main()
