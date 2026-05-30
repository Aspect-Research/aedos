"""Shared `.env` loader (F3 §6 / F-013).

A small, explicit, opt-in helper. Callers that want `.env` loading
invoke `load_dotenv_if_present()`; callers that don't (tests by
default) ignore it. The utility avoids the test-coupling risk of
loading `.env` from `conftest.py` while making it easy for every
deployment / runner entry point to load `.env` explicitly.

Idempotent per operator's Q3 refinement: multiple entry points may
invoke during a single process; calling twice is a no-op. python-
dotenv's `load_dotenv()` itself does not override existing env vars
by default, so the wrapper's idempotency check just guards against
the file-read overhead.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union


# Module-level idempotency state. Set after the first successful load
# so subsequent calls are no-ops at the file-read level. python-dotenv's
# load_dotenv is itself non-overriding by default, so a repeated call
# would be safe — this guard just makes it explicitly cheap.
_loaded: bool = False


def _find_env_in_cwd_or_parents(start: Path) -> Optional[Path]:
    """Look for `.env` in `start` or any parent up to the filesystem
    root. Returns the first match's absolute path, or None."""
    current = start.resolve()
    for parent in (current, *current.parents):
        candidate = parent / ".env"
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def load_dotenv_if_present(
    path: Union[str, Path, None] = None,
    *,
    override: bool = False,
) -> bool:
    """Load environment variables from `.env` if it exists.

    `path` — explicit `.env` location. If None, search CWD and parents.
    `override` — by default, existing process env vars take precedence
    over `.env` values (the python-dotenv convention; matches operator
    expectation that explicit shell `export` overrides `.env`). Pass
    `override=True` only when the caller specifically wants the
    `.env` values to win.

    Returns True if `.env` was loaded (whether for the first time or as
    a repeat no-op), False if no file was found or `python-dotenv` is
    not installed.

    Idempotent: subsequent calls in the same process are no-ops at the
    file-read level after the first successful load. Safe to call from
    multiple entry points without duplicate side effects.
    """
    global _loaded

    if _loaded and path is None:
        # Already loaded once in this process via the default path
        # search; don't repeat the work.
        return True

    try:
        from dotenv import load_dotenv
    except ImportError:
        return False

    if path is None:
        env_path = _find_env_in_cwd_or_parents(Path.cwd())
    else:
        env_path = Path(path)
        if not env_path.exists():
            return False

    if env_path is None:
        return False

    load_dotenv(env_path, override=override)
    _loaded = True
    return True


def _reset_for_tests() -> None:
    """Reset the module-level idempotency flag. Tests-only helper."""
    global _loaded
    _loaded = False
