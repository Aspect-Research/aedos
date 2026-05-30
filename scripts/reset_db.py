"""Wipe the Aedos SQLite file clean and recreate the v0.15 schema.

Usage:
    python scripts/reset_db.py [path]

Defaults to ``aedos.db`` (override via ``AEDOS_DB_PATH``). Deletes the database
file (and any ``-journal`` / ``-wal`` / ``-shm`` sidecars) if present, then
opens a fresh connection so the v0.15 schema lands clean.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the `aedos` package importable when invoked as
# ``python scripts/reset_db.py`` without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv

load_dotenv()


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else os.getenv("AEDOS_DB_PATH", "aedos.db")
    p = Path(path)
    removed = False
    for f in (p, *(p.with_name(p.name + s) for s in ("-journal", "-wal", "-shm"))):
        if f.exists():
            f.unlink()
            print(f"deleted {f}")
            removed = True
    if not removed:
        print(f"{p} does not exist; nothing to delete")

    from aedos.database import open_db

    open_db(str(path)).close()
    print(f"v0.15 schema recreated at {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
