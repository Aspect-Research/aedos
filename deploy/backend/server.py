"""FastAPI service for the Aedos v0.16.2 live deployment.

Endpoints (all except /health require the X-Aedos-Key access header; the session
token travels in the X-Aedos-Session header — never the URL/body):
  GET  /health                 liveness (unauthenticated)
  POST /chat                   conversational turn (buffered)
  POST /chat/stream            conversational turn, SSE: live steps then result
  POST /verify                 run Aedos on raw text (buffered)
  POST /verify/stream          run Aedos on raw text, SSE: live steps then result
  POST /session/reset          clear THIS session's Tier-U context ("start fresh")
  GET  /session/context        what Tier-U this session has retained (inspector)
  GET  /verification/{id}      verbose audit view, party-scoped

Blocking engine work runs in a threadpool so it never freezes the event loop
(Phase B: the prior async handlers blocked uvicorn entirely). The SSE endpoints
bridge the engine's synchronous progress callback to a live event stream, so a
long multi-claim turn keeps the connection alive AND shows its steps in real time.
`create_app(...)` accepts injected settings / pipeline / chat_wrapper so tests run
without live KB/LLM calls.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from aedos import __version__

from .ratelimit import SlidingWindowLimiter
from .sessions import InvalidSessionId, party_for_session
from .settings import DeploySettings

_log = logging.getLogger("aedos.deploy")

# Bound on the per-session verification-id -> party map so a long-lived process
# cannot accumulate one entry per /chat verification without limit.
_MAX_TRACKED_VERIFICATIONS = 5000


def _key_matches(provided: Optional[str], expected: str) -> bool:
    """Constant-time access-key comparison on BYTES.

    `hmac.compare_digest` raises TypeError on non-ASCII `str` input; comparing
    on utf-8 bytes sidesteps that so a caller-supplied non-ASCII key fails the
    gate cleanly (401) instead of raising a 500. An empty expected key never
    matches — the gate fails CLOSED when no secret is configured.
    """
    expected_b = (expected or "").encode("utf-8")
    if not expected_b:
        return False
    provided_b = (provided or "").encode("utf-8")
    return hmac.compare_digest(provided_b, expected_b)


# --------------------------------------------------------------------------- #
# Request models (session token travels in the X-Aedos-Session header, NOT here)
# --------------------------------------------------------------------------- #

class ChatRequest(BaseModel):
    message: str


class VerifyRequest(BaseModel):
    text: str


# --------------------------------------------------------------------------- #
# SSE helper: run blocking `work(emit)` in a thread, stream emit() events then
# the return value, ending with a result/error event. Keeps the loop free.
# --------------------------------------------------------------------------- #

async def _sse_response(
    work: Callable[[Callable[[dict], None]], dict],
    *,
    lock: Optional[asyncio.Lock] = None,
) -> StreamingResponse:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    _DONE = object()

    def emit(event: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, ("step", event))

    async def _runner() -> None:
        try:
            # Serialize engine work (the engine assumes a single-threaded
            # pipeline + one shared SQLite connection); the lock keeps the loop
            # free while ensuring one engine call at a time.
            if lock is not None:
                async with lock:
                    result = await run_in_threadpool(work, emit)
            else:
                result = await run_in_threadpool(work, emit)
            loop.call_soon_threadsafe(queue.put_nowait, ("result", result))
        except Exception as exc:  # surface as a clean SSE error, never a silent drop
            # Match the buffered routes' disclosure policy: class name only to the
            # client (B7); full detail stays in the server log.
            _log.exception("deploy stream work failed")
            loop.call_soon_threadsafe(
                queue.put_nowait,
                ("error", {"detail": f"verification failed: {type(exc).__name__}"}),
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, (_DONE, None))

    async def _gen() -> AsyncGenerator[str, None]:
        task = asyncio.create_task(_runner())
        try:
            while True:
                kind, payload = await queue.get()
                if kind is _DONE:
                    break
                yield f"event: {kind}\ndata: {json.dumps(payload)}\n\n"
        finally:
            await task

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #

def create_app(
    *,
    settings: Optional[DeploySettings] = None,
    pipeline: Any = None,
    chat_wrapper: Any = None,
) -> FastAPI:
    settings = settings or DeploySettings.from_env()

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        yield
        if _app.state._db is not None:
            _app.state._db.close()
            _app.state._db = None

    app = FastAPI(title="Aedos (deploy)", version=__version__, lifespan=_lifespan)

    app.state.settings = settings
    app.state._pipeline = pipeline
    app.state._chat_wrapper = chat_wrapper
    app.state._db = None
    app.state._verification_party: dict[str, str] = {}
    app.state.limiter = SlidingWindowLimiter(
        settings.rate_limit_requests, settings.rate_limit_window_seconds
    )
    # Serializes engine work across requests: the engine assumes a single-threaded
    # pipeline (one shared SQLite connection, per-instance rate limiters), so even
    # though work is offloaded to a threadpool (to free the event loop), only one
    # engine call runs at a time. Concurrent requests queue here, loop stays free.
    app.state.engine_lock = asyncio.Lock()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Aedos-Key", "X-Aedos-Session"],
    )

    # ----- helpers (closures over app.state) ----------------------------- #

    def _ensure_pipeline():
        if app.state._pipeline is None:
            from aedos.config import Config
            from aedos.database import open_db
            from aedos.pipeline import build_pipeline
            from aedos.utils.env import load_dotenv_if_present

            load_dotenv_if_present()
            cfg = Config.from_env()
            # Interactive walker budget for a live chat (engine default 30s/claim
            # is too slow when a turn verifies many draft claims serially).
            cfg = dataclasses.replace(
                cfg,
                walker_wall_clock_seconds=settings.walker_wall_clock_seconds,
                walker_max_llm_calls=settings.walker_max_llm_calls,
            )
            app.state._db = open_db(settings.db_path)
            app.state._pipeline = build_pipeline(app.state._db, config=cfg)
            _log.info(
                "Aedos deploy pipeline initialized (db=%s, walk_budget=%ss)",
                settings.db_path, settings.walker_wall_clock_seconds,
            )
        return app.state._pipeline

    def _ensure_chat_wrapper():
        if app.state._chat_wrapper is None:
            from aedos.deployment.chat_wrapper import ChatWrapper

            p = _ensure_pipeline()
            app.state._chat_wrapper = ChatWrapper(
                extractor=p.extractor,
                walker=p.walker,
                aggregator=p.aggregator,
                llm_client=p.llm_client,
                tier_u=p.tier_u,
                kb=p.kb,
            )
        return app.state._chat_wrapper

    def _rate_limit(party: str) -> None:
        if not app.state.limiter.allow(party):
            retry = app.state.limiter.retry_after_seconds(party)
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded; slow down",
                headers={"Retry-After": str(int(retry) + 1)},
            )

    def _track_verification(verification_id: str, party: str) -> None:
        store = app.state._verification_party
        store[verification_id] = party
        while len(store) > _MAX_TRACKED_VERIFICATIONS:
            del store[next(iter(store))]

    # ----- dependencies -------------------------------------------------- #

    async def require_access(
        x_aedos_key: Optional[str] = Header(default=None, alias="X-Aedos-Key"),
    ) -> None:
        if not settings.require_auth:
            return
        if not _key_matches(x_aedos_key, settings.deploy_key):
            raise HTTPException(status_code=401, detail="missing or invalid access key")

    async def session_party(
        x_aedos_session: Optional[str] = Header(default=None, alias="X-Aedos-Session"),
    ) -> str:
        try:
            return party_for_session(
                x_aedos_session, max_len=settings.max_session_id_len
            )
        except InvalidSessionId as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    auth = [Depends(require_access)]

    # ----- response shaping ---------------------------------------------- #

    def _annotate(observability: list[dict]) -> dict:
        """Roll up the given-assertion passes (`conditional` per claim is the
        engine's given-assertion flag) so a UI can badge them at a glance."""
        ids = [o["claim_id"] for o in observability if o.get("conditional")]
        return {"count": len(ids), "claim_ids": ids}

    def _chat_body(response) -> dict:
        from aedos.deployment.chat_wrapper import claim_observability

        observability = claim_observability(response.verification_result)
        return {
            "final_message": response.final_message,
            "intervention_type": response.intervention_type,
            "per_claim_actions": [
                {
                    "claim_id": a.claim_id,
                    "action_type": a.action_type.value,
                    "annotation": a.annotation,
                }
                for a in response.intervention_plan.per_claim_actions
            ],
            "verification_id": response.verification_id,
            "observability": observability,
            "given_assertion": _annotate(observability),
        }

    def _run_verify(party: str, text: str, emit: Callable[[dict], None]) -> dict:
        """The shared /verify body: extract -> walk claims CONCURRENTLY ->
        aggregate, emitting per-step progress (a `verdict` event with the full
        reasoning trace as each claim completes). `emit` is a no-op for the
        buffered endpoint."""
        from aedos.deployment.chat_wrapper import (
            claim_observability, walk_result_observability,
        )
        from aedos.layer1_extraction.extractor import ExtractionContext
        from aedos.layer4_sources.parallel_verify import walk_claims_parallel
        from aedos.layer4_sources.walker import VerificationContext

        pipeline = _ensure_pipeline()
        emit({"phase": "extracting", "detail": "extracting claims from the text"})
        ectx = ExtractionContext(asserting_party=party, context_type="document")
        claims = pipeline.extractor.extract(text, ectx)
        extracted = [
            {
                "claim_id": c.claim_id, "subject": c.subject, "predicate": c.predicate,
                "object": c.object, "polarity": c.polarity,
                "abstention_reason": c.abstention_reason,
            }
            for c in claims
        ]
        emit({"phase": "extracted", "detail": f"found {len(claims)} claim(s)"})
        groundable = [c for c in claims if c.abstention_reason is None]
        if not groundable:
            return {
                "extracted_claims": extracted, "observability": [],
                "given_assertion": {"count": 0, "claim_ids": []},
                "note": "no groundable claims in the input text",
            }
        vctx = VerificationContext(
            current_time=datetime.now(timezone.utc).isoformat(),
            asserting_party=party, source_text=text,
        )
        total = len(groundable)
        for i, c in enumerate(groundable):
            emit({
                "phase": "verifying",
                "detail": f"verifying: {c.subject} {c.predicate} {c.object}",
                "index": i + 1, "total": total, "claim_id": c.claim_id,
            })

        def _on(index, claim, result):
            emit({"phase": "verdict", "detail": str(result.verdict),
                  "index": index + 1, "total": total,
                  **walk_result_observability(claim, result)})

        results = walk_claims_parallel(
            pipeline.walker, groundable, vctx,
            max_workers=settings.verify_workers, on_result=_on,
        )
        vr = pipeline.aggregator.aggregate(groundable, results)
        observability = claim_observability(vr)
        return {
            "extracted_claims": extracted, "observability": observability,
            "given_assertion": _annotate(observability),
        }

    # ----- routes -------------------------------------------------------- #

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/chat", dependencies=auth)
    async def chat(request: ChatRequest, party: str = Depends(session_party)) -> JSONResponse:
        _rate_limit(party)

        def work() -> dict:
            wrapper = _ensure_chat_wrapper()
            response = wrapper.respond(
                request.message,
                conversation_context={"asserting_party_id": party},
                verify_workers=settings.verify_workers,
            )
            _track_verification(response.verification_id, party)
            return _chat_body(response)

        try:
            async with app.state.engine_lock:
                body = await run_in_threadpool(work)
        except Exception as exc:
            _log.exception("chat failed")
            raise HTTPException(status_code=500, detail=f"verification failed: {type(exc).__name__}")
        return JSONResponse(body)

    @app.post("/chat/stream", dependencies=auth)
    async def chat_stream(request: ChatRequest, party: str = Depends(session_party)):
        _rate_limit(party)

        def work(emit: Callable[[dict], None]) -> dict:
            wrapper = _ensure_chat_wrapper()
            response = wrapper.respond(
                request.message,
                conversation_context={"asserting_party_id": party},
                progress=emit,
                verify_workers=settings.verify_workers,
            )
            _track_verification(response.verification_id, party)
            return _chat_body(response)

        return await _sse_response(work, lock=app.state.engine_lock)

    @app.post("/verify", dependencies=auth)
    async def verify(request: VerifyRequest, party: str = Depends(session_party)) -> JSONResponse:
        _rate_limit(party)
        try:
            async with app.state.engine_lock:
                body = await run_in_threadpool(_run_verify, party, request.text, lambda e: None)
        except Exception as exc:
            _log.exception("verify failed")
            raise HTTPException(status_code=500, detail=f"verification failed: {type(exc).__name__}")
        return JSONResponse(body)

    @app.post("/verify/stream", dependencies=auth)
    async def verify_stream(request: VerifyRequest, party: str = Depends(session_party)):
        _rate_limit(party)
        return await _sse_response(
            lambda emit: _run_verify(party, request.text, emit), lock=app.state.engine_lock
        )

    @app.post("/session/reset", dependencies=auth)
    async def session_reset(party: str = Depends(session_party)) -> JSONResponse:
        def work() -> int:
            return _ensure_pipeline().tier_u.clear_party(party)

        async with app.state.engine_lock:
            removed = await run_in_threadpool(work)
            # Prune this party's tracked verification ids in place, under the lock
            # (B5: a rebind outside the lock could drop a concurrent track write).
            store = app.state._verification_party
            for vid in [v for v, p in store.items() if p == party]:
                del store[vid]
        return JSONResponse({"rows_cleared": removed})

    @app.get("/session/context", dependencies=auth)
    async def session_context(party: str = Depends(session_party)) -> JSONResponse:
        def work() -> list[dict]:
            return _ensure_pipeline().tier_u.rows_for_party(party)

        async with app.state.engine_lock:
            rows = await run_in_threadpool(work)
        return JSONResponse({"count": len(rows), "rows": rows})

    @app.get("/verification/{verification_id}", dependencies=auth)
    async def get_verification(
        verification_id: str, party: str = Depends(session_party)
    ) -> JSONResponse:
        # Party-scoped (pure dict get — safe on the loop; same 404 for missing vs.
        # other-party, no existence oracle).
        if app.state._verification_party.get(verification_id) != party:
            raise HTTPException(status_code=404, detail="verification not found")

        # B1: get_verification can re-walk stale claims (live KB/LLM). Run it off
        # the loop and under the engine lock like every other engine route —
        # otherwise it re-introduces the loop-blocking + shared-connection race.
        def work():
            from aedos.deployment.chat_wrapper import claim_observability

            wrapper = _ensure_chat_wrapper()
            vr = wrapper.get_verification(verification_id)
            if vr is None:
                return None
            return {
                "verification_id": verification_id,
                "per_claim_verdicts": vr.per_claim_verdicts,
                "aggregate_metadata": vr.aggregate_metadata,
                "claims": claim_observability(vr, verbose=True),
            }

        async with app.state.engine_lock:
            body = await run_in_threadpool(work)
        if body is None:
            raise HTTPException(status_code=404, detail="verification not found")
        return JSONResponse(body)

    return app
