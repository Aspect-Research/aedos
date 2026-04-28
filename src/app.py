"""FastAPI backend for the chat UI.

Endpoints are deliberately thin — the pipeline does the work, the API
marshals state in and out. Every read-only endpoint the UI depends on
reads directly from the fact store, so whatever's persisted is what the
inspector shows.

Run with:

    python -m src.app

or:

    uvicorn src.app:app --reload
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.pipeline import Pipeline, build_pipeline
from src.pattern_registry import load_default_registry

load_dotenv()

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_path = os.getenv("AEDOS_DB_PATH", "aedos.db")
    app.state.pipeline = build_pipeline(db_path)
    try:
        yield
    finally:
        app.state.pipeline.store.close()


app = FastAPI(title="Aedos", version="0.1.0", lifespan=lifespan)


def _pipeline(app: FastAPI) -> Pipeline:
    return app.state.pipeline


# ---- request / response models ---------------------------------------


class ChatRequest(BaseModel):
    message: str


# ---- chat endpoint ---------------------------------------------------


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict[str, Any]:
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")
    try:
        trace = _pipeline(app).run_turn(req.message)
    except Exception as exc:
        # Return a structured error rather than letting FastAPI's
        # generic 500-with-no-body propagate. The chat backend
        # raising (Modal down, Anthropic 429, etc.) is the most
        # common failure here; surface the type so the UI can show
        # something useful.
        raise HTTPException(
            status_code=502,
            detail={
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "hint": (
                    "The pipeline raised. Common causes: chat backend "
                    "down (Modal upstream / Anthropic rate limit), "
                    "extractor LLM unreachable, or retrieval verifier "
                    "network timeout. Check the most recent assistant "
                    "turn's pipeline_events for details."
                ),
            },
        ) from exc
    return trace.to_dict()


# ---- inspectors ------------------------------------------------------


@app.get("/api/turns")
def list_turns() -> list[dict[str, Any]]:
    # Inspector view: show every turn regardless of user_id. The chat
    # endpoint scopes by user_id; this endpoint is for debugging and
    # reads everything.
    return _pipeline(app).store.list_turns(user_id=None)


@app.get("/api/trace/{turn_id}")
def get_trace(turn_id: int) -> list[dict[str, Any]]:
    events = _pipeline(app).store.get_pipeline_events(turn_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"no events for turn {turn_id}")
    return events


@app.get("/api/facts")
def list_facts(
    pattern: str | None = None,
    predicate: str | None = None,
    asserted_by: str | None = None,
    verification_status: str | None = None,
    only_valid: bool = False,
) -> list[dict[str, Any]]:
    # Inspector view: show every fact regardless of user_id (admin view).
    # The router scopes by user_id internally.
    facts = _pipeline(app).store.query_facts(
        pattern=pattern,
        predicate=predicate,
        asserted_by=asserted_by,
        verification_status=verification_status,
        only_valid=only_valid,
        user_id=None,
    )
    return [f.to_dict() for f in facts]


@app.get("/api/patterns")
def list_patterns() -> list[dict[str, Any]]:
    reg = load_default_registry()
    return [
        {
            "name": p.name,
            "description": p.description,
            "slots": [
                {"name": s.name, "type": s.type, "required": s.required}
                for s in p.slots
            ],
            "example_predicates": list(p.example_predicates),
            "query_strategy": list(p.query_strategy),
            "disambiguation_notes": p.disambiguation_notes,
        }
        for p in reg.all()
    ]


@app.get("/api/cache")
def list_cache_entries(limit: int = 200) -> dict[str, Any]:
    """v0.6 — Tier 2 verification cache inspector.

    Returns aggregate stats + the most-recently-cached entries (capped
    at ``limit``). The cache table is small for solo dogfooding;
    serving everything is fine. Adds a server-side ``is_expired`` flag
    so the UI doesn't have to do datetime parsing in JS.
    """
    from datetime import datetime, timezone

    store = _pipeline(app).store
    rows = store._conn.execute(
        "SELECT * FROM verification_cache ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    now = datetime.now(timezone.utc)
    entries = []
    for r in rows:
        d = dict(r)
        expires_at = d.get("expires_at")
        is_expired = False
        if expires_at:
            try:
                if datetime.fromisoformat(expires_at) < now:
                    is_expired = True
            except ValueError:
                is_expired = True  # malformed → treat as expired
        d["is_expired"] = is_expired
        entries.append(d)

    # Aggregate stats.
    stats_row = store._conn.execute(
        "SELECT COUNT(*) AS total, "
        "       COUNT(CASE WHEN expires_at IS NULL THEN 1 END) AS immutable, "
        "       SUM(hit_count) AS total_hits "
        "FROM verification_cache"
    ).fetchone()
    return {
        "stats": {
            "total_entries": int(stats_row["total"] or 0),
            "immutable_entries": int(stats_row["immutable"] or 0),
            "total_hits": int(stats_row["total_hits"] or 0),
        },
        "entries": entries,
    }


@app.post("/api/reset")
def reset() -> dict[str, bool]:
    _pipeline(app).store.reset()
    return {"ok": True}


# ---- static UI -------------------------------------------------------


app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.app:app",
        host="127.0.0.1",
        port=int(os.getenv("AEDOS_PORT", "8000")),
        reload=False,
    )
