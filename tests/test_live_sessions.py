"""Disconnect/reconnect and shutdown through both real ASGI frontends."""

from __future__ import annotations

import asyncio
import copy
import json
import shlex
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from lingcore.message import ToolCall
from lingcore.sandbox import ShellExecution, _kill_and_reap

from agentgui.backends.lingcore_backend import LingCoreBackend
from agentgui.catalog import Catalog, ModelEntry
from agentgui.server import create_app as create_gui
from lingchat.live import LiveChannel
from lingchat.server import create_app as create_legacy
from tests.agentgui_fakes.llm import FakeLLM
from tests.agentgui_fakes.socket import Socket


@pytest.fixture(params=[False, True], ids=["lingchat", "agentgui"])
def live_app(request, tmp_path):
    gui = request.param

    def make(turns, *, process=False, persist=True):
        tools = ["run_shell"]
        if process:
            tools += [
                "process_start",
                "process_list",
                "process_monitor",
                "process_stop",
            ]
        profile = tmp_path / "config.yaml"
        profile.write_text(
            f"name: live\nworkspace: {tmp_path}\nllm:\n  model: fake\n"
            f"tools: {tools}\n"
            + ("plugins: [process]\n" if process else "")
            + ("sessions:\n  enabled: false\n" if not persist else "")
            + "tool_options:\n  run_shell:\n    require_confirmation: true\n"
            + ("    sandbox:\n      backend: bubblewrap\n" if process else "")
        )
        fake = FakeLLM(turns)
        if gui:
            entry = ModelEntry(
                "fake", "Fake", "lingcore", options={"profile": str(profile)}
            )
            app = create_gui(
                catalog=Catalog([entry]),
                store_path=tmp_path / "gui.db",
                auth_token="secret",
                backend_factory=lambda rec, store, cache: LingCoreBackend(
                    store, cache, lambda: fake
                ),
            )

            def new_session():
                return app.state.store.create(entry, str(tmp_path), "edit").id
        else:
            app = create_legacy(profile, auth_token="secret", llm_factory=lambda: fake)

            def new_session():
                return uuid4().hex

        return app, new_session, fake, gui

    return make


def runtime_agent(runtime, gui):
    return runtime.backend.agent if gui else runtime.agent


def runtime_task(runtime, gui):
    return runtime.turn_task if gui else runtime._turn_task


async def wait_until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_monitor_waits_for_active_turn_then_wakes_detached_agent(
    live_app, monkeypatch
):
    gate = asyncio.Event()
    script = (
        "import time; from pathlib import Path; "
        "exec(\"while not Path('emit-event').exists(): time.sleep(.01)\"); "
        "print('watched file changed', flush=True); time.sleep(300)"
    )
    command = f"{shlex.quote(sys.executable)} -u -c {shlex.quote(script)}"
    app, new_session, fake, gui = live_app(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="watch",
                        name="process_monitor",
                        arguments={"command": command},
                    )
                ]
            },
            {"wait": gate, "text": "watching"},
            {
                "tool_calls": [
                    ToolCall(
                        id="shell",
                        name="run_shell",
                        arguments={"command": "echo approval-still-required"},
                    )
                ]
            },
            {"text": "Checked change; action declined"},
        ],
        process=True,
    )
    sid = new_session()

    async def launch(command, *, workspace, **kwargs):
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=workspace,
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async def cleanup(abort):
            if abort:
                await _kill_and_reap(proc)

        return ShellExecution(proc, "test", proc.wait, cleanup)

    async with app.router.lifespan_context(app):
        async with Socket(app, sid) as ws:
            await ws.recv()
            runtime = app.state.sessions[sid]
            agent = runtime_agent(runtime, gui)
            hooks = agent.tool_ctx.plugins["process"]
            monkeypatch.setattr(
                sys.modules[type(hooks).__module__], "launch_shell", launch
            )
            await ws.send({"type": "user", "text": "watch the file"})
            request = (await ws.until("approval" if gui else "confirm"))[-1]
            assert request["kind"] == ("shell" if gui else "process")
            await ws.send(
                {"type": "approval_response", "id": request["id"], "decision": "once"}
                if gui
                else {"type": "confirm_response", "id": request["id"], "approved": True}
            )
            await ws.until("text")
            proc = hooks.processes()[0].execution.process
            (agent.tool_ctx.workspace / "emit-event").write_text("go")
            await asyncio.wait_for(agent.background.wait(), 3)
            await asyncio.sleep(0.2)
            assert (
                len(fake.messages) == 2
            )  # event cannot interleave with an active turn
        gate.set()
        pending = runtime.pending if gui else runtime._pending_confirms
        await wait_until(lambda: bool(pending))
        assert len(fake.messages) == 3
        monitor = next(m for m in agent.memory.messages if m.name == "monitor")
        assert "watched file changed" in monitor.content
        async with Socket(app, sid) as ws:
            hello = await ws.recv()
            assert hello["running"] and len(hello["approvals"]) == 1
            assert "monitor" in str(hello["history"])
            request = hello["approvals"][0]
            await ws.send(
                {"type": "approval_response", "id": request["id"], "decision": "deny"}
                if gui
                else {
                    "type": "confirm_response",
                    "id": request["id"],
                    "approved": False,
                }
            )
            frames = await ws.until("turn_end")
            assert any(f["type"] == "tool_result" and not f["ok"] for f in frames)
        assert len(fake.messages) == 4
        assert proc.returncode is None
    assert proc.returncode is not None
    assert len(fake.messages) == 4  # shutdown cannot wake the Agent again


