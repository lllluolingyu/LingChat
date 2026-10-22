"""Native monitor traffic through the real SDK and AgentGUI ASGI server."""

import asyncio
import json

import pytest

from agentgui.backends.claude_backend import ClaudeBackend
from agentgui.catalog import Catalog, ModelEntry
from agentgui.server import create_app
from tests.agentgui_fakes.socket import Socket
from tests.test_agentgui_claude_backend import FAKE


@pytest.fixture
def monitor_app(tmp_path, monkeypatch):
    gate = tmp_path / "emit-monitor"
    log = tmp_path / "cli.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_GATE", str(gate))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    entry = ModelEntry("claude", "Claude", "claude")
    app = create_app(
        catalog=Catalog([entry]),
        store_path=tmp_path / "gui.db",
        auth_token="secret",
        backend_factory=lambda rec, store, cache: ClaudeBackend(store, FAKE),
    )
    sid = app.state.store.create(entry, str(tmp_path)).id
    return app, sid, gate, log


async def wait_until(predicate):
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(0.01)


def frames(app, sid):
    return [f for t in app.state.store.turns(sid) for f in t["frames"]]


async def start_watch(app, sid, mode):
    async with Socket(app, sid) as ws:
        await ws.recv()
        await ws.send({"type": "user", "text": mode})
        initial = await ws.until("turn_end")
        assert any(f.get("name") == "Monitor" for f in initial)
    runtime = app.state.sessions[sid]
    await wait_until(lambda: not runtime.busy)
    return runtime


async def test_native_monitor_detached_approval_replay_and_next_user(monitor_app):
    app, sid, gate, log = monitor_app
    async with app.router.lifespan_context(app):
        runtime = await start_watch(app, sid, "monitor-approval")
        gate.touch()
        await wait_until(lambda: bool(runtime.pending))
        saved = frames(app, sid)
        assert [f["text"] for f in saved if f["type"] == "user"] == ["monitor-approval"]
        assert any(f.get("source") == "claude_event" for f in saved)
        assert {f.get("status") for f in saved if f.get("source") == "claude_task"} >= {
            "started",
            "progress",
            "completed",
        }
        async with Socket(app, sid) as ws:
            hello = await ws.recv()
            assert hello["running"]
            assert len(hello["approvals"]) == 1
            assert any(f.get("name") == "Bash" for f in hello["replay"])
            await ws.send(
                {
                    "type": "approval_response",
                    "id": hello["approvals"][0]["id"],
                    "decision": "deny",
                }
            )
            reply = await ws.until("turn_end")
            assert next(f for f in reply if f["type"] == "tool_result")["ok"] is False
            assert [f["delta"] for f in reply if f["type"] == "text"] == [
                "Automatic monitor reply"
            ]
            await wait_until(lambda: not runtime.busy)
            await ws.send({"type": "user", "text": "new question"})
            approval = (await ws.until("approval"))[-1]
            await ws.send(
                {"type": "approval_response", "id": approval["id"], "decision": "deny"}
            )
            answer = await ws.until("turn_end")
            assert [f["delta"] for f in answer if f["type"] == "text"] == ["hello"]
        records = [json.loads(line) for line in log.read_text().splitlines()]
        submitted = [r for r in records if r.get("type") == "user"]
        assert len(submitted) == 2  # never re-submit a native monitor notification
        assert all(r["origin"] == {"kind": "human"} for r in submitted)
        async with Socket(app, sid) as ws:
            hello = await ws.recv()
            assert not hello["running"]
            history = [f for t in hello["history"]["turns"] for f in t["frames"]]
            assert (
                sum(f.get("delta") == "Automatic monitor reply" for f in history) == 1
            )
    assert runtime.backend.reader.done()


@pytest.mark.parametrize("mode", ["monitor-notice", "monitor-update-only"])
async def test_native_task_notice_does_not_leave_a_turn_running(monitor_app, mode):
    app, sid, gate, _ = monitor_app
    async with app.router.lifespan_context(app):
        runtime = await start_watch(app, sid, mode)
        gate.touch()
        await wait_until(
            lambda: any(f.get("status") == "completed" for f in frames(app, sid))
        )
        await wait_until(lambda: not runtime.busy)
        async with Socket(app, sid) as ws:
            hello = await ws.recv()
            assert not hello["running"]
            assert not runtime.native_responding
            assert len([f for f in frames(app, sid) if f["type"] == "user"]) == 1


