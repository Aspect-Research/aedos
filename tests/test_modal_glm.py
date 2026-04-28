"""Tests for the Modal-hosted GLM-5.1-FP8 chat backend.

The HTTP layer is mocked with a stub httpx.Client so tests are
hermetic. A real-API smoke test lives in scripts/smoke_test_glm.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.fact_store import FactStore
from src.llm_client import ChatMessage
from src.llm_clients.modal_glm import (
    MODAL_ENDPOINT,
    MODAL_MODEL,
    ModalAuthError,
    ModalGLMBackend,
    ModalRateLimitError,
    ModalResponseError,
    ModalServerError,
    ModalTimeoutError,
)


@dataclass
class StubResponse:
    status_code: int
    body: dict[str, Any] | str
    text: str = ""

    def json(self) -> Any:
        if isinstance(self.body, str):
            # Mirror httpx behavior: trying to .json() a non-JSON body raises.
            raise json.JSONDecodeError("not json", self.body, 0)
        return self.body


class StubClient:
    """Minimal httpx.Client stand-in. Records the last call, returns a queued
    response, or raises a queued exception."""

    def __init__(self, responses: list[Any]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(self, url, *, headers, json, timeout):  # noqa: A002 — match httpx signature
        self.calls.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        nxt = self._responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def _ok_response(content="hi", response_id="resp-123", status=200):
    body = {
        "id": response_id,
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }
    return StubResponse(status_code=status, body=body, text=json.dumps(body))


# ---- payload translation -------------------------------------------------


def test_payload_translates_system_and_messages_to_openai_shape():
    client = StubClient([_ok_response("hello back")])
    backend = ModalGLMBackend(api_key="k", client=client)
    text = backend.chat(
        system="You are helpful.",
        messages=[
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="hello"),
            ChatMessage(role="user", content="how are you"),
        ],
        max_tokens=128,
    )
    assert text == "hello back"
    assert len(client.calls) == 1
    sent = client.calls[0]
    assert sent["url"] == MODAL_ENDPOINT
    assert sent["headers"]["Authorization"] == "Bearer k"
    assert sent["headers"]["Content-Type"] == "application/json"
    payload = sent["json"]
    assert payload["model"] == MODAL_MODEL
    assert payload["max_tokens"] == 128
    assert payload["messages"] == [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "how are you"},
    ]


def test_empty_system_omits_system_message():
    client = StubClient([_ok_response()])
    backend = ModalGLMBackend(api_key="k", client=client)
    backend.chat(system="", messages=[ChatMessage(role="user", content="hi")])
    payload = client.calls[0]["json"]
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


def test_unsupported_role_raises_value_error():
    client = StubClient([_ok_response()])
    backend = ModalGLMBackend(api_key="k", client=client)
    with pytest.raises(ValueError, match="unsupported chat role"):
        backend.chat(
            system="s", messages=[ChatMessage(role="tool", content="x")],
        )


# ---- error handling ------------------------------------------------------


def test_missing_api_key_raises():
    with pytest.raises(RuntimeError, match="MODAL_API_KEY not set"):
        ModalGLMBackend(api_key="")


def test_401_raises_auth_error():
    bad = StubResponse(status_code=401, body="unauthorized", text="unauthorized")
    backend = ModalGLMBackend(api_key="k", client=StubClient([bad]))
    with pytest.raises(ModalAuthError):
        backend.chat(system="", messages=[ChatMessage("user", "hi")])


def test_429_raises_rate_limit_error_after_retries(monkeypatch):
    # Exhaust retries: 4 total attempts (initial + 3 retries) all 429.
    monkeypatch.setattr(
        "src.llm_clients.modal_glm.MODAL_429_BACKOFF_S", (0.0, 0.0, 0.0),
    )
    bads = [StubResponse(status_code=429, body="slow down", text="slow down")
            for _ in range(4)]
    client = StubClient(list(bads))
    backend = ModalGLMBackend(api_key="k", client=client)
    with pytest.raises(ModalRateLimitError):
        backend.chat(system="", messages=[ChatMessage("user", "hi")])
    assert len(client.calls) == 4  # all retries consumed


def test_429_then_200_succeeds_via_retry(monkeypatch):
    monkeypatch.setattr(
        "src.llm_clients.modal_glm.MODAL_429_BACKOFF_S", (0.0, 0.0, 0.0),
    )
    bad = StubResponse(status_code=429, body="slow down", text="slow down")
    good = _ok_response("recovered")
    client = StubClient([bad, bad, good])
    backend = ModalGLMBackend(api_key="k", client=client)
    out = backend.chat(system="", messages=[ChatMessage("user", "hi")])
    assert out == "recovered"
    assert len(client.calls) == 3


def test_5xx_raises_server_error():
    # 500 is not retried; raises immediately.
    bad = StubResponse(status_code=500, body="oops", text="oops")
    backend = ModalGLMBackend(api_key="k", client=StubClient([bad]))
    with pytest.raises(ModalServerError):
        backend.chat(system="", messages=[ChatMessage("user", "hi")])


def test_502_retried_then_recovers(monkeypatch):
    monkeypatch.setattr(
        "src.llm_clients.modal_glm.MODAL_5XX_BACKOFF_S", (0.0, 0.0),
    )
    bad = StubResponse(status_code=502, body="bad gateway", text="bad gateway")
    good = _ok_response("recovered after 502")
    client = StubClient([bad, good])
    backend = ModalGLMBackend(api_key="k", client=client)
    out = backend.chat(system="", messages=[ChatMessage("user", "hi")])
    assert out == "recovered after 502"
    assert len(client.calls) == 2


def test_chat_calls_cost_recorder_with_usage():
    """ModalGLMBackend extracts prompt/completion tokens from the
    GLM response and feeds them to the cost_recorder callable."""
    body = {
        "id": "x",
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        "usage": {
            "prompt_tokens": 42, "completion_tokens": 17,
            "reasoning_tokens": 100,
        },
    }
    good = StubResponse(status_code=200, body=body, text=json.dumps(body))
    backend = ModalGLMBackend(api_key="k", client=StubClient([good]))

    captured: list[tuple] = []
    def recorder(model, in_tok, out_tok):
        captured.append((model, in_tok, out_tok))

    backend.chat(system="", messages=[ChatMessage("user", "hi")],
                 cost_recorder=recorder)

    assert captured == [(MODAL_MODEL, 42, 17)]


def test_chat_event_includes_usage():
    """The chat_model_call event payload should include the usage
    fields when present, so the trace UI / cost telemetry can use them."""
    body = {
        "id": "x",
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                  "reasoning_tokens": 3},
    }
    good = StubResponse(status_code=200, body=body, text=json.dumps(body))
    store = FactStore(":memory:")
    turn_id = store.insert_turn("assistant", "")
    backend = ModalGLMBackend(api_key="k", client=StubClient([good]))
    backend.chat(system="", messages=[ChatMessage("user", "hi")],
                 store=store, turn_id=turn_id)

    events = store.get_pipeline_events(turn_id)
    chat_event = next(e for e in events if e["stage"] == "chat_model_call")
    data = chat_event["data"]
    assert data["input_tokens"] == 10
    assert data["output_tokens"] == 5
    assert data["reasoning_tokens"] == 3


def test_chat_works_without_usage_field():
    """A response without a usage block (older endpoint version)
    should still chat normally — usage values just come back as 0."""
    body = {
        "id": "x",
        "choices": [{"message": {"role": "assistant", "content": "hi"}}],
        # No usage field.
    }
    good = StubResponse(status_code=200, body=body, text=json.dumps(body))
    backend = ModalGLMBackend(api_key="k", client=StubClient([good]))

    captured: list[tuple] = []
    text = backend.chat(
        system="", messages=[ChatMessage("user", "hi")],
        cost_recorder=lambda m, i, o: captured.append((m, i, o)),
    )
    assert text == "hi"
    # No usage field → recorder is called with zeros (we record the
    # call happened, just with no token counts to bill against). The
    # cost module will then report total_usd=0.0 for that call, which
    # is the correct accounting.
    assert captured == [(MODAL_MODEL, 0, 0)]


def test_503_exhausts_retries(monkeypatch):
    monkeypatch.setattr(
        "src.llm_clients.modal_glm.MODAL_5XX_BACKOFF_S", (0.0, 0.0),
    )
    # 3 attempts (initial + 2 retries) all 503.
    bads = [StubResponse(status_code=503, body="down", text="down")
            for _ in range(3)]
    client = StubClient(list(bads))
    backend = ModalGLMBackend(api_key="k", client=client)
    with pytest.raises(ModalServerError) as excinfo:
        backend.chat(system="", messages=[ChatMessage("user", "hi")])
    assert excinfo.value.status_code == 503
    assert len(client.calls) == 3  # 1 initial + 2 retries


def test_timeout_raises_timeout_error():
    import httpx

    backend = ModalGLMBackend(
        api_key="k",
        client=StubClient([httpx.TimeoutException("timed out")]),
    )
    with pytest.raises(ModalTimeoutError):
        backend.chat(system="", messages=[ChatMessage("user", "hi")])


def test_malformed_response_raises_response_error():
    body = {"unexpected": "shape"}
    bad = StubResponse(status_code=200, body=body, text=json.dumps(body))
    backend = ModalGLMBackend(api_key="k", client=StubClient([bad]))
    with pytest.raises(ModalResponseError, match="missing choices"):
        backend.chat(system="", messages=[ChatMessage("user", "hi")])


def test_non_json_response_raises_response_error():
    bad = StubResponse(status_code=200, body="not-json", text="not-json")
    backend = ModalGLMBackend(api_key="k", client=StubClient([bad]))
    with pytest.raises(ModalResponseError, match="non-JSON"):
        backend.chat(system="", messages=[ChatMessage("user", "hi")])


def test_content_null_raises_response_error_with_reasoning_hint():
    # GLM-5.1 returns content=null when it spent its max_tokens on
    # reasoning_content before producing any user-facing content.
    body = {
        "id": "x",
        "choices": [{"message": {
            "role": "assistant",
            "content": None,
            "reasoning_content": "1. Think hard about this..." * 20,
        }}],
    }
    bad = StubResponse(status_code=200, body=body, text=json.dumps(body))
    backend = ModalGLMBackend(api_key="k", client=StubClient([bad]))
    with pytest.raises(ModalResponseError, match="content=null"):
        backend.chat(system="", messages=[ChatMessage("user", "hi")])


# ---- pipeline event logging ---------------------------------------------


def test_chat_logs_pipeline_event_on_success(tmp_path):
    store = FactStore(tmp_path / "t.db")
    turn_id = store.insert_turn("assistant", "")
    backend = ModalGLMBackend(
        api_key="k",
        client=StubClient([_ok_response("hello", response_id="resp-9")]),
    )
    backend.chat(
        system="sys",
        messages=[ChatMessage("user", "hi")],
        max_tokens=64,
        store=store,
        turn_id=turn_id,
    )
    events = store.get_pipeline_events(turn_id)
    chat_events = [e for e in events if e["stage"] == "chat_model_call"]
    assert len(chat_events) == 1
    data = chat_events[0]["data"]
    assert data["provider"] == "modal"
    assert data["model"] == MODAL_MODEL
    assert data["status_code"] == 200
    assert data["response_id"] == "resp-9"
    assert data["response_chars"] == len("hello")
    assert data["error"] is None
    assert data["max_tokens"] == 64
    assert data["system_chars"] == 3
    assert data["message_count"] == 1
    assert isinstance(data["duration_ms"], int)


def test_chat_logs_pipeline_event_on_failure(tmp_path):
    store = FactStore(tmp_path / "t.db")
    turn_id = store.insert_turn("assistant", "")
    bad = StubResponse(status_code=401, body="nope", text="nope")
    backend = ModalGLMBackend(api_key="k", client=StubClient([bad]))
    with pytest.raises(ModalAuthError):
        backend.chat(
            system="sys",
            messages=[ChatMessage("user", "hi")],
            store=store,
            turn_id=turn_id,
        )
    events = store.get_pipeline_events(turn_id)
    chat_events = [e for e in events if e["stage"] == "chat_model_call"]
    assert len(chat_events) == 1
    data = chat_events[0]["data"]
    assert data["provider"] == "modal"
    assert data["status_code"] == 401
    assert data["response_chars"] == 0
    assert data["error"] is not None
    assert "ModalAuthError" in data["error"]


# ---- end-to-end: pipeline routes the assistant draft via the backend ----


def test_pipeline_caps_chat_max_tokens(tmp_path):
    """Originally (commit 4d81d59) the cap moved off the legacy 4096
    default that let GLM's reasoning chain blow past the 300s Modal
    timeout. Caps are now per-backend (CHAT_MAX_TOKENS_ANTHROPIC=1024,
    CHAT_MAX_TOKENS_MODAL=4096); see the per_backend tests above for
    the dispatch contract.

    This test still covers the legacy single-knob backward-compat
    surface (Pipeline.CHAT_MAX_TOKENS) and the unknown-provider
    fallback path: a backend with provider='stub' goes through
    CHAT_MAX_TOKENS_DEFAULT (1024), which equals the alias's value
    when no global override is set. So the assertion that
    captured == [Pipeline.CHAT_MAX_TOKENS] still holds for the stub
    backend, and the 256–4096-but-not-4096 sanity bound on the alias
    still rules out the legacy regression."""
    from src.corrector import Corrector
    from src.extractor import ClaimExtractor
    from src.fact_store import FactStore
    from src.llm_router import RoutingDecision
    from src.pattern_registry import load_default_registry, reset_cache
    from src.pipeline import Pipeline
    from src.router import Router

    reset_cache()

    @dataclass
    class _MockLLM:
        extracts: list = field(default_factory=list)
        rewrites: list = field(default_factory=list)
        corrector_model: str = "mock"

        def extract_with_tool(self, system, user_message, tool, max_tokens=2048):
            return self.extracts.pop(0)

        def rewrite(self, system, user_message, max_tokens=2048, temperature=None):
            return self.rewrites.pop(0)

    captured: list[int] = []

    class CapturingBackend:
        provider = "stub"
        model = "stub"

        def chat(self, system, messages, *, max_tokens, store, turn_id):
            captured.append(max_tokens)
            return "ok"

    store = FactStore(tmp_path / "p.db")
    registry = load_default_registry()
    mock = _MockLLM(extracts=[{"facts": []}, {"facts": []}])
    extractor = ClaimExtractor(mock, registry)
    router = Router(store, registry, routing_fn=lambda c: RoutingDecision(
        method="unverifiable", reason="x", confidence=0.9))
    p = Pipeline(store, registry, mock, extractor, router, Corrector(mock),
                 chat_backend=CapturingBackend())

    p.run_turn("hi")
    assert captured == [Pipeline.CHAT_MAX_TOKENS]
    # Sanity: cap is a sensible value for chat use. NOT 4096 (the
    # legacy default that caused the cold-start timeout regression).
    assert 256 <= Pipeline.CHAT_MAX_TOKENS <= 4096
    assert Pipeline.CHAT_MAX_TOKENS != 4096


def _build_pipeline_with_backend(tmp_path, backend):
    """Helper for the per-backend max_tokens tests below — builds a
    minimal Pipeline plumbed to ``backend`` and runs one turn."""
    from src.corrector import Corrector
    from src.extractor import ClaimExtractor
    from src.fact_store import FactStore
    from src.llm_router import RoutingDecision
    from src.pattern_registry import load_default_registry, reset_cache
    from src.pipeline import Pipeline
    from src.router import Router

    reset_cache()
    tmp_path.mkdir(parents=True, exist_ok=True)

    @dataclass
    class _MockLLM:
        extracts: list = field(default_factory=list)
        rewrites: list = field(default_factory=list)
        corrector_model: str = "mock"

        def extract_with_tool(self, system, user_message, tool, max_tokens=2048):
            return self.extracts.pop(0)

        def rewrite(self, system, user_message, max_tokens=2048, temperature=None):
            return self.rewrites.pop(0)

    store = FactStore(tmp_path / "p.db")
    registry = load_default_registry()
    mock = _MockLLM(extracts=[{"facts": []}, {"facts": []}])
    extractor = ClaimExtractor(mock, registry)
    router = Router(store, registry, routing_fn=lambda c: RoutingDecision(
        method="unverifiable", reason="x", confidence=0.9))
    p = Pipeline(store, registry, mock, extractor, router, Corrector(mock),
                 chat_backend=backend)
    p.run_turn("hi")


def test_per_backend_max_tokens_modal_gets_more_headroom(tmp_path, monkeypatch):
    """Reasoning models (provider='modal') need more max_tokens because
    the cap counts reasoning_content too. Anthropic chat doesn't.

    With the per-backend split, a Modal backend should receive the
    CHAT_MAX_TOKENS_MODAL default (4096) while an Anthropic backend
    receives CHAT_MAX_TOKENS_ANTHROPIC (1024). This was the floccinau-
    cinihilipilification fix: turn 4 of the corpus returned content=null
    because GLM spent all 1024 tokens inside the reasoning chain."""
    from src.pipeline import Pipeline

    # Force the per-backend defaults regardless of the env at test run.
    monkeypatch.setattr(Pipeline, "CHAT_MAX_TOKENS_GLOBAL_OVERRIDE", None,
                        raising=False)
    monkeypatch.setattr(Pipeline, "CHAT_MAX_TOKENS_MODAL", 4096)
    monkeypatch.setattr(Pipeline, "CHAT_MAX_TOKENS_ANTHROPIC", 1024)

    captured_modal: list[int] = []
    captured_anthropic: list[int] = []

    class ModalBackend:
        provider = "modal"
        model = "GLM"

        def chat(self, system, messages, *, max_tokens, store, turn_id):
            captured_modal.append(max_tokens)
            return "ok"

    class AnthropicBackend:
        provider = "anthropic"
        model = "claude"

        def chat(self, system, messages, *, max_tokens, store, turn_id):
            captured_anthropic.append(max_tokens)
            return "ok"

    _build_pipeline_with_backend(tmp_path / "modal", ModalBackend())
    _build_pipeline_with_backend(tmp_path / "anthropic", AnthropicBackend())

    assert captured_modal == [4096], (
        "Modal/reasoning backend should get the larger cap")
    assert captured_anthropic == [1024], (
        "Anthropic chat backend should get the smaller cap")


def test_per_backend_max_tokens_global_override_wins(tmp_path, monkeypatch):
    """If AEDOS_CHAT_MAX_TOKENS is set, it overrides per-backend
    defaults — backward compat with the pre-split single-knob world."""
    from src.pipeline import Pipeline

    monkeypatch.setattr(Pipeline, "CHAT_MAX_TOKENS_GLOBAL_OVERRIDE", 2048)
    monkeypatch.setattr(Pipeline, "CHAT_MAX_TOKENS_MODAL", 4096)
    monkeypatch.setattr(Pipeline, "CHAT_MAX_TOKENS_ANTHROPIC", 1024)

    captured: list[int] = []

    class ModalBackend:
        provider = "modal"
        model = "GLM"

        def chat(self, system, messages, *, max_tokens, store, turn_id):
            captured.append(max_tokens)
            return "ok"

    _build_pipeline_with_backend(tmp_path / "ovr", ModalBackend())
    assert captured == [2048], (
        "Global override should win over per-backend default")


def test_per_backend_max_tokens_unknown_provider_uses_default(tmp_path, monkeypatch):
    """An unfamiliar provider falls back to CHAT_MAX_TOKENS_DEFAULT
    (1024) rather than picking one of the per-backend values
    arbitrarily."""
    from src.pipeline import Pipeline

    monkeypatch.setattr(Pipeline, "CHAT_MAX_TOKENS_GLOBAL_OVERRIDE", None)
    monkeypatch.setattr(Pipeline, "CHAT_MAX_TOKENS_MODAL", 4096)
    monkeypatch.setattr(Pipeline, "CHAT_MAX_TOKENS_ANTHROPIC", 1024)
    monkeypatch.setattr(Pipeline, "CHAT_MAX_TOKENS_DEFAULT", 1024)

    captured: list[int] = []

    class UnknownBackend:
        provider = "some-future-model-host"
        model = "?"

        def chat(self, system, messages, *, max_tokens, store, turn_id):
            captured.append(max_tokens)
            return "ok"

    _build_pipeline_with_backend(tmp_path / "u", UnknownBackend())
    assert captured == [1024]


def test_pipeline_uses_chat_backend_and_logs_chat_model_call(tmp_path):
    """Drive a Pipeline with a stub chat_backend instead of the legacy
    llm.chat path. The backend must be invoked, and a chat_model_call
    event must land on the assistant turn."""
    from src.corrector import Corrector
    from src.extractor import ClaimExtractor
    from src.fact_store import FactStore
    from src.llm_router import RoutingDecision
    from src.pattern_registry import load_default_registry, reset_cache
    from src.pipeline import Pipeline
    from src.router import Router

    reset_cache()

    @dataclass
    class _MockLLM:
        extracts: list = field(default_factory=list)
        rewrites: list = field(default_factory=list)
        routings: list = field(default_factory=list)
        corrector_model: str = "mock"

        def extract_with_tool(self, system, user_message, tool, max_tokens=2048):
            return self.extracts.pop(0)

        def rewrite(self, system, user_message, max_tokens=2048, temperature=None):
            return self.rewrites.pop(0)

    class StubBackend:
        provider = "stub"
        model = "stub-model"
        calls: list[dict] = []

        def chat(self, system, messages, *, max_tokens, store, turn_id):
            StubBackend.calls.append({
                "system": system, "messages": list(messages),
                "max_tokens": max_tokens, "turn_id": turn_id,
            })
            store.insert_pipeline_event(
                turn_id, "chat_model_call",
                {"provider": self.provider, "model": self.model,
                 "stub": True, "error": None, "response_chars": 5},
            )
            return "hello"

    StubBackend.calls = []

    store = FactStore(tmp_path / "p.db")
    registry = load_default_registry()
    mock = _MockLLM(extracts=[{"facts": []}, {"facts": []}], rewrites=[])
    extractor = ClaimExtractor(mock, registry)
    router = Router(store, registry, routing_fn=lambda c: RoutingDecision(
        method="unverifiable", reason="x", confidence=0.9))
    corrector = Corrector(mock)
    backend = StubBackend()
    p = Pipeline(store, registry, mock, extractor, router, corrector,
                 chat_backend=backend)

    trace = p.run_turn("hi")

    assert StubBackend.calls, "chat_backend.chat must be invoked"
    assert StubBackend.calls[0]["turn_id"] == trace.assistant_turn_id

    events = store.get_pipeline_events(trace.assistant_turn_id)
    stages = [e["stage"] for e in events]
    assert "chat_model_call" in stages
    chat_event = next(e for e in events if e["stage"] == "chat_model_call")
    assert chat_event["data"]["provider"] == "stub"
    assert trace.final_content == "hello"


def test_pipeline_legacy_llm_chat_path_still_works(tmp_path):
    """When chat_backend is omitted, Pipeline must fall back to llm.chat
    so legacy MockLLM-style tests keep working."""
    from src.corrector import Corrector
    from src.extractor import ClaimExtractor
    from src.fact_store import FactStore
    from src.llm_router import RoutingDecision
    from src.pattern_registry import load_default_registry, reset_cache
    from src.pipeline import Pipeline
    from src.router import Router

    reset_cache()

    @dataclass
    class _MockLLM:
        chats: list = field(default_factory=list)
        extracts: list = field(default_factory=list)
        rewrites: list = field(default_factory=list)
        corrector_model: str = "mock"

        def chat(self, system, messages, max_tokens=4096):
            return self.chats.pop(0)

        def extract_with_tool(self, system, user_message, tool, max_tokens=2048):
            return self.extracts.pop(0)

        def rewrite(self, system, user_message, max_tokens=2048, temperature=None):
            return self.rewrites.pop(0)

    store = FactStore(tmp_path / "p.db")
    registry = load_default_registry()
    mock = _MockLLM(
        chats=["legacy-response"],
        extracts=[{"facts": []}, {"facts": []}],
    )
    extractor = ClaimExtractor(mock, registry)
    router = Router(store, registry, routing_fn=lambda c: RoutingDecision(
        method="unverifiable", reason="x", confidence=0.9))
    corrector = Corrector(mock)
    p = Pipeline(store, registry, mock, extractor, router, corrector)

    trace = p.run_turn("hi")
    assert trace.final_content == "legacy-response"
    # No chat_model_call event because legacy MockLLM has no `provider`.
    events = store.get_pipeline_events(trace.assistant_turn_id)
    assert "chat_model_call" not in [e["stage"] for e in events]
