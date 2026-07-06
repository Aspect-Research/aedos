"""Tests for the deploy backend's BYOK auth mode + public-perimeter hardening
(request-scoped user keys, free-models routing, body/message caps, per-IP
rate limiting) and for `LLMClient.request_overrides` itself.

No live KB/LLM anywhere: the pipeline / chat-wrapper are injected fakes, and
the LLMClient tests use a fake OpenAI-compatible transport module.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from deploy.backend.server import create_app  # noqa: E402
from deploy.backend.settings import DeploySettings  # noqa: E402

from aedos.llm.client import LLMClient  # noqa: E402

from .test_backend import (  # noqa: E402
    KEY,
    FakeChatWrapper,
    FakePipeline,
    _settings,
    _vr,
)

OR_KEY = "sk-or-test-user-key"
ANTH_KEY = "sk-ant-test-user-key"


class FakeLLMClient:
    """Records the overrides the server installs around engine work."""

    def __init__(self):
        self.installed: list[dict] = []
        self.active = False

    @contextmanager
    def request_overrides(self, **kwargs):
        self.installed.append(kwargs)
        self.active = True
        try:
            yield
        finally:
            self.active = False


def _byok_settings(**over) -> DeploySettings:
    return _settings(auth_mode="byok", **over)


class _FakeTierU:
    def rows_for_party(self, party):
        return []


def _byok_client(*, settings=None, llm=None) -> tuple[TestClient, FakeLLMClient, FakeChatWrapper]:
    llm = llm or FakeLLMClient()
    pipeline = FakePipeline(tier_u=_FakeTierU())
    pipeline.llm_client = llm
    wrapper = FakeChatWrapper(_vr())
    app = create_app(
        settings=settings or _byok_settings(), pipeline=pipeline, chat_wrapper=wrapper
    )
    return TestClient(app), llm, wrapper


def BH(
    *,
    session: str | None = "s1",
    anthropic: str | None = None,
    openrouter: str | None = None,
    free: bool = False,
    deploy_key: str | None = None,
) -> dict:
    h: dict[str, str] = {}
    if session is not None:
        h["X-Aedos-Session"] = session
    if anthropic is not None:
        h["X-User-Anthropic-Key"] = anthropic
    if openrouter is not None:
        h["X-User-OpenRouter-Key"] = openrouter
    if free:
        h["X-Aedos-Free-Models"] = "1"
    if deploy_key is not None:
        h["X-Aedos-Key"] = deploy_key
    return h


# --------------------------------------------------------------------------- #
# BYOK auth gate
# --------------------------------------------------------------------------- #

class TestByokAuth:
    def test_no_keys_rejected(self):
        c, _, _ = _byok_client()
        r = c.post("/chat", json={"message": "hi"}, headers=BH())
        assert r.status_code == 401
        assert "X-User-OpenRouter-Key" in r.json()["detail"]

    def test_both_keys_authorized(self):
        c, llm, _ = _byok_client()
        r = c.post(
            "/chat", json={"message": "hi"},
            headers=BH(anthropic=ANTH_KEY, openrouter=OR_KEY),
        )
        assert r.status_code == 200
        assert r.json()["final_message"] == "draft reply"
        assert llm.installed == [{
            "anthropic_api_key": ANTH_KEY,
            "api_keys_by_env_var": {"OPENROUTER_API_KEY": OR_KEY},
        }]
        assert llm.active is False  # cleared after the request

    def test_openrouter_alone_rejected_without_free_mode(self):
        c, _, _ = _byok_client()
        r = c.post("/chat", json={"message": "hi"}, headers=BH(openrouter=OR_KEY))
        assert r.status_code == 401

    def test_anthropic_alone_rejected(self):
        # Default routing sends four purposes to OpenRouter; an Anthropic-only
        # caller would fail mid-turn, so the gate rejects up front.
        c, _, _ = _byok_client()
        r = c.post("/chat", json={"message": "hi"}, headers=BH(anthropic=ANTH_KEY))
        assert r.status_code == 401

    def test_free_mode_with_openrouter_authorized_and_reroutes_all(self):
        c, llm, _ = _byok_client(settings=_byok_settings(free_model="test/free-model"))
        r = c.post(
            "/chat", json={"message": "hi"},
            headers=BH(openrouter=OR_KEY, free=True),
        )
        assert r.status_code == 200
        (kw,) = llm.installed
        assert kw["route_all_to"] == {
            "model": "test/free-model",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env_var": "OPENROUTER_API_KEY",
            "extra_body": None,
        }

    def test_deploy_key_back_door_still_works(self):
        c, llm, _ = _byok_client()
        r = c.post("/chat", json={"message": "hi"}, headers=BH(deploy_key=KEY))
        assert r.status_code == 200
        # Operator-funded path: no overrides installed.
        assert llm.installed == []

    def test_malformed_key_header_400(self):
        c, _, _ = _byok_client()
        r = c.post(
            "/chat", json={"message": "hi"},
            headers=BH(anthropic="x" * 600, openrouter=OR_KEY),
        )
        assert r.status_code == 400
        # The key value itself never appears in the error.
        assert "x" * 32 not in r.json()["detail"]

    def test_key_mode_ignores_user_key_authorization(self):
        # auth_mode="key" (default): user keys alone do NOT authorize.
        c, llm, _ = _byok_client(settings=_settings())
        r = c.post(
            "/chat", json={"message": "hi"},
            headers=BH(anthropic=ANTH_KEY, openrouter=OR_KEY),
        )
        assert r.status_code == 401

    def test_get_routes_gated_too(self):
        c, _, _ = _byok_client()
        assert c.get("/session/context", headers=BH()).status_code == 401
        ok = c.get(
            "/session/context",
            headers=BH(anthropic=ANTH_KEY, openrouter=OR_KEY),
        )
        # Authorized; FakePipeline.tier_u is None so anything but 401 means the
        # gate passed (the real pipeline path is covered in test_backend).
        assert ok.status_code != 401

    def test_stream_route_installs_overrides(self):
        c, llm, _ = _byok_client()
        r = c.post(
            "/chat/stream", json={"message": "hi"},
            headers=BH(anthropic=ANTH_KEY, openrouter=OR_KEY),
        )
        assert r.status_code == 200
        assert len(llm.installed) == 1


# --------------------------------------------------------------------------- #
# Perimeter: body cap, message cap, per-IP limit
# --------------------------------------------------------------------------- #

class TestPerimeter:
    def test_oversized_body_413(self):
        c, _, _ = _byok_client(settings=_byok_settings(max_body_bytes=64))
        r = c.post(
            "/chat", json={"message": "y" * 200},
            headers=BH(anthropic=ANTH_KEY, openrouter=OR_KEY),
        )
        assert r.status_code == 413

    def test_overlong_message_413(self):
        c, _, _ = _byok_client(settings=_byok_settings(max_message_chars=10))
        r = c.post(
            "/chat", json={"message": "y" * 11},
            headers=BH(anthropic=ANTH_KEY, openrouter=OR_KEY),
        )
        assert r.status_code == 413
        assert "413" not in r.json().get("detail", "413")  # detail is human text

    def test_per_ip_limit_backstops_session_rotation(self):
        # Generous per-session limit, tight per-IP limit: rotating the session
        # id must NOT evade the limiter.
        c, _, _ = _byok_client(
            settings=_byok_settings(
                rate_limit_requests=1000, ip_rate_limit_requests=3
            )
        )
        codes = []
        for i in range(5):
            r = c.post(
                "/chat", json={"message": "hi"},
                headers=BH(
                    session=f"rotate-{i}", anthropic=ANTH_KEY, openrouter=OR_KEY
                ),
            )
            codes.append(r.status_code)
        assert codes[:3] == [200, 200, 200]
        assert codes[3] == 429 and codes[4] == 429


# --------------------------------------------------------------------------- #
# LLMClient.request_overrides
# --------------------------------------------------------------------------- #

class _FakeCompletions:
    def __init__(self, log):
        self._log = log

    def create(self, **kwargs):
        self._log.append(kwargs)

        class _Msg:
            content = "ok"
            tool_calls = None

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]
            usage = type("_Usage", (), {"prompt_tokens": 1, "completion_tokens": 1})()

        return _Resp()


class _FakeOpenAIModule:
    """Stands in for the `openai` package: records constructor kwargs."""

    def __init__(self):
        self.constructed: list[dict] = []
        self.calls: list[dict] = []

    def OpenAI(self, *, api_key, base_url=None):  # noqa: N802 (SDK name)
        self.constructed.append({"api_key": api_key, "base_url": base_url})
        chat = type("_Chat", (), {})()
        chat.completions = _FakeCompletions(self.calls)
        return type("_Client", (), {"chat": chat})()


class TestRequestOverrides:
    def _client_with_fake_openai(self, monkeypatch):
        fake = _FakeOpenAIModule()
        monkeypatch.setitem(sys.modules, "openai", fake)
        return LLMClient(anthropic_api_key="proc-anthropic-key"), fake

    def test_route_all_applies_to_chat_purpose(self, monkeypatch):
        client, fake = self._client_with_fake_openai(monkeypatch)
        route = {
            "model": "test/free",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env_var": "OPENROUTER_API_KEY",
            "extra_body": None,
        }
        with client.request_overrides(
            api_keys_by_env_var={"OPENROUTER_API_KEY": OR_KEY}, route_all_to=route
        ):
            # `chat` purpose is normally never rerouted by the env override —
            # the request-scoped route MUST cover it (free-models mode).
            assert client._cfg("chat")["model"] == "test/free"
            client.chat("sys", [], purpose="chat")
        assert fake.constructed == [
            {"api_key": OR_KEY, "base_url": "https://openrouter.ai/api/v1"}
        ]
        assert fake.calls[0]["model"] == "test/free"

    def test_request_openai_client_not_cached(self, monkeypatch):
        client, fake = self._client_with_fake_openai(monkeypatch)
        route = {
            "model": "test/free",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env_var": "OPENROUTER_API_KEY",
            "extra_body": None,
        }
        for key in ("key-a", "key-b"):
            with client.request_overrides(
                api_keys_by_env_var={"OPENROUTER_API_KEY": key}, route_all_to=route
            ):
                client.chat("sys", [], purpose="chat")
        # One fresh client per request, each with its caller's key; nothing
        # landed in the process-wide cache.
        assert [c["api_key"] for c in fake.constructed] == ["key-a", "key-b"]
        assert client._openai_clients == {}

    def test_overrides_cleared_after_block(self, monkeypatch):
        client, _ = self._client_with_fake_openai(monkeypatch)
        with client.request_overrides(
            anthropic_api_key="user-key",
            api_keys_by_env_var={"OPENROUTER_API_KEY": OR_KEY},
        ):
            assert client._req_anthropic_key == "user-key"
        assert client._req_anthropic_key is None
        assert client._req_keys_by_env == {}
        assert client._req_route_all is None
        # Back to the process routing: chat resolves to the default model.
        assert client._cfg("chat")["model"] == client.model

    def test_anthropic_override_builds_fresh_client(self, monkeypatch):
        built = []

        class _FakeAnthropicClient:
            def __init__(self, api_key):
                self.api_key = api_key
                built.append(api_key)

        import aedos.llm.client as mod

        monkeypatch.setattr(
            mod.anthropic, "Anthropic", lambda api_key: _FakeAnthropicClient(api_key)
        )
        client = LLMClient(anthropic_api_key="proc-key")
        assert built == ["proc-key"]
        with client.request_overrides(anthropic_api_key="user-key"):
            assert client._anthropic().api_key == "user-key"
        assert client._anthropic().api_key == "proc-key"
