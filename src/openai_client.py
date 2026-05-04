"""OpenAI client wrapper (v0.8.0).

Mirrors the three-method shape of LLMClient (Anthropic) so the
per-purpose dispatcher in llm_client.py can route a call to either
provider transparently. Records cost into a shared ledger when
provided so per-turn cost telemetry stays in one place.

OpenAI specifics:
  * Tool use uses the ``tools=[{"type": "function", ...}]`` format
    instead of Anthropic's flat ``tools=[{...}]``. Tool input arrives
    as a JSON-encoded string in ``tool_calls[0].function.arguments``,
    not as a structured dict.
  * Prompt caching is automatic on the gpt-4o / gpt-4.1 family
    (no opt-in needed). We don't pass any cache_control field —
    OpenAI ignores it but the field is non-standard.
  * The system prompt rides inside the ``messages`` array as a
    ``role: system`` message, not in a separate ``system`` parameter.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Iterable

import openai

from src.cost import CallCost, cost_for_call

_log = logging.getLogger(__name__)


def _call_cost_from_openai_usage(
    model: str, usage: Any, *, purpose: str | None, duration_ms: float,
) -> CallCost:
    """Build a CallCost from an OpenAI usage object.

    OpenAI reports ``prompt_tokens`` as the *total* (uncached + cached);
    the cached portion lives in ``usage.prompt_tokens_details.cached_tokens``.
    Cost accounting needs them split so the 50%-cached-input discount
    actually gets applied — passing the full ``prompt_tokens`` to
    ``cost_for_call`` would bill cached tokens at the full rate.
    """
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    out_toks = int(getattr(usage, "completion_tokens", 0) or 0)
    cached = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = int(getattr(details, "cached_tokens", 0) or 0)
    uncached = max(prompt - cached, 0)
    return cost_for_call(
        model, uncached, out_toks,
        cache_read_tokens=cached,
        purpose=purpose, duration_ms=duration_ms,
    )


class OpenAIClient:
    """OpenAI-side counterpart to ``LLMClient``. Same external API
    shape (chat / extract_with_tool / rewrite) so the dispatcher
    doesn't care which side a call goes to.

    Cost recording is delegated to a caller-provided callable so
    every recorded call lands on the same per-turn ledger as the
    Anthropic-side calls (the LLMClient.dispatcher passes its own
    ``_recorded_calls.append`` here)."""

    def __init__(
        self,
        api_key: str | None = None,
        cost_recorder: Any | None = None,
    ):
        api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY not set. Add it to .env to enable OpenAI "
                "models. (Pure-Anthropic deployments don't need it.)"
            )
        self._client = openai.OpenAI(api_key=api_key)
        # Cost recorder: a callable(CallCost) that the dispatcher
        # supplies. Lets the OpenAI calls land on the same ledger as
        # the Anthropic calls without each side knowing about the other.
        self._cost_recorder = cost_recorder

    def _record(
        self, model: str, response: Any, *,
        purpose: str | None, duration_ms: float,
    ) -> None:
        if self._cost_recorder is None:
            return
        try:
            usage = getattr(response, "usage", None)
            if usage is None:
                return
            self._cost_recorder(_call_cost_from_openai_usage(
                model, usage, purpose=purpose, duration_ms=duration_ms,
            ))
        except Exception:
            pass

    # ---- chat -------------------------------------------------------

    def chat(
        self,
        system: str,
        messages: Iterable[Any],   # list[ChatMessage]; duck-typed
        max_tokens: int = 4096,
        purpose: str | None = "chat",
        model: str = "gpt-4.1-mini",
    ) -> str:
        oa_messages = [{"role": "system", "content": system}]
        for m in messages:
            oa_messages.append({"role": m.role, "content": m.content})
        t0 = time.monotonic()
        response = self._client.chat.completions.create(
            model=model,
            messages=oa_messages,
            max_completion_tokens=max_tokens,
        )
        self._record(model, response, purpose=purpose,
                     duration_ms=(time.monotonic() - t0) * 1000)
        choice = response.choices[0]
        return choice.message.content or ""

    # ---- chat_stream (v0.9.0) ---------------------------------------

    def chat_stream(
        self,
        system: str,
        messages: Iterable[Any],   # list[ChatMessage]; duck-typed
        on_token: Any,
        max_tokens: int = 4096,
        purpose: str | None = "chat",
        model: str = "gpt-4.1-mini",
    ) -> str:
        """Streaming variant of chat. Calls ``on_token(text_delta)`` for
        each chunk; returns the full accumulated text.

        OpenAI streaming requires ``stream_options={"include_usage":
        True}`` to get usage metrics in the final chunk; without it
        the cost ledger entry would be a zero-token sentinel.
        ``on_token`` exceptions are silently swallowed."""
        oa_messages = [{"role": "system", "content": system}]
        for m in messages:
            oa_messages.append({"role": m.role, "content": m.content})
        t0 = time.monotonic()
        stream = self._client.chat.completions.create(
            model=model,
            messages=oa_messages,
            max_completion_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        full_text_parts: list[str] = []
        final_usage: Any = None
        for chunk in stream:
            # Each chunk has zero or one choices with a delta.
            choices = getattr(chunk, "choices", None) or []
            if choices:
                delta = getattr(choices[0], "delta", None)
                if delta is not None:
                    content = getattr(delta, "content", None)
                    if content:
                        full_text_parts.append(content)
                        try:
                            on_token(content)
                        except Exception:
                            pass
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                final_usage = usage
        # Record cost from the usage chunk that arrived last. Pull
        # cached_tokens out of prompt_tokens_details so the cache
        # discount gets applied (otherwise OpenAI's prompt_tokens
        # double-bills the cached portion at the full input rate).
        if self._cost_recorder is not None and final_usage is not None:
            try:
                self._cost_recorder(_call_cost_from_openai_usage(
                    model, final_usage,
                    purpose=purpose,
                    duration_ms=(time.monotonic() - t0) * 1000,
                ))
            except Exception:
                pass
        return "".join(full_text_parts)

    # ---- extract_with_tool ------------------------------------------

    def extract_with_tool(
        self,
        system: str,
        user_message: str,
        tool: dict[str, Any],
        max_tokens: int = 2048,
        purpose: str | None = None,
        model: str = "gpt-4.1-mini",
    ) -> dict[str, Any]:
        """Forced function call. OpenAI's tool schema has slightly
        different shape than Anthropic's: a ``type: function`` wrapper
        and a ``parameters`` field instead of ``input_schema``.

        Returns the tool's parsed arguments dict. Raises if the model
        didn't call the function (mirrors LLMClient.extract_with_tool).
        """
        oa_tool = {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
            },
        }
        t0 = time.monotonic()
        response = self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
            tools=[oa_tool],
            tool_choice={
                "type": "function",
                "function": {"name": tool["name"]},
            },
            max_completion_tokens=max_tokens,
        )
        self._record(model, response, purpose=purpose,
                     duration_ms=(time.monotonic() - t0) * 1000)
        choice = response.choices[0]
        tool_calls = (choice.message.tool_calls or [])
        for tc in tool_calls:
            if tc.function.name == tool["name"]:
                try:
                    return json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"extract_with_tool: OpenAI returned malformed "
                        f"tool arguments for {tool['name']!r}: "
                        f"{tc.function.arguments!r} ({exc})"
                    )
        raise RuntimeError(
            f"extract_with_tool: OpenAI model {model!r} did not call "
            f"tool {tool['name']!r} (finish_reason={choice.finish_reason})"
        )

    # ---- rewrite ----------------------------------------------------

    def rewrite(
        self,
        system: str,
        user_message: str,
        max_tokens: int = 2048,
        temperature: float | None = None,
        model: str = "gpt-4.1-mini",
        purpose: str | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
            "max_completion_tokens": max_tokens,
        }
        # OpenAI's gpt-4o-and-newer reasoning lineage doesn't accept
        # temperature; older models do. Pass it only when meaningful.
        if temperature is not None:
            kwargs["temperature"] = temperature
        t0 = time.monotonic()
        response = self._client.chat.completions.create(**kwargs)
        self._record(model, response, purpose=purpose,
                     duration_ms=(time.monotonic() - t0) * 1000)
        return response.choices[0].message.content or ""