async def test_live_stream_reconnects_without_losing_or_repeating_progress(live_app):
    gate = asyncio.Event()
    app, new_session, fake, gui = live_app([{"wait": gate, "text": " finished"}])
    sid = new_session()
    async with app.router.lifespan_context(app):
        async with Socket(app, sid) as ws:
            await ws.recv()
            await ws.send({"type": "user", "text": "continue when I leave"})
            await ws.until("text")
        runtime = app.state.sessions[sid]
        task = runtime_task(runtime, gui)
        assert not task.done()
        async with Socket(app, sid) as ws:
            hello = await ws.recv()
            assert hello["running"]
            assert "continue when I leave" in str(hello["history"])
            assert [f for f in hello["replay"] if f["type"] == "text"] == [
                {"type": "text", "delta" if gui else "text": "working"}
            ]
            gate.set()
            frames = await ws.until("turn_end")
            assert (
                "".join(
                    f.get("delta", f.get("text", ""))
                    for f in frames
                    if f["type"] == "text"
                )
                == " finished"
            )
            assert app.state.sessions[sid] is runtime
        assert len(fake.messages) == 1
        async with Socket(app, sid) as ws:
            hello = await ws.recv()
            assert not hello["running"] and not hello["replay"]
            assert "working finished" in str(hello["history"])


async def test_approval_created_while_detached_waits_for_the_same_user(live_app):
    gate = asyncio.Event()
    app, new_session, _, gui = live_app(
        [
            {
                "wait": gate,
                "tool_calls": [
                    ToolCall(
                        id="shell",
                        name="run_shell",
                        arguments={"command": "echo allowed"},
                    )
                ],
            },
            {"text": "done"},
        ]
    )
    sid = new_session()
    async with app.router.lifespan_context(app):
        async with Socket(app, sid) as ws:
            await ws.recv()
            await ws.send({"type": "user", "text": "run"})
            await ws.until("text")
        runtime = app.state.sessions[sid]
        gate.set()
        pending = runtime.pending if gui else runtime._pending_confirms
        await wait_until(lambda: bool(pending))
        assert not runtime_task(runtime, gui).done()
        async with Socket(app, sid) as ws:
            hello = await ws.recv()
            assert hello["running"] and len(hello["approvals"]) == 1
            request = hello["approvals"][0]
            await ws.send(
                {"type": "approval_response", "id": request["id"], "decision": "once"}
                if gui
                else {"type": "confirm_response", "id": request["id"], "approved": True}
            )
            frames = await ws.until("turn_end")
            assert any(f["type"] == "tool_result" and f["ok"] for f in frames)


async def test_shutdown_interrupts_all_detached_turns_and_pending_approvals(live_app):
    app, new_session, _, gui = live_app(
        [
            {"wait": True},
            {
                "tool_calls": [
                    ToolCall(
                        id="shell",
                        name="run_shell",
                        arguments={"command": "echo never"},
                    )
                ]
            },
        ]
    )
    runtimes = []
    tasks = []
    async with app.router.lifespan_context(app):
        for index in range(2):
            sid = new_session()
            async with Socket(app, sid) as ws:
                await ws.recv()
                await ws.send({"type": "user", "text": f"task {index}"})
                await ws.until(
                    "text" if index == 0 else "approval" if gui else "confirm"
                )
            runtime = app.state.sessions[sid]
            runtimes.append(runtime)
            tasks.append(runtime_task(runtime, gui))
        assert all(not task.done() for task in tasks)
    assert all(task.done() for task in tasks)
    assert not app.state.sessions
    for runtime in runtimes:
        agent = runtime_agent(runtime, gui)
        assert agent._closed and agent._turn_checkpoint is None
        assert not (runtime.pending if gui else runtime._pending_confirms)


