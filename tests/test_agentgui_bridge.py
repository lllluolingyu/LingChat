# Adapted from LingChat bridge/session/auth tests; Apache-2.0.
import base64
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from lingcore.message import ToolCall

from tests.agentgui_fakes.socket import Socket

AUTH = {"X-AgentGUI-Token": "secret"}


async def test_auth_and_origin(app_factory):
    app, rec, _ = app_factory()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/api/sessions")).status_code == 401
        assert (await client.get("/api/sessions", headers=AUTH)).status_code == 200
        assert (
            await client.post(
                "/api/sessions",
                headers=AUTH,
                json={"model_id": "fake", "workspace": "/missing"},
            )
        ).status_code == 422
        assert (
            await client.get("/api/sessions/missing", headers=AUTH)
        ).status_code == 404
    for token, origin, code in [
        ("wrong", "http://test", 4401),
        ("secret", "http://evil", 4403),
        ("secret", "https://test", 4403),
    ]:
        async with Socket(app, rec.id, token, origin) as ws:
            assert ws.handshake == {
                "type": "websocket.close",
                "code": code,
                "reason": "",
            }


async def test_stream_replay_resume_and_session_lease(app_factory):
    app, rec, fake = app_factory([{"text": "one"}, {"text": "two"}])
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=AUTH
    ) as client:
        async with Socket(app, rec.id) as ws:
            hello = await ws.recv()
            assert hello["type"] == "hello" and hello["backend"] == "lingcore"
            async with Socket(app, rec.id) as other:
                assert (await other.recv())["type"] == "session_busy"
            assert (await client.delete(f"/api/sessions/{rec.id}")).status_code == 409
            await ws.send({"type": "user", "text": "remember the name Alice"})
            frames = await ws.until("turn_end")
            assert "".join(f["delta"] for f in frames if f["type"] == "text") == "one"
            replay = (await client.get(f"/api/sessions/{rec.id}")).json()["turns"]
            assert [t["role"] for t in replay] == ["user", "assistant"]
            assert replay[0]["seq"] == 0
            assert (
                await client.patch(f"/api/sessions/{rec.id}", json={"title": "Renamed"})
            ).status_code == 200
        async with Socket(app, rec.id) as ws:
            assert (await ws.recv())["title"] == "Renamed"
            await ws.send({"type": "user", "text": "what name?"})
            await ws.until("turn_end")
            assert any("Alice" in m.content for m in fake.messages[-1])
        assert (await client.delete(f"/api/sessions/{rec.id}")).status_code == 200


@pytest.mark.parametrize(
    "answer,approved",
    [
        ("once", True),
        ("session", True),
        ("deny", False),
        (True, False),
        ("true", False),
        ({}, False),
    ],
)
async def test_approval_is_explicit_and_keyed(app_factory, answer, approved):
    app, rec, _ = app_factory(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="shell-1",
                        name="run_shell",
                        arguments={"command": "echo hello"},
                    )
                ]
            },
            {"text": "done"},
        ],
        ["run_shell"],
    )
    async with Socket(app, rec.id) as ws:
        await ws.recv()
        await ws.send({"type": "user", "text": "echo"})
        before = await ws.until("approval")
        assert any(f["type"] == "tool_call" and f["id"] == "shell-1" for f in before)
        prompt = before[-1]
        assert prompt["kind"] == "shell"
        await ws.send({"type": "approval_response", "id": "wrong", "decision": "once"})
        await ws.send(
            {"type": "approval_response", "id": prompt["id"], "decision": answer}
        )
        frames = await ws.until("turn_end")
        result = next(f for f in frames if f["type"] == "tool_result")
        assert result["id"] == "shell-1" and result["ok"] == approved


async def test_stop_pending_approval_and_next_turn(app_factory):
    app, rec, _ = app_factory(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="shell",
                        name="run_shell",
                        arguments={"command": "echo hello"},
                    )
                ]
            },
            {"text": "next"},
        ],
        ["run_shell"],
    )
    async with Socket(app, rec.id) as ws:
        await ws.recv()
        await ws.send({"type": "user", "text": "run"})
        await ws.until("approval")
        await ws.send({"type": "user", "text": "double submit"})
        assert (await ws.recv())["type"] == "turn_busy"
        await ws.send({"type": "stop"})
        frames = await ws.until("turn_end")
        assert sum(f["type"] == "cancelled" for f in frames) == 1
        await ws.send({"type": "user", "text": "continue"})
        frames = await ws.until("turn_end")
        assert any(f["type"] == "final" for f in frames)


