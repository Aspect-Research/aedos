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

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
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
    # Operator's per-turn model selection from the chat UI dropdown.
    # When set, drives every Anthropic-backed call (chat, extraction,
    # router, judge, corrector, scoping, stability, code-gen). ``glm-5.1``
    # routes only the chat call to Modal — internal calls keep the
    # pipeline's prior Anthropic model since GLM doesn't do tool use.
    # ``None`` falls back to the pipeline's defaults (Opus 4.7).
    model: str | None = None


# ---- chat endpoint ---------------------------------------------------


def _sse_event(event: str, data: Any) -> str:
    """Format an SSE frame. Always JSON-encodes ``data`` so the client
    can ``JSON.parse`` uniformly."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """Streaming variant of /api/chat. Server-Sent Events:

      * ``event: pipeline_event`` — fired for every pipeline_events
        row as the pipeline runs. ``data`` is
        ``{turn_id, stage, data, created_at}``. The Flow View in the
        chat panel renders these incrementally so the operator sees
        the chart expand in real time.
      * ``event: done`` — fired once when the turn completes. ``data``
        is the full ``TurnTrace.to_dict()``.
      * ``event: error`` — fired if the pipeline raises. ``data`` is
        ``{error_type, error_message}``.

    Single-process subscriber registry on FactStore — fine for
    single-user dogfooding; a multi-user deployment would need
    per-request thread-local subscribers.
    """
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")

    p = _pipeline(app)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def subscriber(turn_id: int, stage: str, data: Any) -> None:
        # Fires from the worker thread that runs run_turn. Cross to
        # the event loop with a thread-safe call.
        loop.call_soon_threadsafe(
            queue.put_nowait,
            ("event", {"turn_id": turn_id, "stage": stage, "data": data}),
        )

    token = p.store.register_event_subscriber(subscriber)

    async def _run_pipeline() -> None:
        try:
            trace = await asyncio.to_thread(
                p.run_turn, req.message, model=req.model,
            )
            await queue.put(("done", trace.to_dict()))
        except Exception as exc:
            await queue.put(("error", {
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            }))
        finally:
            await queue.put(("close", None))

    async def event_stream():
        # Padded comment as the very first frame. Browsers (especially
        # Chrome) buffer the initial bytes of a chunked HTTP response
        # until they see ~2KB before they start delivering data to the
        # fetch ReadableStream consumer. Without this preamble the live
        # Flow View doesn't update until the first ~2KB of real
        # pipeline events have accumulated — which can take 5+ seconds
        # if the first stage is a slow LLM call. The ":" prefix marks
        # an SSE comment; clients drop it but the bytes still flush
        # the buffer.
        yield ": " + (" " * 2048) + "\n\n"
        # Initial "started" event so the consumer's onEvent fires
        # immediately and the UI can move from "idle" to "running".
        yield _sse_event("started", {"ts": None})

        runner = asyncio.create_task(_run_pipeline())
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "close":
                    break
                yield _sse_event(kind if kind != "event" else "pipeline_event",
                                 payload)
                if kind in ("done", "error"):
                    # Wait for the close sentinel so the runner finishes
                    # cleanly before we tear down.
                    while True:
                        k2, _ = await queue.get()
                        if k2 == "close":
                            break
                    break
        finally:
            p.store.unregister_event_subscriber(token)
            try:
                await runner
            except Exception:
                pass

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # disable proxy buffering for live updates
    }
    return StreamingResponse(
        event_stream(), media_type="text/event-stream", headers=headers,
    )


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict[str, Any]:
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")
    try:
        trace = _pipeline(app).run_turn(req.message, model=req.model)
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


@app.get("/api/models")
def list_models() -> dict[str, Any]:
    """Models the chat UI can offer in the per-turn selector. Returns
    the canonical model id, a display label, and an ``available`` flag
    so the UI can grey out GLM when MODAL_API_KEY is missing.

    The selected model drives every Anthropic-backed call in a turn
    (chat, extraction, router, judge, corrector, scoping, stability,
    code-gen). GLM routes only the chat call to Modal — internal calls
    keep the prior Anthropic model since GLM doesn't do tool use.
    """
    from src.llm_client import ALLOWED_MODELS
    p = _pipeline(app)
    modal_available = p._modal_backend is not None
    labels = {
        "claude-opus-4-7": "Claude Opus 4.7",
        "claude-sonnet-4-6": "Claude Sonnet 4.6",
        "claude-haiku-4-5": "Claude Haiku 4.5",
        "glm-5.1": "GLM 5.1 (chat only — internal calls stay on Anthropic)",
    }
    default_model = getattr(p.llm, "model", "claude-opus-4-7")
    return {
        "default": default_model,
        "models": [
            {
                "id": m,
                "label": labels.get(m, m),
                "available": (modal_available if m == "glm-5.1" else True),
            }
            for m in ALLOWED_MODELS
        ],
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Lightweight health check. Confirms the pipeline is constructed
    and the DB is reachable. Useful for monitoring / readiness probes."""
    p = _pipeline(app)
    try:
        # SQLite read — confirms the file is accessible + schema present.
        n_turns = p.store._conn.execute(
            "SELECT COUNT(*) AS n FROM turns"
        ).fetchone()["n"]
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "ok": True,
        "user_id": p.user_id,
        "chat_provider": getattr(p.chat_backend, "provider", "anthropic"),
        "chat_model": getattr(p.chat_backend, "model",
                              getattr(p.llm, "model", "?")),
        "db_path": p.store.db_path,
        "turns_in_db": int(n_turns),
        "cache_enabled": p._verification_cache is not None,
        "scoping_enabled": p._scoping_classifier is not None,
        "stability_enabled": p._stability_classifier is not None,
    }


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

    # Aggregate stats from the cache table itself.
    stats_row = store._conn.execute(
        "SELECT COUNT(*) AS total, "
        "       COUNT(CASE WHEN expires_at IS NULL THEN 1 END) AS immutable, "
        "       SUM(hit_count) AS total_hits "
        "FROM verification_cache"
    ).fetchone()

    # Live hit-rate stats from pipeline_events. Cache hits / misses /
    # errors are written by the router on every cache_lookup. The hit
    # rate here measures actual short-circuited retrievals — distinct
    # from total_hits, which is per-cache-entry and accumulates over
    # the entry's lifetime.
    lookup_rows = store._conn.execute(
        "SELECT data FROM pipeline_events WHERE stage = 'cache_lookup'"
    ).fetchall()
    hits = misses = errors = 0
    by_stability_hits: dict[str, int] = {}
    import json as _json
    for r in lookup_rows:
        try:
            data = _json.loads(r["data"])
        except (TypeError, ValueError):
            continue
        if data.get("error"):
            errors += 1
            continue
        result = data.get("result")
        if result == "hit":
            hits += 1
            stab = data.get("stability_class") or "unknown"
            by_stability_hits[stab] = by_stability_hits.get(stab, 0) + 1
        elif result == "miss":
            misses += 1
    total_lookups = hits + misses
    hit_rate = (hits / total_lookups) if total_lookups else None

    return {
        "stats": {
            "total_entries": int(stats_row["total"] or 0),
            "immutable_entries": int(stats_row["immutable"] or 0),
            "total_hits": int(stats_row["total_hits"] or 0),
            "lookups": total_lookups,
            "lookup_hits": hits,
            "lookup_misses": misses,
            "lookup_errors": errors,
            "hit_rate": hit_rate,
            "hits_by_stability": by_stability_hits,
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
