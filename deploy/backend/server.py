"""FastAPI service for the Aedos live deployment.

Endpoints (all except /health require authorization; the session token travels
in the X-Aedos-Session header — never the URL/body). Two auth modes
(AEDOS_AUTH_MODE): "key" gates on the shared X-Aedos-Key secret; "byok" gates
on the CALLER'S provider keys (X-User-Anthropic-Key + X-User-OpenRouter-Key,
or OpenRouter alone with X-Aedos-Free-Models: 1), which fund the LLM calls and
are scoped to the request — never logged or persisted:
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
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
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
# BYOK: caller-supplied provider keys, scoped to one request. Never logged,
# never persisted; threaded into the engine's LLM client for the call only.
# --------------------------------------------------------------------------- #

_MAX_USER_KEY_LEN = 512


class InvalidUserKey(ValueError):
    pass


def _clean_user_key(raw: Optional[str], header: str) -> Optional[str]:
    """Normalize a caller-supplied provider key header: strip whitespace,
    reject absurd lengths and non-printable content. Returns None for
    absent/empty. The key VALUE never appears in the error."""
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        return None
    if len(key) > _MAX_USER_KEY_LEN or not key.isprintable():
        raise InvalidUserKey(f"malformed {header} header")
    return key


@dataclasses.dataclass(frozen=True)
class UserKeys:
    anthropic: Optional[str] = None
    openrouter: Optional[str] = None
    free_models: bool = False

    def authorizes(self) -> bool:
        """BYOK authorization: the default routing needs BOTH providers; free
        mode reroutes every purpose to OpenRouter so that key alone suffices."""
        if not self.openrouter:
            return False
        return bool(self.anthropic) or self.free_models


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

    # v0.16.2 hardening: when the access gate is ON (the networked / public
    # posture), disable the interactive API docs and the OpenAPI schema. They
    # carry no auth dependency, so leaving them on would let any unauthenticated
    # scanner read the full API contract (every route + request/response schema)
    # at /openapi.json, /docs, /redoc — advertising the service behind the gate.
    # They stay available only when the gate is explicitly off (local dev,
    # AEDOS_REQUIRE_AUTH=0), which the README already scopes to no-network use.
    _docs_kwargs: dict = (
        {"openapi_url": None, "docs_url": None, "redoc_url": None}
        if settings.require_auth
        else {}
    )
    app = FastAPI(
        title="Aedos (deploy)", version=__version__, lifespan=_lifespan, **_docs_kwargs
    )

    app.state.settings = settings
    app.state._pipeline = pipeline
    app.state._chat_wrapper = chat_wrapper
    app.state._db = None
    app.state._verification_party: dict[str, str] = {}
    app.state.limiter = SlidingWindowLimiter(
        settings.rate_limit_requests, settings.rate_limit_window_seconds
    )
    # Per-client-IP backstop: session ids are caller-chosen, so the per-session
    # limiter alone is bypassable by rotating ids.
    app.state.ip_limiter = SlidingWindowLimiter(
        settings.ip_rate_limit_requests, settings.rate_limit_window_seconds
    )
    # Serializes engine work across requests: the engine assumes a single-threaded
    # pipeline (one shared SQLite connection, per-instance rate limiters), so even
    # though work is offloaded to a threadpool (to free the event loop), only one
    # engine call runs at a time. Concurrent requests queue here, loop stays free.
    app.state.engine_lock = asyncio.Lock()

    # Registered BEFORE CORSMiddleware so CORS stays outermost — a 413 must
    # still carry CORS headers or the browser reports it as a CORS failure.
    @app.middleware("http")
    async def _body_cap(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.max_body_bytes:
            return JSONResponse(
                {"detail": "request body too large"}, status_code=413
            )
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "X-Aedos-Key",
            "X-Aedos-Session",
            "X-User-Anthropic-Key",
            "X-User-OpenRouter-Key",
            "X-Aedos-Free-Models",
        ],
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
            from aedos.deployment.verification_store import VerificationStore

            p = _ensure_pipeline()
            app.state._chat_wrapper = ChatWrapper(
                extractor=p.extractor,
                walker=p.walker,
                aggregator=p.aggregator,
                llm_client=p.llm_client,
                tier_u=p.tier_u,
                kb=p.kb,
                # Durable observability store on the shared pipeline connection
                # (single conn, check_same_thread=False; writes run under engine_lock).
                verification_store=VerificationStore(p.db),
            )
        return app.state._chat_wrapper

    def _verification_store():
        """The durable store on the shared pipeline DB (read + /verify write path)."""
        from aedos.deployment.verification_store import VerificationStore

        return VerificationStore(_ensure_pipeline().db)

    def _client_ip(http_request: Request) -> str:
        # Fly terminates TLS and forwards the true client address here; the
        # direct peer address is the proxy's. Fall back for local/dev runs.
        return (
            http_request.headers.get("fly-client-ip")
            or (http_request.client.host if http_request.client else "unknown")
        )

    def _rate_limit(party: str, http_request: Optional[Request] = None) -> None:
        if not app.state.limiter.allow(party):
            retry = app.state.limiter.retry_after_seconds(party)
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded; slow down",
                headers={"Retry-After": str(int(retry) + 1)},
            )
        if http_request is not None:
            ip = _client_ip(http_request)
            if not app.state.ip_limiter.allow(ip):
                retry = app.state.ip_limiter.retry_after_seconds(ip)
                raise HTTPException(
                    status_code=429,
                    detail="rate limit exceeded; slow down",
                    headers={"Retry-After": str(int(retry) + 1)},
                )

    def _check_text_len(text: str) -> None:
        if len(text) > settings.max_message_chars:
            raise HTTPException(
                status_code=413,
                detail=f"text too long (max {settings.max_message_chars} characters)",
            )

    def _llm_overrides(keys: UserKeys) -> Optional[dict]:
        """kwargs for `LLMClient.request_overrides(...)` when the caller
        supplied their own provider keys; None → operator-key mode."""
        if not (keys.anthropic or keys.openrouter):
            return None
        kw: dict = {
            "anthropic_api_key": keys.anthropic,
            "api_keys_by_env_var": (
                {"OPENROUTER_API_KEY": keys.openrouter} if keys.openrouter else {}
            ),
        }
        if keys.free_models and keys.openrouter:
            kw["route_all_to"] = {
                "model": settings.free_model,
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env_var": "OPENROUTER_API_KEY",
                "extra_body": None,
            }
        return kw

    def _override_ctx(overrides: Optional[dict]):
        if overrides is None:
            return nullcontext()
        return _ensure_pipeline().llm_client.request_overrides(**overrides)

    def _track_verification(verification_id: str, party: str) -> None:
        store = app.state._verification_party
        store[verification_id] = party
        while len(store) > _MAX_TRACKED_VERIFICATIONS:
            del store[next(iter(store))]

    # ----- dependencies -------------------------------------------------- #

    async def user_keys(
        x_user_anthropic_key: Optional[str] = Header(
            default=None, alias="X-User-Anthropic-Key"
        ),
        x_user_openrouter_key: Optional[str] = Header(
            default=None, alias="X-User-OpenRouter-Key"
        ),
        x_aedos_free_models: Optional[str] = Header(
            default=None, alias="X-Aedos-Free-Models"
        ),
    ) -> UserKeys:
        try:
            return UserKeys(
                anthropic=_clean_user_key(x_user_anthropic_key, "X-User-Anthropic-Key"),
                openrouter=_clean_user_key(
                    x_user_openrouter_key, "X-User-OpenRouter-Key"
                ),
                free_models=(x_aedos_free_models or "").strip() == "1",
            )
        except InvalidUserKey as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    async def require_access(
        x_aedos_key: Optional[str] = Header(default=None, alias="X-Aedos-Key"),
        keys: UserKeys = Depends(user_keys),
    ) -> None:
        if not settings.require_auth:
            return
        if settings.auth_mode == "byok":
            # A request is authorized by carrying the caller's own provider
            # keys (they fund the LLM calls). The shared deploy key remains an
            # ops back door when configured.
            if keys.authorizes():
                return
            if _key_matches(x_aedos_key, settings.deploy_key):
                return
            raise HTTPException(
                status_code=401,
                detail=(
                    "provide your provider keys: X-User-OpenRouter-Key plus "
                    "X-User-Anthropic-Key, or the OpenRouter key alone with "
                    "X-Aedos-Free-Models: 1"
                ),
            )
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
            # Phase D: claims not central to the question, passed through unverified.
            "not_assessed": list(getattr(response, "not_assessed_claims", []) or []),
            "selection": getattr(response, "selection_summary", ""),
        }

    def _run_verify(
        party: str,
        text: str,
        emit: Callable[[dict], None],
        overrides: Optional[dict] = None,
    ) -> dict:
        """BYOK-aware wrapper: installs the caller's request-scoped provider
        keys (if any) around the verify body. Runs under the engine lock."""
        with _override_ctx(overrides):
            return _run_verify_inner(party, text, emit)

    def _run_verify_inner(party: str, text: str, emit: Callable[[dict], None]) -> dict:
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
        vctx = VerificationContext(
            current_time=datetime.now(timezone.utc).isoformat(),
            asserting_party=party, source_text=text,
        )
        results: list = []
        if groundable:
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
        # EVERY /verify run is durably addressable — incl. an all-abstained input,
        # whose extracted (abstention-reason-bearing) claims are still captured. For
        # the empty-groundable case build a minimal empty result directly (no walk,
        # nothing to aggregate) rather than aggregating an empty list.
        if groundable:
            vr = pipeline.aggregator.aggregate(
                groundable, results, text_input={"draft": text}
            )
            observability = claim_observability(vr)
        else:
            from aedos.layer5_result.aggregator import VerificationResult

            vr = VerificationResult(
                claims_extracted=[], per_claim_verdicts={}, per_claim_traces={},
                aggregate_metadata={"claim_count": 0}, audit_log_entries=[],
                text_input={"draft": text}, consistency_warnings=[], claim_verdicts=[],
            )
            observability = []
        # Mint an id + persist durably so GET /verification/{id} resolves a /verify
        # run too (it produced no id before). The id is returned ONLY on a confirmed
        # persist, so a client never holds an id that 404s forever; a store failure
        # is logged and the verify response (observability inline) still returns.
        import uuid as _uuid

        verification_id = str(_uuid.uuid4())
        persisted = False
        try:
            _verification_store().persist(
                verification_id, party, vr,
                source_kind="verify",
                created_at=vctx.current_time,
                walk_results=results,
                chat_extras=None,
                extracted_claims=extracted,
            )
            _track_verification(verification_id, party)
            persisted = True
        except Exception:
            _log.exception("verification_store.persist (verify) failed")
        body = {
            "extracted_claims": extracted, "observability": observability,
            "given_assertion": _annotate(observability),
        }
        if not groundable:
            body["note"] = "no groundable claims in the input text"
        if persisted:
            body["verification_id"] = verification_id
        return body

    # ----- routes -------------------------------------------------------- #

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/chat", dependencies=auth)
    async def chat(
        request: ChatRequest,
        http_request: Request,
        party: str = Depends(session_party),
        keys: UserKeys = Depends(user_keys),
    ) -> JSONResponse:
        _check_text_len(request.message)
        _rate_limit(party, http_request)
        overrides = _llm_overrides(keys)

        def work() -> dict:
            wrapper = _ensure_chat_wrapper()
            with _override_ctx(overrides):
                response = wrapper.respond(
                    request.message,
                    conversation_context={"asserting_party_id": party},
                    verify_workers=settings.verify_workers,
                    select_central=settings.select_central_claims,
                    select_min_claims=settings.select_min_claims,
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
    async def chat_stream(
        request: ChatRequest,
        http_request: Request,
        party: str = Depends(session_party),
        keys: UserKeys = Depends(user_keys),
    ):
        _check_text_len(request.message)
        _rate_limit(party, http_request)
        overrides = _llm_overrides(keys)

        def work(emit: Callable[[dict], None]) -> dict:
            wrapper = _ensure_chat_wrapper()
            with _override_ctx(overrides):
                response = wrapper.respond(
                    request.message,
                    conversation_context={"asserting_party_id": party},
                    progress=emit,
                    verify_workers=settings.verify_workers,
                    select_central=settings.select_central_claims,
                    select_min_claims=settings.select_min_claims,
                )
            _track_verification(response.verification_id, party)
            return _chat_body(response)

        return await _sse_response(work, lock=app.state.engine_lock)

    @app.post("/verify", dependencies=auth)
    async def verify(
        request: VerifyRequest,
        http_request: Request,
        party: str = Depends(session_party),
        keys: UserKeys = Depends(user_keys),
    ) -> JSONResponse:
        _check_text_len(request.text)
        _rate_limit(party, http_request)
        overrides = _llm_overrides(keys)
        try:
            async with app.state.engine_lock:
                body = await run_in_threadpool(
                    _run_verify, party, request.text, lambda e: None, overrides
                )
        except Exception as exc:
            _log.exception("verify failed")
            raise HTTPException(status_code=500, detail=f"verification failed: {type(exc).__name__}")
        return JSONResponse(body)

    @app.post("/verify/stream", dependencies=auth)
    async def verify_stream(
        request: VerifyRequest,
        http_request: Request,
        party: str = Depends(session_party),
        keys: UserKeys = Depends(user_keys),
    ):
        _check_text_len(request.text)
        _rate_limit(party, http_request)
        overrides = _llm_overrides(keys)
        return await _sse_response(
            lambda emit: _run_verify(party, request.text, emit, overrides),
            lock=app.state.engine_lock,
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
        # Reads the DURABLE store — no re-walk, survives restart. Party-scoping is
        # the PERSISTED `asserting_party` (the in-memory party map didn't survive a
        # restart). Same 404 for missing vs. other-party (no existence oracle). The
        # SQLite read runs under the engine lock + off the loop like every engine
        # route (shared single connection).
        def work():
            payload = _verification_store().load(verification_id)
            if payload is None or payload.get("asserting_party") != party:
                return None
            return payload

        async with app.state.engine_lock:
            body = await run_in_threadpool(work)
        if body is None:
            raise HTTPException(status_code=404, detail="verification not found")
        return JSONResponse(body)

    return app
