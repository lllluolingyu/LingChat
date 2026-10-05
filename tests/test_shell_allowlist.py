"""Connection-local shell confirmation allowlists."""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

from lingcore.llm import LLMChunk
from lingcore.message import Message, ToolCall
from starlette.testclient import TestClient

from lingchat.server import create_app


class FakeLLM:
    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self._turns = list(turns)

    async def stream(
        self, messages: list[Message], tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[LLMChunk]:
        turn = self._turns.pop(0) if self._turns else {}
        text = str(turn.get("text", ""))
        if text:
            yield LLMChunk(text_delta=text)
        yield LLMChunk(
            tool_calls=turn.get("tool_calls"),
            finish_reason="stop",
        )


def _write_shell_profile(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    profile = tmp_path / "config.yaml"
    profile.write_text(
        "name: shell\n"
        f"workspace: {workspace}\n"
        "llm:\n"
        "  model: fake\n"
        "tools: [run_shell]\n"
        "tool_options:\n"
        "  run_shell:\n"
        "    require_confirmation: true\n",
        encoding="utf-8",
    )
    return profile


def _turns(*commands: str) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for index, command in enumerate(commands):
        turns.extend(
            [
                {
                    "tool_calls": [
                        ToolCall(
                            id=f"call-{index}",
                            name="run_shell",
                            arguments={"command": command},
                        )
                    ]
                },
                {"text": f"done {index}"},
            ]
        )
    return turns


def _receive_until(ws, stop_type: str) -> list[dict]:
    frames = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] == stop_type:
            return frames


def _confirm(ws) -> dict:
    return next(
        frame for frame in _receive_until(ws, "confirm") if frame["type"] == "confirm"
    )


def test_session_approval_skips_the_next_identical_confirmation(tmp_path):
    profile = _write_shell_profile(tmp_path)
    app = create_app(
        profile,
        require_auth=False,
        llm_factory=lambda: FakeLLM(_turns("printf ok", "printf ok")),
    )

    with TestClient(app).websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "user", "text": "first"})
        confirm = _confirm(ws)
        assert confirm["kind"] == "shell"
        assert confirm["runner"] == "host (unsandboxed)"
        assert confirm["allowlist_pattern"] == "printf ok"
        ws.send_json(
            {
                "type": "confirm_response",
                "id": confirm["id"],
                "approved": True,
                "scope": "session",
            }
        )
        first = _receive_until(ws, "turn_end")
        assert {
            "type": "shell_allowlist",
            "pattern": "printf ok",
            "added": True,
        } in first

        ws.send_json({"type": "user", "text": "again"})
        second = _receive_until(ws, "turn_end")
        assert not any(frame["type"] == "confirm" for frame in second)
        assert any(frame["type"] == "tool_result" and frame["ok"] for frame in second)


def test_shell_control_command_cannot_be_session_allowlisted(tmp_path):
    command = "echo a && echo b"
    app = create_app(
        _write_shell_profile(tmp_path),
        require_auth=False,
        llm_factory=lambda: FakeLLM(_turns(command, command)),
    )

    with TestClient(app).websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "user", "text": "first"})
        first_confirm = _confirm(ws)
        assert "allowlist_pattern" not in first_confirm
        ws.send_json(
            {
                "type": "confirm_response",
                "id": first_confirm["id"],
                "approved": True,
                "scope": "session",
            }
        )
        first = _receive_until(ws, "turn_end")
        assert {"type": "shell_allowlist", "pattern": None, "added": False} in first

        ws.send_json({"type": "user", "text": "again"})
        second_confirm = _confirm(ws)
        assert second_confirm["command"] == command
        ws.send_json(
            {
                "type": "confirm_response",
                "id": second_confirm["id"],
                "approved": False,
            }
        )
        _receive_until(ws, "turn_end")


def test_client_supplied_allowlist_pattern_is_ignored(tmp_path):
    app = create_app(
        _write_shell_profile(tmp_path),
        require_auth=False,
        llm_factory=lambda: FakeLLM(_turns("printf safe", "printf forged")),
    )

    with TestClient(app).websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "user", "text": "safe"})
        confirm = _confirm(ws)
        ws.send_json(
            {
                "type": "confirm_response",
                "id": confirm["id"],
                "approved": True,
                "scope": "session",
                "pattern": "printf forged",
            }
        )
        first = _receive_until(ws, "turn_end")
        allowlist = next(frame for frame in first if frame["type"] == "shell_allowlist")
        assert allowlist["pattern"] == "printf safe"

        ws.send_json({"type": "user", "text": "forged"})
        forged = _confirm(ws)
        assert forged["command"] == "printf forged"
        ws.send_json(
            {"type": "confirm_response", "id": forged["id"], "approved": False}
        )
        _receive_until(ws, "turn_end")