async def test_background_process_outlives_viewer_and_is_reaped_on_shutdown(
    live_app, monkeypatch
):
    command = f"{shlex.quote(sys.executable)} -u -c 'import time; print(\"ready\"); time.sleep(300)'"
    app, new_session, _, gui = live_app(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="start",
                        name="process_start",
                        arguments={"command": command, "ready_pattern": "ready"},
                    )
                ]
            },
            {"text": "server is running"},
        ],
        process=True,
    )
    sid = new_session()
    executions = []

    async def launch(command, *, workspace, **kwargs):
        # Exercise actual process supervision without requiring Bubblewrap in CI.
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=workspace,
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async def cleanup(abort):
            if abort:
                await _kill_and_reap(proc)

        execution = ShellExecution(proc, "test", proc.wait, cleanup)
        executions.append(execution)
        return execution

    async with app.router.lifespan_context(app):
        async with Socket(app, sid) as ws:
            await ws.recv()
            runtime = app.state.sessions[sid]
            agent = runtime_agent(runtime, gui)
            hooks = agent.tool_ctx.plugins["process"]
            monkeypatch.setattr(
                sys.modules[type(hooks).__module__], "launch_shell", launch
            )
            await ws.send({"type": "user", "text": "start server"})
            request = (await ws.until("approval" if gui else "confirm"))[-1]
            await ws.send(
                {"type": "approval_response", "id": request["id"], "decision": "once"}
                if gui
                else {"type": "confirm_response", "id": request["id"], "approved": True}
            )
            frames = await ws.until("turn_end")
            assert any(f["type"] == "tool_result" and f["ok"] for f in frames)
        assert len(executions) == 1 and executions[0].process.returncode is None
        async with Socket(app, sid) as ws:
            await ws.recv()
            assert runtime_agent(app.state.sessions[sid], gui) is agent
            assert len(hooks.processes()) == 1
    assert executions[0].process.returncode is not None
    assert not hooks.processes()


@pytest.mark.parametrize("kind", ["claude", "codex"])
async def test_native_backend_survives_disconnect_and_receives_shutdown_interrupt(
    tmp_path, monkeypatch, kind
):
    from agentgui.backends.claude_backend import ClaudeBackend
    from agentgui.backends.codex_backend import CodexBackend

    fixtures = Path(__file__).parent / "agentgui_fakes"
    log = tmp_path / "native.jsonl"
    monkeypatch.setenv(f"FAKE_{kind.upper()}_LOG", str(log))
    entry = ModelEntry(kind, kind.title(), kind)

    def backend_factory(rec, store, cache):
        if kind == "claude":
            return ClaudeBackend(store, str(fixtures / "fake_claude"))
        return CodexBackend(
            store, [sys.executable, str(fixtures / "fake_codex_appserver.py")]
        )

    app = create_gui(
        catalog=Catalog([entry]),
        store_path=tmp_path / "gui.db",
        auth_token="secret",
        backend_factory=backend_factory,
    )
    sid = app.state.store.create(entry, str(tmp_path)).id
    async with app.router.lifespan_context(app):
        async with Socket(app, sid) as ws:
            await ws.recv()
            await ws.send({"type": "user", "text": "slow"})
            await ws.until("thinking" if kind == "claude" else "text")
            runtime = app.state.sessions[sid]
            backend = runtime.backend
            proc = (
                backend.client._transport._process
                if kind == "claude"
                else backend.rpc.proc
            )
            task = runtime.native_task if kind == "claude" else runtime.turn_task
        assert not task.done() and proc.returncode is None
        async with Socket(app, sid) as ws:
            hello = await ws.recv()
            assert hello["running"] and hello["replay"]
            assert app.state.sessions[sid].backend is backend
    assert task.done() and proc.returncode is not None
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert any(
        r.get("request", {}).get("subtype") == "interrupt"
        if kind == "claude"
        else r.get("method") == "turn/interrupt"
        for r in records
    )


@pytest.mark.parametrize("publish_first", [False, True])
async def test_reconnect_handoff_delivers_progress_and_approval_exactly_once(
    publish_first,
):
    class Viewer:
        def __init__(self):
            self.frames = []

        async def send_json(self, wire):
            self.frames.append(copy.deepcopy(wire))
            await asyncio.sleep(0)

    old, current = Viewer(), Viewer()
    channel = LiveChannel(old)
    channel.begin({"messages": []})
    channel.detach(old)
    channel.viewer = current
    approvals = []

    async def attach():
        await channel.attach(
            current, lambda: {"type": "hello"}, lambda: {}, lambda: approvals
        )

    async def publish():
        await channel.send(
            {"type": "confirm", "id": "approval"}, record=approvals.append
        )
        await channel.send({"type": "text", "text": "progress"}, replay=True)

    # Queue both operations behind the lock to exercise both handoff orders.
    async with channel.lock:
        first = asyncio.create_task(publish() if publish_first else attach())
        await asyncio.sleep(0)
        second = asyncio.create_task(attach() if publish_first else publish())
        await asyncio.sleep(0)
    await asyncio.gather(first, second)
    hello, *live = current.frames
    assert hello["type"] == "hello"
    assert hello["approvals"] + [f for f in live if f["type"] == "confirm"] == [
        {"type": "confirm", "id": "approval"}
    ]
    assert hello["replay"] + [f for f in live if f["type"] == "text"] == [
        {"type": "text", "text": "progress"}
    ]
    channel.detach(old)
    assert channel.viewer is current and channel.ws is current
