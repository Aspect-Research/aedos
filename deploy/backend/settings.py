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
    # Browser origins permitted by CORS (the React dev server + the deployed UI).
    allowed_origins: list[str] = field(default_factory=lambda: ["http://localhost:5173"])
    # Engine DB path (the seeded substrate).
    db_path: str = "aedos_phase10_5.db"
    # Per-session sliding-window rate limit.
    rate_limit_requests: int = 30
    rate_limit_window_seconds: float = 60.0
    # Bound on caller-supplied session ids (hygiene; SQL is parameterized anyway).
    max_session_id_len: int = 128

    @classmethod
    def from_env(cls) -> "DeploySettings":
        return cls(
            deploy_key=os.environ.get("AEDOS_DEPLOY_KEY", ""),
            require_auth=os.environ.get("AEDOS_REQUIRE_AUTH", "1") != "0",
            allowed_origins=_split_origins(
                os.environ.get("AEDOS_ALLOWED_ORIGINS", "http://localhost:5173")
            ),
            db_path=os.environ.get("AEDOS_DB_PATH", "aedos_phase10_5.db"),
            rate_limit_requests=int(os.environ.get("AEDOS_RATE_LIMIT_REQUESTS", "30")),
            rate_limit_window_seconds=float(
                os.environ.get("AEDOS_RATE_LIMIT_WINDOW", "60")
            ),
            max_session_id_len=int(os.environ.get("AEDOS_MAX_SESSION_ID_LEN", "128")),
        )
