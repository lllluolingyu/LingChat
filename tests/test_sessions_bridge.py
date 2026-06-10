"""Tests for LingChat's session REST endpoints and WebSocket resume flow.

Same approach as test_bridge.py: the real FastAPI app via Starlette's
TestClient with a scripted fake LLM. Profiles are written to tmp_path, which
sits outside the lingcore package tree, so sessions are enabled by default and
the store lands at ``<tmp profile dir>/sessions.db``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, AsyncIterator

from starlette.testclient import TestClient

from lingchat.server import create_app
from lingcore.llm import LLMChunk
from lingcore.message import Message, ToolCall


class FakeLLM:
    """Scripted fake that also records the messages seen by each call."""

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self._turns = list(turns)
        self.calls: list[list[Message]] = []

    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[LLMChunk]:
        self.calls.append(list(messages))
        if not self._turns:
            yield LLMChunk(tool_calls=None, finish_reason="stop")
            return
        turn = self._turns.pop(0)
        text = turn.get("text", "")
        for i in range(0, len(text), 4):
            yield LLMChunk(text_delta=text[i : i + 4])
        yield LLMChunk(tool_calls=turn.get("tool_calls"), finish_reason="stop")


def _write_profile(tmp_path: Path, extra: str = "") -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "name: test\n"
        f"workspace: {ws}\n"
        "llm:\n"
        "  model: fake\n"
        "tools: ['read_file']\n" + extra,
        encoding="utf-8",
    )
    return cfg


def _drain_until(ws, stop_type: str) -> list[dict]:
    msgs = []
    while True:
        m = ws.receive_json()
        msgs.append(m)
        if m["type"] == stop_type:
            return msgs


def _delete_when_released(client: TestClient, sid: str):
    """DELETE a session, allowing the just-closed socket's detach to land."""
    for _ in range(100):
        r = client.delete(f"/api/sessions/{sid}")
        if r.status_code != 409:
            return r
        time.sleep(0.01)
    return r


def test_turn_is_stored_and_listed(tmp_path):
    profile = _write_profile(tmp_path)
    app = create_app(profile, llm_factory=lambda: FakeLLM([{"text": "Hello!"}]))
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        sid = hello["session"]
        assert sid is not None and len(sid) == 32
        assert hello["title"] == ""  # fresh session: no row yet

        ws.send_json({"type": "user", "text": "first question"})
        _drain_until(ws, "turn_end")

    listing = client.get("/api/sessions").json()
    assert listing["enabled"] is True
    assert [s["id"] for s in listing["sessions"]] == [sid]
    assert listing["sessions"][0]["title"] == "first question"
    assert listing["sessions"][0]["message_count"] == 2


