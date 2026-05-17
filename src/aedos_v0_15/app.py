"""FastAPI server for Aedos v0.15."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from src.aedos_v0_15 import __version__
from src.aedos_v0_15.config import Config
from src.aedos_v0_15.database import open_db

_log = logging.getLogger(__name__)

_db = None
_config: Config | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _db, _config
    _config = Config.from_env()
    try:
        _db = open_db(_config.db_path)
        _log.info("Aedos v0.15 database initialized at %s", _config.db_path)
    except Exception as exc:
        _log.error("Failed to initialize database: %s", exc)
        raise
    yield
    if _db is not None:
        _db.close()
        _db = None


app = FastAPI(title="Aedos", version=__version__, lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/audit/substrate-rows")
async def audit_substrate_rows(
    table: str | None = None,
    retracted: bool | None = None,
    predicate: str | None = None,
    limit: int = 100,
) -> JSONResponse:
    if _db is None:
        raise HTTPException(status_code=503, detail="database not initialized")
    from src.aedos_v0_15.audit import log as audit_log
    events = audit_log.query_events(_db, event_type="row_created", limit=limit)
    return JSONResponse({"events": events})


@app.get("/audit/consistency-checks")
async def audit_consistency_checks(limit: int = 100) -> JSONResponse:
    if _db is None:
        raise HTTPException(status_code=503, detail="database not initialized")
    from src.aedos_v0_15.audit import log as audit_log
    events = audit_log.query_events(_db, event_type="consistency_violation", limit=limit)
    return JSONResponse({"events": events})


@app.get("/audit/circuit-breakers")
async def audit_circuit_breakers(limit: int = 100) -> JSONResponse:
    if _db is None:
        raise HTTPException(status_code=503, detail="database not initialized")
    from src.aedos_v0_15.audit import log as audit_log
    events = audit_log.query_events(_db, event_type="circuit_breaker_triggered", limit=limit)
    return JSONResponse({"events": events})


@app.get("/audit/retractions")
async def audit_retractions(limit: int = 100) -> JSONResponse:
    if _db is None:
        raise HTTPException(status_code=503, detail="database not initialized")
    from src.aedos_v0_15.audit import log as audit_log
    events = audit_log.query_events(_db, event_type="row_retracted", limit=limit)
    return JSONResponse({"events": events})
