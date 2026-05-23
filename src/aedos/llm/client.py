"""LLM client for Aedos v0.15.

Lifted from v0.14 src/llm_client.py with these changes:
- Cost tracking replaced with a lightweight call counter (used by walker budget).
- Model defaults updated for v0.15.
- Added `complete` as an alias for `chat`.
- Removed dependency on src.cost.

Phase E1 — per-purpose, per-provider routing. `DEFAULT_MODEL_BY_PURPOSE` is a
dict of dicts: each purpose carries a `model`, a `base_url` (None → the native
Anthropic SDK; a URL → the OpenAI-compatible SDK pointed at that endpoint —
OpenAI itself, or OpenRouter, or any OpenAI-API-compatible host), and an
`api_key_env_var`. Provider routing is now explicit (the `base_url`), not
inferred from the model-name prefix.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

import anthropic

_log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5"

# Endpoint shorthands. A purpose's `base_url` of None routes to the native
# Anthropic SDK; a URL routes to the OpenAI-compatible SDK.
_ANTHROPIC = {"base_url": None, "api_key_env_var": "ANTHROPIC_API_KEY"}
_OPENAI = {"base_url": "https://api.openai.com/v1", "api_key_env_var": "OPENAI_API_KEY"}
# OpenRouter (OpenAI-API-compatible) — used by the Phase E comparison and, after
# Phase E5, by whichever purposes the operator migrates to open-weight models.
_OPENROUTER = {"base_url": "https://openrouter.ai/api/v1", "api_key_env_var": "OPENROUTER_API_KEY"}

# Per-purpose routing. Each value is {"model", "base_url", "api_key_env_var"}.
# Phase E1 keeps the v0.15 model assignments unchanged — only the config *shape*
# and the routing *mechanism* change here; the open-weight migration of the
# model values is Phase E5, after the comparison.
#
# Phase F2 (F-009 closure): the substrate / verifier call-site purpose strings
# now match the keys in this table. Before F2 the call sites used names like
# `subsumption_generation` / `python_code_generation` that did not match the
# table, so four call types fell through to the chat-default model in
# deployment — see docs/phase_F/deployment_readiness_audit.md F-009 for the
# project-level account.
#
# `extractor:assistant` is architecturally distinct from `extractor:user`
# for asserting-party reasons (the asserting party for an assistant-extracted
# claim is the assistant / deployment, not the user; see architecture §4.1).
# Pinned here even though no call site currently uses it, so the architectural
# distinction is preserved in configuration.
DEFAULT_MODEL_BY_PURPOSE: dict[str, dict] = {
    "chat":                             {"model": "claude-haiku-4-5", **_ANTHROPIC},
    # Phase E3 (2026-05-23) — extractor purposes on claude-haiku-4-5 with the
    # v5 prompt (100% on the cleaned extraction corpus; see
    # docs/phase_E_report.md).
    "extractor:user":                   {"model": "claude-haiku-4-5", **_ANTHROPIC},
    "extractor:assistant":              {"model": "claude-haiku-4-5", **_ANTHROPIC},
    # Phase E5 (2026-05-23) — per-component selection from candidate × corpus
    # measurement + prompt iteration on the proposed-winner model. See
    # docs/phase_E_v2_report.md for the per-component data, iteration logs,
    # and architectural-ceiling interpretation. Three of five components
    # clear their calibration thresholds at this configuration; the two that
    # don't (entity_resolution, walker) are bounded by D47 / D5 / D16/D23,
    # not by the model.
    "substrate:predicate_translation":  {"model": "claude-haiku-4-5", **_ANTHROPIC},
    "substrate:subsumption":            {"model": "qwen/qwen3-next-80b-a3b-instruct", **_OPENROUTER},
    "substrate:predicate_distribution": {"model": "qwen/qwen3-next-80b-a3b-instruct", **_OPENROUTER},
    "substrate:entity_resolution":      {"model": "qwen/qwen3-next-80b-a3b-instruct", **_OPENROUTER},
    # Phase E (python_verifier soundness winner) — Devstral Small 1.1 had 0
    # false-verifieds on python_verification_corpus where five other
    # candidates each produced 1. See docs/phase_E_report.md.
    "python_verifier":                  {"model": "mistralai/devstral-small", **_OPENROUTER},
    # Phase H D47 step 2 — Wikipedia normalizer Stage 2 selection. Bounded
    # closed-set selection over disambiguation-page candidates with explicit
    # abstention. Haiku 4.5 (Anthropic native) — small, fast, reliable tool
    # calls; matches the extractor model.
    "layer1:entity_normalization":      {"model": "claude-haiku-4-5", **_ANTHROPIC},
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


def is_openai_model(model: str) -> bool:
    """Heuristic: does this model string name an OpenAI model? Used only for
    provider inference on the legacy single-variable overrides (`AEDOS_MODEL_*`,
    `AEDOS_CHAT_MODEL`, `rewrite(model=...)`). Default and full-override routing
    is explicit via `base_url` and does not consult this."""
    return model.startswith(("gpt-", "o1-", "o3-", "o4-"))


def _config_for_model(model: str) -> dict:
    """Provider-inferred routing config for a bare model string. Used for the
    legacy single-variable overrides, which name a model but no endpoint."""
    endpoint = _OPENAI if is_openai_model(model) else _ANTHROPIC
    return {"model": model, **endpoint}


def _purpose_override(purpose: Optional[str]) -> Optional[dict]:
    """A whole-run routing override from the `AEDOS_OVERRIDE_MODEL_BY_PURPOSE`
    env var (JSON: purpose → config). The Phase E comparison harness uses this
    to drive every internal purpose with one candidate model. A `"*"` key
    applies to every purpose except `chat` (the chat slot is never overridden).
    An entry may be a full `{model, base_url, api_key_env_var}` config or a bare
    model string (provider then inferred)."""
    raw = os.getenv("AEDOS_OVERRIDE_MODEL_BY_PURPOSE")
    if not raw or not purpose:
        return None
    try:
        overrides = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(overrides, dict):
        return None
    entry = overrides.get(purpose)
    if entry is None and purpose != "chat":
        entry = overrides.get("*")
    if entry is None:
        return None
    if isinstance(entry, str):
        return _config_for_model(entry)
    if isinstance(entry, dict) and entry.get("model"):
        if "base_url" not in entry:  # model only → infer provider
            return _config_for_model(entry["model"])
        return {
            "model": entry["model"],
            "base_url": entry["base_url"],
            "api_key_env_var": entry.get("api_key_env_var", "OPENROUTER_API_KEY"),
            # extra_body: provider-specific request fields passed straight
            # through to the OpenAI-compatible call (e.g. OpenRouter's
            # `reasoning` toggle). Optional; None for ordinary configs.
            "extra_body": entry.get("extra_body"),
        }
    return None


def _resolve_purpose_config(purpose: Optional[str], fallback_model: str) -> dict:
    """Resolve a purpose to a full routing config: the
    `AEDOS_OVERRIDE_MODEL_BY_PURPOSE` whole-run override → `AEDOS_MODEL_<purpose>`
    (a model-only override, provider inferred) → the built-in per-purpose
    default → a provider-inferred config for `fallback_model`."""
    override = _purpose_override(purpose)
    if override is not None:
        return override
    if purpose:
        env_model = os.getenv(f"AEDOS_MODEL_{purpose}")
        if env_model:
            return _config_for_model(env_model)
        if purpose in DEFAULT_MODEL_BY_PURPOSE:
            return dict(DEFAULT_MODEL_BY_PURPOSE[purpose])
    return _config_for_model(fallback_model)


def _resolve_purpose_model(purpose: Optional[str], fallback: str) -> str:
    """Backward-compatible model-string resolver — returns just the model id."""
    return _resolve_purpose_config(purpose, fallback)["model"]


def _model_accepts_temperature(model: str) -> bool:
    return not any(model.startswith(p) for p in _TEMPERATURE_DEPRECATED_PREFIXES)


def _first_text(response: Any) -> str:
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise RuntimeError(
        f"response contained no text block (stop_reason={getattr(response, 'stop_reason', '?')})"
    )


def _attach_raw_response(exc: BaseException, resp: Any) -> None:
    """Attach the raw SDK response (best-effort serialized) to an exception
    raised while parsing it. The Phase E diagnostic transcript wrapper reads
    `_raw_response` so the response shape is captured for failed calls."""
    if resp is None or hasattr(exc, "_raw_response"):
        return
    try:
        if hasattr(resp, "model_dump"):
            exc._raw_response = resp.model_dump()  # pydantic v2
        else:
            exc._raw_response = repr(resp)
    except Exception:
        exc._raw_response = repr(resp)


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
        self._constructor_openai_key = openai_api_key
        # OpenAI-compatible clients, cached per base_url (OpenAI, OpenRouter, …).
        self._openai_clients: dict[str, Any] = {}

        if _transport is not None:
            self._anthropic_client: Any = None
            return

        api_key = anthropic_api_key or os.getenv("ANTHROPIC_API_KEY")
        if api_key:
            self._anthropic_client = anthropic.Anthropic(api_key=api_key)
        else:
            self._anthropic_client = None

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _cfg(self, purpose: Optional[str]) -> dict:
        """Routing config for a call. The chat slot (and an untagged call)
        follows `self.model` (constructor / `AEDOS_CHAT_MODEL` / default), with
        the provider inferred; every other purpose resolves via the per-purpose
        table."""
        if purpose in (None, "chat"):
            return _config_for_model(self.model)
        return _resolve_purpose_config(purpose, self.model)

    def _openai_client(self, base_url: Optional[str], api_key_env_var: str) -> Any:
        """Return (cached) an OpenAI-compatible client for the given endpoint.
        Raises a clear error if the endpoint's API key env var is unset."""
        cache_key = base_url or "openai-default"
        client = self._openai_clients.get(cache_key)
        if client is None:
            try:
                import openai as _openai
            except ImportError:
                raise RuntimeError(
                    "openai package not installed; cannot route to an "
                    "OpenAI-compatible endpoint"
                )
            key = os.getenv(api_key_env_var)
            if not key and api_key_env_var == "OPENAI_API_KEY":
                key = self._constructor_openai_key
            if not key:
                raise RuntimeError(
                    f"LLMClient: API key env var {api_key_env_var!r} is not set "
                    f"(required to reach {base_url or 'the OpenAI API'})"
                )
            client = _openai.OpenAI(api_key=key, base_url=base_url)
            self._openai_clients[cache_key] = client
        return client

    # ------------------------------------------------------------------
    # Call records
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Anthropic / OpenAI-compatible primitives
    # ------------------------------------------------------------------

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
        cfg: dict,
        max_tokens: int,
        purpose: Optional[str],
    ) -> str:
        if self._transport is not None:
            return self._transport.chat(system, list(messages), model=cfg["model"], purpose=purpose)
        client = self._openai_client(cfg["base_url"], cfg["api_key_env_var"])
        msgs = [{"role": "system", "content": system}]
        msgs += [{"role": m.role, "content": m.content} for m in messages]
        create_kwargs: dict[str, Any] = {
            "model": cfg["model"], "messages": msgs, "max_tokens": max_tokens,
        }
        if cfg.get("extra_body"):
            create_kwargs["extra_body"] = cfg["extra_body"]
        resp = None
        try:
            t0 = time.monotonic()
            resp = client.chat.completions.create(**create_kwargs)
            duration_ms = (time.monotonic() - t0) * 1000
            text = resp.choices[0].message.content or ""
            class _U:
                input_tokens = getattr(resp.usage, "prompt_tokens", 0)
                output_tokens = getattr(resp.usage, "completion_tokens", 0)
            self._record(purpose, cfg["model"], type("_R", (), {"usage": _U()})(), duration_ms)
            return text
        except Exception as exc:
            _attach_raw_response(exc, resp)
            raise

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def chat(
        self,
        system: str,
        messages: Iterable[ChatMessage],
        max_tokens: int = 4096,
        purpose: Optional[str] = "chat",
    ) -> str:
        cfg = self._cfg(purpose)
        if cfg["base_url"] is not None:
            return self._openai_chat(system, messages, cfg, max_tokens, purpose)
        return self._anthropic_chat(system, messages, cfg["model"], max_tokens, purpose)

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
        cfg = self._cfg(purpose)
        if self._transport is not None:
            text = self._transport.chat(system, list(messages), model=cfg["model"], purpose=purpose)
            on_token(text)
            return text
        if cfg["base_url"] is not None:
            text = self._openai_chat(system, messages, cfg, max_tokens, purpose)
            on_token(text)
            return text
        if self._anthropic_client is None:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        t0 = time.monotonic()
        parts: list[str] = []
        with self._anthropic_client.messages.stream(
            model=cfg["model"],
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
        self._record(purpose, cfg["model"], final, (time.monotonic() - t0) * 1000)
        return "".join(parts)

    def extract_with_tool(
        self,
        system: str,
        user_message: str,
        tool: dict[str, Any],
        # 8192 (vs the OpenAI-compatible 2048 default) accommodates
        # reasoning-heavy candidates. Qwen 3.6's extraction surfaced
        # budget-exhaustion failures at 2048 — reasoning filled the budget
        # before the tool call was emitted on ~14% of cases. Applied
        # uniformly so all candidates measure capability under their natural
        # reasoning budget, not under an artificial 2048-token ceiling.
        max_tokens: int = 8192,
        purpose: Optional[str] = None,
    ) -> dict[str, Any]:
        cfg = self._cfg(purpose)
        if self._transport is not None:
            return self._transport.extract_with_tool(
                system, user_message, tool, model=cfg["model"], purpose=purpose
            )
        if cfg["base_url"] is not None:
            # OpenAI-compatible function-calling path (OpenAI, OpenRouter, …).
            client = self._openai_client(cfg["base_url"], cfg["api_key_env_var"])
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
            create_kwargs: dict[str, Any] = {
                "model": cfg["model"],
                "messages": msgs,
                "tools": [fn],
                "tool_choice": {"type": "function", "function": {"name": tool["name"]}},
                "max_tokens": max_tokens,
            }
            if cfg.get("extra_body"):
                create_kwargs["extra_body"] = cfg["extra_body"]
            resp = None
            try:
                t0 = time.monotonic()
                resp = client.chat.completions.create(**create_kwargs)
                duration_ms = (time.monotonic() - t0) * 1000
                class _U:
                    input_tokens = getattr(resp.usage, "prompt_tokens", 0)
                    output_tokens = getattr(resp.usage, "completion_tokens", 0)
                self._record(purpose, cfg["model"], type("_R", (), {"usage": _U()})(), duration_ms)
                tc = resp.choices[0].message.tool_calls
                if tc:
                    return json.loads(tc[0].function.arguments)
                raise RuntimeError("extract_with_tool: no tool call in OpenAI-compatible response")
            except Exception as exc:
                _attach_raw_response(exc, resp)
                raise
        if self._anthropic_client is None:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        t0 = time.monotonic()
        response = self._anthropic_client.messages.create(
            model=cfg["model"],
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_message}],
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
        )
        self._record(purpose, cfg["model"], response, (time.monotonic() - t0) * 1000)
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
        cfg = _config_for_model(model) if model else self._cfg(purpose)
        if self._transport is not None:
            return self._transport.chat(
                system,
                [ChatMessage(role="user", content=user_message)],
                model=cfg["model"],
                purpose=purpose,
            )
        if cfg["base_url"] is not None:
            return self._openai_chat(
                system, [ChatMessage(role="user", content=user_message)], cfg, max_tokens, purpose,
            )
        if self._anthropic_client is None:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        kwargs: dict[str, Any] = {
            "model": cfg["model"],
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user_message}],
        }
        if temperature is not None:
            if _model_accepts_temperature(cfg["model"]):
                kwargs["temperature"] = temperature
            else:
                _log.warning(
                    "rewrite: dropping temperature=%s — model %s deprecated it",
                    temperature, cfg["model"],
                )
        t0 = time.monotonic()
        response = self._anthropic_client.messages.create(**kwargs)
        self._record(purpose, cfg["model"], response, (time.monotonic() - t0) * 1000)
        return _first_text(response)
