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

import contextlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Iterable

import anthropic

from src.cost import CallCost, cost_for_call

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

# Models the operator can pick from in the chat UI. The selected model
# drives every Anthropic-backed call this client makes (chat, extraction,
# rewrite). GLM is here for completeness so the UI can list it; the
# Pipeline routes the chat call to ``ModalGLMBackend`` when GLM is
# selected and keeps internal calls on the prior Anthropic model
# (GLM doesn't support tool use, so extraction etc. can't run on it).
ALLOWED_MODELS: tuple[str, ...] = (
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "glm-5.1",
)

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
        # Cost telemetry: every API call records a CallCost entry here.
        # Pipeline pops the list at end-of-turn to emit a turn_cost event.
        self._recorded_calls: list[CallCost] = []

    def pop_recorded_calls(self) -> list[CallCost]:
        """Return and clear the per-instance call ledger. Pipeline
        calls this at end-of-turn to aggregate cost into an event."""
        out = self._recorded_calls
        self._recorded_calls = []
        return out

    @contextlib.contextmanager
    def with_active_model(self, model: str | None):
        """Temporarily set chat / extractor / corrector model to ``model``
        for the duration of the block. Restores the previous values on
        exit. Pass ``None`` for a no-op (preserves current model state).

        GLM is a no-op on this client because GLM is dispatched at the
        Pipeline level (Modal chat backend) and doesn't run any
        Anthropic-side calls. Selecting GLM in the UI keeps the
        internal Anthropic-side calls on whatever model was previously
        active.

        Single-threaded use only — restoration is not thread-safe."""
        if model is None or model == "glm-5.1":
            yield
            return
        if model not in ALLOWED_MODELS:
            raise ValueError(
                f"unknown model {model!r}; allowed: {ALLOWED_MODELS}"
            )
        saved = (self.model, self.extractor_model, self.corrector_model)
        self.model = model
        self.extractor_model = model
        self.corrector_model = model
        try:
            yield
        finally:
            self.model, self.extractor_model, self.corrector_model = saved

    def _record_call(
        self, model: str, response: Any,
        purpose: str | None = None, duration_ms: float | None = None,
    ) -> None:
        """Capture token counts from an Anthropic response and stash a
        CallCost entry with purpose + duration. Best-effort — never
        crash the call on metric failure."""
        try:
            usage = getattr(response, "usage", None)
            if usage is None:
                return
            in_toks = int(getattr(usage, "input_tokens", 0) or 0)
            out_toks = int(getattr(usage, "output_tokens", 0) or 0)
            self._recorded_calls.append(
                cost_for_call(model, in_toks, out_toks,
                              purpose=purpose, duration_ms=duration_ms)
            )
        except Exception:
            pass

    def record_external_call(
        self, model: str, input_tokens: int, output_tokens: int,
        purpose: str | None = None, duration_ms: float | None = None,
    ) -> None:
        """Public hook for non-Anthropic chat backends (e.g. ModalGLMBackend)
        to feed their token usage + purpose into the per-turn cost
        ledger. Best-effort — never raises."""
        try:
            self._recorded_calls.append(
                cost_for_call(model, int(input_tokens), int(output_tokens),
                              purpose=purpose, duration_ms=duration_ms)
            )
        except Exception:
            pass

    # ---- chat ------------------------------------------------------------

    def chat(
        self,
        system: str,
        messages: Iterable[ChatMessage],
        max_tokens: int = 4096,
        purpose: str | None = "chat",
    ) -> str:
        """Single-shot chat. Returns the first text block of the response."""
        t0 = time.monotonic()
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
        self._record_call(self.model, response,
                          purpose=purpose, duration_ms=(time.monotonic() - t0) * 1000)
        return _first_text(response)

    # ---- extraction via forced tool use ---------------------------------

    def extract_with_tool(
        self,
        system: str,
        user_message: str,
        tool: dict[str, Any],
        max_tokens: int = 2048,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        """Force the model to call ``tool`` and return its parsed input."""
        t0 = time.monotonic()
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
        self._record_call(self.extractor_model, response,
                          purpose=purpose, duration_ms=(time.monotonic() - t0) * 1000)
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
        model: str | None = None,
        purpose: str | None = None,
    ) -> str:
        """Single-shot text-rewrite call.

        ``model`` overrides ``self.corrector_model`` for this call only.
        Used by the canonical-constants cross-check to force Sonnet 4.6
        (which still accepts ``temperature``) when the default is
        Opus 4.7 (which doesn't), preserving the variation signal."""
        chosen_model = model or self.corrector_model
        kwargs: dict[str, Any] = {
            "model": chosen_model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_message}],
        }
        if temperature is not None:
            if _model_accepts_temperature(chosen_model):
                kwargs["temperature"] = temperature
            else:
                _log.warning(
                    "rewrite: dropping temperature=%s — model %s no longer "
                    "accepts it; cross-check will not see temperature variation",
                    temperature, chosen_model,
                )
        t0 = time.monotonic()
        response = self._client.messages.create(**kwargs)
        self._record_call(chosen_model, response,
                          purpose=purpose, duration_ms=(time.monotonic() - t0) * 1000)
        return _first_text(response)


def _first_text(response: Any) -> str:
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise RuntimeError(
        f"response contained no text block (stop_reason={getattr(response, 'stop_reason', '?')})"
    )