async def test_native_monitor_stop_and_shutdown(monitor_app):
    app, sid, gate, log = monitor_app
    async with app.router.lifespan_context(app):
        runtime = await start_watch(app, sid, "monitor-slow")
        gate.touch()
        await wait_until(lambda: runtime.native_responding)
        async with Socket(app, sid) as ws:
            assert (await ws.recv())["running"]
            await ws.send({"type": "stop"})
            ended = await ws.until("turn_end")
            assert sum(f["type"] == "cancelled" for f in ended) == 1
            await wait_until(lambda: not runtime.busy)
            assert runtime.backend.reader and not runtime.backend.reader.done()
        assert any(
            '"subtype": "interrupt"' in line for line in log.read_text().splitlines()
        )
    assert runtime.backend.reader.done()
    assert runtime.native_task.done()


async def test_native_reader_failure_finishes_automatic_response(monitor_app):
    app, sid, gate, _ = monitor_app
    async with app.router.lifespan_context(app):
        runtime = await start_watch(app, sid, "monitor-exit")
        gate.touch()
        await wait_until(lambda: runtime.backend.reader.done())
        await wait_until(lambda: not runtime.native_responding)
        assert any(f["type"] == "error" for f in frames(app, sid))
        native_id = runtime.session.native_id
        async with Socket(app, sid) as ws:
            assert not (await ws.recv())["running"]
            assert app.state.sessions[sid] is not runtime
            await ws.send({"type": "user", "text": "next"})
            approval = (await ws.until("approval"))[-1]
            await ws.send(
                {"type": "approval_response", "id": approval["id"], "decision": "deny"}
            )
            response = await ws.until("turn_end")
            assert any(f.get("delta") == "hello" for f in response)
            assert app.state.sessions[sid].session.native_id == native_id


async def test_interrupt_timeout_releases_turn_and_allows_reconnect(
    monitor_app, monkeypatch
):
    app, sid, gate, _ = monitor_app
    async with app.router.lifespan_context(app):
        runtime = await start_watch(app, sid, "monitor-slow")
        gate.touch()
        await wait_until(lambda: runtime.native_responding)

        async def timeout():
            raise TimeoutError

        monkeypatch.setattr(runtime.backend.client, "interrupt", timeout)
        async with Socket(app, sid) as ws:
            assert (await ws.recv())["running"]
            await ws.send({"type": "stop"})
            ended = await ws.until("turn_end")
            assert any(f["type"] == "cancelled" for f in ended)
            assert any(f["type"] == "error" for f in ended)
            await wait_until(lambda: runtime.native_task.done())
            assert not runtime.busy
        async with Socket(app, sid) as ws:
            assert not (await ws.recv())["running"]
            assert app.state.sessions[sid] is not runtime


async def test_injected_result_does_not_complete_pending_human_input(monitor_app):
    app, sid, gate, _ = monitor_app
    async with app.router.lifespan_context(app):
        async with Socket(app, sid) as ws:
            await ws.recv()
            runtime = app.state.sessions[sid]
            await ws.send({"type": "user", "text": "injection race"})
            automatic = await ws.until("turn_end")
            assert any(f.get("delta") == "Automatic monitor reply" for f in automatic)
            assert runtime.backend.pending
            assert runtime.native_waiting
            await ws.send({"type": "user", "text": "must wait"})
            await ws.until("turn_busy")
        async with Socket(app, sid) as ws:
            assert (await ws.recv())["running"]
            gate.touch()
            approval = (await ws.until("approval"))[-1]
            await ws.send(
                {"type": "approval_response", "id": approval["id"], "decision": "deny"}
            )
            human = await ws.until("turn_end")
            assert [f["delta"] for f in human if f["type"] == "text"] == ["hello"]
            await wait_until(lambda: not runtime.busy)
            replies = [
                t
                for t in app.state.store.turns(sid)
                if any(f["type"] == "final" for f in t["frames"])
            ]
            assert len(replies) == 2
            assert any(f.get("source") == "claude_event" for f in replies[0]["frames"])
            assert not any(
                f.get("source") == "claude_event" for f in replies[1]["frames"]
            )


async def test_shutdown_denies_detached_native_approval_and_reaps_cli(monitor_app):
    app, sid, gate, _ = monitor_app
    async with app.router.lifespan_context(app):
        runtime = await start_watch(app, sid, "monitor-approval")
        gate.touch()
        await wait_until(lambda: bool(runtime.pending))
        process = runtime.backend.client._transport._process
        assert process.returncode is None
    assert not runtime.pending
    assert runtime.backend.reader.done()
    assert runtime.native_task.done()
    assert process.returncode is not None