async def test_disconnect_cancels_stream_and_releases_lease(app_factory):
    app, rec, _ = app_factory([{"wait": True}, {"text": "resumed"}])
    async with Socket(app, rec.id) as ws:
        await ws.recv()
        await ws.send({"type": "user", "text": "slow"})
        await ws.until("text")
    async with Socket(app, rec.id) as ws:
        assert (await ws.recv())["type"] == "hello"
        await ws.send({"type": "user", "text": "resume"})
        assert any(f["type"] == "final" for f in await ws.until("turn_end"))


async def test_edit_and_native_fork(app_factory):
    app, rec, fake = app_factory([{"text": "first"}, {"text": "second"}])
    async with (
        Socket(app, rec.id) as ws,
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=AUTH
        ) as client,
    ):
        await ws.recv()
        await ws.send({"type": "user", "text": "original"})
        await ws.until("turn_end")
        fork = await client.post(
            f"/api/sessions/{rec.id}/fork", json={"through_seq": 1}
        )
        assert fork.status_code == 200, fork.text
        child = fork.json()
        assert child["native_id"] != app.state.store.get(rec.id).native_id
        await ws.send({"type": "edit", "seq": 0, "text": "replacement"})
        frames = await ws.until("turn_end")
        assert frames[0]["type"] == "edit_accepted"
        assert fake.messages[-1][-1].content == "replacement"
        original = (await client.get(f"/api/sessions/{rec.id}")).json()["turns"]
        copied = (await client.get(f"/api/sessions/{child['id']}")).json()["turns"]
        assert original[0]["frames"][0]["text"] == "replacement"
        assert copied[0]["frames"][0]["text"] == "original"
        assert (
            await client.post(
                f"/api/sessions/{rec.id}/fork", json={"through_seq": True}
            )
        ).status_code == 422


async def test_attachments_validation_and_private_download(app_factory):
    app, rec, _ = app_factory()
    attachment = {
        "kind": "text",
        "media_type": "text/plain",
        "name": "notes.txt",
        "data": base64.b64encode(b"private notes").decode(),
    }
    async with (
        Socket(app, rec.id) as ws,
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=AUTH
        ) as client,
    ):
        await ws.recv()
        await ws.send({"type": "user", "text": "attached", "attachments": [attachment]})
        await ws.until("turn_end")
        turns = (await client.get(f"/api/sessions/{rec.id}")).json()["turns"]
        displayed = turns[0]["frames"][0]["attachments"][0]
        assert (
            displayed["download"]
            and "data" not in displayed
            and "fallback_text" not in displayed
        )
        response = await client.get(f"/api/sessions/{rec.id}/messages/0/attachments/0")
        assert (
            response.content == b"private notes"
            and response.headers["x-content-type-options"] == "nosniff"
        )
        assert (
            await client.get(f"/api/sessions/{rec.id}/messages/0/attachments/-1")
        ).status_code == 404
        await ws.send(
            {
                "type": "user",
                "text": "bad",
                "attachments": [{**attachment, "data": "not base64!"}],
            }
        )
        assert (await ws.until("turn_end"))[0]["type"] == "error"


async def test_ask_offers_no_direct_write_tool(app_factory):
    """LingCore cannot approve an individual write, so `ask` withholds the tool.

    `ctx.confirm` is reachable from run_shell, skill gating and subagent spawn
    only -- never from write_file -- so the ceiling is the only way this backend
    can promise that nothing is written without the user agreeing to it. An
    approved shell command remains the way to act.
    """

    app, rec, _ = app_factory(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="write",
                        name="write_file",
                        arguments={"path": "bad.txt", "content": "bad"},
                    )
                ]
            },
            {"text": "denied"},
        ],
        ["write_file", "run_shell", "read_file"],
        "ask",
    )
    async with Socket(app, rec.id) as ws:
        await ws.recv()
        await ws.send({"type": "user", "text": "write"})
        frames = await ws.until("turn_end")
        assert next(f for f in frames if f["type"] == "tool_result")["ok"] is False
        assert not (Path(rec.workspace) / "bad.txt").exists()


async def test_edit_writes_the_workspace_without_asking(app_factory):
    app, rec, _ = app_factory(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="write",
                        name="write_file",
                        arguments={"path": "good.txt", "content": "good"},
                    )
                ]
            },
            {"text": "written"},
        ],
        ["write_file", "run_shell", "read_file"],
        "edit",
    )
    async with Socket(app, rec.id) as ws:
        await ws.recv()
        await ws.send({"type": "user", "text": "write"})
        frames = await ws.until("turn_end")
        assert next(f for f in frames if f["type"] == "tool_result")["ok"] is True
        # No approval frame: that is what distinguishes this level from `ask`.
        assert not [f for f in frames if f["type"] == "approval"]
        assert (Path(rec.workspace) / "good.txt").read_text() == "good"
