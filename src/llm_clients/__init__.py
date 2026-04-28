"""Pluggable chat-model backends for the assistant draft turn.

The "chat model under test" — the model whose claims AEDOS extracts and
verifies — is selected at pipeline construction time via
``AEDOS_CHAT_MODEL_PROVIDER``. Everything else (extractor, router,
code-writer, judge, corrector) stays on the Anthropic ``LLMClient``.

A backend implements one method:

    chat(system: str,
         messages: Iterable[ChatMessage],
         *,
         max_tokens: int = 4096,
         store: FactStore | None = None,
         turn_id: int | None = None) -> str

When ``store`` and ``turn_id`` are provided, the backend is expected to
log a ``chat_model_call`` pipeline event with provider, model, prompt
shape, response, latency, and any error. The Anthropic backend logs a
slim record; the Modal/GLM backend logs the full HTTP exchange shape.

Backends MUST surface errors as exceptions; they MUST NOT silently
return empty strings. The pipeline currently has no fallback path for a
chat failure — failing loudly is the right behavior.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from src.llm_clients.anthropic_chat import AnthropicChatBackend
from src.llm_clients.modal_glm import ModalGLMBackend

if TYPE_CHECKING:  # pragma: no cover
    from src.llm_client import LLMClient


VALID_PROVIDERS = ("anthropic", "modal")


def build_chat_backend(
    *,
    llm: "LLMClient | None" = None,
    provider: str | None = None,
):
    """Construct the chat backend for ``AEDOS_CHAT_MODEL_PROVIDER``.

    ``llm`` is required when the provider is "anthropic" — the Anthropic
    backend reuses the existing client for the chat call so prompt
    caching and credentials stay in one place.
    """
    provider = (provider or os.getenv("AEDOS_CHAT_MODEL_PROVIDER") or "anthropic").lower()
    if provider not in VALID_PROVIDERS:
        raise RuntimeError(
            f"AEDOS_CHAT_MODEL_PROVIDER must be one of {VALID_PROVIDERS}, "
            f"got {provider!r}"
        )
    if provider == "anthropic":
        if llm is None:
            raise RuntimeError(
                "AnthropicChatBackend requires an LLMClient instance"
            )
        return AnthropicChatBackend(llm)
    return ModalGLMBackend.from_env()


__all__ = [
    "AnthropicChatBackend",
    "ModalGLMBackend",
    "build_chat_backend",
    "VALID_PROVIDERS",
]
