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
from src.predicate_registry import load_default_registry

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
    trace = _pipeline(app).run_turn(req.message)
    return trace.to_dict()


# ---- inspectors ------------------------------------------------------


@app.get("/api/turns")
def list_turns() -> list[dict[str, Any]]:
    return _pipeline(app).store.list_turns()


@app.get("/api/trace/{turn_id}")
def get_trace(turn_id: int) -> list[dict[str, Any]]:
    events = _pipeline(app).store.get_pipeline_events(turn_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"no events for turn {turn_id}")
    return events


@app.get("/api/facts")
def list_facts(
    subject: str | None = None,
    predicate: str | None = None,
    asserted_by: str | None = None,
    verification_status: str | None = None,
    only_valid: bool = False,
) -> list[dict[str, Any]]:
    facts = _pipeline(app).store.query_facts(
        subject=subject,
        predicate=predicate,
        asserted_by=asserted_by,
        verification_status=verification_status,
        only_valid=only_valid,
    )
    return [f.to_dict() for f in facts]


@app.get("/api/predicates")
def list_predicates() -> list[dict[str, Any]]:
    reg = load_default_registry()
    return [
        {
            "name": p.name,
            "object_type": p.object_type,
            "verification_method": p.verification_method,
            "python_verifier": p.python_verifier,
            "retrieval_query_template": p.retrieval_query_template,
            "description": p.description,
            "example": p.example,
        }
        for p in reg.all()
    ]


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
