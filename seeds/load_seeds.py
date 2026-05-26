"""
Load predicate translation seeds into a v0.15 database.

Usage:
    python seeds/load_seeds.py [--db-path PATH]

Defaults to $AEDOS_DB_PATH or aedos.db in the current directory.
The load is idempotent: existing rows matching (aedos_predicate,
kb_namespace) are replaced (INSERT OR REPLACE), preserving row
semantics.

Phase H Cluster 3 (2026-05-26): the loading logic moved into
`src/aedos/seed_loader.py` so `database.create_schema(load_seeds=True)`
can auto-load at DB-open time. This script remains the CLI entry
point for explicit operator-initiated seed loads.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from aedos.seed_loader import load_seeds_into_connection  # noqa: E402

_SEED_VERSION_FILE = Path(__file__).parent / "SEED_VERSION.txt"


def load_seeds(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        n = load_seeds_into_connection(conn)
    finally:
        conn.close()
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description="Load Aedos v0.15 predicate seeds")
    parser.add_argument(
        "--db-path",
        default=os.environ.get("AEDOS_DB_PATH", "aedos.db"),
        help="Path to the SQLite database (default: $AEDOS_DB_PATH or aedos.db)",
    )
    args = parser.parse_args()

    version_info = _SEED_VERSION_FILE.read_text(encoding="utf-8").strip()
    print(f"Seed version info:\n{version_info}\n")

    n = load_seeds(args.db_path)
    print(f"Loaded {n} predicate translation seeds into {args.db_path}")


if __name__ == "__main__":
    main()