def test_session_allowlist_does_not_cross_connections(tmp_path):
    profile = _write_shell_profile(tmp_path)
    fakes = [
        FakeLLM(_turns("printf isolated")),
        FakeLLM(_turns("printf isolated")),
    ]
    app = create_app(
        profile,
        require_auth=False,
        llm_factory=lambda: fakes.pop(0),
    )
    client = TestClient(app)

    with client.websocket_connect("/ws") as first:
        first.receive_json()
        first.send_json({"type": "user", "text": "approve"})
        confirm = _confirm(first)
        first.send_json(
            {
                "type": "confirm_response",
                "id": confirm["id"],
                "approved": True,
                "scope": "session",
            }
        )
        _receive_until(first, "turn_end")

    with client.websocket_connect("/ws") as second:
        second.receive_json()
        second.send_json({"type": "user", "text": "must ask again"})
        confirm = _confirm(second)
        assert confirm["command"] == "printf isolated"
        second.send_json(
            {"type": "confirm_response", "id": confirm["id"], "approved": False}
        )
        _receive_until(second, "turn_end")


def test_skill_confirmation_never_offers_a_shell_allowlist(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_dir = tmp_path / "skills" / "dangerous"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.md").write_text(
        "---\n"
        "name: dangerous\n"
        "description: Requests a high-risk file tool.\n"
        "requested_tools: [write_file]\n"
        "---\n"
        "Write files only after activation.\n",
        encoding="utf-8",
    )
    profile = tmp_path / "config.yaml"
    profile.write_text(
        "name: skills\n"
        f"workspace: {workspace}\n"
        "llm:\n"
        "  model: fake\n"
        "tools: [activate_skill, write_file]\n"
        "initial_tools: [activate_skill]\n",
        encoding="utf-8",
    )
    turns = [
        {
            "tool_calls": [
                ToolCall(
                    id="activate",
                    name="activate_skill",
                    arguments={"name": "dangerous"},
                )
            ]
        },
        {"text": "activated"},
    ]
    app = create_app(
        profile,
        require_auth=False,
        llm_factory=lambda: FakeLLM(turns),
    )

    with TestClient(app).websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "user", "text": "activate"})
        confirm = _confirm(ws)
        assert confirm["command"].startswith("Activate skill 'dangerous'?")
        assert "allowlist_pattern" not in confirm
        ws.send_json(
            {
                "type": "confirm_response",
                "id": confirm["id"],
                "approved": True,
                "scope": "session",
            }
        )
        frames = _receive_until(ws, "turn_end")
        assert {"type": "shell_allowlist", "pattern": None, "added": False} in frames


def test_non_shell_confirmation_is_a_plain_action(tmp_path):
    # fetch_url asks before a local target. That prompt is not a run_shell
    # command, so it carries no runner and offers no session allowlist.
    profile = _write_shell_profile(tmp_path)
    profile.write_text(
        profile.read_text(encoding="utf-8").replace(
            "tools: [run_shell]", "tools: [run_shell, fetch_url]"
        ),
        encoding="utf-8",
    )
    fetch = ToolCall(
        id="call-0", name="fetch_url", arguments={"url": "http://localhost:9/"}
    )
    app = create_app(
        profile,
        require_auth=False,
        llm_factory=lambda: FakeLLM([{"tool_calls": [fetch]}, {"text": "done"}]),
    )

    with TestClient(app).websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "user", "text": "fetch"})
        confirm = _confirm(ws)
        assert confirm["kind"] == "action"
        assert "localhost" in confirm["command"]
        assert "runner" not in confirm
        assert "allowlist_pattern" not in confirm
        ws.send_json(
            {"type": "confirm_response", "id": confirm["id"], "approved": False}
        )
        frames = _receive_until(ws, "turn_end")
        assert not any(frame["type"] == "shell_allowlist" for frame in frames)
