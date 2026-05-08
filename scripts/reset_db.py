"""Wipe the Aedos SQLite file clean.

Usage:
    python scripts/reset_db.py [path]            # v0.14 default (aedos.db)
    python scripts/reset_db.py --legacy [path]   # v0.13 legacy (aedos_v1.db)

The default mode resets the v0.14 store using ``src.fact_store``
(post-cutover top-level). The ``--legacy`` flag resets the v0.13
store using ``src.legacy.fact_store`` for operators on the rollback
path during the v0.14.x minor-version line.
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


def _default_path(legacy: bool) -> str:
    """v0.14 default path is aedos.db; legacy default is aedos_v1.db
    (the cutover renamed the working-copy v0.13 db to this name).
    Override either with AEDOS_DB_PATH."""
    env = os.getenv("AEDOS_DB_PATH")
    if env:
        return env
    return "aedos_v1.db" if legacy else "aedos.db"


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    legacy = False
    if args and args[0] == "--legacy":
        legacy = True
        args.pop(0)
    path = args[0] if args else _default_path(legacy)
    p = Path(path)
    if p.exists():
        p.unlink()
        print(f"deleted {p}")
    else:
        print(f"{p} does not exist; nothing to do")

    if legacy:
        from src.legacy.fact_store import FactStore
    else:
        from src.fact_store import FactStore

    store = FactStore(path)
    store.close()
    label = "v0.13 (legacy)" if legacy else "v0.14"
    print(f"{label} schema recreated at {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
