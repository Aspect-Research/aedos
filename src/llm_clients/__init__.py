"""Chat-model backend for the assistant draft turn.

The "chat model under test" — the model whose claims AEDOS extracts and
verifies — runs through ``AnthropicChatBackend`` (the only backend
post-v0.7.15). Everything else (extractor, router, code-writer, judge,
corrector) stays on the Anthropic ``LLMClient``.

A backend implements one method:

    chat(system: str,
         messages: Iterable[ChatMessage],
         *,
         max_tokens: int = 4096,
         store: FactStore | None = None,
         turn_id: int | None = None) -> str

When ``store`` and ``turn_id`` are provided, the backend logs a
``chat_model_call`` pipeline event with provider, model, prompt shape,
response, latency, and any error.

Backends MUST surface errors as exceptions; they MUST NOT silently
return empty strings. The pipeline currently has no fallback path for
a chat failure — failing loudly is the right behavior.

History: pre-v0.7.15 there was a Modal-hosted GLM-5.1 backend behind
``AEDOS_CHAT_MODEL_PROVIDER=modal``. Removed because GLM doesn't
support tool use (so it couldn't drive any internal calls anyway) and
the dual-backend abstraction was carrying its weight in cruft, not
capability.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.llm_clients.anthropic_chat import AnthropicChatBackend

if TYPE_CHECKING:  # pragma: no cover
    from src.llm_client import LLMClient


def build_chat_backend(*, llm: "LLMClient"):
    """Construct the chat backend (Anthropic — the only one post-v0.7.15).

    ``llm`` is required: the Anthropic backend reuses the existing
    client for the chat call so prompt caching and credentials stay
    in one place.
    """
    if llm is None:
        raise RuntimeError(
            "AnthropicChatBackend requires an LLMClient instance"
        )
    return AnthropicChatBackend(llm)


__all__ = [
    "AnthropicChatBackend",
    "build_chat_backend",
]
