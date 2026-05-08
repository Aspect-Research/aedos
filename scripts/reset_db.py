"""Wipe the Aedos SQLite file clean and recreate the schema.

Usage:
    python scripts/reset_db.py [path]

Defaults to ``aedos.db`` (override via ``AEDOS_DB_PATH``). Drops the
file if it exists, then constructs a fresh ``FactStore`` so the
v0.14 schema lands clean.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make `src.*` importable when invoked as ``python scripts/reset_db.py``
# without first installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else os.getenv("AEDOS_DB_PATH", "aedos.db")
    p = Path(path)
    if p.exists():
        p.unlink()
        print(f"deleted {p}")
    else:
        print(f"{p} does not exist; nothing to do")

    from src.fact_store import FactStore

    store = FactStore(path)
    store.close()
    print(f"v0.14 schema recreated at {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
