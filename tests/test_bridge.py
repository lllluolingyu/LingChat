"""Tests for the LingChat WebSocket bridge.

These drive the real FastAPI app via Starlette's TestClient, but with a scripted
fake LLM injected through ``create_app(llm_factory=...)`` — no network, no model,
no API key. ``tests/fakes.py`` from the lingcore repo isn't shipped, so the fake
is defined locally here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

from starlette.testclient import TestClient

from lingchat.server import create_app
from lingcore.llm import LLMChunk
from lingcore.message import Message, ToolCall


class FakeLLM:
    """Scripted stand-in for LLMClient: pops one turn per ``stream`` call."""

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
    ws.mkdir()
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


def test_hello_then_streamed_text_and_final(tmp_path):
    profile = _write_profile(tmp_path, tools=[])
    app = create_app(profile, llm_factory=lambda: FakeLLM([{"text": "Hello there!"}]))
    with TestClient(app).websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["agent"] == "test"

        ws.send_json({"type": "user", "text": "hi"})
        msgs = _drain_until(ws, "turn_end")
        text = "".join(m["text"] for m in msgs if m["type"] == "text")
        assert text == "Hello there!"
        assert any(m["type"] == "final" for m in msgs)


def test_shell_confirm_round_trip_approved(tmp_path):
    profile = _write_profile(tmp_path, tools=["run_shell"])
    turns = [
        {"tool_calls": [ToolCall(id="c1", name="run_shell", arguments={"command": "echo hi"})]},
        {"text": "done"},
    ]
    app = create_app(profile, llm_factory=lambda: FakeLLM(turns))
    with TestClient(app).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "user", "text": "run echo"})

        # Collect events until the agent asks us to confirm the command.
        saw_tool_call = False
        while True:
            m = ws.receive_json()
            if m["type"] == "tool_call":
                saw_tool_call = True
                assert m["name"] == "run_shell"
            if m["type"] == "confirm":
                assert m["command"] == "echo hi"
                break
        assert saw_tool_call

        ws.send_json({"type": "confirm_response", "approved": True})

        msgs = _drain_until(ws, "turn_end")
        results = [m for m in msgs if m["type"] == "tool_result"]
        assert results and results[0]["ok"] is True
        assert "hi" in results[0]["content"]
        assert any(m["type"] == "final" and m["text"] == "done" for m in msgs)


def test_shell_confirm_round_trip_denied(tmp_path):
    profile = _write_profile(tmp_path, tools=["run_shell"])
    turns = [
        {"tool_calls": [ToolCall(id="c1", name="run_shell", arguments={"command": "rm -rf /"})]},
        {"text": "ok, skipped"},
    ]
    app = create_app(profile, llm_factory=lambda: FakeLLM(turns))
    with TestClient(app).websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "user", "text": "danger"})

        while ws.receive_json()["type"] != "confirm":
            pass
        ws.send_json({"type": "confirm_response", "approved": False})

        msgs = _drain_until(ws, "turn_end")
        results = [m for m in msgs if m["type"] == "tool_result"]
        # Denied command becomes a failed tool result fed back to the model.
        assert results and results[0]["ok"] is False
        assert "declined" in results[0]["content"]
