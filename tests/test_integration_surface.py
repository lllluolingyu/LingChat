"""LingCore integration features exposed through the LingChat bridge."""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

from lingcore.llm import LLMChunk
from lingcore.message import Message, ToolCall
from starlette.testclient import TestClient

from lingchat.server import WebSession, create_app


class FakeLLM:
    def __init__(self, text: str = "done") -> None:
        self.text = text

    async def stream(
        self, messages: list[Message], tools: list[dict] | None = None
    ) -> AsyncIterator[LLMChunk]:
        yield LLMChunk(text_delta=self.text)
        yield LLMChunk(tool_calls=None, finish_reason="stop")


class FakeWebSocket:
    async def send_json(self, message: dict) -> None:
        pass


def _write_shell_profile(tmp_path: Path, *, sandbox: bool) -> Path:
    workspace = tmp_path / ("sandbox-workspace" if sandbox else "host-workspace")
    workspace.mkdir(exist_ok=True)
    profile = tmp_path / ("sandbox.yaml" if sandbox else "host.yaml")
    sandbox_yaml = "    sandbox:\n      backend: bubblewrap\n" if sandbox else ""
    profile.write_text(
        "name: integration\n"
        f"workspace: {workspace}\n"
        "llm:\n"
        "  model: fake\n"
        "tools: [run_shell]\n"
        "tool_options:\n"
        "  run_shell:\n"
        "    require_confirmation: true\n"
        f"{sandbox_yaml}",
        encoding="utf-8",
    )
    return profile


def _receive_until(ws, stop_type: str) -> list[dict]:
    frames = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] == stop_type:
            return frames


def test_bubblewrap_profile_reaches_agent_and_confirmation(tmp_path):
    class ShellLLM:
        def __init__(self) -> None:
            self.turn = 0

        async def stream(
            self, messages: list[Message], tools: list[dict] | None = None
        ) -> AsyncIterator[LLMChunk]:
            self.turn += 1
            if self.turn == 1:
                yield LLMChunk(
                    tool_calls=[
                        ToolCall(
                            id="sandboxed",
                            name="run_shell",
                            arguments={"command": "printf sandboxed"},
                        )
                    ],
                    finish_reason="stop",
                )
            else:
                yield LLMChunk(text_delta="done")
                yield LLMChunk(tool_calls=None, finish_reason="stop")

    app = create_app(
        _write_shell_profile(tmp_path, sandbox=True),
        require_auth=False,
        llm_factory=ShellLLM,
    )
    session = WebSession(
        FakeWebSocket(),
        app.state.profile,
        tmp_path,
        llm_factory=lambda: FakeLLM(),
    )
    assert session.agent.tool_ctx.options["run_shell"]["sandbox"]["backend"] == (
        "bubblewrap"
    )

    with TestClient(app).websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "user", "text": "run it"})
        confirm = next(
            frame
            for frame in _receive_until(ws, "confirm")
            if frame["type"] == "confirm"
        )
        assert confirm["runner"] == "bubblewrap"
        ws.send_json(
            {"type": "confirm_response", "id": confirm["id"], "approved": False}
        )
        _receive_until(ws, "turn_end")


def test_host_profile_confirmation_names_unsandboxed_runner(tmp_path):
    class ShellLLM:
        def __init__(self) -> None:
            self.turn = 0

        async def stream(
            self, messages: list[Message], tools: list[dict] | None = None
        ) -> AsyncIterator[LLMChunk]:
            self.turn += 1
            if self.turn == 1:
                yield LLMChunk(
                    tool_calls=[
                        ToolCall(
                            id="host",
                            name="run_shell",
                            arguments={"command": "printf host"},
                        )
                    ],
                    finish_reason="stop",
                )
            else:
                yield LLMChunk(text_delta="done")
                yield LLMChunk(tool_calls=None, finish_reason="stop")

    app = create_app(
        _write_shell_profile(tmp_path, sandbox=False),
        require_auth=False,
        llm_factory=ShellLLM,
    )
    with TestClient(app).websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "user", "text": "run it"})
        confirm = next(
            frame
            for frame in _receive_until(ws, "confirm")
            if frame["type"] == "confirm"
        )
        assert confirm["runner"] == "host (unsandboxed)"
        ws.send_json(
            {"type": "confirm_response", "id": confirm["id"], "approved": False}
        )
        _receive_until(ws, "turn_end")


def test_profile_guardrail_transforms_websocket_final(tmp_path, monkeypatch):
    module = tmp_path / "custom_guardrail.py"
    module.write_text(
        "class SuffixGuardrail:\n"
        "    def __init__(self, suffix=''):\n"
        "        self.suffix = suffix\n"
        "    async def pre_input(self, text):\n"
        "        return text\n"
        "    async def post_output(self, text):\n"
        "        return text + self.suffix\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(tmp_path)

    workspace = tmp_path / "guardrail-workspace"
    workspace.mkdir()
    profile = tmp_path / "guardrail.yaml"
    profile.write_text(
        "name: guarded\n"
        f"workspace: {workspace}\n"
        "llm:\n"
        "  model: fake\n"
        "tools: []\n"
        "guardrail:\n"
        "  policy: custom_guardrail:SuffixGuardrail\n"
        "  options:\n"
        "    suffix: ' [guarded]'\n",
        encoding="utf-8",
    )
    app = create_app(
        profile,
        require_auth=False,
        llm_factory=lambda: FakeLLM("answer"),
    )
    session = WebSession(
        FakeWebSocket(),
        app.state.profile,
        tmp_path,
        llm_factory=lambda: FakeLLM("answer"),
    )
    assert session.agent.guardrail.__class__.__name__ == "SuffixGuardrail"

    with TestClient(app).websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "user", "text": "hello"})
        frames = _receive_until(ws, "turn_end")
    assert any(
        frame["type"] == "final" and frame["text"] == "answer [guarded]"
        for frame in frames
    )
