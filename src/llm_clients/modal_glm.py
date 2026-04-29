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
# Modal containers cold-start; first request after idle can take 90+ seconds
# while the GLM weights load. GLM-5.1-FP8 is also a reasoning model whose
# completion includes a long ``reasoning_content`` chain before the actual
# message content, so even warm requests with high max_tokens can run for
# minutes. 300s covers cold-start + a long reasoning chain at max_tokens=4096.
# A timeout here also frees the upstream concurrency slot so the next request
# isn't blocked behind a runaway one.
MODAL_REQUEST_TIMEOUT = 300.0

# Modal endpoint enforces ~1 concurrent request per model. A timed-out
# request can occupy the slot for a while after our client gives up,
# causing subsequent requests to get 429 'Too many concurrent requests'.
# Retry handles that transient state without making the caller deal
# with it. On 429: sleep and try again, up to MODAL_429_MAX_RETRIES.
MODAL_429_MAX_RETRIES = 3
MODAL_429_BACKOFF_S = (30.0, 60.0, 120.0)  # one entry per retry

# Modal upstream sometimes serves 502/503 during deploy / container
# restart / brief outage. Empirically these resolve in 30-90s and are
# distinct from a sustained outage (where retrying won't help). Two
# retries with longer backoff: catches the brief blips, gives up on
# anything sustained so we don't burn tens of minutes on a dead
# endpoint.
MODAL_5XX_MAX_RETRIES = 2
MODAL_5XX_BACKOFF_S = (60.0, 120.0)


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
        cost_recorder: Any | None = None,
    ) -> str:
        """``cost_recorder`` is an optional callable
        ``(model, input_tokens, output_tokens) -> None`` for cost
        telemetry. The Pipeline binds this to LLMClient.record_external_call
        so Modal usage flows into the turn_cost aggregate."""
        msg_list = list(messages)
        payload = self._build_payload(system, msg_list, max_tokens)
        started = time.monotonic()
        error: str | None = None
        status_code: int | None = None
        text = ""
        response_id: str | None = None
        usage: dict[str, int] = {}
        try:
            text, status_code, response_id, usage = self._post_with_retry(payload)
            if cost_recorder is not None and usage:
                try:
                    cost_recorder(
                        self.model,
                        usage.get("input_tokens", 0),
                        usage.get("output_tokens", 0),
                        purpose="chat",
                        duration_ms=(time.monotonic() - started) * 1000,
                    )
                except Exception:
                    pass
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
                        "input_tokens": usage.get("input_tokens"),
                        "output_tokens": usage.get("output_tokens"),
                        "reasoning_tokens": usage.get("reasoning_tokens"),
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

    def _post_with_retry(self, payload: dict[str, Any]) -> tuple[str, int, str | None, dict[str, int]]:
        """Wrap ``_post`` with bounded retry on transient errors.

        429 'Too many concurrent requests' — slot held by previous
            request. Retry up to MODAL_429_MAX_RETRIES times.
        502 / 503 — Modal upstream brief outage / restart. Retry up
            to MODAL_5XX_MAX_RETRIES times with longer backoff.
        Other errors (401, other 5xx, timeout, malformed response)
            propagate immediately — retrying won't fix them."""
        attempts_429 = 0
        attempts_5xx = 0
        last_exc: ModalError | None = None
        # Hard upper bound on total attempts so a perpetual 429+5xx
        # alternation doesn't loop forever.
        max_total_attempts = (MODAL_429_MAX_RETRIES + 1
                              + MODAL_5XX_MAX_RETRIES + 1)
        for _ in range(max_total_attempts):
            try:
                return self._post(payload)
            except ModalRateLimitError as exc:
                last_exc = exc
                if attempts_429 >= MODAL_429_MAX_RETRIES:
                    raise
                backoff = MODAL_429_BACKOFF_S[
                    min(attempts_429, len(MODAL_429_BACKOFF_S) - 1)
                ]
                attempts_429 += 1
                time.sleep(backoff)
            except ModalServerError as exc:
                last_exc = exc
                # Only retry 502/503 — other 5xx (500, 504, 511) are
                # less likely to clear quickly. Conservative: 502/503 only.
                if exc.status_code not in (502, 503):
                    raise
                if attempts_5xx >= MODAL_5XX_MAX_RETRIES:
                    raise
                backoff = MODAL_5XX_BACKOFF_S[
                    min(attempts_5xx, len(MODAL_5XX_BACKOFF_S) - 1)
                ]
                attempts_5xx += 1
                time.sleep(backoff)
        # Fell out of the loop — shouldn't happen given the raises
        # above, but be explicit.
        assert last_exc is not None
        raise last_exc

    def _post(self, payload: dict[str, Any]) -> tuple[str, int, str | None, dict[str, int]]:
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
            message = body["choices"][0]["message"]
            content = message["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModalResponseError(
                f"missing choices[0].message.content in response: "
                f"{json.dumps(body)[:200]}",
                status_code=status,
            ) from exc

        # GLM-5.1 emits ``reasoning_content`` separately and ``content`` is
        # null when the response was truncated mid-reasoning (max_tokens cap
        # hit before the model produced any user-facing content). Surface
        # this as an explicit error rather than returning an empty string —
        # the pipeline can't extract claims from no content.
        if content is None:
            reasoning_chars = len(message.get("reasoning_content") or "")
            raise ModalResponseError(
                f"GLM returned content=null (likely truncated mid-reasoning; "
                f"reasoning_content was {reasoning_chars} chars). Try a "
                f"larger max_tokens.",
                status_code=status,
            )
        if not isinstance(content, str):
            raise ModalResponseError(
                f"choices[0].message.content was {type(content).__name__}, "
                "expected str",
                status_code=status,
            )

        # GLM returns usage with input_tokens, output_tokens, and
        # (for reasoning models) reasoning_tokens. We capture all three;
        # cost telemetry treats output_tokens as the billable count
        # since reasoning_tokens are bundled in the same field upstream.
        usage_in = body.get("usage") or {}
        usage = {
            "input_tokens": int(usage_in.get("prompt_tokens", 0) or 0),
            "output_tokens": int(usage_in.get("completion_tokens", 0) or 0),
            "reasoning_tokens": int(usage_in.get("reasoning_tokens", 0) or 0),
        }
        return content, status, body.get("id"), usage
