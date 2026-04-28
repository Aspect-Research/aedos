"""Thin wrapper around the Anthropic SDK.

Three entry points cover every call the pipeline makes:

* ``chat(...)`` — assistant turn generation, returns plain text.
* ``extract_with_tool(...)`` — forces the model to call a specified tool and
  returns its parsed input dict. Used for claim extraction so we never parse
  freeform JSON out of a prose response.
* ``rewrite(...)`` — a text transform call (correction rewrite).

Prompt caching is applied to stable system prompts by passing a top-level
``cache_control={"type": "ephemeral"}``. That's sufficient here — the
extractor's system prompt is the largest reused prefix and benefits most.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable

import anthropic

# Per the Claude API skill, claude-opus-4-7 is the recommended default.
# User spec listed claude-sonnet-4-5 or claude-opus-4-7 as options.
DEFAULT_MODEL = "claude-opus-4-7"

# Models that no longer accept the ``temperature`` parameter (Anthropic
# deprecated it for the reasoning-heavy Opus 4.7 line). Calls that pass
# temperature against one of these get a 400. We silently drop the
# parameter and log a warning rather than crash the request — the worst
# case is the canonical-constants cross-check loses its temperature-
# variation signal, which is documented in OBSERVATIONS.md.
_TEMPERATURE_DEPRECATED_PREFIXES = ("claude-opus-4-7",)

_log = logging.getLogger(__name__)


def _model_accepts_temperature(model: str) -> bool:
    return not any(model.startswith(p) for p in _TEMPERATURE_DEPRECATED_PREFIXES)


@dataclass
class ChatMessage:
    role: str  # 'user' | 'assistant'
    content: str


class LLMClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        extractor_model: str | None = None,
        corrector_model: str | None = None,
    ):
        api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Copy .env.example to .env and paste your key."
            )
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model or os.getenv("AEDOS_CHAT_MODEL") or DEFAULT_MODEL
        self.extractor_model = (
            extractor_model or os.getenv("AEDOS_EXTRACTOR_MODEL") or DEFAULT_MODEL
        )
        self.corrector_model = (
            corrector_model or os.getenv("AEDOS_CORRECTOR_MODEL") or DEFAULT_MODEL
        )

    # ---- chat ------------------------------------------------------------

    def chat(
        self,
        system: str,
        messages: Iterable[ChatMessage],
        max_tokens: int = 4096,
    ) -> str:
        """Single-shot chat. Returns the first text block of the response."""
        response = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": m.role, "content": m.content} for m in messages],
        )
        return _first_text(response)

    # ---- extraction via forced tool use ---------------------------------

    def extract_with_tool(
        self,
        system: str,
        user_message: str,
        tool: dict[str, Any],
        max_tokens: int = 2048,
    ) -> dict[str, Any]:
        """Force the model to call ``tool`` and return its parsed input."""
        response = self._client.messages.create(
            model=self.extractor_model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_message}],
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool["name"]:
                return dict(block.input)
        raise RuntimeError(
            f"extract_with_tool: model did not call tool {tool['name']!r} "
            f"(stop_reason={response.stop_reason})"
        )

    # ---- rewrite --------------------------------------------------------

    def rewrite(
        self,
        system: str,
        user_message: str,
        max_tokens: int = 2048,
        temperature: float | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": self.corrector_model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_message}],
        }
        if temperature is not None:
            if _model_accepts_temperature(self.corrector_model):
                kwargs["temperature"] = temperature
            else:
                _log.warning(
                    "rewrite: dropping temperature=%s — model %s no longer "
                    "accepts it; cross-check will not see temperature variation",
                    temperature, self.corrector_model,
                )
        response = self._client.messages.create(**kwargs)
        return _first_text(response)


def _first_text(response: Any) -> str:
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise RuntimeError(
        f"response contained no text block (stop_reason={getattr(response, 'stop_reason', '?')})"
    )
