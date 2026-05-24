"""FastAPI server for Aedos v0.15."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from aedos import __version__
from aedos.config import Config
from aedos.database import open_db

_log = logging.getLogger(__name__)

_db = None
_config: Config | None = None
_chat_wrapper = None  # populated lazily on first POST /chat call


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _db, _config
    # F-013: load .env before Config.from_env() so the env-var reads
    # in Config's default_factory pick up values from the file. Safe
    # to call repeatedly (idempotent per F3 §6 / aedos.utils.env).
    # No-op if the file isn't present or python-dotenv isn't installed.
    from aedos.utils.env import load_dotenv_if_present
    load_dotenv_if_present()
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
    from aedos.audit import log as audit_log
    events = audit_log.query_events(_db, event_type="row_created", limit=limit)
    return JSONResponse({"events": events})


@app.get("/audit/consistency-checks")
async def audit_consistency_checks(limit: int = 100) -> JSONResponse:
    if _db is None:
        raise HTTPException(status_code=503, detail="database not initialized")
    from aedos.audit import log as audit_log
    events = audit_log.query_events(_db, event_type="consistency_violation", limit=limit)
    return JSONResponse({"events": events})


@app.get("/audit/circuit-breakers")
async def audit_circuit_breakers(limit: int = 100) -> JSONResponse:
    if _db is None:
        raise HTTPException(status_code=503, detail="database not initialized")
    from aedos.audit import log as audit_log
    events = audit_log.query_events(_db, event_type="circuit_breaker_triggered", limit=limit)
    return JSONResponse({"events": events})


@app.get("/audit/retractions")
async def audit_retractions(limit: int = 100) -> JSONResponse:
    if _db is None:
        raise HTTPException(status_code=503, detail="database not initialized")
    from aedos.audit import log as audit_log
    events = audit_log.query_events(_db, event_type="row_retracted", limit=limit)
    return JSONResponse({"events": events})


# ---------------------------------------------------------------------------
# Chat endpoint
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    asserting_party_id: Optional[str] = "user"


@app.post("/chat")
async def chat(request: ChatRequest) -> JSONResponse:
    global _chat_wrapper
    if _db is None:
        raise HTTPException(status_code=503, detail="database not initialized")

    if _chat_wrapper is None:
        from aedos.deployment.chat_wrapper import ChatWrapper
        from aedos.pipeline import build_pipeline

        # build_pipeline assembles the full verification pipeline with the
        # correctness mechanisms wired in (architecture 5.4 / 7.3). It is shared
        # with the medium-bar benchmark so app and benchmark have one wiring
        # definition rather than two drifting copies. Phase F2 threads `_config`
        # through so the deployed pipeline reaches Wikidata with the configured
        # endpoints, HTTP cache, and User-Agent (F-004/F-005/F-006/F-007).
        pipeline = build_pipeline(_db, config=_config)
        _chat_wrapper = ChatWrapper(
            extractor=pipeline.extractor,
            walker=pipeline.walker,
            aggregator=pipeline.aggregator,
            llm_client=pipeline.llm_client,
            # Phase H Cluster 2 step 2 (Q-ChatWrapperSource): thread
            # Tier U through so the wrapper can promote user-message
            # claims as `asserted_unverified` premises before draft
            # generation.
            tier_u=pipeline.tier_u,
        )

    ctx = {"asserting_party_id": request.asserting_party_id or "user"}
    response = _chat_wrapper.respond(request.message, conversation_context=ctx)
    return JSONResponse({
        "final_message": response.final_message,
        "intervention_type": response.intervention_type,
        "verification_id": response.verification_id,
    })


@app.get("/verification/{verification_id}")
async def get_verification(verification_id: str) -> JSONResponse:
    if _chat_wrapper is None:
        raise HTTPException(status_code=404, detail="no verification results available")
    vr = _chat_wrapper.get_verification(verification_id)
    if vr is None:
        raise HTTPException(status_code=404, detail="verification not found")
    return JSONResponse({
        "verification_id": verification_id,
        "per_claim_verdicts": vr.per_claim_verdicts,
        "aggregate_metadata": vr.aggregate_metadata,
    })
