"""Tests for the v0.16.2 deployment backend (deploy/backend).

No live KB/LLM: the pipeline / chat-wrapper are injected fakes, EXCEPT the
session-isolation + reset tests, which use a REAL TierU on an in-memory DB so the
party-scoping that the multi-tenant model rests on is exercised end-to-end.

The session token travels in the X-Aedos-Session header (never the URL/body), and
the access key in X-Aedos-Key.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

# deploy/ is a sibling of src/ — not installed; add it to the path.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from deploy.backend.ratelimit import SlidingWindowLimiter  # noqa: E402
from deploy.backend.server import _key_matches, create_app  # noqa: E402
from deploy.backend.settings import DeploySettings  # noqa: E402

from aedos.database import open_memory_db  # noqa: E402
from aedos.layer1_extraction.extractor import Claim  # noqa: E402
from aedos.layer1_extraction.triage import TriageDecision  # noqa: E402
from aedos.layer4_sources.tier_u import TierU  # noqa: E402

KEY = "test-secret-key"


def H(session: str | None = "s1", key: str | None = KEY) -> dict:
    h: dict[str, str] = {}
    if key is not None:
        h["X-Aedos-Key"] = key
    if session is not None:
        h["X-Aedos-Session"] = session
    return h


def _settings(**over) -> DeploySettings:
    base = dict(
        deploy_key=KEY,
        require_auth=True,
        allowed_origins=["http://localhost:5173"],
        db_path=":memory:",
        rate_limit_requests=100,
        rate_limit_window_seconds=60.0,
        max_session_id_len=128,
    )
    base.update(over)
    return DeploySettings(**base)


def _vr(verdict="verified_given_assertion"):
    cv = SimpleNamespace(
        claim_id="c1",
        claim=SimpleNamespace(subject="Paris", predicate="located_in",
                              object="France", polarity=1),
        verdict=verdict,
        abstention_reason=None,
        contradicting_value=None,
        contradicting_value_type=None,
    )
    return SimpleNamespace(
        claim_verdicts=[cv],
        per_claim_traces={},
        per_claim_verdicts={"c1": verdict},
        aggregate_metadata={"note": "fake"},
    )


class FakeChatWrapper:
    def __init__(self, vr):
        self._vr = vr
        self.calls = []

    def respond(self, message, conversation_context=None, progress=None,
                verify_workers=None):
        self.calls.append((message, conversation_context))
        if progress is not None:
            progress({"phase": "reading", "detail": "reading your message"})
            progress({"phase": "verdict", "detail": "verified", "claim_id": "c1"})
        action = SimpleNamespace(
            claim_id="c1",
            action_type=SimpleNamespace(value="pass_through"),
            annotation="looks fine",
        )
        return SimpleNamespace(
            final_message="draft reply",
            intervention_type="pass_through",
            intervention_plan=SimpleNamespace(per_claim_actions=[action]),
            verification_id="ver-123",
            verification_result=self._vr,
        )

    def get_verification(self, vid):
        return self._vr if vid == "ver-123" else None


class FakePipeline:
    def __init__(self, *, extractor=None, walker=None, aggregator=None, tier_u=None):
        self.extractor = extractor
        self.walker = walker
        self.aggregator = aggregator
        self.tier_u = tier_u
        self.kb = None
        self.llm_client = None


def _client(*, settings=None, pipeline=None, chat_wrapper=None) -> TestClient:
    app = create_app(settings=settings or _settings(), pipeline=pipeline,
                     chat_wrapper=chat_wrapper)
    return TestClient(app)


def _parse_sse(text: str) -> list[tuple[str, dict | None]]:
    events: list[tuple[str, dict | None]] = []
    for block in text.strip().split("\n\n"):
        kind = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                kind = line[len("event: "):]
            elif line.startswith("data: "):
                data = line[len("data: "):]
        if kind:
            events.append((kind, json.loads(data) if data else None))
    return events


# --------------------------------------------------------------------------- #
# Auth gate
# --------------------------------------------------------------------------- #

class TestAuth:
    def test_health_is_unauthenticated(self):
        c = _client(chat_wrapper=FakeChatWrapper(_vr()))
        r = c.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "ok"

    def test_chat_without_key_rejected(self):
        c = _client(chat_wrapper=FakeChatWrapper(_vr()))
        r = c.post("/chat", json={"message": "hi"}, headers=H(key=None))
        assert r.status_code == 401

    def test_chat_with_wrong_key_rejected(self):
        c = _client(chat_wrapper=FakeChatWrapper(_vr()))
        r = c.post("/chat", json={"message": "hi"}, headers=H(key="wrong"))
        assert r.status_code == 401

    def test_auth_fails_closed_when_no_key_configured(self):
        c = _client(settings=_settings(deploy_key=""),
                    chat_wrapper=FakeChatWrapper(_vr()))
        r = c.post("/chat", json={"message": "hi"}, headers=H())
        assert r.status_code == 401

    def test_chat_with_correct_key_admitted(self):
        c = _client(chat_wrapper=FakeChatWrapper(_vr()))
        r = c.post("/chat", json={"message": "hi"}, headers=H())
        assert r.status_code == 200


# --------------------------------------------------------------------------- #
# CORS
# --------------------------------------------------------------------------- #

class TestKeyMatch:
    """F3: the access-key compare fails closed on every mismatch — including a
    non-ASCII provided key — without raising (which would surface as a 500)."""

    def test_non_ascii_provided_key_is_false_not_raise(self):
        assert _key_matches("\xe9bad-key", KEY) is False

    def test_correct_key_matches(self):
        assert _key_matches(KEY, KEY) is True

    def test_empty_expected_fails_closed(self):
        assert _key_matches("anything", "") is False
        assert _key_matches("", "") is False

    def test_none_provided_is_false(self):
        assert _key_matches(None, KEY) is False


class TestCORS:
    def test_preflight_allows_configured_origin(self):
        c = _client(chat_wrapper=FakeChatWrapper(_vr()))
        r = c.options(
            "/chat",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Aedos-Key,X-Aedos-Session",
            },
        )
        assert r.status_code in (200, 204)
        assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"

    def test_disallowed_origin_not_reflected(self):
        c = _client(chat_wrapper=FakeChatWrapper(_vr()))
        r = c.options(
            "/chat",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert r.headers.get("access-control-allow-origin") != "http://evil.example"


# --------------------------------------------------------------------------- #
# Session id validation (now via header)
# --------------------------------------------------------------------------- #

class TestSessionValidation:
    def test_missing_session_header_rejected(self):
        c = _client(chat_wrapper=FakeChatWrapper(_vr()))
        r = c.post("/chat", json={"message": "hi"}, headers=H(session=None))
        assert r.status_code == 400

    def test_malformed_session_id_rejected(self):
        c = _client(chat_wrapper=FakeChatWrapper(_vr()))
        r = c.post("/chat", json={"message": "hi"},
                   headers=H(session="bad id with spaces!"))
        assert r.status_code == 400

    def test_session_id_cannot_escape_namespace(self):
        # ':' is forbidden, so a tester cannot collapse "session:<id>" into a bare
        # engine/seed party like "user".
        c = _client(chat_wrapper=FakeChatWrapper(_vr()))
        r = c.post("/chat", json={"message": "hi"}, headers=H(session="x:user"))
        assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #

class TestRateLimit:
    def test_exceeding_window_returns_429(self):
        c = _client(settings=_settings(rate_limit_requests=2),
                    chat_wrapper=FakeChatWrapper(_vr()))
        assert c.post("/chat", json={"message": "hi"}, headers=H("s1")).status_code == 200
        assert c.post("/chat", json={"message": "hi"}, headers=H("s1")).status_code == 200
        r3 = c.post("/chat", json={"message": "hi"}, headers=H("s1"))
        assert r3.status_code == 429
        assert "retry-after" in {k.lower() for k in r3.headers}

    def test_limit_is_per_session(self):
        c = _client(settings=_settings(rate_limit_requests=1),
                    chat_wrapper=FakeChatWrapper(_vr()))
        assert c.post("/chat", json={"message": "x"}, headers=H("a")).status_code == 200
        assert c.post("/chat", json={"message": "x"}, headers=H("b")).status_code == 200
        assert c.post("/chat", json={"message": "x"}, headers=H("a")).status_code == 429

    def test_limiter_evicts_stale_keys(self):
        # F2: rotating keys must not grow the table without bound — once past the
        # cap, a fresh request GCs keys whose whole window has expired.
        t = [0.0]
        lim = SlidingWindowLimiter(5, 10.0, clock=lambda: t[0], max_keys=3)
        for i in range(5):
            assert lim.allow(f"k{i}")
        t[0] = 100.0  # past the window for all earlier keys
        assert lim.allow("k-new")
        assert len(lim._hits) == 1


# --------------------------------------------------------------------------- #
# Chat response shape + given-assertion annotation
# --------------------------------------------------------------------------- #

class TestChat:
    def test_party_derived_from_session_header(self):
        wrapper = FakeChatWrapper(_vr())
        c = _client(chat_wrapper=wrapper)
        c.post("/chat", json={"message": "hi"}, headers=H("tester-7"))
        _, ctx = wrapper.calls[-1]
        assert ctx["asserting_party_id"] == "session:tester-7"

    def test_response_shape_and_given_assertion(self):
        c = _client(chat_wrapper=FakeChatWrapper(_vr("verified_given_assertion")))
        r = c.post("/chat", json={"message": "hi"}, headers=H())
        body = r.json()
        assert body["final_message"] == "draft reply"
        assert body["verification_id"] == "ver-123"
        assert body["observability"][0]["conditional"] is True
        assert body["given_assertion"]["count"] == 1
        assert body["given_assertion"]["claim_ids"] == ["c1"]

    def test_non_conditional_verdict_not_flagged(self):
        c = _client(chat_wrapper=FakeChatWrapper(_vr("verified")))
        r = c.post("/chat", json={"message": "hi"}, headers=H())
        assert r.json()["given_assertion"]["count"] == 0


# --------------------------------------------------------------------------- #
# /verify (run Aedos on raw text)
# --------------------------------------------------------------------------- #

class TestVerify:
    def _verify_pipeline(self, abstention=None):
        claim = SimpleNamespace(claim_id="c1", subject="Paris", predicate="located_in",
                                object="France", polarity=1, abstention_reason=abstention)
        extractor = SimpleNamespace(extract=lambda text, ctx: [claim])
        walker = SimpleNamespace(walk=lambda c, ctx: SimpleNamespace(verdict="verified"))
        aggregator = SimpleNamespace(
            aggregate=lambda claims, results, text_input=None: _vr("verified"))
        return FakePipeline(extractor=extractor, walker=walker, aggregator=aggregator)

    def test_verify_returns_per_claim_observability(self):
        c = _client(pipeline=self._verify_pipeline())
        r = c.post("/verify", json={"text": "Paris is in France."}, headers=H())
        body = r.json()
        assert r.status_code == 200
        assert body["extracted_claims"][0]["subject"] == "Paris"
        assert body["observability"][0]["verdict"] == "verified"
        assert "given_assertion" in body

    def test_verify_all_abstain_returns_note(self):
        c = _client(pipeline=self._verify_pipeline(abstention="self_referential"))
        r = c.post("/verify", json={"text": "blah"}, headers=H())
        body = r.json()
        assert body["observability"] == []
        assert "no groundable" in body["note"]


# --------------------------------------------------------------------------- #
# SSE streaming (live steps then result)
# --------------------------------------------------------------------------- #

class TestStreaming:
    def test_chat_stream_emits_steps_then_result(self):
        c = _client(chat_wrapper=FakeChatWrapper(_vr()))
        r = c.post("/chat/stream", json={"message": "hi"}, headers=H())
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse(r.text)
        kinds = [k for k, _ in events]
        assert "step" in kinds          # progress events from the (fake) wrapper
        assert kinds.count("result") == 1
        result = next(p for k, p in events if k == "result")
        assert result["final_message"] == "draft reply"
        assert result["verification_id"] == "ver-123"

    def test_verify_stream_emits_per_claim_steps_then_result(self):
        claim = SimpleNamespace(claim_id="c1", subject="Paris", predicate="located_in",
                                object="France", polarity=1, abstention_reason=None)
        extractor = SimpleNamespace(extract=lambda text, ctx: [claim])
        walker = SimpleNamespace(walk=lambda c, ctx: SimpleNamespace(verdict="verified"))
        aggregator = SimpleNamespace(
            aggregate=lambda claims, results, text_input=None: _vr("verified"))
        c = _client(pipeline=FakePipeline(extractor=extractor, walker=walker,
                                          aggregator=aggregator))
        r = c.post("/verify/stream", json={"text": "Paris is in France."}, headers=H())
        assert r.status_code == 200
        events = _parse_sse(r.text)
        phases = [p.get("phase") for k, p in events if k == "step"]
        assert "extracting" in phases and "verifying" in phases and "verdict" in phases
        result = next(p for k, p in events if k == "result")
        assert result["observability"][0]["verdict"] == "verified"

    def test_stream_surfaces_engine_error_as_error_event(self):
        # An engine exception becomes a clean SSE 'error' event, not a dropped
        # connection / silent "network error".
        def boom(text, ctx):
            raise RuntimeError("kaboom")
        pipeline = FakePipeline(extractor=SimpleNamespace(extract=boom))
        c = _client(pipeline=pipeline)
        r = c.post("/verify/stream", json={"text": "x"}, headers=H())
        assert r.status_code == 200  # the stream itself opened fine
        events = _parse_sse(r.text)
        assert any(k == "error" for k, _ in events)
        err = next(p for k, p in events if k == "error")
        assert "RuntimeError" in err["detail"]


# --------------------------------------------------------------------------- #
# Context inspector (REAL TierU)
# --------------------------------------------------------------------------- #

class TestContextInspector:
    def test_context_returns_only_this_sessions_rows(self):
        db = open_memory_db()
        tier_u = TierU(db)
        tier_u.write(_claim("session:alice", "alice"), status="asserted_unverified")
        tier_u.write(_claim("session:alice", "ada"), status="asserted_unverified")
        tier_u.write(_claim("session:bob", "bob"), status="asserted_unverified")
        c = _client(pipeline=FakePipeline(tier_u=tier_u))

        r = c.get("/session/context", headers=H("alice"))
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2
        subjects = sorted(row["subject"] for row in body["rows"])
        assert subjects == ["ada", "alice"]
        # Bob's row is invisible to alice.
        rb = c.get("/session/context", headers=H("bob"))
        assert rb.json()["count"] == 1


# --------------------------------------------------------------------------- #
# Session isolation + reset (REAL TierU on in-memory DB)
# --------------------------------------------------------------------------- #

def _claim(party: str, subject: str) -> Claim:
    return Claim(
        claim_id=f"{party}:{subject}", subject=subject, predicate="likes",
        object="coffee", polarity=1, source_text=f"{subject} likes coffee",
        asserting_party=party, triage_decision=TriageDecision.VERIFY,
    )


class TestSessionIsolationAndReset:
    def test_reset_clears_only_calling_session(self):
        db = open_memory_db()
        tier_u = TierU(db)
        tier_u.write(_claim("session:alice", "alice"), status="asserted_unverified")
        tier_u.write(_claim("session:bob", "bob"), status="asserted_unverified")
        c = _client(pipeline=FakePipeline(tier_u=tier_u))

        r = c.post("/session/reset", headers=H("alice"))
        assert r.status_code == 200 and r.json()["rows_cleared"] == 1

        def _count(party):
            return db.execute(
                "SELECT COUNT(*) FROM tier_u WHERE asserting_party=?", (party,)
            ).fetchone()[0]

        assert _count("session:alice") == 0   # caller cleared
        assert _count("session:bob") == 1     # other session untouched

    def test_verification_is_party_scoped_via_header(self):
        c = _client(chat_wrapper=FakeChatWrapper(_vr()))
        # Session A produces ver-123.
        c.post("/chat", json={"message": "hi"}, headers=H("alice"))
        # A can read it (session in header, NOT the URL — F1).
        ok = c.get("/verification/ver-123", headers=H("alice"))
        assert ok.status_code == 200
        # B cannot (404 — never reveal another session's verification).
        nope = c.get("/verification/ver-123", headers=H("bob"))
        assert nope.status_code == 404


# --------------------------------------------------------------------------- #
# Engine hook: TierU.clear_party isolation
# --------------------------------------------------------------------------- #

class TestClearParty:
    def test_clears_party_and_isolates(self):
        db = open_memory_db()
        tier_u = TierU(db)
        tier_u.write(_claim("session:A", "a1"), status="asserted_unverified")
        tier_u.write(_claim("session:A", "a2"), status="asserted_unverified")
        tier_u.write(_claim("session:B", "b1"), status="asserted_unverified")

        removed = tier_u.clear_party("session:A")
        assert removed == 2
        assert db.execute("SELECT COUNT(*) FROM tier_u WHERE asserting_party=?",
                          ("session:A",)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM tier_u WHERE asserting_party=?",
                          ("session:B",)).fetchone()[0] == 1

    def test_falsy_party_clears_nothing(self):
        db = open_memory_db()
        tier_u = TierU(db)
        tier_u.write(_claim("session:A", "a1"), status="asserted_unverified")
        assert tier_u.clear_party("") == 0
        assert db.execute("SELECT COUNT(*) FROM tier_u").fetchone()[0] == 1


class TestRowsForParty:
    def test_returns_party_rows_only(self):
        db = open_memory_db()
        tier_u = TierU(db)
        tier_u.write(_claim("session:A", "a1"), status="asserted_unverified")
        tier_u.write(_claim("session:B", "b1"), status="asserted_unverified")
        rows = tier_u.rows_for_party("session:A")
        assert len(rows) == 1
        assert rows[0]["subject"] == "a1"
        assert rows[0]["predicate"] == "likes" and rows[0]["object"] == "coffee"
        assert "status" in rows[0] and "valid_from" in rows[0]
        assert tier_u.rows_for_party("session:B")[0]["subject"] == "b1"

    def test_falsy_party_returns_empty(self):
        db = open_memory_db()
        tier_u = TierU(db)
        tier_u.write(_claim("session:A", "a1"), status="asserted_unverified")
        assert tier_u.rows_for_party("") == []
