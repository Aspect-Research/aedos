"""FastAPI service for the Aedos v0.16.2 live deployment.

Endpoints (all except /health require the X-Aedos-Key access header; all session
endpoints take the session via the X-Aedos-Session header — never the URL/body,
so the per-session capability stays out of access logs / history / Referer):
  GET  /health                 liveness (unauthenticated)
  POST /chat                   conversational turn (ChatWrapper)
  POST /verify                 run Aedos on raw text -> per-claim verdicts
  POST /session/reset          clear THIS session's Tier-U context ("start fresh")
  GET  /verification/{id}      verbose audit view, party-scoped

Per-session isolation: the caller's opaque session token becomes the Tier-U
`asserting_party` (A+); the engine's existing keying makes sessions invisible to
each other. `create_app(...)` accepts injected settings / pipeline / chat_wrapper
so tests run without live KB/LLM calls.
"""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from aedos import __version__

from .ratelimit import SlidingWindowLimiter
from .sessions import InvalidSessionId, party_for_session
from .settings import DeploySettings

_log = logging.getLogger("aedos.deploy")

# Bound on the per-session verification-id -> party map so a long-lived process
# cannot accumulate one entry per /chat verification without limit (F2).
_MAX_TRACKED_VERIFICATIONS = 5000


def _key_matches(provided: Optional[str], expected: str) -> bool:
    """Constant-time access-key comparison on BYTES.

    `hmac.compare_digest` raises TypeError on non-ASCII `str` input; comparing
    on utf-8 bytes sidesteps that so a caller-supplied non-ASCII key fails the
    gate cleanly (401) instead of raising a 500 (F3). An empty expected key
    never matches — the gate fails CLOSED when no secret is configured.
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
            app.state._db = open_db(settings.db_path)
            app.state._pipeline = build_pipeline(app.state._db, config=cfg)
            _log.info("Aedos deploy pipeline initialized (db=%s)", settings.db_path)
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
        # FIFO-bound the map (F2): drop the oldest entries past the cap.
        while len(store) > _MAX_TRACKED_VERIFICATIONS:
            oldest = next(iter(store))
            del store[oldest]

    # ----- dependencies -------------------------------------------------- #

    async def require_access(
        x_aedos_key: Optional[str] = Header(default=None, alias="X-Aedos-Key"),
    ) -> None:
        if not settings.require_auth:
            return
        # Fail CLOSED: no configured key (or any mismatch, incl. non-ASCII) => 401.
        if not _key_matches(x_aedos_key, settings.deploy_key):
            raise HTTPException(status_code=401, detail="missing or invalid access key")

    async def session_party(
        x_aedos_session: Optional[str] = Header(default=None, alias="X-Aedos-Session"),
    ) -> str:
        # The session token is the Tier-U party (A+). It travels in a header so it
        # never lands in a URL/query-string/log (F1). Validated + namespaced.
        try:
            return party_for_session(
                x_aedos_session, max_len=settings.max_session_id_len
            )
        except InvalidSessionId as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    auth = [Depends(require_access)]

    # ----- given-assertion annotation ------------------------------------ #

    def _annotate(observability: list[dict]) -> dict:
        """Surface the given-assertion passes prominently (the operator asked for
        this). `conditional` per claim is the engine's given-assertion flag; we
        roll up a summary so a caller/UI can badge 'conditional on your
        assertion' without rescanning."""
        conditional_ids = [o["claim_id"] for o in observability if o.get("conditional")]
        return {"count": len(conditional_ids), "claim_ids": conditional_ids}

    # ----- routes -------------------------------------------------------- #

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/chat", dependencies=auth)
    async def chat(
        request: ChatRequest, party: str = Depends(session_party)
    ) -> JSONResponse:
        from aedos.deployment.chat_wrapper import claim_observability

        _rate_limit(party)
        wrapper = _ensure_chat_wrapper()
        response = wrapper.respond(
            request.message, conversation_context={"asserting_party_id": party}
        )
        _track_verification(response.verification_id, party)
        observability = claim_observability(response.verification_result)
        return JSONResponse({
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
        })

    @app.post("/verify", dependencies=auth)
    async def verify(
        request: VerifyRequest, party: str = Depends(session_party)
    ) -> JSONResponse:
        from aedos.deployment.chat_wrapper import claim_observability
        from aedos.layer1_extraction.extractor import ExtractionContext
        from aedos.layer4_sources.walker import VerificationContext

        _rate_limit(party)
        pipeline = _ensure_pipeline()

        ectx = ExtractionContext(asserting_party=party, context_type="document")
        claims = pipeline.extractor.extract(request.text, ectx)
        extracted = [
            {
                "claim_id": c.claim_id,
                "subject": c.subject,
                "predicate": c.predicate,
                "object": c.object,
                "polarity": c.polarity,
                "abstention_reason": c.abstention_reason,
            }
            for c in claims
        ]
        groundable = [c for c in claims if c.abstention_reason is None]
        if not groundable:
            return JSONResponse({
                "extracted_claims": extracted,
                "observability": [],
                "given_assertion": {"count": 0, "claim_ids": []},
                "note": "no groundable claims in the input text",
            })

        vctx = VerificationContext(
            current_time=datetime.now(timezone.utc).isoformat(),
            asserting_party=party,
            source_text=request.text,
        )
        results = [pipeline.walker.walk(c, vctx) for c in groundable]
        vr = pipeline.aggregator.aggregate(groundable, results)
        observability = claim_observability(vr)
        return JSONResponse({
            "extracted_claims": extracted,
            "observability": observability,
            "given_assertion": _annotate(observability),
        })

    @app.post("/session/reset", dependencies=auth)
    async def session_reset(party: str = Depends(session_party)) -> JSONResponse:
        pipeline = _ensure_pipeline()
        removed = pipeline.tier_u.clear_party(party)
        # Drop this party's tracked verification ids too.
        app.state._verification_party = {
            vid: p for vid, p in app.state._verification_party.items() if p != party
        }
        return JSONResponse({"rows_cleared": removed})

    @app.get("/verification/{verification_id}", dependencies=auth)
    async def get_verification(
        verification_id: str, party: str = Depends(session_party)
    ) -> JSONResponse:
        from aedos.deployment.chat_wrapper import claim_observability

        owner = app.state._verification_party.get(verification_id)
        # Party-scoped: a verification is visible only to the session that
        # produced it (404 otherwise — never reveal another session's data, and
        # the same 404 for missing vs. other-party gives no existence oracle).
        if owner != party:
            raise HTTPException(status_code=404, detail="verification not found")
        wrapper = _ensure_chat_wrapper()
        vr = wrapper.get_verification(verification_id)
        if vr is None:
            raise HTTPException(status_code=404, detail="verification not found")
        return JSONResponse({
            "verification_id": verification_id,
            "per_claim_verdicts": vr.per_claim_verdicts,
            "aggregate_metadata": vr.aggregate_metadata,
            "claims": claim_observability(vr, verbose=True),
        })

    return app
