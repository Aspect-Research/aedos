"""Per-session identity for the A+ deployment model.

A tester's stable, opaque `session_id` (a UUID generated + persisted client-side)
IS the Tier-U `asserting_party`. Isolation between sessions comes FREE from the
engine's existing Tier-U keying (`WHERE asserting_party=?`); this module only
validates the id and derives the party string. No Tier-U read/write path is
touched.
"""

from __future__ import annotations

import re

_SESSION_PARTY_PREFIX = "session:"
# Opaque, URL/UUID-safe ids only. Bounds length + charset (SQL is parameterized,
# so this is hygiene, not an injection guard).
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")


class InvalidSessionId(ValueError):
    """Raised when a caller-supplied session id is empty or malformed."""


def normalize_session_id(session_id: str | None, *, max_len: int = 128) -> str:
    sid = (session_id or "").strip()
    if not sid or len(sid) > max_len or not _SESSION_ID_RE.match(sid):
        raise InvalidSessionId(
            "session_id must be a non-empty opaque token of [A-Za-z0-9._-], "
            f"<= {max_len} chars"
        )
    return sid


def party_for_session(session_id: str | None, *, max_len: int = 128) -> str:
    """Derive the Tier-U asserting_party for a session. Namespaced so deployment
    sessions never collide with engine/benchmark parties (e.g. the default
    'user' or 'benchmark')."""
    return _SESSION_PARTY_PREFIX + normalize_session_id(session_id, max_len=max_len)
