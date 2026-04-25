"""Wipe the Aedos SQLite file clean.

Usage:
    python scripts/reset_db.py [path]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

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

    # Recreate schema so the next `python -m src.app` starts clean.
    from src.fact_store import FactStore

    store = FactStore(path)
    store.close()
    print(f"schema recreated at {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
