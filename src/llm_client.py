"""LLM dispatcher (v0.8.0 — multi-provider).

Three entry points cover every call the pipeline makes:

* ``chat(...)`` — assistant turn generation, returns plain text.
* ``extract_with_tool(...)`` — forces the model to call a specified tool and
  returns its parsed input dict. Used for claim extraction so we never parse
  freeform JSON out of a prose response.
* ``rewrite(...)`` — a text transform call (correction rewrite).

Each call carries a ``purpose`` tag (extractor:user, router, judge, etc.).
The dispatcher resolves purpose → model → provider via
DEFAULT_MODEL_BY_PURPOSE (env-var overridable) and forwards to either the
Anthropic SDK (claude-* models) or the OpenAI SDK (gpt-* models). Cost
recording lands on a single per-instance ledger regardless of provider.

Prompt caching:
  * Anthropic: explicit ``cache_control: ephemeral`` on stable system
    prompts (extractor + chat already; rewrite added in v0.7.16).
  * OpenAI: automatic on the gpt-4o / gpt-4.1 family — no opt-in needed.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

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

# Models the operator can pick from in the chat UI. The selected
# model drives the CHAT purpose only — internal calls (extractor,
# router, judge, etc.) follow DEFAULT_MODEL_BY_PURPOSE so the cheap
# OpenAI-mini-class models run those even when the operator picks
# Opus 4.7 for the chat-side hallucination test.
ALLOWED_MODELS: tuple[str, ...] = (
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    # OpenAI options for the chat slot.
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4o",
    "gpt-4o-mini",
)


# v0.8.0 — per-purpose model routing. EVERY internal LLM call carries
# a ``purpose`` tag; the dispatcher resolves it through this map to a
# concrete model name. The model name's prefix (claude-* vs gpt-*)
# determines which provider's SDK handles the call. Operator can
# override any entry via env:
#     AEDOS_MODEL_extractor:user=gpt-4.1-mini
#     AEDOS_MODEL_router=claude-haiku-4-5
#     AEDOS_MODEL_cache_classify=gpt-4.1-nano
#
# Defaults reflect Scenario D from the v0.7.16 cost discussion:
# cheap OpenAI mini-class for everything internal, Anthropic Haiku
# default for the chat slot (operator-overridable in the UI).
DEFAULT_MODEL_BY_PURPOSE: dict[str, str] = {
    "chat":                "claude-haiku-4-5",
    "extractor:user":      "gpt-4.1-mini",
    "extractor:assistant": "gpt-4.1-mini",
    "router":              "gpt-4.1-mini",
    "cache_classify":      "gpt-4.1-nano",
    "cache_scoping":       "gpt-4.1-nano",   # legacy two-call path
    "cache_stability":     "gpt-4.1-nano",   # legacy two-call path
    "prompt_builder":      "gpt-4.1-mini",
    "code_writer":         "gpt-4.1-mini",
    "retrieval_judge":     "gpt-4.1-mini",
    "corrector":           "gpt-4.1-mini",
}


def _resolve_purpose_model(purpose: str | None, fallback: str) -> str:
    """purpose → concrete model name. Env override wins; then the
    DEFAULT_MODEL_BY_PURPOSE entry; then ``fallback``."""
    if purpose:
        env = os.getenv(f"AEDOS_MODEL_{purpose}")
        if env:
            return env
        if purpose in DEFAULT_MODEL_BY_PURPOSE:
            return DEFAULT_MODEL_BY_PURPOSE[purpose]
    return fallback


def is_openai_model(model: str) -> bool:
    return (model.startswith("gpt-")
            or model.startswith("o1-") or model.startswith("o3-")
            or model.startswith("o4-"))


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
        # v0.8.0 — lazy OpenAI client. Built on first OpenAI-routed
        # call. None means uninitialized; the property below builds it
        # on demand. Pure-Anthropic deployments never instantiate it.
        self._openai_client: Any | None = None

    @property
    def openai(self) -> Any:
        """Lazily-built OpenAI client wrapper. Routes its cost
        recordings into this instance's _recorded_calls so the
        per-turn ledger stays unified across providers."""
        if self._openai_client is None:
            from src.openai_client import OpenAIClient
            self._openai_client = OpenAIClient(
                cost_recorder=self._recorded_calls.append,
            )
        return self._openai_client

    def pop_recorded_calls(self) -> list[CallCost]:
        """Return and clear the per-instance call ledger. Pipeline
        calls this at end-of-turn to aggregate cost into an event."""
        out = self._recorded_calls
        self._recorded_calls = []
        return out

    @contextlib.contextmanager
    def with_active_model(self, model: str | None):
        """Temporarily set the CHAT model for the duration of the
        block. v0.8.0 narrowing: extractor/corrector are NO LONGER
        switched — those follow DEFAULT_MODEL_BY_PURPOSE so the
        operator picking Opus 4.7 to test the chat side doesn't
        also blow up internal-call cost. Restores on exit.

        Single-threaded use only — restoration is not thread-safe."""
        if model is None:
            yield
            return
        if model not in ALLOWED_MODELS:
            raise ValueError(
                f"unknown model {model!r}; allowed: {ALLOWED_MODELS}"
            )
        saved = self.model
        self.model = model
        try:
            yield
        finally:
            self.model = saved

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
        """Public hook for non-Anthropic chat backends to feed their
        token usage + purpose into the per-turn cost ledger. Currently
        unused (post-v0.7.15 the only chat backend is Anthropic, which
        records cost natively); kept as a stable extension point for
        future backends. Best-effort — never raises."""
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
        """Single-shot chat. Returns the first text block of the response.

        v0.8.0 dispatch: the chat purpose ALWAYS uses
        ``self.model`` — operator selection (via with_active_model
        in the per-turn pipeline) is the source of truth here. We
        deliberately bypass DEFAULT_MODEL_BY_PURPOSE for "chat"
        because the chat slot is the model under test for hallucination
        and the operator is in charge of it. Other purposes still
        flow through the per-purpose router."""
        target_model = (
            self.model if purpose == "chat"
            else _resolve_purpose_model(purpose, self.model)
        )
        if is_openai_model(target_model):
            return self.openai.chat(
                system, messages, max_tokens=max_tokens,
                purpose=purpose, model=target_model,
            )
        t0 = time.monotonic()
        response = self._client.messages.create(
            model=target_model,
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
        self._record_call(target_model, response,
                          purpose=purpose, duration_ms=(time.monotonic() - t0) * 1000)
        return _first_text(response)

    # ---- streaming chat (v0.9.0) ----------------------------------------

    def chat_stream(
        self,
        system: str,
        messages: Iterable[ChatMessage],
        on_token: Any,
        max_tokens: int = 4096,
        purpose: str | None = "chat",
    ) -> str:
        """Streaming variant of chat. Calls ``on_token(text_delta)`` for
        each text fragment as the response arrives, then returns the
        full accumulated text. Used by the live chat-draft path so the
        UI can render the assistant's response token-by-token instead
        of waiting for the full response.

        Same dispatch as ``chat`` — the chat purpose always uses
        ``self.model``. gpt-* targets route to OpenAI's streaming
        endpoint; claude-* targets use Anthropic's
        ``client.messages.stream()``.

        ``on_token`` exceptions are silently swallowed so a buggy
        subscriber can't break the chat call. Returns the full text
        regardless of whether on_token errored on individual tokens.
        """
        target_model = (
            self.model if purpose == "chat"
            else _resolve_purpose_model(purpose, self.model)
        )
        if is_openai_model(target_model):
            return self.openai.chat_stream(
                system, messages, on_token=on_token,
                max_tokens=max_tokens,
                purpose=purpose, model=target_model,
            )
        t0 = time.monotonic()
        full_text_parts: list[str] = []
        with self._client.messages.stream(
            model=target_model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": m.role, "content": m.content} for m in messages],
        ) as stream:
            for delta in stream.text_stream:
                full_text_parts.append(delta)
                try:
                    on_token(delta)
                except Exception:
                    pass
            final_message = stream.get_final_message()
        self._record_call(target_model, final_message,
                          purpose=purpose, duration_ms=(time.monotonic() - t0) * 1000)
        return "".join(full_text_parts)

    # ---- extraction via forced tool use ---------------------------------

    def extract_with_tool(
        self,
        system: str,
        user_message: str,
        tool: dict[str, Any],
        max_tokens: int = 2048,
        purpose: str | None = None,
    ) -> dict[str, Any]:
        """Force the model to call ``tool`` and return its parsed input.

        v0.8.0 dispatch: purpose resolves through DEFAULT_MODEL_BY_PURPOSE
        (or AEDOS_MODEL_<purpose> env override). gpt-* targets route
        to the OpenAI side; claude-* targets stay on Anthropic."""
        target_model = _resolve_purpose_model(purpose, self.extractor_model)
        if is_openai_model(target_model):
            return self.openai.extract_with_tool(
                system, user_message, tool, max_tokens=max_tokens,
                purpose=purpose, model=target_model,
            )
        t0 = time.monotonic()
        response = self._client.messages.create(
            model=target_model,
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
        self._record_call(target_model, response,
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

        Resolution order for the target model:
          1. ``model`` parameter (explicit override; preserves the
             canonical-constants cross-check's force-Sonnet behavior).
          2. v0.8.0 purpose → DEFAULT_MODEL_BY_PURPOSE / env override.
          3. self.corrector_model (Anthropic legacy default).

        gpt-* targets dispatch to the OpenAI side; claude-* stay on
        Anthropic with the cache_control: ephemeral system block."""
        explicit = model
        chosen_model = explicit or _resolve_purpose_model(purpose, self.corrector_model)
        if is_openai_model(chosen_model):
            return self.openai.rewrite(
                system, user_message,
                max_tokens=max_tokens, temperature=temperature,
                model=chosen_model, purpose=purpose,
            )
        kwargs: dict[str, Any] = {
            "model": chosen_model,
            "max_tokens": max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
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
