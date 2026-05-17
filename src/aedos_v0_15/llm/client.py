"""LLM client for Aedos v0.15.

Lifted from v0.14 src/llm_client.py with these changes:
- Cost tracking replaced with a lightweight call counter (used by walker budget).
- Model defaults updated for v0.15 (Haiku 4.5 for chat, gpt-4.1-mini for substrate
  oracle calls, gpt-4.1 for extraction).
- Added `complete` as an alias for `chat`.
- Removed dependency on src.cost.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

import anthropic

_log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5"

DEFAULT_MODEL_BY_PURPOSE: dict[str, str] = {
    "chat": "claude-haiku-4-5",
    "extractor:user": "gpt-4.1-mini",
    "extractor:assistant": "gpt-4.1",
    "substrate:predicate_translation": "gpt-4.1-mini",
    "substrate:subsumption": "gpt-4.1-mini",
    "substrate:predicate_distribution": "gpt-4.1-mini",
    "substrate:entity_resolution": "gpt-4.1-mini",
    "python_verifier": "gpt-4.1-mini",
    "walker": "gpt-4.1-mini",
}

_TEMPERATURE_DEPRECATED_PREFIXES = ("claude-opus-4-7",)


@dataclass
class CallRecord:
    purpose: Optional[str]
    model: str
    input_tokens: int
    output_tokens: int
    duration_ms: float


@dataclass
class ChatMessage:
    role: str
    content: str


def _resolve_purpose_model(purpose: Optional[str], fallback: str) -> str:
    if purpose:
        env = os.getenv(f"AEDOS_MODEL_{purpose}")
        if env:
            return env
        if purpose in DEFAULT_MODEL_BY_PURPOSE:
            return DEFAULT_MODEL_BY_PURPOSE[purpose]
    return fallback


def is_openai_model(model: str) -> bool:
    return model.startswith(("gpt-", "o1-", "o3-", "o4-"))


def _model_accepts_temperature(model: str) -> bool:
    return not any(model.startswith(p) for p in _TEMPERATURE_DEPRECATED_PREFIXES)


def _first_text(response: Any) -> str:
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise RuntimeError(
        f"response contained no text block (stop_reason={getattr(response, 'stop_reason', '?')})"
    )


class LLMClient:
    def __init__(
        self,
        anthropic_api_key: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        model: Optional[str] = None,
        _transport: Optional[Any] = None,
    ):
        self.model = model or os.getenv("AEDOS_CHAT_MODEL") or DEFAULT_MODEL
        self._call_records: list[CallRecord] = []
        self._transport = _transport

        if _transport is not None:
            self._anthropic_client: Any = None
            self._openai_raw: Any = None
            return

        api_key = anthropic_api_key or os.getenv("ANTHROPIC_API_KEY")
        if api_key:
            self._anthropic_client = anthropic.Anthropic(api_key=api_key)
        else:
            self._anthropic_client = None

        oai_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        self._openai_raw = oai_key
        self._openai_client: Any = None

    @property
    def openai(self) -> Any:
        if self._openai_client is None:
            try:
                import openai as _openai
                key = self._openai_raw or os.getenv("OPENAI_API_KEY")
                self._openai_client = _openai.OpenAI(api_key=key)
            except ImportError:
                raise RuntimeError("openai package not installed; cannot route to OpenAI models")
        return self._openai_client

    def pop_call_records(self) -> list[CallRecord]:
        out = self._call_records
        self._call_records = []
        return out

    def call_count_since_last_pop(self) -> int:
        return len(self._call_records)

    def _record(self, purpose: Optional[str], model: str, response: Any, duration_ms: float) -> None:
        try:
            usage = getattr(response, "usage", None)
            if usage is None:
                return
            self._call_records.append(CallRecord(
                purpose=purpose,
                model=model,
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
                duration_ms=duration_ms,
            ))
        except Exception:
            pass

    def _anthropic_chat(
        self,
        system: str,
        messages: Iterable[ChatMessage],
        model: str,
        max_tokens: int,
        purpose: Optional[str],
    ) -> str:
        if self._transport is not None:
            return self._transport.chat(system, list(messages), model=model, purpose=purpose)
        if self._anthropic_client is None:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        t0 = time.monotonic()
        response = self._anthropic_client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": m.role, "content": m.content} for m in messages],
        )
        self._record(purpose, model, response, (time.monotonic() - t0) * 1000)
        return _first_text(response)

    def _openai_chat(
        self,
        system: str,
        messages: Iterable[ChatMessage],
        model: str,
        max_tokens: int,
        purpose: Optional[str],
    ) -> str:
        if self._transport is not None:
            return self._transport.chat(system, list(messages), model=model, purpose=purpose)
        msgs = [{"role": "system", "content": system}]
        msgs += [{"role": m.role, "content": m.content} for m in messages]
        t0 = time.monotonic()
        resp = self.openai.chat.completions.create(model=model, messages=msgs, max_tokens=max_tokens)
        duration_ms = (time.monotonic() - t0) * 1000
        text = resp.choices[0].message.content or ""
        # fake usage object for recording
        class _U:
            input_tokens = getattr(resp.usage, "prompt_tokens", 0)
            output_tokens = getattr(resp.usage, "completion_tokens", 0)
        self._record(purpose, model, type("_R", (), {"usage": _U()})(), duration_ms)
        return text

    def chat(
        self,
        system: str,
        messages: Iterable[ChatMessage],
        max_tokens: int = 4096,
        purpose: Optional[str] = "chat",
    ) -> str:
        target = self.model if purpose == "chat" else _resolve_purpose_model(purpose, self.model)
        if is_openai_model(target):
            return self._openai_chat(system, messages, target, max_tokens, purpose)
        return self._anthropic_chat(system, messages, target, max_tokens, purpose)

    def complete(
        self,
        system: str,
        messages: Iterable[ChatMessage],
        max_tokens: int = 4096,
        purpose: Optional[str] = None,
    ) -> str:
        return self.chat(system, messages, max_tokens=max_tokens, purpose=purpose)

    def chat_stream(
        self,
        system: str,
        messages: Iterable[ChatMessage],
        on_token: Callable[[str], None],
        max_tokens: int = 4096,
        purpose: Optional[str] = "chat",
    ) -> str:
        target = self.model if purpose == "chat" else _resolve_purpose_model(purpose, self.model)
        if self._transport is not None:
            text = self._transport.chat(system, list(messages), model=target, purpose=purpose)
            on_token(text)
            return text
        if is_openai_model(target):
            text = self._openai_chat(system, messages, target, max_tokens, purpose)
            on_token(text)
            return text
        if self._anthropic_client is None:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        t0 = time.monotonic()
        parts: list[str] = []
        with self._anthropic_client.messages.stream(
            model=target,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": m.role, "content": m.content} for m in messages],
        ) as stream:
            for delta in stream.text_stream:
                parts.append(delta)
                try:
                    on_token(delta)
                except Exception:
                    pass
            final = stream.get_final_message()
        self._record(purpose, target, final, (time.monotonic() - t0) * 1000)
        return "".join(parts)

    def extract_with_tool(
        self,
        system: str,
        user_message: str,
        tool: dict[str, Any],
        max_tokens: int = 2048,
        purpose: Optional[str] = None,
    ) -> dict[str, Any]:
        target = _resolve_purpose_model(purpose, self.model)
        if self._transport is not None:
            return self._transport.extract_with_tool(
                system, user_message, tool, model=target, purpose=purpose
            )
        if is_openai_model(target):
            # OpenAI function-calling path
            fn = {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", tool.get("parameters", {})),
                },
            }
            msgs = [
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ]
            t0 = time.monotonic()
            resp = self.openai.chat.completions.create(
                model=target,
                messages=msgs,
                tools=[fn],
                tool_choice={"type": "function", "function": {"name": tool["name"]}},
                max_tokens=max_tokens,
            )
            duration_ms = (time.monotonic() - t0) * 1000
            class _U:
                input_tokens = getattr(resp.usage, "prompt_tokens", 0)
                output_tokens = getattr(resp.usage, "completion_tokens", 0)
            self._record(purpose, target, type("_R", (), {"usage": _U()})(), duration_ms)
            import json as _json
            tc = resp.choices[0].message.tool_calls
            if tc:
                return _json.loads(tc[0].function.arguments)
            raise RuntimeError(f"extract_with_tool: no tool call in OpenAI response")
        if self._anthropic_client is None:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        t0 = time.monotonic()
        response = self._anthropic_client.messages.create(
            model=target,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_message}],
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
        )
        self._record(purpose, target, response, (time.monotonic() - t0) * 1000)
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == tool["name"]:
                return dict(block.input)
        raise RuntimeError(
            f"extract_with_tool: model did not call tool {tool['name']!r} "
            f"(stop_reason={response.stop_reason})"
        )

    def rewrite(
        self,
        system: str,
        user_message: str,
        max_tokens: int = 2048,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
        purpose: Optional[str] = None,
    ) -> str:
        target = model or _resolve_purpose_model(purpose, self.model)
        if self._transport is not None:
            return self._transport.chat(
                system,
                [ChatMessage(role="user", content=user_message)],
                model=target,
                purpose=purpose,
            )
        if is_openai_model(target):
            return self._openai_chat(
                system,
                [ChatMessage(role="user", content=user_message)],
                target,
                max_tokens,
                purpose,
            )
        if self._anthropic_client is None:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        kwargs: dict[str, Any] = {
            "model": target,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user_message}],
        }
        if temperature is not None:
            if _model_accepts_temperature(target):
                kwargs["temperature"] = temperature
            else:
                _log.warning("rewrite: dropping temperature=%s — model %s deprecated it", temperature, target)
        t0 = time.monotonic()
        response = self._anthropic_client.messages.create(**kwargs)
        self._record(purpose, target, response, (time.monotonic() - t0) * 1000)
        return _first_text(response)
