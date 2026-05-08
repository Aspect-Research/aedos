"""Anthropic chat backend.

Thin adapter that hands the chat call back to ``LLMClient.chat`` (which
already does the API call with prompt caching) and logs a slim
``chat_model_call`` event when a store + turn_id are provided so the
trace UI can show the same provenance row regardless of provider.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:  # pragma: no cover
    from src.legacy.fact_store import FactStore
    from src.llm_client import ChatMessage, LLMClient


class AnthropicChatBackend:
    provider = "anthropic"

    def __init__(self, llm: "LLMClient"):
        self._llm = llm

    @property
    def model(self) -> str:
        return self._llm.model

    def chat(
        self,
        system: str,
        messages: "Iterable[ChatMessage]",
        *,
        max_tokens: int = 4096,
        store: "FactStore | None" = None,
        turn_id: int | None = None,
        cost_recorder: Any | None = None,  # accepted but unused — LLMClient
                                            # records cost natively for
                                            # Anthropic calls
        on_token: Any | None = None,        # v0.9.0: when supplied, stream
                                            # tokens via LLMClient.chat_stream
                                            # and call on_token(delta) for
                                            # each text fragment as it arrives.
    ) -> str:
        msg_list = list(messages)
        started = time.monotonic()
        error: str | None = None
        text = ""
        try:
            if on_token is not None:
                text = self._llm.chat_stream(
                    system, msg_list, on_token=on_token,
                    max_tokens=max_tokens, purpose="chat",
                )
            else:
                text = self._llm.chat(
                    system, msg_list, max_tokens=max_tokens, purpose="chat",
                )
            return text
        except Exception as exc:
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
                        "system_chars": len(system),
                        "message_count": len(msg_list),
                        "max_tokens": max_tokens,
                        "response_chars": len(text),
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "error": error,
                        "streamed": on_token is not None,
                    },
                )