def test_transcript_display_shapes(tmp_path):
    profile = _write_profile(tmp_path)
    turns = [
        {"tool_calls": [ToolCall(id="c1", name="read_file", arguments={"path": "missing.txt"})]},
        {"text": "could not read it"},
    ]
    app = create_app(profile, llm_factory=lambda: FakeLLM(turns))
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        sid = ws.receive_json()["session"]
        ws.send_json({"type": "user", "text": "read that file"})
        _drain_until(ws, "turn_end")

    data = client.get(f"/api/sessions/{sid}").json()
    roles = [m["role"] for m in data["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert data["messages"][0]["text"] == "read that file"
    assert data["messages"][1]["tool_calls"] == [
        {"name": "read_file", "arguments": {"path": "missing.txt"}}
    ]
    assert data["messages"][2]["ok"] is False  # ERROR: prefix → failed result
    assert data["messages"][3]["text"] == "could not read it"


def test_resume_restores_history(tmp_path):
    profile = _write_profile(tmp_path)
    fake1 = FakeLLM([{"text": "first answer"}])
    fake2 = FakeLLM([{"text": "second answer"}])
    fakes = [fake1, fake2]
    app = create_app(profile, llm_factory=lambda: fakes.pop(0))
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        sid = ws.receive_json()["session"]
        ws.send_json({"type": "user", "text": "question one"})
        _drain_until(ws, "turn_end")

    with client.websocket_connect(f"/ws?session={sid}") as ws:
        hello = ws.receive_json()
        assert hello["session"] == sid
        assert hello["title"] == "question one"
        ws.send_json({"type": "user", "text": "question two"})
        msgs = _drain_until(ws, "turn_end")
        assert any(m["type"] == "final" and m["text"] == "second answer" for m in msgs)

    # The resumed agent's first LLM call saw the stored turn-1 history.
    contents = [m.content for m in fake2.calls[0]]
    assert "question one" in contents and "first answer" in contents

    data = client.get(f"/api/sessions/{sid}").json()
    assert [m["role"] for m in data["messages"]] == [
        "user", "assistant", "user", "assistant",
    ]


def test_unknown_valid_id_is_adopted_and_malformed_replaced(tmp_path):
    profile = _write_profile(tmp_path)
    app = create_app(profile, llm_factory=lambda: FakeLLM([{"text": "hi"}]))
    client = TestClient(app)

    minted = "ab" * 16
    with client.websocket_connect(f"/ws?session={minted}") as ws:
        assert ws.receive_json()["session"] == minted
        ws.send_json({"type": "user", "text": "speak"})
        _drain_until(ws, "turn_end")
    assert client.get(f"/api/sessions/{minted}").status_code == 200

    with client.websocket_connect("/ws?session=not-a-valid-id") as ws:
        sid = ws.receive_json()["session"]
        assert sid != "not-a-valid-id" and len(sid) == 32


def test_concurrent_attach_refused(tmp_path):
    profile = _write_profile(tmp_path)
    app = create_app(profile, llm_factory=lambda: FakeLLM([]))
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws1:
        sid = ws1.receive_json()["session"]
        with client.websocket_connect(f"/ws?session={sid}") as ws2:
            busy = ws2.receive_json()
            assert busy == {"type": "session_busy", "session": sid}


def test_delete_and_rename(tmp_path):
    profile = _write_profile(tmp_path)
    app = create_app(profile, llm_factory=lambda: FakeLLM([{"text": "yo"}]))
    client = TestClient(app)

    with client.websocket_connect("/ws") as ws:
        sid = ws.receive_json()["session"]
        ws.send_json({"type": "user", "text": "hello"})
        _drain_until(ws, "turn_end")

        # Attached: deletion refused so the live agent can't resurrect the row.
        assert client.delete(f"/api/sessions/{sid}").status_code == 409

    r = client.patch(f"/api/sessions/{sid}", json={"title": "renamed chat"})
    assert r.status_code == 200 and r.json()["title"] == "renamed chat"
    assert client.patch(f"/api/sessions/{sid}", json={"title": "   "}).status_code == 422
    assert client.patch(f"/api/sessions/{'9' * 32}", json={"title": "x"}).status_code == 404

    assert _delete_when_released(client, sid).status_code == 200
    assert client.get(f"/api/sessions/{sid}").status_code == 404
    assert client.delete(f"/api/sessions/{sid}").status_code == 404


def test_sessions_disabled_profile(tmp_path):
    profile = _write_profile(tmp_path, extra="sessions:\n  enabled: false\n")
    app = create_app(profile, llm_factory=lambda: FakeLLM([{"text": "ephemeral"}]))
    client = TestClient(app)

    listing = client.get("/api/sessions").json()
    assert listing == {"enabled": False, "notice": None, "sessions": []}

    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["session"] is None
        ws.send_json({"type": "user", "text": "hi"})
        msgs = _drain_until(ws, "turn_end")
        assert any(m["type"] == "final" for m in msgs)

    assert not (tmp_path / "sessions.db").exists()
    assert client.get(f"/api/sessions/{'a' * 32}").status_code == 404


def test_sessions_in_package_profile_serves_notice(tmp_path, monkeypatch):
    """A profile inside the installed package can't persist — the API says why."""
    import lingcore.sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "_PACKAGE_DIR", tmp_path.resolve())
    profile = _write_profile(tmp_path)
    app = create_app(profile, llm_factory=lambda: FakeLLM([]))
    client = TestClient(app)

    listing = client.get("/api/sessions").json()
    assert listing["enabled"] is False
    assert "inside the installed" in listing["notice"]
    assert not (tmp_path / "sessions.db").exists()
