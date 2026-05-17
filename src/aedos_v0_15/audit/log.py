"""Audit log writes and queries for Aedos v0.15."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(
    conn: sqlite3.Connection,
    event_type: str,
    event_subject: str,
    event_data: dict[str, Any],
    verification_context: Optional[str] = None,
) -> int:
    """Write an audit event. Returns the new row id."""
    cur = conn.execute(
        """
        INSERT INTO audit_log (event_type, event_subject, event_data, occurred_at, verification_context)
        VALUES (?, ?, ?, ?, ?)
        """,
        (event_type, event_subject, json.dumps(event_data), _now(), verification_context),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def query_events(
    conn: sqlite3.Connection,
    event_type: Optional[str] = None,
    event_subject: Optional[str] = None,
    verification_context: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Query audit log entries with optional filters."""
    clauses = []
    params: list[Any] = []

    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)
    if event_subject is not None:
        clauses.append("event_subject = ?")
        params.append(event_subject)
    if verification_context is not None:
        clauses.append("verification_context = ?")
        params.append(verification_context)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)

    rows = conn.execute(
        f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()

    result = []
    for row in rows:
        d = dict(row)
        try:
            d["event_data"] = json.loads(d["event_data"])
        except (json.JSONDecodeError, TypeError):
            pass
        result.append(d)
    return result


def get_event(conn: sqlite3.Connection, event_id: int) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM audit_log WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["event_data"] = json.loads(d["event_data"])
    except (json.JSONDecodeError, TypeError):
        pass
    return d
