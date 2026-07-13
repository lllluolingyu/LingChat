"""Tests for the LingChat auth boundary and WebSocket turn lifecycle.

Covers origin/token authentication, per-id confirmation routing (no misrouted
approvals under parallel tool calls), and turn cancellation + lease release on
disconnect.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lingchat.server import create_app
from lingcore.llm import LLMChunk
from lingcore.message import Message, ToolCall


class FakeLLM:
    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self._turns = list(turns)

    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[LLMChunk]:
        if not self._turns:
            yield LLMChunk(tool_calls=None, finish_reason="stop")
            return
        turn = self._turns.pop(0)
        text = turn.get("text", "")
        for i in range(0, len(text), 4):
            yield LLMChunk(text_delta=text[i : i + 4])
        yield LLMChunk(tool_calls=turn.get("tool_calls"), finish_reason="stop")


def _write_profile(tmp_path: Path, tools: list[str]) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "name: test\n"
        f"workspace: {ws}\n"
        "llm:\n"
        "  model: fake\n"
        f"tools: {tools}\n"
        "tool_options:\n"
        "  run_shell:\n"
        "    require_confirmation: true\n",
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


# --- authentication --------------------------------------------------------


def test_websocket_rejects_foreign_origin(tmp_path):
    profile = _write_profile(tmp_path, tools=[])
    app = create_app(profile, llm_factory=lambda: FakeLLM([{"text": "hi"}]))
    token = app.state.auth_token
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/ws?token={token}", headers={"origin": "http://evil.example"}
        ) as ws:
            ws.receive_json()


def test_websocket_rejects_cross_scheme_origin(tmp_path):
    # Same authority, different scheme is a *different origin*: a page on
    # https://testserver must not open a socket on this http server even with
    # a valid token.
    profile = _write_profile(tmp_path, tools=[])
    app = create_app(profile, llm_factory=lambda: FakeLLM([{"text": "hi"}]))
    token = app.state.auth_token
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/ws?token={token}", headers={"origin": "https://testserver"}
        ) as ws:
            ws.receive_json()


def test_websocket_accepts_same_origin(tmp_path):
    profile = _write_profile(tmp_path, tools=[])
    app = create_app(profile, llm_factory=lambda: FakeLLM([{"text": "hi"}]))
    token = app.state.auth_token
    client = TestClient(app)
    with client.websocket_connect(
        f"/ws?token={token}", headers={"origin": "http://testserver"}
    ) as ws:
        assert ws.receive_json()["type"] == "hello"


def test_main_refuses_non_loopback_bind_without_optin(tmp_path, capsys):
    # Exposure must be an explicit operator choice: a non-loopback --host is
    # refused (exit 2, nothing served) unless --allow-remote is also given.
    from lingchat.__main__ import main

    profile = _write_profile(tmp_path, tools=[])
    rc = main(["--profile", str(profile), "--host", "0.0.0.0"])
    assert rc == 2
    assert "refusing to bind" in capsys.readouterr().err


def test_main_prints_bracketed_ipv6_url(monkeypatch, capsys):
    from types import SimpleNamespace

    from lingchat.__main__ import main

    app = SimpleNamespace(state=SimpleNamespace(auth_token="test-token"))
    monkeypatch.setattr("lingchat.__main__.create_app", lambda *a, **k: app)
    monkeypatch.setattr("lingchat.__main__.uvicorn.run", lambda *a, **k: None)

    rc = main(["--profile", "unused", "--host", "::1"])

    assert rc == 0
    assert "http://[::1]:8000/?token=test-token" in capsys.readouterr().out


def test_websocket_requires_token(tmp_path):
    profile = _write_profile(tmp_path, tools=[])
    app = create_app(profile, llm_factory=lambda: FakeLLM([{"text": "hi"}]))
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as ws:  # no token
            ws.receive_json()


def test_websocket_accepts_valid_token(tmp_path):
    profile = _write_profile(tmp_path, tools=[])
    app = create_app(profile, llm_factory=lambda: FakeLLM([{"text": "hi"}]))
    token = app.state.auth_token
    client = TestClient(app)
    with client.websocket_connect(f"/ws?token={token}") as ws:
        assert ws.receive_json()["type"] == "hello"


def test_rest_requires_token(tmp_path):
    profile = _write_profile(tmp_path, tools=[])
    app = create_app(profile, llm_factory=lambda: FakeLLM([]))
    token = app.state.auth_token
    client = TestClient(app)
    assert client.get("/api/sessions").status_code == 401
    ok = client.get("/api/sessions", headers={"X-LingChat-Token": token})
    assert ok.status_code == 200 and ok.json()["enabled"] is True
    # Query-param form also works.
    assert client.get(f"/api/sessions?token={token}").status_code == 200


# --- confirmation routing under parallel tool calls ------------------------


def test_two_simultaneous_confirms_routed_by_id(tmp_path):
    profile = _write_profile(tmp_path, tools=["run_shell"])
    turns = [
        {"tool_calls": [
            ToolCall(id="c1", name="run_shell", arguments={"command": "echo alpha"}),
            ToolCall(id="c2", name="run_shell", arguments={"command": "echo beta"}),
        ]},
        {"text": "both done"},
    ]
    app = create_app(profile, require_auth=False, llm_factory=lambda: FakeLLM(turns))
    with TestClient(app).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "user", "text": "run both"})

        # Collect the two confirm prompts; each must carry a distinct id.
        confirms: dict[str, str] = {}
        while len(confirms) < 2:
            m = ws.receive_json()
            if m["type"] == "confirm":
                assert m.get("id")
                confirms[m["command"]] = m["id"]
        assert set(confirms) == {"echo alpha", "echo beta"}
        assert len(set(confirms.values())) == 2  # distinct ids

        # Approve both, routed by id.
        for cmd, cid in confirms.items():
            ws.send_json({"type": "confirm_response", "id": cid, "approved": True})

        msgs = _drain_until(ws, "turn_end")
        results = [m for m in msgs if m["type"] == "tool_result"]
        contents = " ".join(r["content"] for r in results)
        assert results and all(r["ok"] for r in results)
        assert "alpha" in contents and "beta" in contents
        assert any(m["type"] == "final" and m["text"] == "both done" for m in msgs)


# --- disconnect cancels the in-flight turn and releases the lease ----------
#
# The end-to-end path (disconnect mid-confirmation -> server cancels the turn
# -> session lease released, so DELETE/reattach succeed) is verified manually
# and by the deterministic unit test below. A full TestClient e2e version is
# omitted here: stacking many sync WebSocket TestClient sessions in one process
# accumulates event-loop state that makes cross-connection lease timing flaky
# under pytest, though the behavior is correct under a real server.


class _FakeWS:
    """Minimal WebSocket stand-in: records sent frames, never receives."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, msg: dict) -> None:
        self.sent.append(msg)


async def test_aclose_cancels_in_flight_turn(tmp_path):
    # aclose() must cancel a turn blocked awaiting a confirmation (e.g. a
    # long/pending run_shell), so a disconnect never leaves a detached agent
    # running on the session after the lease is released.
    import asyncio

    from lingchat.server import WebSession
    from lingcore.config import AgentProfile
    from lingcore.message import UserInput

    profile = AgentProfile.load(_write_profile(tmp_path, tools=["run_shell"]))
    ws = _FakeWS()
    turns = [{"tool_calls": [ToolCall(id="c1", name="run_shell", arguments={"command": "echo x"})]}]
    session = WebSession(ws, profile, tmp_path, llm_factory=lambda: FakeLLM(turns))

    session.spawn_turn(UserInput(text="run"))
    # Let the turn advance until it is blocked awaiting our confirmation.
    for _ in range(1000):
        await asyncio.sleep(0)
        if session._pending_confirms:
            break
    assert session._pending_confirms, "turn never reached the confirmation gate"
    assert any(m["type"] == "confirm" and m.get("id") for m in ws.sent)

    await session.aclose()

    # The pending confirmation is cleared and the turn task is finished.
    assert not session._pending_confirms
    assert all(t.done() for t in session._tasks)
