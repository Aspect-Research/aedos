"""FastAPI server for Aedos v0.15."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.aedos_v0_15 import __version__
from src.aedos_v0_15.config import Config
from src.aedos_v0_15.database import open_db

_log = logging.getLogger(__name__)

_db = None
_config: Config | None = None
_chat_wrapper = None  # populated lazily on first POST /chat call


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
        from src.aedos_v0_15.deployment.chat_wrapper import ChatWrapper
        from src.aedos_v0_15.layer1_extraction.extractor import Extractor
        from src.aedos_v0_15.layer3_substrate import Substrate
        from src.aedos_v0_15.layer3_substrate.consistency import ConsistencyChecker
        from src.aedos_v0_15.layer3_substrate.predicate_distribution import PredicateDistributionOracle
        from src.aedos_v0_15.layer3_substrate.predicate_translation import PredicateTranslation
        from src.aedos_v0_15.layer3_substrate.resolver import EntityResolver
        from src.aedos_v0_15.layer3_substrate.subsumption import SubsumptionOracle
        from src.aedos_v0_15.layer4_sources.kb_wikidata import WikidataAdapter
        from src.aedos_v0_15.layer4_sources.kb_verifier import KBVerifier
        from src.aedos_v0_15.layer4_sources.python_verifier import PythonVerifier
        from src.aedos_v0_15.layer4_sources.tier_u import TierU
        from src.aedos_v0_15.layer4_sources.walker import Walker
        from src.aedos_v0_15.layer5_result.aggregator import Aggregator
        from src.aedos_v0_15.layer5_result.retraction import RetractionPropagator
        from src.aedos_v0_15.llm.client import LLMClient

        client = LLMClient()
        kb = WikidataAdapter()
        # Correctness mechanisms: the consistency checker runs on every oracle
        # row write (architecture 5.4) and propagates retractions through the
        # propagator's verdict-trace index (architecture 7.3).
        propagator = RetractionPropagator(db=_db)
        consistency = ConsistencyChecker(db=_db, retraction_propagator=propagator)
        pt = PredicateTranslation(db=_db, llm_client=client, consistency_checker=consistency)
        resolver = EntityResolver(kb_protocol=kb, db=_db)
        sub = SubsumptionOracle(db=_db, llm_client=client, kb_protocol=kb, consistency_checker=consistency)
        pd = PredicateDistributionOracle(db=_db, llm_client=client, consistency_checker=consistency)
        substrate = Substrate(resolver=resolver, predicate_translation=pt, subsumption=sub, predicate_distribution=pd)
        tier_u = TierU(db=_db, predicate_translation=pt)
        kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
        py_verifier = PythonVerifier(llm_client=client)
        walker = Walker(tier_u=tier_u, kb_verifier=kb_verifier, python_verifier=py_verifier, substrate=substrate)
        extractor = Extractor(llm_client=client)
        aggregator = Aggregator(retraction_propagator=propagator, db=_db)
        _chat_wrapper = ChatWrapper(
            extractor=extractor,
            walker=walker,
            aggregator=aggregator,
            llm_client=client,
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
