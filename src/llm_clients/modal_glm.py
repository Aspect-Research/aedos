"""Modal-hosted GLM-5.1-FP8 chat backend.

The Modal endpoint exposes an OpenAI-compatible chat-completions API.
Translation from AEDOS's internal format is straightforward: the
``system`` string becomes a leading ``{"role": "system"}`` message and
each ``ChatMessage`` becomes one user/assistant entry. GLM does not
need the structured-output tool-use machinery — the chat role is the
only place GLM is invoked.

The endpoint is free to use until 2026-04-30. After that, switch the
provider back to anthropic per MISSION.md.

Errors are explicit and re-raised:

  * ``ModalAuthError`` — 401 from the endpoint (missing/invalid token)
  * ``ModalRateLimitError`` — 429 from the endpoint
  * ``ModalServerError`` — any 5xx response
  * ``ModalTimeoutError`` — httpx timeout
  * ``ModalResponseError`` — endpoint returned 200 but malformed JSON
                             (no ``choices[0].message.content``)

Every call writes a ``chat_model_call`` pipeline event whether it
succeeds or fails, so failures are visible in the trace UI rather than
silent.
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any, Iterable

import httpx

if TYPE_CHECKING:  # pragma: no cover
    from src.fact_store import FactStore
    from src.llm_client import ChatMessage


MODAL_ENDPOINT = "https://api.us-west-2.modal.direct/v1/chat/completions"
MODAL_MODEL = "zai-org/GLM-5.1-FP8"
MODAL_REQUEST_TIMEOUT = 60.0


class ModalError(RuntimeError):
    """Base class for Modal/GLM endpoint failures.

    ``status_code`` is the HTTP status when known (None for transport
    failures and timeouts).
    """

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ModalAuthError(ModalError):
    """401 from the Modal endpoint."""


class ModalRateLimitError(ModalError):
    """429 from the Modal endpoint."""


class ModalServerError(ModalError):
    """5xx from the Modal endpoint."""


class ModalTimeoutError(ModalError):
    """httpx timeout while talking to the Modal endpoint."""


class ModalResponseError(ModalError):
    """200 OK but body wasn't a usable chat-completion response."""


class ModalGLMBackend:
    provider = "modal"
    model = MODAL_MODEL

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = MODAL_ENDPOINT,
        model: str = MODAL_MODEL,
        timeout: float = MODAL_REQUEST_TIMEOUT,
        client: httpx.Client | None = None,
    ):
        if not api_key:
            raise RuntimeError(
                "MODAL_API_KEY not set. Copy .env.example to .env and paste "
                "your Modal token (free until 2026-04-30)."
            )
        self._api_key = api_key
        self._endpoint = endpoint
        self.model = model
        self._timeout = timeout
        self._client = client  # injected only by tests

    @classmethod
    def from_env(cls) -> "ModalGLMBackend":
        return cls(api_key=os.getenv("MODAL_API_KEY", ""))

    def chat(
        self,
        system: str,
        messages: "Iterable[ChatMessage]",
        *,
        max_tokens: int = 4096,
        store: "FactStore | None" = None,
        turn_id: int | None = None,
    ) -> str:
        msg_list = list(messages)
        payload = self._build_payload(system, msg_list, max_tokens)
        started = time.monotonic()
        error: str | None = None
        status_code: int | None = None
        text = ""
        response_id: str | None = None
        try:
            text, status_code, response_id = self._post(payload)
            return text
        except ModalError as exc:
            error = f"{type(exc).__name__}: {exc}"
            status_code = exc.status_code
            raise
        except Exception as exc:
            # Anything else (programming error, unexpected JSON shape) we
            # still want to surface in the trace before re-raising.
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if store is not None and turn_id is not None:
                store.insert_pipeline_event(
                    turn_id,
                    "chat_model_call",
                    {
                        "provider": self.provider,
                        "model": self.model,
                        "endpoint": self._endpoint,
                        "system_chars": len(system),
                        "message_count": len(msg_list),
                        "max_tokens": max_tokens,
                        "status_code": status_code,
                        "response_chars": len(text),
                        "response_id": response_id,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "error": error,
                    },
                )

    # ---- internals -------------------------------------------------------

    def _build_payload(
        self,
        system: str,
        msg_list: list["ChatMessage"],
        max_tokens: int,
    ) -> dict[str, Any]:
        oai_messages: list[dict[str, str]] = []
        if system:
            oai_messages.append({"role": "system", "content": system})
        for m in msg_list:
            role = m.role
            if role not in ("user", "assistant", "system"):
                raise ValueError(
                    f"ModalGLMBackend: unsupported chat role {role!r}; "
                    "expected 'user', 'assistant', or 'system'"
                )
            oai_messages.append({"role": role, "content": m.content})
        return {
            "model": self.model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
        }

    def _post(self, payload: dict[str, Any]) -> tuple[str, int, str | None]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        try:
            if self._client is not None:
                resp = self._client.post(
                    self._endpoint, headers=headers, json=payload,
                    timeout=self._timeout,
                )
            else:
                resp = httpx.post(
                    self._endpoint, headers=headers, json=payload,
                    timeout=self._timeout,
                )
        except httpx.TimeoutException as exc:
            raise ModalTimeoutError(
                f"timeout talking to {self._endpoint} after {self._timeout}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise ModalError(f"HTTP error talking to {self._endpoint}: {exc}") from exc

        status = resp.status_code
        if status == 401:
            raise ModalAuthError(
                f"401 from {self._endpoint}; check MODAL_API_KEY",
                status_code=status,
            )
        if status == 429:
            raise ModalRateLimitError(
                f"429 from {self._endpoint}", status_code=status,
            )
        if status >= 500:
            raise ModalServerError(
                f"{status} from {self._endpoint}: {resp.text[:200]}",
                status_code=status,
            )
        if status >= 400:
            raise ModalError(
                f"{status} from {self._endpoint}: {resp.text[:200]}",
                status_code=status,
            )

        try:
            body = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ModalResponseError(
                f"non-JSON response from {self._endpoint}: {resp.text[:200]}",
                status_code=status,
            ) from exc

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModalResponseError(
                f"missing choices[0].message.content in response: "
                f"{json.dumps(body)[:200]}",
                status_code=status,
            ) from exc

        if not isinstance(content, str):
            raise ModalResponseError(
                f"choices[0].message.content was {type(content).__name__}, "
                "expected str",
                status_code=status,
            )

        return content, status, body.get("id")
