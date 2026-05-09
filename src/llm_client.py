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

# Default model when no per-purpose entry matches and no env var is set.
# Haiku 4.5 since the chat slot is locked to it (the UI no longer offers
# a model dropdown — see DEFAULT_MODEL_BY_PURPOSE['chat']) and Haiku is
# the right cost/quality tradeoff for the unknown-purpose fallback too.
DEFAULT_MODEL = "claude-haiku-4-5"

# Models that no longer accept the ``temperature`` parameter (Anthropic
# deprecated it for the reasoning-heavy Opus 4.7 line). Calls that pass
# temperature against one of these get a 400. We silently drop the
# parameter and log a warning rather than crash the request — the worst
# case is the canonical-constants cross-check loses its temperature-
# variation signal, which is documented in OBSERVATIONS.md.
_TEMPERATURE_DEPRECATED_PREFIXES = ("claude-opus-4-7",)

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
    # v0.14.1 — assistant-side extractor bumped to gpt-4.1 (full).
    # The assistant draft can carry 20+ claims per turn; pattern
    # misclassifications cascade into wrong routing → wrong verifier
    # → poisoned cache. The cost amplifier on this call is high
    # enough that the per-call delta vs mini is justified. Calibration
    # corpus at tests/calibration/extraction_corpus.jsonl pins the
    # accuracy floor. User-side extractor stays on mini (lower
    # volume + lower downstream-cost amplification).
    "extractor:user":      "gpt-4.1-mini",
    "extractor:assistant": "gpt-4.1",
    "router":              "gpt-4.1-mini",
    "cache_classify":      "gpt-4.1-nano",
    "cache_scoping":       "gpt-4.1-nano",   # legacy two-call path
    # v0.14.1 — bumped from nano → mini. The classifier picks one of 6
    # TTL bins for verdicts that will be cached for hours-to-immutable;
    # nano produced too many decade_stable picks on facts that should
    # have been months/days_stable, and the bias-toward-shorter-TTL
    # rule needs better instruction-following than nano provides. Cost
    # delta is small — the call only fires on never-before-cached
    # claims (canonical-key cache misses).
    "cache_stability":     "gpt-4.1-mini",
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

    def _record_call(
        self, model: str, response: Any,
        purpose: str | None = None, duration_ms: float | None = None,
    ) -> None:
        """Capture token counts from an Anthropic response and stash a
        CallCost entry with purpose + duration. Best-effort — never
        crash the call on metric failure.

        v0.9.x: reads the prompt-cache fields (``cache_creation_input_tokens``,
        ``cache_read_input_tokens``) so the ledger reflects what we actually
        get billed once a cached system prompt is in play. ``input_tokens``
        on the Anthropic usage object is the UNCACHED remainder — adding
        the cache fields gives the full input volume."""
        try:
            usage = getattr(response, "usage", None)
            if usage is None:
                return
            in_toks = int(getattr(usage, "input_tokens", 0) or 0)
            cc_toks = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
            cr_toks = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
            out_toks = int(getattr(usage, "output_tokens", 0) or 0)
            self._recorded_calls.append(
                cost_for_call(
                    model, in_toks, out_toks,
                    cache_creation_tokens=cc_toks,
                    cache_read_tokens=cr_toks,
                    purpose=purpose, duration_ms=duration_ms,
                )
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

        The chat purpose uses ``self.model`` — set at construction
        time from ``AEDOS_CHAT_MODEL`` / ``DEFAULT_MODEL`` (Haiku 4.5
        by default). The model dropdown was removed from the chat UI;
        operators who need to swap the chat model do so via the env
        var. Other purposes flow through DEFAULT_MODEL_BY_PURPOSE."""
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
