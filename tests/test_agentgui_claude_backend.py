import asyncio
import json
from pathlib import Path

import pytest
from lingcore.message import Attachment

from agentgui.backends.base import UserTurn
from agentgui.backends.claude_backend import ClaudeBackend
from agentgui.catalog import ModelEntry
from agentgui.store import Store

FAKE = str(Path(__file__).parent.joinpath("agentgui_fakes/fake_claude").resolve())


@pytest.mark.parametrize("answer", ["once", "session", "deny"])
async def test_real_sdk_fake_cli_approval_images_resume_fork(
    tmp_path, monkeypatch, answer
):
    log = tmp_path / "claude.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "gui.db")
    rec = store.create(
        ModelEntry("claude", "Claude", "claude", "sonnet"), str(workspace)
    )
    approvals = []

    async def approve(request):
        approvals.append(request)
        return answer

    backend = ClaudeBackend(store, FAKE)
    try:
        await asyncio.wait_for(backend.start(rec, approve), 5)
        image = Attachment(
            kind="image",
            media_type="image/png",
            data="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6Pb8AAAAASUVORK5CYII=",
            name="pixel.png",
        )
        async with asyncio.timeout(8):
            frames = [
                f.to_wire() async for f in backend.run_turn(UserTurn("edit", [image]))
            ]
        assert approvals[0].kind == "edit"
        assert "-before" in approvals[0].diff and "+after" in approvals[0].diff
        assert next(f for f in frames if f["type"] == "tool_result")["ok"] == (
            answer != "deny"
        )
        assert [f["delta"] for f in frames if f["type"] == "text"] == ["hello"]
        assert next(f for f in frames if f["type"] == "usage")["cost_usd"] == 0.001
        assert rec.native_id
        native = rec.native_id
        child = await backend.fork(None)
        assert child.options["_fork_pending"] is True
        await backend.close()
        backend = ClaudeBackend(store, FAKE)
        await backend.start(rec, approve)
        async with asyncio.timeout(8):
            frames = [f async for f in backend.run_turn(UserTurn("resume"))]
        assert rec.native_id == native
        await backend.close()
        backend = ClaudeBackend(store, FAKE)
        await backend.start(child, approve)
        async with asyncio.timeout(8):
            frames = [f async for f in backend.run_turn(UserTurn("fork"))]
        assert child.native_id != native and "_fork_pending" not in child.options
        records = [json.loads(line) for line in log.read_text().splitlines()]
        assert all(
            r["pid"] == r["pgid"]
            for r in records
            if "pid" in r and "--output-format" in r.get("argv", [])
        )
        assert any(
            any(arg.startswith("--resume=") for arg in r.get("argv", []))
            for r in records
        )
        user = next(r for r in records if r.get("type") == "user")
        assert user["message"]["content"][1]["source"]["type"] == "base64"
    finally:
        await backend.close()
        store.close()


async def test_claude_interrupt_drains_result(tmp_path):
    store = Store(tmp_path / "gui.db")
    rec = store.create(ModelEntry("claude", "Claude", "claude"), str(tmp_path))
    backend = ClaudeBackend(store, FAKE)

    async def deny(request):
        return "deny"

    await asyncio.wait_for(backend.start(rec, deny), 5)
    started = asyncio.Event()
    frames = []

    async def consume():
        async for value in backend.run_turn(UserTurn("slow")):
            frames.append(value.to_wire())
            if value.type == "thinking":
                started.set()

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert await asyncio.wait_for(backend.stop(), 5) == []
        await task
        assert frames[-1]["type"] == "cancelled"
        assert any(f["type"] == "usage" for f in frames)
    finally:
        await backend.close()
        store.close()
