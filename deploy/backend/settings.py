"""Environment-driven settings for the v0.16.2 deployment backend.

All configuration comes from the PROCESS ENVIRONMENT (12-factor) — never a
file read at request time. Provider API keys (ANTHROPIC_API_KEY /
OPENROUTER_API_KEY) are read by the engine's own Config.from_env(); they are
NOT surfaced here and never logged. The sandbox child never receives them
(see aedos.utils.sandbox).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _split_origins(raw: str) -> list[str]:
    return [o.strip() for o in raw.split(",") if o.strip()]


@dataclass(frozen=True)
class DeploySettings:
    # Shared access secret. Required (non-empty) whenever require_auth is on;
    # an unset key with auth on means the gate fails CLOSED (rejects all).
    deploy_key: str = ""
    require_auth: bool = True
    # Authorization model when require_auth is on:
    #   "key"  — the shared X-Aedos-Key gate (default; matches prior releases).
    #   "byok" — a request is authorized by carrying the CALLER'S provider keys
    #            (X-User-Anthropic-Key + X-User-OpenRouter-Key, or OpenRouter
    #            only with X-Aedos-Free-Models: 1). The caller pays for LLM
    #            calls; keys are scoped to the request and never persisted.
    #            The shared deploy key still works as an ops back door when set.
    auth_mode: str = "key"
    # Free-models mode: the single OpenRouter model every purpose (incl. chat)
    # is routed to when a BYOK caller sets X-Aedos-Free-Models: 1.
    free_model: str = "deepseek/deepseek-chat-v3-0324:free"
    # Public-perimeter caps (BYOK posture): largest accepted request body and
    # longest accepted chat/verify text.
    max_body_bytes: int = 32_768
    max_message_chars: int = 8_000
    # Per-client-IP sliding-window limit (same window as the per-session limit).
    # Session ids are caller-chosen, so the per-session limit alone is
    # bypassable by rotating ids; the IP limit is the backstop.
    ip_rate_limit_requests: int = 60
    # Browser origins permitted by CORS (the Vite dev server, both hostnames, +
    # the deployed UI). localhost and 127.0.0.1 are distinct origins to a browser.
    allowed_origins: list[str] = field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )
    # Engine DB path (the seeded substrate).
    db_path: str = "aedos_phase10_5.db"
    # Per-session sliding-window rate limit.
    rate_limit_requests: int = 30
    rate_limit_window_seconds: float = 60.0
    # Bound on caller-supplied session ids (hygiene; SQL is parameterized anyway).
    max_session_id_len: int = 128
    # Walker budget per claim. Matches the engine default (30s). Phase B lowered
    # this to 12s for chat responsiveness, but Phase C made verification PARALLEL
    # (a turn's wall-time is ~max(per-claim), not the sum), so the lower budget
    # only bought over-abstention (budget_wall_clock on simple lookups) — and the
    # project order is soundness > coverage > simplicity > LATENCY. Restored to 30.
    walker_wall_clock_seconds: float = 30.0
    walker_max_llm_calls: int = 10
    # Max claims verified concurrently within one turn (intra-turn parallelism;
    # turns are still serialized by the engine lock). Bounds outbound KB/LLM
    # concurrency. Per-walk state is thread-local so verdicts are unchanged.
    verify_workers: int = 8
    # Phase D: in /chat, verify only the claims central to the user's question
    # (the rest pass through "not assessed"). Skipped when a turn has <= this many
    # claims. Fails open to verifying all on any selector failure.
    select_central_claims: bool = True
    select_min_claims: int = 4

    @classmethod
    def from_env(cls) -> "DeploySettings":
        return cls(
            deploy_key=os.environ.get("AEDOS_DEPLOY_KEY", ""),
            require_auth=os.environ.get("AEDOS_REQUIRE_AUTH", "1") != "0",
            auth_mode=os.environ.get("AEDOS_AUTH_MODE", "key"),
            free_model=os.environ.get(
                "AEDOS_FREE_MODEL", "deepseek/deepseek-chat-v3-0324:free"
            ),
            max_body_bytes=int(os.environ.get("AEDOS_MAX_BODY_BYTES", "32768")),
            max_message_chars=int(os.environ.get("AEDOS_MAX_MESSAGE_CHARS", "8000")),
            ip_rate_limit_requests=int(
                os.environ.get("AEDOS_IP_RATE_LIMIT_REQUESTS", "60")
            ),
            allowed_origins=_split_origins(
                os.environ.get("AEDOS_ALLOWED_ORIGINS", "http://localhost:5173")
            ),
            db_path=os.environ.get("AEDOS_DB_PATH", "aedos_phase10_5.db"),
            rate_limit_requests=int(os.environ.get("AEDOS_RATE_LIMIT_REQUESTS", "30")),
            rate_limit_window_seconds=float(
                os.environ.get("AEDOS_RATE_LIMIT_WINDOW", "60")
            ),
            max_session_id_len=int(os.environ.get("AEDOS_MAX_SESSION_ID_LEN", "128")),
            walker_wall_clock_seconds=float(
                os.environ.get("AEDOS_WALKER_WALL_CLOCK_SECONDS", "30")
            ),
            walker_max_llm_calls=int(os.environ.get("AEDOS_WALKER_MAX_LLM_CALLS", "10")),
            verify_workers=int(os.environ.get("AEDOS_VERIFY_WORKERS", "8")),
            select_central_claims=os.environ.get("AEDOS_SELECT_CENTRAL_CLAIMS", "1") != "0",
            select_min_claims=int(os.environ.get("AEDOS_SELECT_MIN_CLAIMS", "4")),
        )
